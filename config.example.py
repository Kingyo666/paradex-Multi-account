# Paradex BTC 秒开关配置 - 多号轮换版

import os

# ==================== API 配置 ====================
# Paradex 环境
PARADEX_ENV = "MAINNET"  # MAINNET 或 TESTNET

# Paradex API URLs
API_BASE_URL = "https://api.prod.paradex.trade"
WS_URL = "wss://ws.api.prod.paradex.trade/v1"

# ==================== 多账号配置 ====================
# 多账号列表，依次轮换
ACCOUNTS = [
    {
        "name": "账号1",
        "L2_ADDRESS": "0x你的L2地址",
        "L2_PRIVATE_KEY": "0x你的L2私钥",
    },
    {
        "name": "账号2",
        "L2_ADDRESS": "0x你的L2地址",
        "L2_PRIVATE_KEY": "0x你的L2私钥",
    },
    {
        "name": "账号3",
        "L2_ADDRESS": "0x你的L2地址",
        "L2_PRIVATE_KEY": "0x你的L2私钥",
    },
    {
        "name": "账号4",
        "L2_ADDRESS": "0x你的L2地址",
        "L2_PRIVATE_KEY": "0x你的L2私钥",
    },
]

# ==================== 轮换切换条件 ====================
# 磨损切换阈值：每万成交量的磨损超过此值时切换账号
SWITCH_COST_PER_10K = 0.3  # 单位：元/万

# ==================== 交易配置 ====================
MARKET = "BTC-USD-PERP"

# 每单大小 (BTC)
ORDER_SIZE_BTC = 0.008

# 价差阈值 (百分比)
# 当价差 <= 此值时触发开仓
MAX_SPREAD_PERCENT = 0.0005  # 0.0005%

# 最大循环次数 (一开一关为一个循环)
# 每循环下2单，800循环 = 1600单 = Retail 24h 上限
MAX_CYCLES = 800

# ==================== 波动率过滤配置 ====================
# 波动率过高时暂停交易，降低损耗
VOLATILITY_FILTER_ENABLED = True     # 是否启用波动率过滤
MAX_VOLATILITY_PCT = 0.08            # 最大波动率（百分比），超过则暂停交易
VOLATILITY_WINDOW_SECONDS = 15       # 波动率计算窗口（秒）

# ==================== 日志配置 ====================
LOG_FILE = "scalper.log"
LOG_LEVEL = "INFO"

# ==================== 安全配置 ====================
# 最大连续失败次数 (超过则暂停)
MAX_CONSECUTIVE_FAILURES = 5

# 紧急停止文件 (存在此文件则停止运行)
EMERGENCY_STOP_FILE = "STOP"

# ==================== 定时配置 ====================
# 启用定时交易（脚本24/7运行，但只在指定时间窗口内交易）
SCHEDULE_ENABLED = False         # True=启用定时, False=24小时交易
SCHEDULE_START_HOUR = 0          # 开始小时 (0-23)
SCHEDULE_START_MINUTE = 0        # 开始分钟
SCHEDULE_END_HOUR = 23           # 结束小时 (0-23)
SCHEDULE_END_MINUTE = 59          # 结束分钟

# ==================== Telegram 通知配置 ====================
TG_ENABLED = False              # True=启用Telegram通知
TG_BOT_TOKEN = "你的BotToken"                # Telegram Bot Token
TG_CHAT_ID = "你的ChatID"                  # Telegram Chat ID

# 通知开关
TG_NOTIFY_START_STOP = True      # 脚本启动/停止通知
TG_NOTIFY_FEE_PAUSE = True       # 检测到手续费时通知
TG_NOTIFY_SCHEDULE = True        # 定时开始/结束通知
TG_NOTIFY_REPORT_INTERVAL = 60   # 状态报告间隔 (秒)
