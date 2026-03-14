"""
FTSeek 配置文件
所有可配置项集中管理，部署时只需修改此文件或对应环境变量

⚠️ 安全提示:
  - API_TOKEN 必须通过环境变量设置，禁止使用默认值
  - 钉钉 Webhook Secret 必须通过环境变量设置
  - 富途账号密码建议通过环境变量设置
"""
import os
import json
import secrets
from pathlib import Path

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# 加载 .env 文件中的环境变量
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
#  OpenD 配置
# ============================================================
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))
OPEND_DIR = os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7")

# 富途账号 (首次使用需要在 FutuOpenD.xml 中配置)
# ⚠️ 安全建议：优先使用 OpenD 的配置文件存储账号密码，而非环境变量
FUTU_LOGIN_ACCOUNT = os.getenv("FUTU_LOGIN_ACCOUNT", "")
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 交易环境：0=真实环境，1=模拟环境
TRADE_ENV = int(os.getenv("TRADE_ENV", "1"))  # 默认模拟环境，安全第一

# ============================================================
#  钉钉推送配置
# ============================================================
# ⚠️ 安全：必须通过环境变量设置，禁止硬编码
DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET")

if not DINGTALK_WEBHOOK_URL:
    # 仅保留默认值用于开发测试，生产环境必须设置环境变量
    DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL_DEFAULT", "")
if not DINGTALK_SECRET:
    DINGTALK_SECRET = os.getenv("DINGTALK_SECRET_DEFAULT", "")

# ============================================================
#  FastAPI 确认回调服务
# ============================================================
CALLBACK_HOST = os.getenv("CALLBACK_HOST", "0.0.0.0")
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "8899"))
# 外部可访问地址（用于生成确认链接）
CALLBACK_EXTERNAL_URL = os.getenv(
    "CALLBACK_EXTERNAL_URL",
    f"http://39.97.58.4:{CALLBACK_PORT}"
)

# 确认链接有效期（秒）
SIGNAL_EXPIRE_SECONDS = int(os.getenv("SIGNAL_EXPIRE_SECONDS", "300"))  # 5 分钟

# ============================================================
#  🔐 API 鉴权 Token (安全加固)
# ============================================================
# 必须通过环境变量设置，禁止使用默认值
API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    # 开发环境：如果没有设置，生成一个临时 token 并警告
    if os.getenv("FTSEEK_DEV_MODE", "").lower() in ("1", "true", "yes"):
        API_TOKEN = secrets.token_hex(16)
        print(f"⚠️  警告：API_TOKEN 未设置，已生成临时 Token: {API_TOKEN}")
        print("⚠️  生产环境必须设置环境变量 API_TOKEN")
    else:
        raise RuntimeError(
            "🔐 安全错误：API_TOKEN 环境变量未设置！\n"
            "请执行以下命令生成安全 Token 并设置：\n"
            "  export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')\n"
            "或在 systemd 服务文件中添加：\n"
            "  Environment=\"API_TOKEN=你的安全 token\""
        )

# ============================================================
#  监控标的 & 市场配置
# ============================================================
# 监控标的列表 (港股 + 美股双市场)
# 港股格式："HK.00700", 美股格式："US.AAPL"
# 注意：美股需开通行情权限后才能订阅，未开通前美股标的会自动跳过
WATCH_LIST_HK = json.loads(os.getenv("WATCH_LIST_HK", '["HK.00700", "HK.09988", "HK.09992"]'))
WATCH_LIST_US = json.loads(os.getenv("WATCH_LIST_US", '["US.AAPL", "US.TSLA", "US.NVDA", "US.MSFT", "US.AMZN"]'))
WATCH_LIST = WATCH_LIST_HK + WATCH_LIST_US

# 默认订阅类型
# "QUOTE" - 报价，"K_1M" - 1 分钟 K 线，"K_5M" - 5 分钟 K 线,
# "K_15M" - 15 分钟 K 线，"K_DAY" - 日 K
SUBSCRIBE_TYPES = json.loads(os.getenv("SUBSCRIBE_TYPES", '["QUOTE", "K_1M"]'))

# ============================================================
#  策略配置 (可按需扩展)
# ============================================================
STRATEGIES = {
    # 价格阈值突破
    "price_break": {
        "enabled": True,
        "thresholds": {
            # 港股标的阈值 (港币) - 基于 2026-02-24 实时价格 ±3%
            "HK.00700": {"upper": 536.0, "lower": 504.0},   # 腾讯 现价 520
            "HK.09988": {"upper": 152.5, "lower": 143.5},   # 阿里 现价 148
            # 美股标的阈值 (美元) - 开通美股权限后自动生效
            "US.AAPL": {"upper": 260.0, "lower": 230.0},    # 苹果
            "US.TSLA": {"upper": 380.0, "lower": 320.0},    # 特斯拉
            "US.NVDA": {"upper": 145.0, "lower": 120.0},    # 英伟达
            "US.MSFT": {"upper": 430.0, "lower": 390.0},    # 微软
            "US.AMZN": {"upper": 235.0, "lower": 210.0},    # 亚马逊
        }
    },
    # 均线交叉
    "ma_cross": {
        "enabled": False,
        "fast_period": 5,
        "slow_period": 20,
    },
    # 成交量异动
    "volume_alert": {
        "enabled": False,
        "volume_ratio_threshold": 2.0,  # 成交量是近期均值的 N 倍时触发
        "lookback_periods": 20,
    },
}

# ============================================================
#  交易配置
# ============================================================
# 默认下单数量 (港股最小 100 股，美股最小 1 股，会按市场自动判断)
DEFAULT_ORDER_QTY_HK = int(os.getenv("DEFAULT_ORDER_QTY_HK", "100"))
DEFAULT_ORDER_QTY_US = int(os.getenv("DEFAULT_ORDER_QTY_US", "1"))
DEFAULT_ORDER_QTY = int(os.getenv("DEFAULT_ORDER_QTY", "100"))  # 兼容旧逻辑
# 默认订单类型："MARKET" (市价) / "LIMIT" (限价)
DEFAULT_ORDER_TYPE = os.getenv("DEFAULT_ORDER_TYPE", "MARKET")
# 最大单笔金额限制
MAX_ORDER_AMOUNT = float(os.getenv("MAX_ORDER_AMOUNT", "50000.0"))

# ============================================================
#  日志配置
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ============================================================
#  新增策略配置 (TradingView 风格)
# ============================================================
STRATEGIES.update({
    # RSI 超买超卖
    "rsi": {
        "enabled": True,
        "period": 14,
        "overbought": 70,
        "oversold": 30,
    },
    # MACD 趋势跟踪
    "macd": {
        "enabled": True,
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
        "signal_mode": "histogram",  # histogram | zero_cross | both
    },
    # 布林带
    "bollinger": {
        "enabled": True,
        "period": 20,
        "std_dev": 2.0,
        "mode": "reversion",  # reversion | breakout
    },
    # SuperTrend 自适应趋势
    "supertrend": {
        "enabled": True,
        "atr_period": 10,
        "multiplier": 3.0,
    },
    # 多因子融合 (核心策略)
    "confluence": {
        "enabled": True,
        "min_factors": 3,  # 至少 N 个因子同向确认
        "enabled_factors": ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"],
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "bb_period": 20,
        "bb_std": 2.0,
        "st_period": 10,
        "st_multiplier": 3.0,
        "ma_fast": 10,
        "ma_slow": 30,
        "vol_ratio": 1.5,
        "adx_threshold": 25,
    },
    # 融合策略 v2.0 (技术 + 情绪)
    "fusion_v2": {
        "enabled": True,
        "technical_weight": float(os.getenv("TECHNICAL_WEIGHT", "0.6")),
        "sentiment_weight": float(os.getenv("SENTIMENT_WEIGHT", "0.4")),
        "min_confidence": float(os.getenv("MIN_CONFIDENCE", "0.4")),
        "signal_level_threshold": os.getenv("SIGNAL_LEVEL_THRESHOLD", "recommended"),
        "per_stock_daily_limit": int(os.getenv("PER_STOCK_DAILY_LIMIT", "3")),
        "global_daily_limit": int(os.getenv("GLOBAL_DAILY_LIMIT", "20")),
        # 技术指标参数
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "ma_fast_period": 10,
        "ma_slow_period": 30,
        "bb_period": 20,
        "bb_std": 2.0,
    },
})

# TradingView Webhook 配置
# ⚠️ 安全：可以与 API_TOKEN 相同，但建议单独设置
TRADINGVIEW_WEBHOOK_TOKEN = os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", API_TOKEN)

# ============================================================
#  共识引擎配置
# ============================================================
CONSENSUS_WINDOW = int(os.getenv("CONSENSUS_WINDOW", "60"))
CONSENSUS_THRESHOLD = float(os.getenv("CONSENSUS_THRESHOLD", "1.5"))
CONSENSUS_ADX_TREND = float(os.getenv("CONSENSUS_ADX_TREND", "25"))
CONSENSUS_ADX_RANGE = float(os.getenv("CONSENSUS_ADX_RANGE", "20"))

# 信号冷却时间 (秒)
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "300"))
