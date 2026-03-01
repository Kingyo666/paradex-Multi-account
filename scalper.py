"""
Paradex BTC 秒开关脚本 v6 - 双向智能版 (WebSocket 实时推送)

特点:
1. WebSocket 实时接收 BBO 价格 (~10-50ms 延迟)
2. 双向开平仓：根据买一/卖一厚度决定方向
3. 通过账户余额变化计算真实盈亏
4. 速率限制：30单/分钟, 300单/小时, 1000单/24小时
5. 延迟监控：实时延迟 + 近5单延迟统计
6. 固定面板显示，不滚动
"""

import asyncio
import logging
import time
import os
import sys
import threading
import select
from collections import deque
from datetime import datetime, time as dt_time
from typing import Optional, Dict, Any

import aiohttp

from config import (
    ORDER_SIZE_BTC, MAX_SPREAD_PERCENT, MAX_CYCLES,
    LOG_FILE, LOG_LEVEL,
    MAX_CONSECUTIVE_FAILURES, EMERGENCY_STOP_FILE,
    PARADEX_ENV, ACCOUNTS, SWITCH_COST_PER_10K,
    SCHEDULE_ENABLED, SCHEDULE_START_HOUR, SCHEDULE_START_MINUTE,
    SCHEDULE_END_HOUR, SCHEDULE_END_MINUTE,
    TG_ENABLED, TG_BOT_TOKEN, TG_CHAT_ID,
    TG_NOTIFY_START_STOP, TG_NOTIFY_FEE_PAUSE, TG_NOTIFY_SCHEDULE,
    VOLATILITY_FILTER_ENABLED, MAX_VOLATILITY_PCT, VOLATILITY_WINDOW_SECONDS,
)

from paradex_py import ParadexSubkey
from paradex_py.api.ws_client import ParadexWebsocketChannel
from paradex_py.common.order import Order, OrderType, OrderSide

# ==================== 日志配置 ====================
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(logging.Formatter('%(message)s'))

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

logging.getLogger('websockets').setLevel(logging.WARNING)
logging.getLogger('paradex_py').setLevel(logging.WARNING)


# ==================== 配置 ====================
MARKET = "BTC-USD-PERP"
MAX_ORDERS_PER_MINUTE = 26
MAX_ORDERS_PER_HOUR = 500
MAX_ORDERS_PER_DAY = 2000
MIN_DEPTH_BTC = 0.02
MAX_FRESHNESS_MS = 100  # 最大数据新鲜度（毫秒），超过则跳过


class RateLimiter:
    """三级速率限制器"""
    def __init__(self, per_minute: int, per_hour: int, per_day: int):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.per_day = per_day
        self.minute_orders = deque()
        self.hour_orders = deque()
        self.day_orders = deque()

    def can_place_order(self) -> tuple[bool, float, str]:
        now = time.time()
        while self.minute_orders and now - self.minute_orders[0] > 60:
            self.minute_orders.popleft()
        while self.hour_orders and now - self.hour_orders[0] > 3600:
            self.hour_orders.popleft()
        while self.day_orders and now - self.day_orders[0] > 86400:
            self.day_orders.popleft()

        if len(self.minute_orders) >= self.per_minute:
            return False, 60 - (now - self.minute_orders[0]), "分钟"
        if len(self.hour_orders) >= self.per_hour:
            return False, 3600 - (now - self.hour_orders[0]), "小时"
        if len(self.day_orders) >= self.per_day:
            return False, 86400 - (now - self.day_orders[0]), "24h"
        return True, 0, ""

    def record_order(self):
        now = time.time()
        self.minute_orders.append(now)
        self.hour_orders.append(now)
        self.day_orders.append(now)

    def get_counts(self) -> tuple[int, int, int]:
        return len(self.minute_orders), len(self.hour_orders), len(self.day_orders)


class VolatilityTracker:
    """波动率追踪器"""

    def __init__(self, window_seconds: int = 10, max_records: int = 1000):
        self.window_seconds = window_seconds
        self.price_history: deque = deque(maxlen=max_records)

    def add_price(self, mid_price: float, timestamp: float = None):
        """添加价格点"""
        if timestamp is None:
            timestamp = time.time()
        self.price_history.append((mid_price, timestamp))

    def get_volatility(self) -> float:
        """计算当前时间窗口内的波动率（百分比）

        公式: (最高价 - 最低价) / 平均价 * 100
        """
        if not self.price_history:
            return 0.0

        now = time.time()
        cutoff = now - self.window_seconds

        # 获取窗口内的价格
        prices = [p for p, t in self.price_history if t >= cutoff]

        if len(prices) < 2:
            return 0.0

        high = max(prices)
        low = min(prices)
        avg = sum(prices) / len(prices)

        if avg <= 0:
            return 0.0

        volatility = (high - low) / avg * 100
        return volatility

    def is_stable(self, threshold: float = None) -> tuple[bool, float]:
        """检查市场是否稳定

        Returns:
            (是否稳定, 当前波动率)
        """
        if threshold is None:
            threshold = MAX_VOLATILITY_PCT

        volatility = self.get_volatility()
        return volatility <= threshold, volatility


class LatencyTracker:
    """延迟追踪器"""
    def __init__(self, max_records: int = 5):
        self.recent_latencies = deque(maxlen=max_records)
        self.current_ws_latency = 0.0
    
    def record_cycle_latency(self, latency_ms: float):
        self.recent_latencies.append(latency_ms)
    
    def update_ws_latency(self, latency_ms: float):
        self.current_ws_latency = latency_ms
    
    def get_stats(self) -> dict:
        if not self.recent_latencies:
            return {"recent": [], "avg": 0, "min": 0, "max": 0, "ws": self.current_ws_latency}
        latencies = list(self.recent_latencies)
        return {
            "recent": latencies,
            "avg": sum(latencies) / len(latencies),
            "min": min(latencies),
            "max": max(latencies),
            "ws": self.current_ws_latency
        }
    
    def format_recent(self) -> str:
        if not self.recent_latencies:
            return "-"
        return "/".join([f"{l:.0f}" for l in self.recent_latencies])


class BalancePnLTracker:
    """盈亏追踪器"""
    def __init__(self):
        self.initial_balance = 0.0
        self.current_balance = 0.0
        self.total_volume_usd = 0.0
        self.last_valid_balance = 0.0
        self.long_count = 0
        self.short_count = 0
        self.recent_cycles = deque(maxlen=3)  # 记录最近3次 (balance_after, volume)

    def set_initial_balance(self, balance: float):
        if balance <= 0:
            return False
        self.initial_balance = balance
        self.current_balance = balance
        self.last_valid_balance = balance
        return True

    def update_balance(self, balance: float, cycle_volume: float = 0.0) -> bool:
        if balance <= 0:
            return False
        self.current_balance = balance
        self.last_valid_balance = balance
        # 记录到最近循环队列
        if cycle_volume > 0:
            self.recent_cycles.append((balance, cycle_volume))
        return True

    def record_cycle_volume(self, price: float, size: float, direction: str):
        self.total_volume_usd += price * size * 2
        if direction == "LONG":
            self.long_count += 1
        else:
            self.short_count += 1

    def get_real_pnl(self) -> float:
        return self.current_balance - self.initial_balance

    def get_recent_wear(self) -> float:
        """最近3次的磨损/万U"""
        if len(self.recent_cycles) < 2:
            return 0.0
        first_balance = self.recent_cycles[0][0]
        last_balance = self.recent_cycles[-1][0]
        total_vol = sum(v for _, v in self.recent_cycles)
        if total_vol == 0:
            return 0.0
        return abs(last_balance - first_balance) / total_vol * 10000

    def get_stats(self) -> dict:
        real_pnl = self.get_real_pnl()
        if self.total_volume_usd == 0:
            return {
                "pnl": real_pnl, "volume": 0,
                "per_10k": 0, "per_100k": 0, "per_million": 0,
                "recent_wear": 0.0,
                "initial": self.initial_balance, "current": self.current_balance,
                "long": self.long_count, "short": self.short_count,
            }
        cost_rate = real_pnl / self.total_volume_usd
        return {
            "pnl": real_pnl, "volume": self.total_volume_usd,
            "per_10k": cost_rate * 10000,
            "per_100k": cost_rate * 100000,
            "per_million": cost_rate * 1000000,
            "recent_wear": self.get_recent_wear(),
            "initial": self.initial_balance, "current": self.current_balance,
            "long": self.long_count, "short": self.short_count,
        }


class FixedPanel:
    """固定面板显示器 - 不滚动"""

    PANEL_LINES = 11  # 面板行数
    
    def __init__(self):
        self.initialized = False
    
    def init_panel(self):
        """初始化面板（打印空行占位）"""
        if not self.initialized:
            print("\n" * self.PANEL_LINES, end="")
            self.initialized = True
    
    def update(self, lines: list[str]):
        """更新整个面板"""
        # 移动光标到面板顶部
        sys.stdout.write(f"\033[{self.PANEL_LINES}A")  # 向上移动N行
        sys.stdout.write("\033[J")  # 清除从光标到屏幕底部
        
        # 打印所有行
        for i, line in enumerate(lines):
            if i < self.PANEL_LINES:
                print(line)
        
        # 补足剩余行
        for _ in range(self.PANEL_LINES - len(lines)):
            print()
        
        sys.stdout.flush()


class TelegramNotifier:
    """Telegram 通知器"""

    def __init__(self, bot_token: str, chat_id: str, enabled: bool):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled
        self.session: Optional[aiohttp.ClientSession] = None
        self.control_message_id: Optional[int] = None  # 控制面板消息ID

    async def send(self, message: str) -> bool:
        """发送 Telegram 消息

        Returns:
            bool: 是否发送成功
        """
        if not message.startswith("[P4]"):
            message = f"[P4] {message}"

        if not self.enabled or not self.bot_token or not self.chat_id:
            return False

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "text": message
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                await resp.text()
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram 发送失败: {e}")
            return False

    async def send_control_panel(self, status_data: dict = None) -> bool:
        """发送带按钮的控制面板

        Args:
            status_data: 状态数据字典

        Returns:
            bool: 是否发送成功
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return False

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            if status_data:
                text = (
                    f"🤖 *Paradex Scalper 多号版*\n\n"
                    f"👤 账号: {status_data.get('account', '')}\n"
                    f"🔄 循环: {status_data.get('cycles', 0)}/{MAX_CYCLES}\n"
                    f"💵 盈亏: ${status_data.get('pnl', 0):+.2f}\n"
                    f"📊 磨损: {status_data.get('cost', 0):+.2f}/万\n"
                    f"📈 成交量: ${status_data.get('volume', 0):,.0f}\n"
                    f"⏱️ 运行: {status_data.get('runtime', 0):.0f}分钟\n"
                    f"⚙️ 状态: {status_data.get('status', '')}"
                )
            else:
                text = "🤖 *Paradex Scalper 多号版*\n\n正在启动..."

            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📊 状态", "callback_data": "status"},
                        {"text": "⏸️ 暂停", "callback_data": "pause"},
                        {"text": "▶️ 继续", "callback_data": "resume"}
                    ],
                    [
                        {"text": "⏰ 定时", "callback_data": "schedule"},
                        {"text": "🔄 切号", "callback_data": "switch_account"},
                        {"text": "🛑 停止", "callback_data": "stop"}
                    ]
                ]
            }

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                if resp.status == 200 and result.get("ok"):
                    self.control_message_id = result["result"]["message_id"]
                    return True
                return False
        except Exception as e:
            logger.warning(f"Telegram 发送控制面板失败: {e}")
            return False

    async def update_control_panel(self, status_data: dict) -> bool:
        """更新控制面板消息

        Args:
            status_data: 状态数据字典

        Returns:
            bool: 是否更新成功
        """
        if not self.enabled or not self.control_message_id:
            return await self.send_control_panel(status_data)

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            text = (
                f"🤖 *Paradex Scalper 多号版*\n\n"
                f"👤 账号: {status_data.get('account', '')}\n"
                f"🔄 循环: {status_data.get('cycles', 0)}/{MAX_CYCLES}\n"
                f"💵 盈亏: ${status_data.get('pnl', 0):+.2f}\n"
                f"📊 磨损: ¥{status_data.get('cost', 0):.2f}/万\n"
                f"📈 成交量: ${status_data.get('volume', 0):,.0f}\n"
                f"⏱️ 运行: {status_data.get('runtime', 0):.0f}分钟\n"
                f"⚙️ 状态: {status_data.get('status', '')}"
            )

            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📊 状态", "callback_data": "status"},
                        {"text": "⏸️ 暂停", "callback_data": "pause"},
                        {"text": "▶️ 继续", "callback_data": "resume"}
                    ],
                    [
                        {"text": "⏰ 定时", "callback_data": "schedule"},
                        {"text": "🔄 切号", "callback_data": "switch_account"},
                        {"text": "🛑 停止", "callback_data": "stop"}
                    ]
                ]
            }

            url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "message_id": self.control_message_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                result = await resp.json()
                return resp.status == 200 and result.get("ok")
        except Exception as e:
            logger.warning(f"Telegram 更新控制面板失败: {e}")
            return False

    async def answer_callback(self, callback_query_id: str, text: str = None, alert: bool = False):
        """回答 Callback Query

        Args:
            callback_query_id: 回调ID
            text: 回复文本
            alert: 是否作为弹窗显示
        """
        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
            async with self.session.post(url, json={
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": alert
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram 回答回调失败: {e}")
            return False

    async def send_switch_account_panel(self, accounts: list, current_index: int):
        """发送账号切换面板

        Args:
            accounts: 账号列表
            current_index: 当前账号索引
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            text = (
                f"🔄 *切换账号*\n\n"
                f"当前: {accounts[current_index]['name']}\n"
                f"请选择要切换到的账号："
            )

            # 构建账号按钮
            keyboard = {"inline_keyboard": []}

            # 每行2个账号按钮
            row = []
            for i, acc in enumerate(accounts):
                has_key = bool(acc.get("L2_ADDRESS") and acc.get("L2_PRIVATE_KEY"))
                if has_key:
                    label = f"{acc['name']}" if i != current_index else f"✅ {acc['name']}"
                    row.append({"text": label, "callback_data": f"switch_to_{i}"})
                    if len(row) == 2:
                        keyboard["inline_keyboard"].append(row)
                        row = []
                else:
                    row.append({"text": f"🔒 {acc['name']}", "callback_data": "switch_locked"})
                    if len(row) == 2:
                        keyboard["inline_keyboard"].append(row)
                        row = []

            if row:
                keyboard["inline_keyboard"].append(row)

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram 发送切号面板失败: {e}")
            return False

    async def send_schedule_panel(self, schedule_info: dict):
        """发送定时设置面板 - 多选时间段模式

        Args:
            schedule_info: 定时信息字典，包含 slots 数组
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            slots = schedule_info.get("slots", [False] * 24)

            # 统计已选择的时间段
            active_hours = [h for h, enabled in enumerate(slots) if enabled]

            if active_hours:
                ranges = []
                start = active_hours[0]
                prev = active_hours[0]
                for h in active_hours[1:]:
                    if h == prev + 1:
                        prev = h
                    else:
                        ranges.append((start, prev))
                        start = prev = h
                ranges.append((start, prev))

                range_strs = []
                for s, e in ranges:
                    if s == e:
                        range_strs.append(f"{s:02d}点")
                    else:
                        range_strs.append(f"{s:02d}-{e:02d}点")

                current_text = f"已选: {', '.join(range_strs)}"
            else:
                current_text = "未选择时间段"

            text = (
                f"⏰ *定时交易设置*\n\n"
                f"{current_text}\n\n"
                f"请点击选择/取消时间段："
            )

            # 构建时间选择按钮 - 每行4个小时
            keyboard = {"inline_keyboard": []}

            # 24小时按钮
            for h in range(0, 24, 4):
                row = []
                for i in range(4):
                    hour = h + i
                    if hour < 24:
                        is_selected = slots[hour]
                        label = f"✅{hour:02d}点" if is_selected else f"⬜{hour:02d}点"
                        row.append({"text": label, "callback_data": f"toggle_slot_{hour:02d}"})
                keyboard["inline_keyboard"].append(row)

            # 操作按钮行
            keyboard["inline_keyboard"].extend([
                [
                    {"text": "🌙 夜间(0-6点)", "callback_data": "preset_night"},
                    {"text": "🌅 早上(6-12点)", "callback_data": "preset_morning"},
                    {"text": "☀️ 下午(12-18点)", "callback_data": "preset_afternoon"},
                    {"text": "🌆 晚上(18-24点)", "callback_data": "preset_evening"},
                ],
                [
                    {"text": "✅ 全选24h", "callback_data": "select_all"},
                    {"text": "❌ 清空", "callback_data": "clear_all"},
                ],
                [
                    {"text": "💾 确认并返回", "callback_data": "confirm_schedule"},
                ],
            ])

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram 发送定时面板失败: {e}")
            return False

    async def edit_schedule_panel(self, schedule_info: dict, query_id: str = None):
        """编辑定时设置面板（更新按钮状态）

        Args:
            schedule_info: 定时信息字典
            query_id: 回调ID，用于获取消息
        """
        if not self.enabled or not self.bot_token or not self.chat_id:
            return

        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()

            slots = schedule_info.get("slots", [False] * 24)

            # 统计已选择的时间段
            active_hours = [h for h, enabled in enumerate(slots) if enabled]

            if active_hours:
                ranges = []
                start = active_hours[0]
                prev = active_hours[0]
                for h in active_hours[1:]:
                    if h == prev + 1:
                        prev = h
                    else:
                        ranges.append((start, prev))
                        start = prev = h
                ranges.append((start, prev))

                range_strs = []
                for s, e in ranges:
                    if s == e:
                        range_strs.append(f"{s:02d}点")
                    else:
                        range_strs.append(f"{s:02d}-{e:02d}点")

                current_text = f"已选: {', '.join(range_strs)}"
            else:
                current_text = "未选择时间段"

            text = (
                f"⏰ *定时交易设置*\n\n"
                f"{current_text}\n\n"
                f"请点击选择/取消时间段："
            )

            # 构建时间选择按钮 - 每行4个小时
            keyboard = {"inline_keyboard": []}

            # 24小时按钮
            for h in range(0, 24, 4):
                row = []
                for i in range(4):
                    hour = h + i
                    if hour < 24:
                        is_selected = slots[hour]
                        label = f"✅{hour:02d}点" if is_selected else f"⬜{hour:02d}点"
                        row.append({"text": label, "callback_data": f"toggle_slot_{hour:02d}"})
                keyboard["inline_keyboard"].append(row)

            # 操作按钮行
            keyboard["inline_keyboard"].extend([
                [
                    {"text": "🌙 夜间(0-6点)", "callback_data": "preset_night"},
                    {"text": "🌅 早上(6-12点)", "callback_data": "preset_morning"},
                    {"text": "☀️ 下午(12-18点)", "callback_data": "preset_afternoon"},
                    {"text": "🌆 晚上(18-24点)", "callback_data": "preset_evening"},
                ],
                [
                    {"text": "✅ 全选24h", "callback_data": "select_all"},
                    {"text": "❌ 清空", "callback_data": "clear_all"},
                ],
                [
                    {"text": "💾 确认并返回", "callback_data": "confirm_schedule"},
                ],
            ])

            # 先获取消息ID
            if query_id:
                # 通过回调查询获取消息ID
                url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
                async with self.session.post(url, json={
                    "callback_query_id": query_id,
                }) as resp:
                    pass

            # 注意：这里我们用 sendMessage 代替 editMessageText
            # 因为编辑消息需要知道消息ID，而通过 callback_query 获取比较复杂
            # 所以我们简化处理，每次都发送新消息
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with self.session.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Telegram 编辑定时面板失败: {e}")
            return False

    async def close(self):
        """关闭 HTTP 会话"""
        if self.session:
            await self.session.close()
            self.session = None


class WebSocketScalper:
    """WebSocket 实时价格的 BTC 双向秒开关策略 - 多号轮换版"""

    # 交易状态常量
    STATE_RUNNING = "运行中"
    STATE_PAUSING = "暂停中(平仓)"
    STATE_PAUSED = "已暂停"

    def __init__(self, accounts: list):
        self.accounts = accounts
        self.current_account_index = 0
        self.current_account_name = accounts[0]["name"] if accounts else ""
        self.accounts_completed = 0  # 已完成的账号数量
        self.account_volumes = {}  # 记录每个账号的成交量 {account_index: volume}
        self.account_retry_count = {}  # 记录每个账号的重试次数 {account_index: count}
        self.round_completed = False  # 是否完成一轮(所有号都切过)
        self.min_volume_threshold = 100000  # 最小成交量阈值，启动时输入
        self.max_round_retries = 100  # 最大轮询次数，启动时输入
        self.current_round = 0  # 当前轮数

        self.paradex: Optional[ParadexSubkey] = None
        self.rate_limiter = RateLimiter(MAX_ORDERS_PER_MINUTE, MAX_ORDERS_PER_HOUR, MAX_ORDERS_PER_DAY)
        self.pnl_tracker = BalancePnLTracker()
        self.latency_tracker = LatencyTracker()
        self.volatility_tracker = VolatilityTracker(window_seconds=VOLATILITY_WINDOW_SECONDS)
        self.panel = FixedPanel()

        # Telegram 通知器
        self.tg_notifier = TelegramNotifier(TG_BOT_TOKEN, TG_CHAT_ID, TG_ENABLED)

        self.cycle_count = 0
        self.successful_cycles = 0
        self.failed_cycles = 0
        self.consecutive_failures = 0
        self.running = False
        self.start_time = None
        self.last_auth_time = 0
        self.last_direction = "-"

        # 交易状态控制
        self.trade_state = self.STATE_RUNNING  # 当前交易状态
        self.pause_requested = False  # 是否请求暂停
        self.stop_requested = False  # 是否请求停止
        self.switch_requested = False  # 是否请求切换账号
        self.target_account_index = None  # 目标账号索引（None表示下一个）

        # 连续低交易量切号追踪
        self.consecutive_low_volume_switches = 0  # 连续低交易量切号次数
        self.LOW_VOLUME_THRESHOLD = 5000  # 交易量阈值 (USD)
        self.MAX_CONSECUTIVE_LOW_VOLUME = 2  # 连续低量切号上限，达到后暂停

        # 磨损超时追踪 - 磨损超标持续时间
        self.high_cost_start_time = 0.0  # 磨损首次超标的时间
        self.SWITCH_COST_DELAY_SECONDS = 6  # 磨损超标后需要持续的秒数

        # 熔断保护
        self.circuit_break = False  # 熔断状态
        self.CIRCUIT_BREAK_THRESHOLD = 0.3  # 熔断阈值：实时磨损超过0.3/万
        self.CIRCUIT_BREAK_DURATION = 60  # 熔断暂停时长（秒）

        # 定时时间段配置（24小时，每小时一个标记）
        self.schedule_slots = self._load_schedule_config()  # 从文件加载配置
        self.schedule_config_file = "schedule_config.json"  # 配置文件路径

        # 定时交易状态追踪
        self.was_trading = True  # 上一次是否在交易时间
        self.last_panel_update = 0  # 上次更新 TG 面板的时间

        # Telegram 回调处理
        self.tg_callback_queue: asyncio.Queue = None
        self.tg_callback_task = None

        self.current_bbo: Dict[str, Any] = {
            "bid": 0.0, "ask": 0.0,
            "bid_size": 0.0, "ask_size": 0.0,
            "spread": 100.0, "mid_price": 0.0,
            "last_update": 0,
        }

        self.recent_cycle_times = deque(maxlen=5)
        self.last_display_update = 0  # 控制显示刷新频率
        self.recent_freshness = deque(maxlen=3)  # 近3次交易的新鲜度延迟（毫秒）

        # 键盘监听
        self.quit_flag = False
        self._start_keyboard_listener()

    def _start_keyboard_listener(self):
        """启动键盘监听线程"""
        def listen():
            import sys
            import tty
            import termios

            # 保存原始终端设置
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)

            try:
                tty.setcbreak(fd)
                while not self.quit_flag:
                    if sys.stdin in select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1)
                        if ch.lower() == 'q':
                            print("\n\n🛑 检测到 'q' 键，准备退出...")
                            self.quit_flag = True
                            self.running = False
                            break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        listener_thread = threading.Thread(target=listen, daemon=True)
        listener_thread.start()
    
    def update_display(self, status: str = None):
        """更新固定面板显示"""
        bbo = self.current_bbo
        stats = self.pnl_tracker.get_stats()
        latency = self.latency_tracker.get_stats()
        min_o, hr_o, day_o = self.rate_limiter.get_counts()

        now = time.time()

        # 使用 trade_state 作为状态显示，熔断时优先显示熔断状态
        if self.circuit_break:
            display_status = "🔴 熔断保护中"
        else:
            display_status = status if status else self.trade_state
        ws_age = (now - bbo["last_update"]) * 1000 if bbo["last_update"] > 0 else 0
        elapsed = now - self.start_time if self.start_time else 0
        elapsed_min = elapsed / 60

        direction = "🟢多" if bbo["bid_size"] >= bbo["ask_size"] else "🔴空"
        pnl_color = "+" if stats['pnl'] >= 0 else ""

        # 获取波动率
        volatility = self.volatility_tracker.get_volatility()
        is_stable, _ = self.volatility_tracker.is_stable()
        vol_color = "🟢" if is_stable else "🔴"

        # 数据新鲜度状态指示
        if ws_age <= 50:
            freshness = f"🟢 {ws_age:.0f}ms"
        elif ws_age <= 100:
            freshness = f"🟡 {ws_age:.0f}ms"
        else:
            freshness = f"🔴 {ws_age:.0f}ms"

        # 近3次交易新鲜度显示
        if self.recent_freshness:
            recent_freshness_str = "/".join([f"{f:.0f}" for f in self.recent_freshness])
        else:
            recent_freshness_str = "-"

        lines = [
            "═" * 70,
            f"  📊 Paradex BTC 双向秒开关 v6 多号版 | 状态: {display_status}",
            "═" * 70,
            f"  👤 账号: [{self.current_account_index + 1}/{len(self.accounts)}] {self.current_account_name}  |  余额: ${stats['current']:.2f} USDC",
            f"  💰 价格: ${bbo['mid_price']:.0f}  |  价差: {bbo['spread']:.5f}%  |  波动率: {vol_color}{volatility:.4f}%",
            f"  📈 深度: 买一 {bbo['bid_size']:.4f} BTC  |  卖一 {bbo['ask_size']:.4f} BTC  |  方向: {direction}",
            f"  🔄 循环: {self.cycle_count}/{MAX_CYCLES} (多:{stats['long']} 空:{stats['short']})  |  上次: {self.last_direction}  |  轮次: {self.current_round + 1} ({len(self.account_volumes)}/{len(self.accounts)}号)",
            f"  💵 盈亏: {pnl_color}{stats['pnl']:.4f} U  |  成交量: ${stats['volume']/1000:.1f}K",
            f"  🚦 限速: {min_o}/{MAX_ORDERS_PER_MINUTE}分 | {hr_o}/{MAX_ORDERS_PER_HOUR}时 | {day_o}/{MAX_ORDERS_PER_DAY}日",
            f"  ⏱️ 新鲜度: {freshness}  |  近3单: [{recent_freshness_str}]ms",
            f"  ⏱️ 循环延迟: [{self.latency_tracker.format_recent()}]ms  |  磨损: {stats['per_10k']:+.2f}/万(<-¥{SWITCH_COST_PER_10K}切换)  |  实时磨损: {stats['recent_wear']:.2f}/万",
        ]

        self.panel.update(lines)

    def get_status_data(self) -> dict:
        """获取状态数据用于 Telegram 控制面板"""
        stats = self.pnl_tracker.get_stats()
        elapsed = time.time() - self.start_time if self.start_time else 0

        return {
            "account": f"[{self.current_account_index + 1}/{len(self.accounts)}] {self.current_account_name}",
            "cycles": self.cycle_count,
            "pnl": stats['pnl'],
            "cost": stats['per_10k'],
            "volume": stats['volume'],
            "runtime": elapsed / 60,
            "status": self.trade_state,
        }

    async def handle_tg_command(self, command: str) -> tuple:
        """处理 Telegram 命令

        Args:
            command: 命令类型 (status, pause, resume, switch_account, stop, schedule, set_schedule)

        Returns:
            tuple: (message, need_switch_account)
        """
        if command == "status":
            data = self.get_status_data()
            stats = self.pnl_tracker.get_stats()
            return (
                f"📊 详细状态\n\n"
                f"账号: {data['account']}\n"
                f"状态: {data['status']}\n"
                f"循环: {data['cycles']}/{MAX_CYCLES}\n"
                f"盈亏: ${data['pnl']:+.2f}\n"
                f"磨损: ¥{data['cost']:.2f}/万\n"
                f"成交量: ${data['volume']:,.0f}\n"
                f"运行: {data['runtime']:.0f}分钟\n"
                f"方向: 多{stats['long']} 空{stats['short']}"
            , False)

        elif command == "pause":
            if self.trade_state == self.STATE_PAUSED:
                return "⚠️ 已经是暂停状态", False
            self.pause_requested = True
            self.trade_state = self.STATE_PAUSING
            return "✅ 已请求暂停，等待当前持仓平仓后暂停", False

        elif command == "resume":
            self.pause_requested = False
            if self.trade_state != self.STATE_RUNNING:
                self.trade_state = self.STATE_RUNNING
                return "✅ 已恢复交易", False
            return "⚠️ 交易已在运行中", False

        elif command == "switch_account":
            # 设置切换标志，让主循环处理
            self.switch_requested = True
            return "🔄 正在切换账号，请稍候...", True

        elif command == "schedule":
            return self.get_schedule_status(), False

        elif command == "stop":
            self.stop_requested = True
            return "🛑 正在停止，将先平仓再退出...", False

        return "❌ 未知命令", False

    def get_schedule_status(self) -> str:
        """获取定时状态"""
        if any(self.schedule_slots):
            # 显示已选择的时间段
            active_hours = [h for h, enabled in enumerate(self.schedule_slots) if enabled]
            if not active_hours:
                return "⏰ 定时交易状态\n\n未选择任何时间段"

            # 将连续的小时合并显示
            ranges = []
            start = active_hours[0]
            prev = active_hours[0]
            for h in active_hours[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    ranges.append((start, prev))
                    start = prev = h
            ranges.append((start, prev))

            range_strs = []
            for s, e in ranges:
                if s == e:
                    range_strs.append(f"{s:02d}点")
                else:
                    range_strs.append(f"{s:02d}-{e:02d}点")

            return (
                f"⏰ 定时交易状态\n\n"
                f"✅ 已启用\n"
                f"交易时段: {', '.join(range_strs)}"
            )
        else:
            return (
                f"⏰ 定时交易状态\n\n"
                f"启用: {'是' if SCHEDULE_ENABLED else '否'}\n"
                f"时间: {SCHEDULE_START_HOUR:02d}:{SCHEDULE_START_MINUTE:02d} - "
                f"{SCHEDULE_END_HOUR:02d}:{SCHEDULE_END_MINUTE:02d}\n\n"
                f"提示: 请使用 Telegram 菜单修改定时设置"
            )

    def get_schedule_info(self) -> dict:
        """获取定时信息用于面板"""
        return {
            "enabled": SCHEDULE_ENABLED,
            "start_hour": SCHEDULE_START_HOUR,
            "start_min": SCHEDULE_START_MINUTE,
            "end_hour": SCHEDULE_END_HOUR,
            "end_min": SCHEDULE_END_MINUTE,
            "slots": self.schedule_slots.copy(),
        }

    def _load_schedule_config(self) -> list:
        """从文件加载定时配置

        Returns:
            24个元素的布尔列表，表示每个小时是否启用
        """
        import json
        import os

        # 默认全启用（24小时交易）
        default_slots = [True] * 24

        config_file = "schedule_config.json"
        if not os.path.exists(config_file):
            # 文件不存在，使用默认值并保存
            self._save_schedule_config(default_slots)
            return default_slots

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                slots = data.get("slots", [])
                if len(slots) == 24:
                    logger.info(f"📂 已加载定时配置: {sum(slots)}/24小时")
                    return slots
                else:
                    logger.warning("定时配置文件格式错误，使用默认值")
                    return default_slots
        except Exception as e:
            logger.warning(f"加载定时配置失败: {e}，使用默认值")
            return default_slots

    def _save_schedule_config(self, slots: list) -> bool:
        """保存定时配置到文件

        Args:
            slots: 24个元素的布尔列表

        Returns:
            是否保存成功
        """
        import json

        try:
            data = {
                "slots": slots,
                "updated_at": time.time()
            }
            with open(self.schedule_config_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"💾 定时配置已保存: {sum(slots)}/24小时")
            return True
        except Exception as e:
            logger.error(f"保存定时配置失败: {e}")
            return False

    async def update_schedule(self, start_h: int, start_m: int, end_h: int, end_m: int, enabled: bool = True):
        """更新定时设置

        Args:
            start_h: 开始小时
            start_m: 开始分钟
            end_h: 结束小时
            end_m: 结束分钟
            enabled: 是否启用
        """
        import config
        config.SCHEDULE_ENABLED = enabled
        config.SCHEDULE_START_HOUR = start_h
        config.SCHEDULE_START_MINUTE = start_m
        config.SCHEDULE_END_HOUR = end_h
        config.SCHEDULE_END_MINUTE = end_m

        # 同时更新全局变量
        global SCHEDULE_ENABLED, SCHEDULE_START_HOUR, SCHEDULE_START_MINUTE, SCHEDULE_END_HOUR, SCHEDULE_END_MINUTE
        SCHEDULE_ENABLED = enabled
        SCHEDULE_START_HOUR = start_h
        SCHEDULE_START_MINUTE = start_m
        SCHEDULE_END_HOUR = end_h
        SCHEDULE_END_MINUTE = end_m

        logger.info(f"⏰ 定时已更新: {enabled}, {start_h:02d}:{start_m:02d} - {end_h:02d}:{end_m:02d}")

    async def on_bbo_update(self, channel, message):
        try:
            data = message.get("params", {}).get("data", {})
            if data:
                bid = float(data.get("bid", 0))
                ask = float(data.get("ask", 0))
                bid_size = float(data.get("bid_size", 0))
                ask_size = float(data.get("ask_size", 0))

                if bid > 0 and ask > 0:
                    mid = (bid + ask) / 2
                    spread_pct = (ask - bid) / mid * 100

                    now = time.time()

                    # 记录价格到波动率追踪器
                    self.volatility_tracker.add_price(float(mid), now)

                    self.current_bbo = {
                        "bid": bid, "ask": ask,
                        "bid_size": bid_size, "ask_size": ask_size,
                        "spread": spread_pct, "mid_price": mid,
                        "last_update": now,
                    }
        except Exception as e:
            logger.error(f"BBO 解析错误: {e}")
    
    async def connect(self) -> bool:
        try:
            account = self.accounts[self.current_account_index]
            l2_address = account["L2_ADDRESS"]
            l2_private_key = account["L2_PRIVATE_KEY"]
            self.current_account_name = account["name"]

            env = "prod" if PARADEX_ENV == "MAINNET" else "testnet"
            print(f"🔌 连接 Paradex ({env})... 账号: {self.current_account_name}")

            self.paradex = ParadexSubkey(
                env=env,
                l2_private_key=l2_private_key,
                l2_address=l2_address
            )

            await self.paradex.init_account()
            await self._auth_with_interactive_token()

            print("📡 连接 WebSocket...")
            await self.paradex.ws_client.connect()

            print(f"📊 订阅 {MARKET} BBO...")
            await self.paradex.ws_client.subscribe(
                ParadexWebsocketChannel.BBO,
                callback=self.on_bbo_update,
                params={"market": MARKET}
            )

            print("⏳ 等待 BBO 数据...")
            for _ in range(50):
                await asyncio.sleep(0.1)
                if self.current_bbo["last_update"] > 0:
                    print(f"✅ 收到 BBO: ${self.current_bbo['mid_price']:.0f}")
                    break

            return True
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return False
    
    async def _auth_with_interactive_token(self):
        import time as time_module
        from paradex_py.api.models import AuthSchema
        
        api_client = self.paradex.api_client
        account = self.paradex.account
        
        headers = account.auth_headers()
        path = f"auth/{hex(account.l2_public_key)}?token_usage=interactive"
        
        res = api_client.post(api_url=api_client.api_url, path=path, headers=headers)
        
        data = AuthSchema().load(res, unknown="exclude", partial=True)
        api_client.auth_timestamp = int(time_module.time())
        account.set_jwt_token(data.jwt_token)
        api_client.client.headers.update({"Authorization": f"Bearer {data.jwt_token}"})
        
        self.last_auth_time = time_module.time()
        print("🆓 Interactive Token 获取成功")
    
    async def refresh_token_if_needed(self, max_age: int = 240):
        elapsed = time.time() - self.last_auth_time
        if elapsed >= max_age:
            await self._auth_with_interactive_token()
    
    def get_account_balance(self) -> float:
        try:
            summary = self.paradex.api_client.fetch_account_summary()
            if hasattr(summary, 'account_value') and summary.account_value:
                return float(summary.account_value)
            if hasattr(summary, 'equity') and summary.equity:
                return float(summary.equity)
            if hasattr(summary, 'free_collateral') and summary.free_collateral:
                return float(summary.free_collateral)
            return -1
        except:
            return -1
    
    def place_market_order(self, side: str, size: float) -> tuple[dict, bool]:
        """下市价单并检查是否收取手续费
        
        Returns:
            (response, is_free): 订单响应和是否免费
        """
        from decimal import Decimal
        order = Order(
            market=MARKET,
            order_type=OrderType.Market,
            order_side=OrderSide.Buy if side == "BUY" else OrderSide.Sell,
            size=Decimal(str(size))
        )
        response = self.paradex.api_client.submit_order(order)
        
        # 检查是否包含 INTERACTIVE flag (免手续费标志)
        flags = response.get("flags", [])
        is_free = "INTERACTIVE" in flags
        
        if not is_free:
            logger.warning(f"⚠️ 检测到手续费! flags: {flags}")
        
        return response, is_free
    
    def decide_direction(self, bid_size: float, ask_size: float) -> str:
        return "LONG" if bid_size >= ask_size else "SHORT"

    def is_trading_time(self) -> tuple[bool, str]:
        """检查当前是否在交易时间窗口内

        支持多选时间段模式：使用 schedule_slots 检查当前小时是否在交易时间

        Returns:
            (is_trading, status_str): 是否在交易时间, 状态描述
        """
        if not SCHEDULE_ENABLED:
            return True, "Always"

        # 检查是否有配置的时间段
        if not any(self.schedule_slots):
            # 没有配置时间段，使用原来的单段模式
            now = datetime.now()
            current_time = now.time()
            start_time = dt_time(SCHEDULE_START_HOUR, SCHEDULE_START_MINUTE)
            end_time = dt_time(SCHEDULE_END_HOUR, SCHEDULE_END_MINUTE)

            if start_time <= current_time <= end_time:
                return True, "Trading"
            return False, "Waiting"

        # 使用时间段模式
        now = datetime.now()
        current_hour = now.hour
        if self.schedule_slots[current_hour]:
            return True, "Trading"
        return False, "Waiting"

    async def send_status_report(self):
        """发送 Telegram 状态报告"""
        if not TG_ENABLED:
            return

        stats = self.pnl_tracker.get_stats()
        latency = self.latency_tracker.get_stats()
        elapsed = time.time() - self.start_time if self.start_time else 0
        elapsed_min = elapsed / 60

        report = (
            f"🤖 Paradex Scalper Report\n"
            f"⏱️ 运行时长: {elapsed_min:.0f} 分钟\n"
            f"💰 总盈亏: {stats['pnl']:+.2f} USDC\n"
            f"📈 交易量: ${stats['volume']:,.0f}\n"
            f"📉 磨损: {stats['per_10k']:+.2f}/万\n"
            f"🔄 循环: {self.cycle_count} (多:{stats['long']} 空:{stats['short']})\n"
            f"💵 余额: {stats['current']:.2f} USDC\n"
            f"⏱️ 延迟: {latency['avg']:.0f}ms"
        )

        await self.tg_notifier.send(report)
    
    async def start(self):
        print("=" * 70)
        print("🚀 Paradex BTC 秒开关策略 v6 - 多号轮换版")
        print("=" * 70)
        print(f"📊 配置: {ORDER_SIZE_BTC} BTC | 价差≤{MAX_SPREAD_PERCENT}%")
        print(f"🚦 限速: {MAX_ORDERS_PER_MINUTE}/分 | {MAX_ORDERS_PER_HOUR}/时 | {MAX_ORDERS_PER_DAY}/24h")
        print(f"🔄 账号数: {len(self.accounts)} | 切���阈值: ¥{SWITCH_COST_PER_10K}/万")
        print(f"⌨️  按 'q' 键随时退出")
        print(f"📈 最小成交量: ${self.min_volume_threshold:,.0f} USD | 最大轮次: {self.max_round_retries}")
        print("=" * 70)

        # 发送启动通知
        if TG_NOTIFY_START_STOP:
            account_list = "\n".join([f"  - {acc['name']}" for acc in self.accounts])
            await self.tg_notifier.send(
                f"🚀 Paradex Scalper 多号版已启动\n"
                f"👥 账号数: {len(self.accounts)}\n"
                f"⚠️ 切换阈值: ¥{SWITCH_COST_PER_10K}/万\n"
                f"📈 最小成交量: ${self.min_volume_threshold:,.0f} USD\n"
                f"🔄 最大轮次: {self.max_round_retries}\n"
                f"账号列表:\n{account_list}"
            )

        # 多账号轮换循环
        while True:
            if not await self.connect():
                print(f"❌ 连接 {self.current_account_name} 失败，切换下一个...")
                self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
                await asyncio.sleep(5)
                continue

            initial_balance = self.get_account_balance()
            if initial_balance <= 0:
                print(f"❌ 获取余额失败: {initial_balance}")
                return
            if not self.pnl_tracker.set_initial_balance(initial_balance):
                print("❌ 设置初始余额失败")
                return
            print(f"💰 初始余额: ${initial_balance:.4f} USDC")
            print()

            self.running = True
            self.start_time = time.time()
            self.panel.init_panel()

            # 启动 Telegram 回调轮询
            await self.start_tg_callback_polling()

            # 发送 Telegram 控制面板
            await self.tg_notifier.send_control_panel(self.get_status_data())

            try:
                result = await self.main_loop()

                if result == "STOP_REQUESTED":
                    break  # 停止请求，退出
                elif result == "SWITCH_ACCOUNT":
                    # 新的切号逻辑已在 switch_account 中处理
                    # 检查是否达到最大轮次
                    if self.current_round >= self.max_round_retries:
                        logger.info(f"✅ 达到最大轮次 {self.max_round_retries}，停止交易")
                        print(f"\n{'='*70}")
                        print(f"✅ 已完成 {self.current_round} 轮交易")
                        print(f"{'='*70}\n")
                        break

                    await self.reset_for_new_account()
                    await asyncio.sleep(2)  # 等待2秒再连接下一个账号
                    continue
                else:
                    break
            except KeyboardInterrupt:
                break
            finally:
                await self.shutdown()

    async def main_loop(self):
        last_balance_check = 0

        # 初始化定时状态
        is_trading, _ = self.is_trading_time()
        self.was_trading = is_trading

        while self.running and self.cycle_count < MAX_CYCLES:
            if os.path.exists(EMERGENCY_STOP_FILE):
                break

            # 检查 q 键退出
            if self.quit_flag:
                logger.info("🛑 检测到 'q' 键，准备退出...")
                print("\n🛑 正在平仓并退出...")
                position = self.get_current_position()
                if position:
                    await self.close_position(position)
                    await asyncio.sleep(0.3)
                return "STOP_REQUESTED"

            # ========== 交易状态控制 ==========
            # 检查停止请求 - 先平仓再停止
            if self.stop_requested:
                self.update_display("🛑 停止中(平仓)")
                position = self.get_current_position()
                if position:
                    print(f"\n🛑 收到停止信号，正在平仓...")
                    await self.close_position(position)
                    await asyncio.sleep(0.3)
                return "STOP_REQUESTED"

            # 检查暂停请求 - 先平仓再暂停
            if self.pause_requested and self.trade_state != self.STATE_PAUSED:
                self.trade_state = self.STATE_PAUSING
                position = self.get_current_position()
                if position:
                    self.update_display(self.STATE_PAUSING)
                    print(f"\n⏸️ 收到暂停信号，正在平仓...")
                    await self.close_position(position)
                    await asyncio.sleep(0.3)
                self.trade_state = self.STATE_PAUSED
                self.pause_requested = False
                await self.tg_notifier.update_control_panel(self.get_status_data())
                logger.info("⏸️ 已暂停交易")

            # 已暂停状态 - 跳过交易逻辑
            if self.trade_state == self.STATE_PAUSED:
                self.update_display()
                await asyncio.sleep(1)
                continue

            # 检查切换账号请求
            if self.switch_requested:
                self.update_display("🔄 切换账号中")
                position = self.get_current_position()
                if position:
                    print(f"\n🔄 切换账号前，正在平仓...")
                    await self.close_position(position)
                    await asyncio.sleep(0.3)

                # 切换到指定账号
                if self.target_account_index is not None:
                    self.current_account_index = self.target_account_index
                    self.target_account_index = None
                else:
                    # 默认切换到下一个
                    self.current_account_index = (self.current_account_index + 1) % len(self.accounts)

                self.switch_requested = False
                return "SWITCH_ACCOUNT"

            # ========== 正常交易逻辑 ==========
            try:
                await self.refresh_token_if_needed(240)

                now = time.time()
                if now - last_balance_check > 4:
                    balance = self.get_account_balance()
                    if balance > 0:
                        self.pnl_tracker.update_balance(balance)
                        last_balance_check = now

                # 检查定时交易状态
                is_trading, schedule_status = self.is_trading_time()

                # 检测定时状态变化
                if is_trading != self.was_trading:
                    if TG_NOTIFY_SCHEDULE:
                        if is_trading:
                            await self.tg_notifier.send("⏰ 定时开始: 交易已恢复")
                        else:
                            await self.tg_notifier.send("💤 定时结束: 交易已暂停")
                    self.was_trading = is_trading

                # 非交易时间：显示等待状态，跳过交易逻辑
                if not is_trading:
                    self.update_display(f"⏰ 等待定时窗口 ({schedule_status})")
                    await asyncio.sleep(5)  # 每5秒检查一次
                    continue

                can_trade, wait_sec, limit_reason = self.rate_limiter.can_place_order()

                bbo = self.current_bbo
                spread = bbo["spread"]
                price = bbo["mid_price"]
                age = now - bbo["last_update"]
                self.latency_tracker.update_ws_latency(age * 1000)

                # 更新显示 (每500ms刷新一次，减少闪烁)
                now = time.time()
                if now - self.last_display_update >= 0.5:
                    if can_trade:
                        self.update_display()
                    else:
                        self.update_display(f"{limit_reason}限速 {wait_sec:.0f}s")
                    self.last_display_update = now

                # 更新 Telegram 控制面板（每30秒）
                if TG_ENABLED and now - self.last_panel_update >= 30:
                    await self.tg_notifier.update_control_panel(self.get_status_data())
                    self.last_panel_update = now

                if not can_trade:
                    await asyncio.sleep(min(wait_sec, 2))
                    continue

                # 检查波动率
                if VOLATILITY_FILTER_ENABLED:
                    is_stable, volatility = self.volatility_tracker.is_stable()
                    if not is_stable:
                        self.update_display(f"🌊波动率过高 {volatility:.4f}%")
                        await asyncio.sleep(0.1)
                        continue

                # 检查数据新鲜度（超过100ms跳过）
                freshness_ms = age * 1000
                if freshness_ms > MAX_FRESHNESS_MS:
                    await asyncio.sleep(0.01)
                    continue

                if spread <= MAX_SPREAD_PERCENT:
                    bid_size = bbo["bid_size"]
                    ask_size = bbo["ask_size"]
                    if bid_size < MIN_DEPTH_BTC or ask_size < MIN_DEPTH_BTC:
                        await asyncio.sleep(0.05)
                        continue

                    direction = self.decide_direction(bid_size, ask_size)

                    # 记录开单前的数据新鲜度 (ms)
                    freshness_ms = age * 1000

                    cycle_start = time.time()
                    success, is_free = await self.execute_cycle(price, direction)
                    cycle_time = time.time() - cycle_start
                    cycle_latency_ms = cycle_time * 1000

                    if success:
                        self.successful_cycles += 1
                        self.consecutive_failures = 0
                        self.cycle_count += 1
                        self.recent_cycle_times.append(cycle_time)
                        self.latency_tracker.record_cycle_latency(cycle_latency_ms)
                        self.recent_freshness.append(freshness_ms)  # 记录新鲜度
                        self.last_direction = "多" if direction == "LONG" else "空"

                        # 检查是否被收取手续费 - 切换到下一个账号
                        if not is_free:
                            logger.warning("💰 检测到手续费，切换账号...")

                            # 检查是否只剩最后一个号需要刷量
                            if await self.check_and_handle_last_account():
                                continue  # 暂停后继续当前账号

                            # 检查连续低量切号是否达上限
                            if self.consecutive_low_volume_switches >= self.MAX_CONSECUTIVE_LOW_VOLUME:
                                logger.warning("🚫 连续低量切号达上限，暂停脚本")
                                await self.tg_notifier.send(
                                    f"🚫 连续 {self.consecutive_low_volume_switches} 个账号交易量未达 "
                                    f"${self.LOW_VOLUME_THRESHOLD}，自动暂停\n"
                                    f"请通过 Telegram ▶️ 继续 按钮手动恢复"
                                )
                                self.trade_state = self.STATE_PAUSED
                                self.consecutive_low_volume_switches = 0
                                continue

                            await self.switch_account("检测到手续费")
                            return "SWITCH_ACCOUNT"

                        await asyncio.sleep(0.2)
                        balance = self.get_account_balance()
                        if balance > 0:
                            # 计算本次循环的交易量
                            cycle_volume = price * ORDER_SIZE_BTC * 2
                            self.pnl_tracker.update_balance(balance, cycle_volume)
                            last_balance_check = time.time()

                            # 熔断检查：实时磨损过高
                            stats = self.pnl_tracker.get_stats()
                            recent_wear = stats['recent_wear']
                            if recent_wear > self.CIRCUIT_BREAK_THRESHOLD:
                                self.circuit_break = True
                                self.pnl_tracker.recent_cycles.clear()
                                logger.warning(f"🔴 触发熔断！实时磨损: {recent_wear:.2f}/万 > {self.CIRCUIT_BREAK_THRESHOLD}")
                                await self.tg_notifier.send(
                                    f"🔴 *熔断保护触发*\n\n"
                                    f"实时磨损: `{recent_wear:.2f}/万`\n"
                                    f"阈值: `{self.CIRCUIT_BREAK_THRESHOLD}/万`\n"
                                    f"暂停 `{self.CIRCUIT_BREAK_DURATION}` 秒"
                                )
                                for remaining in range(self.CIRCUIT_BREAK_DURATION, 0, -1):
                                    if not self.running:
                                        break
                                    self.update_display(f"🔴 熔断暂停 {remaining}s")
                                    await asyncio.sleep(1)
                                self.circuit_break = False
                                logger.info("✅ 熔断结束，恢复交易")
                                await self.tg_notifier.send("✅ 熔断结束，恢复交易")
                                continue

                            # 检查磨损是否超过阈值 - 持续8秒才切换
                            now = time.time()

                            if stats['per_10k'] < -SWITCH_COST_PER_10K:
                                # 磨损超标
                                if self.high_cost_start_time == 0.0:
                                    # 首次超标，记录时间
                                    self.high_cost_start_time = now
                                    logger.info(f"⚠️ 磨损超标: {stats['per_10k']:+.2f}/万，开始计时...")
                                elif now - self.high_cost_start_time >= self.SWITCH_COST_DELAY_SECONDS:
                                    # 持续超时，切换账号
                                    logger.warning(f"📊 磨损过高持续 {self.SWITCH_COST_DELAY_SECONDS}s: {stats['per_10k']:+.2f}/万，切换账号...")

                                    # 检查是否只剩最后一个号需要刷量
                                    if await self.check_and_handle_last_account():
                                        continue  # 暂停后继续当前账号

                                    # 检查连续低量切号是否达上限
                                    if self.consecutive_low_volume_switches >= self.MAX_CONSECUTIVE_LOW_VOLUME:
                                        logger.warning("🚫 连续低量切号达上限，暂停脚本")
                                        await self.tg_notifier.send(
                                            f"🚫 连续 {self.consecutive_low_volume_switches} 个账号交易量未达 "
                                            f"${self.LOW_VOLUME_THRESHOLD}，自动暂停\n"
                                            f"请通过 Telegram ▶️ 继续 按钮手动恢复"
                                        )
                                        self.trade_state = self.STATE_PAUSED
                                        self.consecutive_low_volume_switches = 0
                                        continue

                                    await self.switch_account(f"磨损过高({stats['per_10k']:+.2f}/万)")
                                    return "SWITCH_ACCOUNT"
                            else:
                                # 磨损正常，重置计时
                                if self.high_cost_start_time > 0:
                                    logger.info(f"✅ 磨损恢复正常: {stats['per_10k']:+.2f}/万")
                                    self.high_cost_start_time = 0.0

                        logger.info(f"循环 {self.cycle_count} | {self.last_direction} | {cycle_latency_ms:.0f}ms")
                    else:
                        self.failed_cycles += 1
                        self.consecutive_failures += 1

            except Exception as e:
                logger.error(f"错误: {e}")
                self.consecutive_failures += 1

            await asyncio.sleep(0.05)

        # 达到最大循环次数，切换到下一个账号
        logger.info(f"✅ 达到最大循环次数 {MAX_CYCLES}，准备切换账号")
        return "SWITCH_ACCOUNT"

    async def execute_cycle(self, price: float, direction: str) -> tuple[bool, bool]:
        """执行一个开平仓循环
        
        Returns:
            (success, is_free): 是否成功, 是否免手续费
        """
        try:
            if direction == "LONG":
                _, is_free1 = self.place_market_order("BUY", ORDER_SIZE_BTC)
                self.rate_limiter.record_order()
                _, is_free2 = self.place_market_order("SELL", ORDER_SIZE_BTC)
                self.rate_limiter.record_order()
            else:
                _, is_free1 = self.place_market_order("SELL", ORDER_SIZE_BTC)
                self.rate_limiter.record_order()
                _, is_free2 = self.place_market_order("BUY", ORDER_SIZE_BTC)
                self.rate_limiter.record_order()
            
            self.pnl_tracker.record_cycle_volume(price, ORDER_SIZE_BTC, direction)
            
            # 只要有一个订单被收费，就返回 is_free=False
            is_free = is_free1 and is_free2
            return True, is_free
        except Exception as e:
            logger.error(f"循环失败: {e}")
            return False, True  # 失败时不触发切换

    def get_current_position(self) -> Optional[dict]:
        """获取当前持仓

        Returns:
            None 或持仓信息 {"size": float, "side": "LONG"/"SHORT"}
        """
        try:
            positions = self.paradex.api_client.fetch_positions()
            for pos in positions:
                if hasattr(pos, 'market') and pos.market == MARKET:
                    size = float(getattr(pos, 'size', 0))
                    if abs(size) > 0.0001:  # 有持仓
                        side = "LONG" if size > 0 else "SHORT"
                        return {"size": abs(size), "side": side}
            return None
        except Exception as e:
            logger.warning(f"获取持仓失败: {e}")
            return None

    async def close_position(self, position: dict) -> bool:
        """平仓

        Args:
            position: 持仓信息 {"size": float, "side": "LONG"/"SHORT"}

        Returns:
            是否成功
        """
        try:
            side = "SELL" if position["side"] == "LONG" else "BUY"
            self.place_market_order(side, position["size"])
            logger.info(f"✅ 平仓订单已提交: {position['side']} {position['size']}")

            # 等待订单成交，最多等待5秒
            for i in range(10):
                await asyncio.sleep(0.5)
                current_pos = self.get_current_position()
                if current_pos is None:
                    logger.info(f"✅ 平仓成功确认")
                    return True
                logger.debug(f"等待平仓完成... ({i+1}/10)")

            # 超时后再次检查
            current_pos = self.get_current_position()
            if current_pos is None:
                logger.info(f"✅ 平仓成功（延迟确认）")
                return True
            else:
                logger.warning(f"⚠️ 平仓可能未完成，剩余持仓: {current_pos}")
                return False

        except Exception as e:
            logger.error(f"平仓失败: {e}")
            return False

    async def start_tg_callback_polling(self):
        """启动 Telegram 回调轮询"""
        if not TG_ENABLED or not self.tg_notifier.enabled:
            return

        self.tg_callback_queue = asyncio.Queue()
        self.tg_callback_task = asyncio.create_task(self._tg_callback_polling_loop())
        logger.info("📡 Telegram 回调轮询已启动")

    async def _tg_callback_polling_loop(self):
        """Telegram 回调轮询循环"""
        while self.running:
            try:
                await asyncio.sleep(2)

                if self.tg_notifier.session is None:
                    continue

                url = f"https://api.telegram.org/bot{self.tg_notifier.bot_token}/getUpdates"
                params = {
                    "timeout": 5,
                    "allowed_updates": ["callback_query"]
                }

                async with self.tg_notifier.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    result = await resp.json()

                if not result.get("ok"):
                    continue

                for update in result.get("result", []):
                    callback_query = update.get("callback_query", {})
                    if not callback_query:
                        continue

                    query_id = callback_query.get("id")
                    data = callback_query.get("data", "")
                    from_id = callback_query.get("from", {}).get("id")

                    if str(from_id) != str(self.tg_notifier.chat_id):
                        continue

                    await self._handle_callback_query(query_id, data)

                    # 确认更新已处理
                    await self.tg_notifier.session.post(
                        f"https://api.telegram.org/bot{self.tg_notifier.bot_token}/getUpdates",
                        json={"offset": update["update_id"] + 1}
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"TG 回调轮询错误: {e}")

    async def _handle_callback_query(self, query_id: str, data: str):
        """处理 Telegram 回调查询

        Args:
            query_id: 回调ID
            data: 回调数据
        """
        try:
            if data == "status":
                result, _ = await self.handle_tg_command("status")
                await self.tg_notifier.answer_callback(query_id, result, alert=True)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data == "pause":
                result, _ = await self.handle_tg_command("pause")
                await self.tg_notifier.answer_callback(query_id, result)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data == "resume":
                result, _ = await self.handle_tg_command("resume")
                await self.tg_notifier.answer_callback(query_id, result)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data == "schedule":
                # 发送多选时间段面板
                await self.tg_notifier.answer_callback(query_id, "")
                await self.tg_notifier.send_schedule_panel(self.get_schedule_info())

            elif data.startswith("toggle_slot_"):
                # 切换小时时间段: toggle_slot_HH
                hour = int(data.split("_")[2])
                self.schedule_slots[hour] = not self.schedule_slots[hour]
                self._save_schedule_config(self.schedule_slots)  # 保存到文件
                # 更新面板（编辑消息）
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "preset_night":
                # 夜间 0-6点
                for h in range(0, 6):
                    self.schedule_slots[h] = True
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "preset_morning":
                # 早上 6-12点
                for h in range(6, 12):
                    self.schedule_slots[h] = True
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "preset_afternoon":
                # 下午 12-18点
                for h in range(12, 18):
                    self.schedule_slots[h] = True
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "preset_evening":
                # 晚上 18-24点
                for h in range(18, 24):
                    self.schedule_slots[h] = True
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "select_all":
                # 全选24小时
                self.schedule_slots = [True] * 24
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "clear_all":
                # 清空选择
                self.schedule_slots = [False] * 24
                self._save_schedule_config(self.schedule_slots)
                await self.tg_notifier.edit_schedule_panel(self.get_schedule_info(), query_id)

            elif data == "confirm_schedule":
                # 确认定时设置
                result = f"✅ 定时已保存\n\n{self.get_schedule_status()}"
                await self.tg_notifier.answer_callback(query_id, result, alert=True)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data.startswith("set_schedule_"):
                # 兼容旧的预设格式
                parts = data.split("_")
                if len(parts) >= 4:
                    start_h = int(parts[2][:2])
                    start_m = int(parts[2][2:])
                    end_h = int(parts[3][:2])
                    end_m = int(parts[3][2:])
                    await self.update_schedule(start_h, start_m, end_h, end_m)
                    result = f"✅ 定时已更新\n\n{self.get_schedule_status()}"
                    await self.tg_notifier.answer_callback(query_id, result, alert=True)
                    await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data == "disable_schedule":
                # 清空时间段并关闭定时
                self.schedule_slots = [False] * 24
                self._save_schedule_config(self.schedule_slots)  # 保存到文件
                import config
                config.SCHEDULE_ENABLED = False
                global SCHEDULE_ENABLED
                SCHEDULE_ENABLED = False
                result = f"✅ 定时已关闭\n\n{self.get_schedule_status()}"
                await self.tg_notifier.answer_callback(query_id, result, alert=True)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            elif data == "switch_account":
                # 发送账号选择面板
                await self.tg_notifier.answer_callback(query_id, "")
                await self.tg_notifier.send_switch_account_panel(self.accounts, self.current_account_index)

            elif data == "switch_locked":
                # 账号未配置密钥
                await self.tg_notifier.answer_callback(query_id, "⚠️ 该账号未配置密钥，请在 config.py 中配置", alert=True)

            elif data.startswith("switch_to_"):
                # 解析目标账号: switch_to_N
                target_index = int(data.split("_")[2])
                if target_index == self.current_account_index:
                    await self.tg_notifier.answer_callback(query_id, "⚠️ 当前已经是这个账号", alert=True)
                else:
                    # 设置目标账号索引
                    self.target_account_index = target_index
                    self.switch_requested = True
                    target_name = self.accounts[target_index]['name']
                    await self.tg_notifier.answer_callback(query_id, f"🔄 正在切换到 {target_name}...", False)

            elif data == "stop":
                result, _ = await self.handle_tg_command("stop")
                await self.tg_notifier.answer_callback(query_id, result, alert=True)
                await self.tg_notifier.update_control_panel(self.get_status_data())

            else:
                await self.tg_notifier.answer_callback(query_id, "")

        except Exception as e:
            logger.error(f"处理回调失败: {e}")
            await self.tg_notifier.answer_callback(query_id, f"错误: {e}")

    async def check_and_handle_last_account(self) -> bool:
        """检查是否只剩最后一个账号需要刷量，如果是则暂停5分钟后继续

        Returns:
            bool: True表示已处理(暂停后继续当前账号)，False表示正常切号
        """
        # 只有在完成一轮后才检查
        if not self.round_completed:
            return False

        # 统计当前有多少账号成交量不足
        stats = self.pnl_tracker.get_stats()
        current_idx = self.current_account_index

        # 临时记录当前账号成交量
        temp_volumes = self.account_volumes.copy()
        temp_volumes[current_idx] = stats['volume']

        # 检查有多少账号成交量不足
        low_volume_accounts = []
        for idx in range(len(self.accounts)):
            volume = temp_volumes.get(idx, 0)
            if volume < self.min_volume_threshold:
                retry_count = self.account_retry_count.get(idx, 0)
                if retry_count < self.max_round_retries:
                    low_volume_accounts.append(idx)

        # 如果只剩当前账号成交量不足
        if len(low_volume_accounts) == 1 and low_volume_accounts[0] == current_idx:
            logger.warning(f"⚠️ 只剩当前账号 {self.current_account_name} 成交量不足")
            logger.info(f"💤 暂停5分钟后继续刷量...")

            await self.tg_notifier.send(
                f"⚠️ *最后一个账号触发切号条件*\n\n"
                f"账号: `{self.current_account_name}`\n"
                f"当前成交量: `${stats['volume']:,.0f}` USD\n"
                f"目标成交量: `${self.min_volume_threshold:,.0f}` USD\n\n"
                f"💤 暂停 5 分钟后继续刷量..."
            )

            # 暂停5分钟 (300秒)
            for remaining in range(300, 0, -1):
                if not self.running:
                    break
                mins, secs = divmod(remaining, 60)
                self.update_display(f"💤 最后一号暂停中 {mins}:{secs:02d}")
                await asyncio.sleep(1)

            logger.info("✅ 暂停结束，继续刷量")
            await self.tg_notifier.send("✅ 暂停结束，继续刷量")
            return True

        return False

    async def switch_account(self, reason: str):
        """切换到下一个账号

        Args:
            reason: 切换原因
        """
        # 打印当前账号统计
        stats = self.pnl_tracker.get_stats()
        logger.info(f"📊 账号切换 [{self.current_account_name}] 原因: {reason}")
        logger.info(f"   统计: 循环 {self.cycle_count} | 盈亏: ${stats['pnl']:+.4f} | 磨损: {stats['per_10k']:+.2f}/万")
        logger.info(f"   成交量: ${stats['volume']:,.2f} | 方向: 多{stats['long']} 空{stats['short']}")

        # 记录当前账号的成交量
        current_idx = self.current_account_index
        self.account_volumes[current_idx] = stats['volume']

        # 判断是否为低交易量切号
        if stats['volume'] < self.LOW_VOLUME_THRESHOLD:
            self.consecutive_low_volume_switches += 1
            logger.warning(f"⚠️ 低交易量切号 #{self.consecutive_low_volume_switches}: "
                           f"${stats['volume']:,.0f} < ${self.LOW_VOLUME_THRESHOLD}")
        else:
            self.consecutive_low_volume_switches = 0  # 交易量达标，重置计数

        if TG_NOTIFY_FEE_PAUSE:
            elapsed = time.time() - self.start_time if self.start_time else 0
            elapsed_min = elapsed / 60

            # 盈亏颜色符号
            pnl_emoji = "🟢" if stats['pnl'] >= 0 else "🔴"

            report = (
                f"📊 *账号 [{self.current_account_name}] 交易结束*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 余额变化:\n"
                f"   初始: `${stats['initial']:.4f}` USDC\n"
                f"   最终: `${stats['current']:.4f}` USDC\n"
                f"   盈亏: {pnl_emoji} `${stats['pnl']:+.4f}` USDC\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 交易统计:\n"
                f"   总交易量: `${stats['volume']:,.0f}` USD\n"
                f"   循环次数: `{self.cycle_count}` 次\n"
                f"   方向: 多 `{stats['long']}` 次 | 空 `{stats['short']}` 次\n"
                f"   运行时长: `{elapsed_min:.1f}` 分钟\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 磨损分析:\n"
                f"   磨损: `{stats['per_10k']:+.2f}` /万\n"
                f"   总{'盈利' if stats['pnl'] >= 0 else '亏损'}: `${abs(stats['pnl']):.4f}` USDC\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔄 切换原因: `{reason}`"
            )
            await self.tg_notifier.send(report)

        # 切换前先平仓
        position = self.get_current_position()
        if position:
            print(f"\n🔄 切换账号前，正在平仓 {position['side']} {position['size']} BTC...")
            close_success = await self.close_position(position)

            if not close_success:
                logger.error("❌ 平仓失败，取消切换账号")
                print("❌ 平仓失败，取消切换账号")
                return

            # 再次确认持仓已清空
            await asyncio.sleep(0.5)
            final_check = self.get_current_position()
            if final_check:
                logger.error(f"❌ 持仓未清空 {final_check}，取消切换账号")
                print(f"❌ 持仓未清空，取消切换账号")
                return

            print(f"✅ 持仓已清空，继续切换账号")

        # 检查是否所有账号都已切过一轮
        if len(self.account_volumes) >= len(self.accounts):
            self.round_completed = True
            logger.info(f"✅ 完成第 {self.current_round + 1} 轮，所有账号已切换一次")

            # 检查哪些账号成交量不足
            low_volume_accounts = []
            for idx, volume in self.account_volumes.items():
                if volume < self.min_volume_threshold:
                    retry_count = self.account_retry_count.get(idx, 0)
                    if retry_count < self.max_round_retries:
                        low_volume_accounts.append((idx, volume, retry_count))

            if low_volume_accounts:
                # 按成交量从小到大排序，优先刷成交量最少的
                low_volume_accounts.sort(key=lambda x: x[1])
                next_idx, vol, retry = low_volume_accounts[0]

                logger.info(f"🔄 账号 {self.accounts[next_idx]['name']} 成交量不足: ${vol:,.0f} < ${self.min_volume_threshold:,.0f}")
                logger.info(f"   重试次数: {retry + 1}/{self.max_round_retries}")

                # 切换到成交量不足的账号
                self.current_account_index = next_idx
                self.account_retry_count[next_idx] = retry + 1

                # 清空该账号的成交量记录，重新开始
                self.account_volumes[next_idx] = 0
            else:
                # 所有账号都达标，完成一轮
                self.current_round += 1
                logger.info(f"🎉 第 {self.current_round} 轮完成！所有账号成交量均达标")

                # 重置轮次数据
                self.account_volumes.clear()
                self.account_retry_count.clear()
                self.round_completed = False

                # 切换到下一个账号
                self.current_account_index = (self.current_account_index + 1) % len(self.accounts)
        else:
            # 还没完成一轮，继续切换到下一个账号
            self.current_account_index = (self.current_account_index + 1) % len(self.accounts)

        # 关闭当前连接
        try:
            await self.paradex.ws_client.close()
        except:
            pass

        print(f"\n{'='*70}")
        print(f"🔄 切换到账号: [{self.current_account_index + 1}/{len(self.accounts)}] {self.accounts[self.current_account_index]['name']}")
        print(f"📊 进度: 第 {self.current_round + 1} 轮 | 已切换 {len(self.account_volumes)}/{len(self.accounts)} 个账号")
        print(f"{'='*70}\n")

    async def reset_for_new_account(self):
        """为新账号重置状态"""
        self.cycle_count = 0
        self.successful_cycles = 0
        self.failed_cycles = 0
        self.consecutive_failures = 0
        self.last_direction = "-"
        self.ws_update_count = 0
        self.high_cost_start_time = 0.0  # 重置磨损计时器
        self.circuit_break = False  # 重置熔断状态
        self.pnl_tracker = BalancePnLTracker()
        self.rate_limiter = RateLimiter(MAX_ORDERS_PER_MINUTE, MAX_ORDERS_PER_HOUR, MAX_ORDERS_PER_DAY)
        self.latency_tracker = LatencyTracker()
        self.volatility_tracker = VolatilityTracker(window_seconds=VOLATILITY_WINDOW_SECONDS)
        self.current_bbo = {
            "bid": 0.0, "ask": 0.0,
            "bid_size": 0.0, "ask_size": 0.0,
            "spread": 100.0, "mid_price": 0.0,
            "last_update": 0,
        }
        self.recent_cycle_times = deque(maxlen=5)
        self.recent_freshness = deque(maxlen=3)

    async def shutdown(self):
        self.running = False

        # 停止 Telegram 回调轮询
        if self.tg_callback_task:
            self.tg_callback_task.cancel()
            try:
                await self.tg_callback_task
            except asyncio.CancelledError:
                pass

        # 检查并平仓
        position = self.get_current_position()
        if position:
            print(f"\n⚠️ 检测到持仓: {position['side']} {position['size']}")
            print("🔄 正在平仓...")
            await self.close_position(position)
            await asyncio.sleep(0.5)

        final_balance = self.get_account_balance()
        if final_balance > 0:
            self.pnl_tracker.update_balance(final_balance)

        elapsed = time.time() - self.start_time if self.start_time else 0
        stats = self.pnl_tracker.get_stats()
        latency = self.latency_tracker.get_stats()

        # 清屏后打印最终统计
        print("\n" * 2)
        print("=" * 70)
        print("📊 策略统计")
        print("=" * 70)
        print(f"   循环: {self.cycle_count} (成功: {self.successful_cycles}, 失败: {self.failed_cycles})")
        print(f"   方向: 多{stats['long']}次 | 空{stats['short']}次")
        print(f"   运行: {elapsed/60:.1f} 分钟")
        print("-" * 70)
        print(f"💰 余额:")
        print(f"   初始: ${stats['initial']:.4f} USDC")
        print(f"   当前: ${stats['current']:.4f} USDC")
        print(f"   盈亏: ${stats['pnl']:+.4f} USDC")
        print("-" * 70)
        print(f"📈 交易量: ${stats['volume']:,.2f} USD")
        print("-" * 70)
        if latency["recent"]:
            print(f"⏱️ 延迟: 平均 {latency['avg']:.0f}ms | 最小 {latency['min']:.0f}ms | 最大 {latency['max']:.0f}ms")
        print("=" * 70)

        # 发送停止通知
        if TG_NOTIFY_START_STOP:
            await self.tg_notifier.send(
                f"⏹️ Paradex Scalper 已停止\n"
                f"💰 最终盈亏: {stats['pnl']:+.2f} USDC\n"
                f"🔄 循环次数: {self.cycle_count}\n"
                f"⏱️ 运行时长: {elapsed/60:.0f} 分钟"
            )

        # 关闭连接
        try:
            await self.paradex.ws_client.close()
        except:
            pass

        # 关闭 Telegram 会话
        await self.tg_notifier.close()

        print("👋 已退出")


async def main():
    if not ACCOUNTS:
        print("❌ 未配置账号! 请在 config.py 中配置 ACCOUNTS 列表。")
        return

    # 检查是否至少有一个账号配置了密钥
    valid_accounts = [acc for acc in ACCOUNTS if acc.get("L2_ADDRESS") and acc.get("L2_PRIVATE_KEY")]
    if not valid_accounts:
        print("❌ 没有有效账号! 请在 config.py 中至少配置一个账号的 L2_ADDRESS 和 L2_PRIVATE_KEY。")
        return

    # 输入最小成交量阈值
    print("\n" + "="*70)
    print("🔧 参数配置")
    print("="*70)

    while True:
        try:
            min_volume_input = input("请输入最小成交量阈值 (10-500万，单位:万USD): ").strip()
            min_volume = float(min_volume_input)
            if 10 <= min_volume <= 500:
                min_volume_threshold = min_volume * 10000  # 转换为USD
                break
            else:
                print("❌ 输入超出范围，请输入 10-500 之间的数字")
        except ValueError:
            print("❌ 输入无效，请输入数字")
        except KeyboardInterrupt:
            print("\n⏹️ 已取消")
            return

    # 输入最大循环次数
    while True:
        try:
            max_cycles_input = input("请输入最大循环次数 (100-10000): ").strip()
            max_cycles = int(max_cycles_input)
            if 100 <= max_cycles <= 10000:
                break
            else:
                print("❌ 输入超出范围，请输入 100-10000 之间的整数")
        except ValueError:
            print("❌ 输入无效，请输入整数")
        except KeyboardInterrupt:
            print("\n⏹️ 已取消")
            return

    print(f"\n✅ 配置完成:")
    print(f"   最小成交量: ${min_volume_threshold:,.0f} USD ({min_volume}万)")
    print(f"   最大循环次数: {max_cycles}")
    print("="*70 + "\n")

    scalper = WebSocketScalper(ACCOUNTS)
    scalper.min_volume_threshold = min_volume_threshold
    scalper.max_round_retries = max_cycles
    await scalper.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹️ 已中断")
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
