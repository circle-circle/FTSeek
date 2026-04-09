"""
FTSeek 配置文件
所有可配置项集中管理，部署时只需修改此文件或对应环境变量

⚠️ 安全提示:
  - API_TOKEN 必须通过环境变量设置，禁止使用默认值
  - 钉钉 Webhook Secret 必须通过环境变量设置
  - 富途账号密码建议通过环境变量设置

📋 配置说明 (2026-04-06 更新):
  - 策略配置、交易配置等已迁移至 config/config.yaml
  - 本文件仅保留基础配置和无法 YAML 化的逻辑
  - 推荐使用配置中心访问配置：get_config_center().get('strategy.xxx')
"""
import os
import json
import logging
import secrets
from pathlib import Path

# ============================================================
#  配置中心 (优先加载)
# ============================================================
from config.config_center import get_config_center
config_center = get_config_center()
config_center.load()

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
OPEND_DIR = os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_10.1.6108_Centos7/Futu_OpenD_10.1.6108_Centos7")

# 富途账号 (首次使用需要在 FutuOpenD.xml 中配置)
# ⚠️ 安全建议：优先使用 OpenD 的配置文件存储账号密码，而非环境变量
FUTU_LOGIN_ACCOUNT = os.getenv("FUTU_LOGIN_ACCOUNT", "")
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 交易环境：0=真实环境，1=模拟环境
# ⚠️ 安全保护：即使环境变量设置为 0，也强制锁定为 1（模拟）
# 如需切换实盘，必须同时在此处修改代码 + 设置 ALLOW_REAL_TRADING=true
_raw_trade_env = int(os.getenv("TRADE_ENV", "1"))
_allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
TRADE_ENV = _raw_trade_env if (_allow_real and _raw_trade_env == 0) else 1
if _raw_trade_env == 0 and not _allow_real:
    import warnings
    warnings.warn(
        "[安全拦截] TRADE_ENV=0 被忽略，当前强制使用模拟环境。"
        "如需实盘，请同时设置 ALLOW_REAL_TRADING=true",
        RuntimeWarning, stacklevel=2
    )

# 模拟账户 ID (2026-03-31: 修正账户配置)
# 账户说明：
#   18820895 = CASH 账户：支持港股普通买卖
#   8246818 = MARGIN 账户：融资账户，支持期权
# 注意：实际账户类型以富途 OpenD 返回为准
SIMULATE_ACCOUNT_ID = os.getenv("SIMULATE_ACCOUNT_ID", "18820895")  # CASH 账户，支持港股交易

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
WATCH_LIST_HK = json.loads(os.getenv("WATCH_LIST_HK", '["HK.09992", "HK.00981", "HK.03690", "HK.00700", "HK.09888", "HK.01024", "HK.09988", "HK.01810", "HK.02318", "HK.09618"]'))  # 2026-04-05: 扩展至 10 只高流动性港股
# 美股默认禁用 (需开通权限后设置 WATCH_LIST_US 环境变量)
WATCH_LIST_US = json.loads(os.getenv("WATCH_LIST_US", '[]'))
WATCH_LIST = WATCH_LIST_HK + WATCH_LIST_US

# 默认订阅类型
# "QUOTE" - 报价，"K_1M" - 1 分钟 K 线，"K_3M" - 3 分钟 K 线，"K_5M" - 5 分钟 K 线,
# "K_15M" - 15 分钟 K 线，"K_30M" - 30 分钟 K 线，"K_60M" - 60 分钟 K 线，"K_DAY" - 日 K
# 2026-03-25: 新增 K_3M/K_5M 超短线周期，用于提高趋势判断精度，减少误判
_subscribe_types_str = os.getenv("SUBSCRIBE_TYPES", 'QUOTE,K_1M,K_3M,K_5M,K_15M,K_30M,K_60M,K_DAY')
# 支持两种格式：JSON 数组 或 逗号分隔字符串
if _subscribe_types_str.startswith('['):
    SUBSCRIBE_TYPES = json.loads(_subscribe_types_str)
else:
    SUBSCRIBE_TYPES = [s.strip() for s in _subscribe_types_str.split(',')]

# ============================================================
#  策略配置 (2026-04-06 更新：从配置中心读取)
# ============================================================
# 说明：策略详细参数已迁移至 config/config.yaml
# 访问方式：使用 config_center.get('strategy.<strategy_name>')
#
# 保留 STRATEGIES 字典用于向后兼容，但数据来源于配置中心
# ============================================================

def _load_strategies_from_config():
    """从配置中心加载策略配置"""
    strategies = {}

    # 从配置中心读取各策略配置
    strategy_names = [
        'price_break', 'volume_alert', 'rsi', 'macd', 'bollinger',
        'supertrend', 'confluence', 'confluence_v2', 'fusion_v2',
        'sentiment_ai', 'open_close_effect'
    ]

    for name in strategy_names:
        cfg = config_center.get(f'strategy.{name}')
        if cfg and hasattr(cfg, 'to_dict'):
            strategies[name] = cfg.to_dict()
        elif cfg and isinstance(cfg, dict):
            strategies[name] = cfg
        else:
            # 配置不存在时使用默认值
            if name == 'supertrend':
                strategies[name] = {'enabled': True, 'atr_period': 10, 'multiplier': 2.0}
            elif name == 'confluence_v2':
                strategies[name] = {'enabled': True, 'min_factors': 3}
            elif name == 'fusion_v2':
                strategies[name] = {'enabled': True}
            elif name == 'sentiment_ai':
                strategies[name] = {'enabled': True}
            elif name == 'open_close_effect':
                strategies[name] = {'enabled': True}
            else:
                strategies[name] = {'enabled': False}

    return strategies

STRATEGIES = _load_strategies_from_config()

# ============================================================
#  交易配置 (2026-04-06 更新：从配置中心读取)
# ============================================================
# 港股每手股数配置 (从配置中心读取，回退到硬编码)
HK_LOT_SIZES_CFG = config_center.get('trading.order.hk_lot_sizes')
HK_LOT_SIZES = HK_LOT_SIZES_CFG.to_dict() if HK_LOT_SIZES_CFG and hasattr(HK_LOT_SIZES_CFG, 'to_dict') else {
    'HK.00700': 100, 'HK.09988': 100, 'HK.03690': 100, 'HK.09992': 200,
    'HK.00981': 500, 'HK.01810': 200, 'HK.01024': 100, 'HK.01347': 1000,
    'HK.01093': 2000, 'HK.00175': 1000, 'HK.01378': 500,
}

# 默认下单数量 (按股票配置)
DEFAULT_ORDER_QTY_BY_CODE_CFG = config_center.get('trading.order.default_order_qty_by_code')
DEFAULT_ORDER_QTY_BY_CODE = DEFAULT_ORDER_QTY_BY_CODE_CFG.to_dict() if DEFAULT_ORDER_QTY_BY_CODE_CFG and hasattr(DEFAULT_ORDER_QTY_BY_CODE_CFG, 'to_dict') else {
    'HK.00700': 100, 'HK.09988': 100, 'HK.03690': 100, 'HK.09992': 200,
    'HK.00981': 500, 'HK.01810': 200, 'HK.01024': 100, 'HK.09888': 100,
    'HK.02318': 500, 'HK.09618': 100, 'HK.01347': 1000, 'HK.01093': 2000,
    'HK.00175': 1000, 'HK.01378': 500,
}

DEFAULT_ORDER_QTY_HK = int(config_center.get('trading.order.default_order_qty_hk', os.getenv("DEFAULT_ORDER_QTY_HK", "100")))
DEFAULT_ORDER_QTY_US = int(config_center.get('trading.order.default_order_qty_us', os.getenv("DEFAULT_ORDER_QTY_US", "1")))
DEFAULT_ORDER_QTY = int(config_center.get('trading.order.default_order_qty', os.getenv("DEFAULT_ORDER_QTY", "100")))

def get_lot_size(code: str) -> int:
    """
    获取股票每手股数

    Args:
        code: 股票代码 (如 "HK.03690")

    Returns:
        每手股数，默认返回 100
    """
    if code in HK_LOT_SIZES:
        return HK_LOT_SIZES[code]
    return 100

# 默认订单类型："MARKET" (市价) / "LIMIT" (限价)
DEFAULT_ORDER_TYPE = config_center.get('trading.order.default_order_type', os.getenv("DEFAULT_ORDER_TYPE", "MARKET"))
# 最大单笔金额限制
MAX_ORDER_AMOUNT = float(config_center.get('trading.order.max_order_amount', os.getenv("MAX_ORDER_AMOUNT", "120000.0")))

# ============================================================
#  日志配置
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ============================================================
#  共识引擎配置 (2026-04-06: 从配置中心读取)
# ============================================================
CONSENSUS_WINDOW = int(config_center.get('strategy.cooldown.signal_cooldown', os.getenv("CONSENSUS_WINDOW", "60")))
CONSENSUS_THRESHOLD = float(config_center.get('strategy.consensus.threshold', os.getenv("CONSENSUS_THRESHOLD", "0.6")))
CONSENSUS_ADX_TREND = float(config_center.get('strategy.consensus.adx_trend', os.getenv("CONSENSUS_ADX_TREND", "25")))
CONSENSUS_ADX_RANGE = float(config_center.get('strategy.consensus.adx_range', os.getenv("CONSENSUS_ADX_RANGE", "20")))

# 信号冷却时间 (秒)
SIGNAL_COOLDOWN = int(config_center.get('strategy.cooldown.signal_cooldown', os.getenv("SIGNAL_COOLDOWN", "120")))

# 最大持仓数限制
MAX_OPEN_POSITIONS = int(config_center.get('trading.max_open_positions', os.getenv("MAX_OPEN_POSITIONS", "4")))

# 卖空功能开关 - 2026-04-08: 模拟盘不支持卖空，默认关闭
SHORT_SELLING_ENABLED_VAL = config_center.get('trading.short_selling_enabled', os.getenv('SHORT_SELLING_ENABLED', 'false'))
SHORT_SELLING_ENABLED = str(SHORT_SELLING_ENABLED_VAL).lower() in ('true', '1', 'yes')

# 2026-04-08: 明确禁用卖空，直到实盘
if not SHORT_SELLING_ENABLED:
    logger = logging.getLogger("ftseek.config")
    logger.info("卖空功能已禁用 (SHORT_SELLING_ENABLED=false)")

# ============================================================
#  执行质量配置 (2026-04-06 新增 - P0 优化)
# ============================================================
MAX_SLIPPAGE_PCT = float(config_center.get('trading.execution.max_slippage_pct', os.getenv("MAX_SLIPPAGE_PCT", "0.005")))
ORDER_RETRY_BASE_OFFSET = float(config_center.get('trading.execution.order_retry_base_offset', os.getenv("ORDER_RETRY_BASE_OFFSET", "0.002")))
EXECUTION_MONITOR_SLIPPAGE_ALERT = float(config_center.get('trading.execution.execution_monitor_slippage_alert', os.getenv("EXECUTION_MONITOR_SLIPPAGE_ALERT", "0.01")))
EXECUTION_MONITOR_REJECT_RATE_ALERT = float(config_center.get('trading.execution.execution_monitor_reject_rate_alert', os.getenv("EXECUTION_MONITOR_REJECT_RATE_ALERT", "0.10")))

# ============================================================
#  TradingView Webhook 配置
# ============================================================
TRADINGVIEW_WEBHOOK_TOKEN = os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", API_TOKEN)
