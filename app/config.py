"""
FTSeek 配置文件

📋 配置架构 (2026-04-09 收敛后):
  - .env → 敏感环境变量 (API_TOKEN, DINGTALK_SECRET, 富途账号等)
  - config/config.yaml → 唯一配置源 (策略/交易/风控/通知等所有配置)
  - ConfigCenter → 统一加载器 (支持热重载/环境变量覆盖)

⚠️ 安全提示:
  - API_TOKEN 必须通过环境变量设置，禁止使用默认值
  - 钉钉 Webhook Secret 必须通过环境变量设置
  - 富途账号密码建议通过环境变量设置
"""
import os
import json
import logging
import secrets
from pathlib import Path
from dotenv import load_dotenv

# ============================================================
#  配置中心 (唯一配置来源)
# ============================================================
from config.config_center import get_config_center
config_center = get_config_center()
config_center.load()

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
#  OpenD 配置 (从环境变量读取)
# ============================================================
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))
OPEND_DIR = os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_10.3.6308_Centos7/Futu_OpenD_10.3.6308_Centos7")

# 富途账号
FUTU_LOGIN_ACCOUNT = os.getenv("FUTU_LOGIN_ACCOUNT", "")
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 交易环境：0=真实环境，1=模拟环境
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

# 模拟账户 ID
SIMULATE_ACCOUNT_ID = os.getenv("SIMULATE_ACCOUNT_ID", "18820895")

# ============================================================
#  钉钉推送配置 (必须通过环境变量)
# ============================================================
DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET")

if not DINGTALK_WEBHOOK_URL:
    DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL_DEFAULT", "")
if not DINGTALK_SECRET:
    DINGTALK_SECRET = os.getenv("DINGTALK_SECRET_DEFAULT", "")

# ============================================================
#  FastAPI 确认回调服务
# ============================================================
CALLBACK_HOST = os.getenv("CALLBACK_HOST", "0.0.0.0")
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "8899"))
CALLBACK_EXTERNAL_URL = os.getenv(
    "CALLBACK_EXTERNAL_URL",
    f"http://39.97.58.4:{CALLBACK_PORT}"
)
SIGNAL_EXPIRE_SECONDS = int(os.getenv("SIGNAL_EXPIRE_SECONDS", "300"))

# ============================================================
#  🔐 API 鉴权 Token (安全加固)
# ============================================================
API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    if os.getenv("FTSEEK_DEV_MODE", "").lower() in ("1", "true", "yes"):
        API_TOKEN = secrets.token_hex(16)
        print(f"⚠️  警告：API_TOKEN 未设置，已生成临时 Token: {API_TOKEN}")
    else:
        raise RuntimeError(
            "🔐 安全错误：API_TOKEN 环境变量未设置！\n"
            "请执行：export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        )

# ============================================================
#  监控标的 & 市场配置 (统一优先级链 2026-04-13)
#
#  优先级 (从高到低):
#   1. 环境变量 WATCH_LIST_HK (运维覆盖/紧急切换)
#   2. config.yaml via config_center (单一事实源)
#   3. 最小 fallback (3 只核心股，防止配置失效时系统裸奔)
# ============================================================

def _load_watch_list_hk() -> list:
    """按优先级链加载港股监控名单"""
    # 1. 环境变量覆盖 (运维/紧急切换)
    env_val = os.getenv("WATCH_LIST_HK")
    if env_val and env_val.strip():
        try:
            lst = json.loads(env_val)
            if isinstance(lst, list) and len(lst) > 0:
                logging.getLogger("ftseek.config").info(
                    f"[watchlist] 从环境变量加载 {len(lst)} 只港股"
                )
                return lst
        except json.JSONDecodeError as e:
            logging.getLogger("ftseek.config").warning(
                f"[watchlist] 环境变量 WATCH_LIST_HK 解析失败: {e}"
            )

    # 2. 从 config.yaml 读取 (config_center 已在文件顶部初始化)
    try:
        yaml_list = config_center.get("trading.watch_list_hk")
        if isinstance(yaml_list, list) and len(yaml_list) > 0:
            logging.getLogger("ftseek.config").info(
                f"[watchlist] 从 config.yaml 加载 {len(yaml_list)} 只港股"
            )
            return yaml_list
    except Exception as e:
        logging.getLogger("ftseek.config").warning(
            f"[watchlist] config_center 读取失败: {e}"
        )

    # 3. 最小 fallback (核心股，防止配置全部失效)
    logging.getLogger("ftseek.config").warning(
        "[watchlist] 无法从环境变量或 config.yaml 加载，使用最小 fallback (3 只)"
    )
    return ["HK.00700", "HK.09988", "HK.03690"]


def _load_watch_list_us() -> list:
    """按优先级链加载美股监控名单"""
    env_val = os.getenv("WATCH_LIST_US")
    if env_val and env_val.strip():
        try:
            lst = json.loads(env_val)
            if isinstance(lst, list) and len(lst) > 0:
                logging.getLogger("ftseek.config").info(
                    f"[watchlist-us] 从环境变量加载 {len(lst)} 只美股"
                )
                return lst
        except json.JSONDecodeError:
            pass

    try:
        yaml_list = config_center.get("trading.watch_list_us")
        if isinstance(yaml_list, list) and len(yaml_list) > 0:
            logging.getLogger("ftseek.config").info(
                f"[watchlist-us] 从 config.yaml 加载 {len(yaml_list)} 只美股"
            )
            return yaml_list
    except Exception as e:
        logging.getLogger("ftseek.config").warning(
            f"[watchlist-us] config_center 读取失败: {e}"
        )

    logging.getLogger("ftseek.config").warning(
        "[watchlist-us] 无法加载美股名单，跨市场功能将受限"
    )
    return []  # 美股默认空列表


WATCH_LIST_HK = _load_watch_list_hk()
WATCH_LIST_US = _load_watch_list_us()
WATCH_LIST = WATCH_LIST_HK + WATCH_LIST_US

# 港股订阅名单: 仅用于港股交易时段实时行情订阅
# 美股不订阅实时行情，仅用于盘前分析和跨市场映射参考
WATCH_LIST_SUBSCRIBE = WATCH_LIST_HK  # 港股时段只订阅港股

# 2026-04-13 扩容优化: 30只股票时减少订阅类型以适配富途100限制
# 2026-04-19 扩容调整: 港股扩至47只 → 订阅类型减至2种 (QUOTE+K_1M) → 47×2=94 < 100
# 原配置: QUOTE,K_1M,K_3M,K_5M,K_15M,K_30M,K_60M,K_DAY (8种)
# 2026-04-13: QUOTE,K_1M,K_5M (3种) -> 30x3=90 < 100
# 2026-04-19: QUOTE,K_1M (2种) -> 47x2=94 < 100 (去掉K_5M，盘中策略用技术指标缓存)
_subscribe_types_str = os.getenv("SUBSCRIBE_TYPES", "QUOTE,K_1M")
if _subscribe_types_str.startswith("["):
    SUBSCRIBE_TYPES = json.loads(_subscribe_types_str)
else:
    SUBSCRIBE_TYPES = [s.strip() for s in _subscribe_types_str.split(",")]

# 订阅槽位校验 — 仅基于港股名单 (美股不参与实时订阅)
_SUBSCRIPTION_SLOTS = len(WATCH_LIST_SUBSCRIBE) * len(SUBSCRIBE_TYPES)
_MAX_FUTU_SLOTS = 100
if _SUBSCRIPTION_SLOTS > _MAX_FUTU_SLOTS:
    import warnings
    warnings.warn(
        f"[安全警告] 订阅槽位 {_SUBSCRIPTION_SLOTS} 超过富途限制 {_MAX_FUTU_SLOTS}! "
        f"({len(WATCH_LIST_SUBSCRIBE)} 只港股 × {len(SUBSCRIBE_TYPES)} 种 = {_SUBSCRIPTION_SLOTS})。"
        f"请减少 SUBSCRIBE_TYPES 或 WATCH_LIST_HK。"
    )

# ============================================================
#  策略配置 (从配置中心读取)
# ============================================================
def _load_strategies_from_config():
    """从配置中心加载策略配置"""
    strategies = {}
    strategy_names = [
        'price_break', 'volume_alert', 'rsi', 'macd', 'bollinger',
        'supertrend', 'confluence', 'confluence_v2', 'fusion_v2',
        'sentiment_ai', 'open_close_effect', 'event_driven',
        'cross_market_arbitrage', 'enhanced_confluence'
    ]
    for name in strategy_names:
        cfg = config_center.get(f'strategy.{name}')
        if cfg and hasattr(cfg, 'to_dict'):
            strategies[name] = cfg.to_dict()
        elif cfg and isinstance(cfg, dict):
            strategies[name] = cfg
        else:
            strategies[name] = {'enabled': False}
    return strategies

STRATEGIES = _load_strategies_from_config()

# ============================================================
#  交易配置 (从配置中心读取)
# ============================================================
HK_LOT_SIZES_CFG = config_center.get('trading.order.hk_lot_sizes')
HK_LOT_SIZES = HK_LOT_SIZES_CFG.to_dict() if HK_LOT_SIZES_CFG and hasattr(HK_LOT_SIZES_CFG, 'to_dict') else {
    'HK.00700': 100, 'HK.09988': 100, 'HK.03690': 100, 'HK.09992': 200,
    'HK.00981': 500, 'HK.01810': 200, 'HK.01024': 100, 'HK.01347': 1000,
    'HK.01093': 2000, 'HK.00175': 1000, 'HK.01378': 500,
}

DEFAULT_ORDER_QTY_BY_CODE_CFG = config_center.get('trading.order.default_order_qty_by_code')
DEFAULT_ORDER_QTY_BY_CODE = DEFAULT_ORDER_QTY_BY_CODE_CFG.to_dict() if DEFAULT_ORDER_QTY_BY_CODE_CFG and hasattr(DEFAULT_ORDER_QTY_BY_CODE_CFG, 'to_dict') else {
    'HK.00700': 100, 'HK.09988': 100, 'HK.03690': 100, 'HK.09992': 200,
    'HK.00981': 500, 'HK.01810': 200, 'HK.01024': 100, 'HK.09888': 100,
    'HK.02318': 500, 'HK.09618': 100, 'HK.01347': 1000, 'HK.01093': 2000,
    'HK.00175': 1000, 'HK.01378': 500,
}

DEFAULT_ORDER_QTY_HK = int(config_center.get('trading.order.default_order_qty_hk', 100))
DEFAULT_ORDER_QTY_US = int(config_center.get('trading.order.default_order_qty_us', 1))
DEFAULT_ORDER_QTY = int(config_center.get('trading.order.default_order_qty', 100))
DEFAULT_ORDER_TYPE = config_center.get('trading.order.default_order_type', "MARKET")
MAX_ORDER_AMOUNT = float(config_center.get('trading.order.max_order_amount', 120000.0))

def get_lot_size(code: str) -> int:
    """获取股票每手股数"""
    if code in HK_LOT_SIZES:
        return HK_LOT_SIZES[code]
    return 100

# ============================================================
#  日志配置
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # 默认 INFO，由 RotatingFileHandler 控制大小 (50MB)

# ============================================================
#  共识引擎配置 (从配置中心读取)
# ============================================================
CONSENSUS_WINDOW = int(config_center.get('strategy.consensus.window_seconds', 90))
CONSENSUS_THRESHOLD = float(config_center.get('strategy.consensus.threshold', 1.5))
CONSENSUS_ADX_TREND = float(config_center.get('strategy.consensus.adx_trend', 25))
CONSENSUS_ADX_RANGE = float(config_center.get('strategy.consensus.adx_range', 20))
SIGNAL_COOLDOWN = int(config_center.get('strategy.cooldown.signal_cooldown', 120))
MAX_OPEN_POSITIONS = int(config_center.get('trading.max_open_positions', 12))

SHORT_SELLING_ENABLED_VAL = config_center.get('trading.short_selling_enabled', 'false')
SHORT_SELLING_ENABLED = str(SHORT_SELLING_ENABLED_VAL).lower() in ('true', '1', 'yes')

if not SHORT_SELLING_ENABLED:
    logger = logging.getLogger("ftseek.config")
    logger.info("卖空功能已禁用 (SHORT_SELLING_ENABLED=false)")

# ============================================================
#  执行质量配置 (从配置中心读取)
# ============================================================
MAX_SLIPPAGE_PCT = float(config_center.get('trading.execution.max_slippage_pct', 0.005))
ORDER_RETRY_BASE_OFFSET = float(config_center.get('trading.execution.order_retry_base_offset', 0.002))
EXECUTION_MONITOR_SLIPPAGE_ALERT = float(config_center.get('trading.execution.execution_monitor_slippage_alert', 0.01))
EXECUTION_MONITOR_REJECT_RATE_ALERT = float(config_center.get('trading.execution.execution_monitor_reject_rate_alert', 0.10))

# ============================================================
#  TradingView Webhook 配置
# ============================================================
TRADINGVIEW_WEBHOOK_TOKEN = os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", API_TOKEN)


# ============================================================
#  配置校验 (启动时检查)
# ============================================================

def validate_dingtalk_config() -> list:
    """校验钉钉推送配置一致性。

    Returns:
        问题列表，空列表表示一切正常。
    """
    issues = []
    
    # 检查主配置
    if not DINGTALK_WEBHOOK_URL:
        issues.append(
            "[钉钉配置] DINGTALK_WEBHOOK_URL 未设置，所有推送将被跳过"
        )
    
    # 检查新闻推送配置是否一致
    try:
        from app.news_push.config import DINGTALK_WEBHOOK_URL as news_url
        if news_url and DINGTALK_WEBHOOK_URL:
            if news_url != DINGTALK_WEBHOOK_URL:
                issues.append(
                    f"[钉钉配置] Webhook URL 不一致: "
                    f"app/config={DINGTALK_WEBHOOK_URL[:30]}... "
                    f"news_push/config={news_url[:30]}..."
                )
    except ImportError:
        pass  # 新闻模块未加载，跳过
    
    # 检查全局 notifier 是否正常初始化
    try:
        from app.notifier import get_dingtalk_notifier
        notifier = get_dingtalk_notifier()
        if notifier.webhook_url and DINGTALK_WEBHOOK_URL:
            if notifier.webhook_url != DINGTALK_WEBHOOK_URL:
                issues.append(
                    f"[钉钉配置] 全局 notifier Webhook 与 app/config 不一致"
                )
        elif not notifier.webhook_url and DINGTALK_WEBHOOK_URL:
            issues.append(
                "[钉钉配置] 全局 notifier 未正确获取 Webhook URL"
            )
    except Exception as e:
        issues.append(f"[钉钉配置] 全局 notifier 初始化失败: {e}")
    
    return issues
