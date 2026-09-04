"""
FTSeek-US 配置文件 - 美股交易平台

⚠️ 2026-04-17 重大更新: 
  所有配置已迁移至 unified_config.yaml
  本文件作为兼容加载器，从 unified_config.yaml 读取配置
  请勿在此文件硬编码配置参数!
  
安全提示:
  - API_TOKEN 必须通过环境变量设置，禁止使用默认值
  - 钉钉 Webhook Secret 必须通过环境变量设置
  - 富途账号密码建议通过环境变量设置
"""
import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
_env_path = Path(__file__).resolve().parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

# ============================================================
#  统一配置加载
# ============================================================
try:
    from config.config_loader import get_config
    _loader = get_config()
    
    # DEB-20260903 #11: 不再做 import 期属性快照 (热重载不生效);
    # 改由模块级 __getattr__ 惰性转发 (见文件下方 _EXPORT_PROPERTIES)
    
except Exception as e:
    # P0-7 修复 2026-04-26: 统一配置加载失败时拒绝启动，避免硬编码 fallback 悄悄接管生产
    raise RuntimeError(
        f"[致命] unified_config 加载失败: {e}。"
        f"请检查 config/unified_config.yaml 和 config/config_loader.py 是否完整"
    ) from e

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
CONFIG_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# 加载 .env 文件中的环境变量
load_dotenv(BASE_DIR / ".env.us")

# ============================================================
#  基础配置
# ============================================================
PROJECT_NAME = "FTSeek-US"
VERSION = "1.0.0"
MARKET = "US"  # 美股市场
CURRENCY = "USD"
TIMEZONE = "America/New_York"  # 美东时间

# 交易时间 (美东时间)
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
PRE_MARKET_OPEN = "04:00"
AFTER_MARKET_CLOSE = "20:00"

# ============================================================
#  OpenD / 券商配置
# ============================================================
FUTU_OPEND_HOST = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")


def _safe_int_env(key: str, default: int) -> int:
    """D-DAY (2026-05-17): .env 中字段异常 (例如 REDACTED 占位) 时 fallback 默认值,
    防止 import 时整个 config_us 加载失败 → ConfigContainer.unified_config 加载失败 → 所有 yaml 读取失效."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "[config_us] %s 解析失败 (raw=%r), 用默认 %d", key, raw, default
        )
        return default


FUTU_OPEND_PORT = _safe_int_env("FUTU_OPEND_PORT", 11111)

# 富途账号
FUTU_LOGIN_ACCOUNT = os.getenv("FUTU_LOGIN_ACCOUNT", "")
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 交易环境：0=真实环境，1=模拟环境
TRADE_ENV = _safe_int_env("TRADE_ENV", 1)  # 默认模拟环境，安全第一

# 模拟账户 ID
SIMULATE_ACCOUNT_ID = os.getenv("SIMULATE_ACCOUNT_ID", "")

# ============================================================
#  钉钉推送配置
# ============================================================
DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL_US")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET_US")

# ============================================================
#  行情数据源配置（TwelveData + Finnhub + Yahoo fallback）
# ============================================================

# TwelveData（免费计划）
# 限速：8 credits/分钟，批量请求按标的数计
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
TWELVEDATA_RATE_LIMIT_PER_MIN = 8          # 免费计划上限
TWELVEDATA_REFILL_INTERVAL_SEC = 7.5       # 60 / 8 = 7.5s/credit
TWELVEDATA_CACHE_TTL_QUOTE_SEC = 300       # 报价缓存 5 分钟
TWELVEDATA_CACHE_TTL_TIMESERIES_SEC = 1800 # 日线历史缓存 30 分钟
TWELVEDATA_CACHE_TTL_VIX_SEC = 600        # VIX 缓存 10 分钟
TWELVEDATA_CACHE_TTL_SPY_SEC = 900        # SPY 缓存 15 分钟
TWELVEDATA_BATCH_SIZE = 8                  # 每批最多 8 个标的

# Finnhub（免费计划）
# 限速：60 calls/分钟（约 1 call/s）
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_RATE_LIMIT_PER_MIN = 60           # 免费计划上限
FINNHUB_REFILL_INTERVAL_SEC = 1.0         # 60 / 60 = 1s/call
FINNHUB_CACHE_TTL_QUOTE_SEC = 60          # 报价缓存 60 秒（准实时）
FINNHUB_CACHE_TTL_NEWS_SEC = 7200         # 新闻缓存 2 小时
FINNHUB_CACHE_TTL_SENTIMENT_SEC = 3600    # 情绪分缓存 1 小时

# 数据源优先级（router 按顺序尝试）
# primary: TwelveData（技术指标） + Finnhub（实时价格/新闻）
# fallback: Yahoo Finance（无 API Key 限制，15分钟延迟）
DATA_SOURCE_PRIMARY = "twelvedata+finnhub"
DATA_SOURCE_FALLBACK = "yahoo"

# ============================================================
#  FastAPI 服务配置
# ============================================================
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = _safe_int_env("API_PORT", 8901)  # 美股使用不同端口
API_EXTERNAL_URL = os.getenv("API_EXTERNAL_URL", f"http://39.97.58.4:{API_PORT}")

# ============================================================
#  🔐 API 鉴权 Token
# ============================================================
API_TOKEN = os.getenv("FTSEEK_US_API_TOKEN")
if not API_TOKEN:
    raise ValueError("⚠️ 必须设置环境变量 FTSEEK_US_API_TOKEN")

# P0-6 修复 2026-04-26: 删除硬编码 DECISION_CONFIG / RISK_CONFIG
# 原因: L28-L35 已从 unified_config.yaml 统一加载，但此处硬编码后赋值会无声覆盖 loader 读取的值。
# 现在 DECISION_CONFIG / RISK_CONFIG 均由 config_loader.get_config() 的属性提供。

# P1 修复 2026-04-28: 已迁移至 unified_config.yaml (通过 _loader.EXECUTION_CONFIG 读取)

# ============================================================
#  监控股票池 (美股)
# ============================================================
# DEB-20260903 #11: 删除 _load_watchlist_from_json() 对 WATCH_LIST_US 的覆盖 —
# 原 33 行从 unified_config.yaml 赋值后又被 JSON 覆盖, 双真源读写分离;
# WATCH_LIST_US 唯一真源 = _loader.WATCH_LIST_US (unified_config.yaml)
#
# DEB-20260903 #11: 快照修复 — RISK_CONFIG/DECISION_CONFIG 等原在 import 期
# 取 property 快照, 热重载不生效; 改用 PEP 562 模块级 __getattr__ 惰性转发
# loader, 属性访问 (config_us.RISK_CONFIG) 始终拿实时值。
# 注意: `from config.config_us import RISK_CONFIG` 仍是引用快照 (Python 语义),
# 热重载敏感的消费方应使用 config_loader.get_config() 或属性访问。

_EXPORT_PROPERTIES = (
    "WATCH_LIST_US", "DECISION_CONFIG", "RISK_CONFIG", "EXECUTION_CONFIG",
    "OBSERVABILITY_CONFIG", "DATABASE_CONFIG", "LOGGING_CONFIG", "AGENT_CONFIG",
)


def __getattr__(name: str):
    """PEP 562: 惰性转发 loader 实时值 (DEB-20260903 #11)."""
    if name in _EXPORT_PROPERTIES:
        return getattr(_loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ============================================================
#  多 Agent 配置 - 已迁移至 unified_config.yaml
# ============================================================
# AGENT_CONFIG 已从 config_loader 导出 (config_us.py:42)

# ============================================================
#  功能开关 - 已迁移至 unified_config.yaml
# ============================================================

# ============================================================
#  加载监控股票池配置
# ============================================================
WATCHLIST_FILE = CONFIG_DIR / "watchlist_us.json"
# DEB-20260904 #138: YAML (unified_config) 为股票池唯一真源 —
# 原 JSON 存在即优先, 与 watchlist_manager (YAML 口径) 形成双真源;
# app/ 内已无该文件的写入方, 若文件再现则告警并忽略
if WATCHLIST_FILE.exists():
    print(
        "⚠️ [config_us] watchlist_us.json 已弃用并被忽略 "
        "(唯一真源: unified_config.yaml watchlist) — 请删除该文件"
    )
WATCHLIST_CONFIG = {
    # DEB-20260903 #11: 模块级 WATCH_LIST_US 已改 __getattr__ 惰性转发,
    # 模块内代码不走 __getattr__, 直接引用 _loader
    "symbols": {symbol: {"enabled": True, "name": symbol} for symbol in _loader.WATCH_LIST_US}
}

def get_watchlist():
    """获取启用的监控股票列表（兼容 us_stocks 数组格式和 symbols 字典格式）"""
    # 新格式：us_stocks 数组
    us_stocks = WATCHLIST_CONFIG.get("us_stocks", [])
    if us_stocks:
        return [s["code"] for s in us_stocks if s.get("enabled", False)]
    # 旧格式：symbols 字典
    return [
        symbol for symbol, config in WATCHLIST_CONFIG.get("symbols", {}).items()
        if config.get("enabled", False)
    ]

# ============================================================
#  验证配置（P0 增强版：增加一致性检查和变更日志）
# ============================================================
def validate_config():
    """验证关键配置项（P0 增强版）"""
    errors = []
    warnings = []
    
    # 1. 基础配置验证
    if not API_TOKEN:
        errors.append("FTSEEK_US_API_TOKEN 未设置")
    
    if TRADE_ENV == 0 and not FUTU_LOGIN_ACCOUNT:
        errors.append("实盘模式需要设置 FUTU_LOGIN_ACCOUNT")
    
    # 2. P0 新增：配置一致性检查
    # 检查 MIN_CONFIDENCE 在各处是否一致
    # DEB-20260903 #11: 模块内函数直接读 _loader (模块级快照已移除, __getattr__ 仅对外生效)
    min_confidence_risk = _loader.RISK_CONFIG.get("MIN_CONFIDENCE", 0.70)  # 2026-06-13 统一: fail-safe 0.70
    try:
        from app.risk.pre_trade_check import PreTradeChecker
        checker = PreTradeChecker()
        min_confidence_checker = checker.min_confidence
        if abs(min_confidence_risk - min_confidence_checker) > 0.01:
            warnings.append(
                f"配置不一致：RISK_CONFIG.MIN_CONFIDENCE={min_confidence_risk:.2f}, "
                f"PreTradeChecker.min_confidence={min_confidence_checker:.2f}"
            )
    except Exception:
        pass  # 忽略导入失败
    
    # 3. 阈值合理性检查
    if _loader.RISK_CONFIG.get("MIN_CONFIDENCE", 0) < 0.3 or _loader.RISK_CONFIG.get("MIN_CONFIDENCE", 1) > 0.9:
        warnings.append(f"MIN_CONFIDENCE={_loader.RISK_CONFIG.get('MIN_CONFIDENCE')} 超出合理范围 (0.3-0.9)")
    
    if _loader.RISK_CONFIG.get("SIGNAL_MIN_SCORE", 0) < 30 or _loader.RISK_CONFIG.get("SIGNAL_MIN_SCORE", 100) > 80:
        warnings.append(f"SIGNAL_MIN_SCORE={_loader.RISK_CONFIG.get('SIGNAL_MIN_SCORE')} 超出合理范围 (30-80)")
    
    # 4. 打印警告
    for warning in warnings:
        print(f"[配置警告] {warning}")
    
    if len(errors) > 0:
        raise ValueError("配置验证失败:\n" + "\n".join(errors))
    
    # 5. P0 新增：配置变更日志记录
    _log_config_change()
    
    return True


def _log_config_change():
    """
    P0 新增：配置变更日志记录
    记录每次配置加载的关键参数，便于追踪变更
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # 仅记录关键配置摘要（避免日志过多）
    config_summary = {
        "MIN_CONFIDENCE": _loader.RISK_CONFIG.get("MIN_CONFIDENCE"),
        "MIN_CONSENSUS": _loader.RISK_CONFIG.get("MIN_CONSENSUS"),
        "SIGNAL_MIN_SCORE": _loader.RISK_CONFIG.get("SIGNAL_MIN_SCORE"),
        "MAX_DAILY_DRAWDOWN": _loader.RISK_CONFIG.get("MAX_DAILY_DRAWDOWN"),
        # MAX_TOTAL_DRAWDOWN 已删除 (2026-08-24): 总回撤门 8/23 随死门删除, 死链清除
    }
    
    logger.info(f"[配置加载] 关键参数：{config_summary}")


# 启动时验证配置
validate_config()

# ============================================================
#  Alpha Vantage 配置 (新增，替换 Yahoo Finance)
# ============================================================
# 限速：5 次/分钟，500 次/天（标准 Key）；75 次/分钟，75,000 次/天（高级 Key）
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")
ALPHAVANTAGE_PREMIUM = os.getenv("ALPHAVANTAGE_PREMIUM", "false").lower() == "true"
ALPHAVANTAGE_RATE_LIMIT_PER_MIN = 75 if ALPHAVANTAGE_PREMIUM else 5
ALPHAVANTAGE_RATE_LIMIT_PER_DAY = 75000 if ALPHAVANTAGE_PREMIUM else 500
ALPHAVANTAGE_CACHE_TTL_QUOTE_SEC = 300       # 报价缓存 5 分钟
ALPHAVANTAGE_CACHE_TTL_TIMESERIES_SEC = 1800 # 日线历史缓存 30 分钟
ALPHAVANTAGE_CACHE_TTL_VIX_SEC = 600         # VIX 缓存 10 分钟
ALPHAVANTAGE_CACHE_TTL_SPY_SEC = 900         # SPY 缓存 15 分钟

# 数据源优先级更新
DATA_SOURCE_FALLBACK = "alphavantage"

# ============================================================
#  飞书推送配置
# ============================================================
import os
FEISHU_WEBHOOK_URL_US = os.getenv("FEISHU_WEBHOOK_URL_US")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

if not FEISHU_WEBHOOK_URL_US:
    FEISHU_WEBHOOK_URL_US = os.getenv("FEISHU_WEBHOOK_URL_US_DEFAULT", "")
if not FEISHU_APP_SECRET:
    FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET_DEFAULT", "")
