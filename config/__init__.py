"""
FTSeek-US 基础配置

从环境变量和 .env.us 加载基础配置。
被 main.py 通过 `import config` 引用。
"""
import os
from dotenv import load_dotenv
from pathlib import Path

# 加载环境变量
load_dotenv(Path(__file__).resolve().parent.parent / ".env.us")

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
#  日志配置
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/ftseek_us.log")

# ============================================================
#  OpenD 配置
# ============================================================
def _safe_int(key: str, default: int) -> int:
    """D-DAY (2026-05-17): .env 值异常 (例如 REDACTED 占位) 时 fallback,
    防止 config package import 失败 → 整个 app 启动崩溃."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _safe_float(key: str, default: float) -> float:
    """D-DAY (2026-05-17): 同 _safe_int, 处理 float 字段."""
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = _safe_int("OPEND_PORT", 11111)
TRADE_ENV = _safe_int("TRADE_ENV", 1)
SIMULATE_ACCOUNT_ID = os.getenv("SIMULATE_ACCOUNT_ID", "8246818")

# ============================================================
#  API 配置
# ============================================================
FTSEEK_US_API_TOKEN = os.getenv("FTSEEK_US_API_TOKEN", "")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY", "")

# ============================================================
#  钉钉推送
# ============================================================
DINGTALK_WEBHOOK_URL_US = os.getenv("DINGTALK_WEBHOOK_URL_US", "")
DINGTALK_SECRET_US = os.getenv("DINGTALK_SECRET_US", "")

# ============================================================
#  Bailian API
# ============================================================
BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")
# P1-3 修复 (2026-04-17): 如果 systemd 截断了 BAILIAN_API_KEY, 从 .env.us 直接读取
if not BAILIAN_API_KEY or BAILIAN_API_KEY.startswith("***") or len(BAILIAN_API_KEY) < 10:
    try:
        import importlib
        dotenv = importlib.import_module("dotenv")
        env_path = Path(__file__).resolve().parent.parent / ".env.us"
        if env_path.exists():
            with open(env_path, "r") as f:
                for line in f:
                    if line.startswith("BAILIAN_API_KEY=") and not line.strip().endswith("***"):
                        key_val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if key_val and len(key_val) > 10 and not key_val.startswith("***"):
                            BAILIAN_API_KEY = key_val
                            break
    except Exception:
        pass
BAILIAN_BASE_URL = os.getenv("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
BAILIAN_MODEL = os.getenv("BAILIAN_MODEL", "qwen-max")
BAILIAN_MODEL_MAIN = os.getenv("BAILIAN_MODEL_MAIN", "qwen-max")
BAILIAN_MODEL_RISK = os.getenv("BAILIAN_MODEL_RISK", "glm-5")
BAILIAN_TIMEOUT = _safe_int("BAILIAN_TIMEOUT", 30)

# ============================================================
#  旧架构兼容配置 (FTSeekUS / USStockMonitor)
# ============================================================
US_CHECK_INTERVAL = _safe_int("US_CHECK_INTERVAL", 5)
US_NIGHT_INTERVAL = _safe_int("US_NIGHT_INTERVAL", 30)
US_DEFAULT_STOP_LOSS = _safe_float("US_DEFAULT_STOP_LOSS", 0.05)
US_DEFAULT_TAKE_PROFIT = _safe_float("US_DEFAULT_TAKE_PROFIT", 0.1)
US_TRAILING_STOP_ENABLED = os.getenv("US_TRAILING_STOP_ENABLED", "true").lower() == "true"
US_TRAILING_TRIGGER = _safe_float("US_TRAILING_TRIGGER", 0.05)
US_TRAILING_PERCENT = _safe_float("US_TRAILING_PERCENT", 0.03)
DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK_URL_US", "")
