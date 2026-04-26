#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置兼容性模块

功能:
- 提供旧 config 模块的兼容接口
- 自动从新配置中心加载数据
- 支持渐进式迁移

使用示例:
    # 旧代码仍然可用
    from config import config as old_config
    print(old_config.TRADE_ENV)

    # 新代码使用配置中心
    from config.config_center import get_config_center
    config = get_config_center()
    print(config.trading.mode)
"""

import os
import warnings
from pathlib import Path
from typing import Any, Optional

# 导入配置中心
try:
    from config.config_center import get_config_center, ConfigCenter
    CONFIG_CENTER_AVAILABLE = True
except ImportError:
    CONFIG_CENTER_AVAILABLE = False
    print("[警告] 配置中心不可用，将使用旧版配置加载")

# 基础路径
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
#  配置中心代理 (优先使用)
# ============================================================
_config_center: Optional[ConfigCenter] = None


def get_center() -> Optional[ConfigCenter]:
    """获取配置中心单例"""
    global _config_center
    if _config_center is None and CONFIG_CENTER_AVAILABLE:
        try:
            _config_center = get_config_center()
            _config_center.load()
        except Exception as e:
            print(f"[警告] 配置中心加载失败：{e}")
    return _config_center


def _get_value(key: str, default: Any = None) -> Any:
    """从配置中心获取值"""
    center = get_center()
    if center:
        try:
            return center.get(key, default)
        except Exception:
            return default
    return default


# ============================================================
#  富途 OpenD 配置 (兼容旧接口)
# ============================================================
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))
OPEND_DIR = os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_10.3.6308_Centos7/Futu_OpenD_10.3.6308_Centos7")

# 富途账号
FUTU_LOGIN_ACCOUNT = os.getenv("FUTU_LOGIN_ACCOUNT", "")
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 交易环境
_raw_trade_env = int(os.getenv("TRADE_ENV", "1"))
_allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
TRADE_ENV = _raw_trade_env if (_allow_real and _raw_trade_env == 0) else 1

if _raw_trade_env == 0 and not _allow_real:
    warnings.warn(
        "[安全拦截] TRADE_ENV=0 被忽略，当前强制使用模拟环境。"
        "如需实盘，请同时设置 ALLOW_REAL_TRADING=true",
        RuntimeWarning, stacklevel=2
    )

# 模拟账户 ID
SIMULATE_ACCOUNT_ID = os.getenv("SIMULATE_ACCOUNT_ID", "18820895")

# ============================================================
#  钉钉推送配置
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
#  API 鉴权 Token
# ============================================================
API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    if os.getenv("FTSEEK_DEV_MODE", "").lower() in ("1", "true", "yes"):
        import secrets
        API_TOKEN = secrets.token_hex(16)
        print(f"⚠️  警告：API_TOKEN 未设置，已生成临时 Token: {API_TOKEN}")
    else:
        # 尝试从配置中心获取
        API_TOKEN = _get_value('server.api_token', '')
        if not API_TOKEN:
            raise RuntimeError(
                "🔐 安全错误：API_TOKEN 环境变量未设置！\n"
                "请执行以下命令生成安全 Token 并设置：\n"
                "  export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
            )

TRADINGVIEW_WEBHOOK_TOKEN = os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", API_TOKEN)

# ============================================================
#  监控标的配置
# ============================================================
import json

_watch_list_hk = os.getenv("WATCH_LIST_HK", '["HK.09992", "HK.00981", "HK.03690", "HK.00700", "HK.09888", "HK.01024", "HK.09988", "HK.01810", "HK.02318", "HK.09618"]')
WATCH_LIST_HK = json.loads(_watch_list_hk) if _watch_list_hk else []
WATCH_LIST_US = json.loads(os.getenv("WATCH_LIST_US", '[]'))
WATCH_LIST = WATCH_LIST_HK + WATCH_LIST_US

# 订阅类型
_subscribe_types_str = os.getenv("SUBSCRIBE_TYPES", 'QUOTE,K_1M,K_3M,K_5M,K_15M,K_30M,K_60M,K_DAY')
if _subscribe_types_str.startswith('['):
    SUBSCRIBE_TYPES = json.loads(_subscribe_types_str)
else:
    SUBSCRIBE_TYPES = [s.strip() for s in _subscribe_types_str.split(',')]

# ============================================================
#  策略配置 (兼容旧接口)
# ============================================================
def _get_strategy_config():
    """获取策略配置 (优先从配置中心加载)"""
    center = get_center()
    if center:
        try:
            strategy = center.get('strategy', {})
            risk_limits = center.get('risk_limits', {})

            # 构建兼容旧格式的配置
            return {
                # 主力策略
                "confluence_v2": {
                    "enabled": True,
                    "min_factors": center.get('strategy.consensus.min_strategies', 3),
                },
                "supertrend": {
                    "enabled": True,
                    "atr_period": 10,
                    "multiplier": center.get('strategy.long_signals.supertrend_multiplier', 2.5),
                },
                "sentiment_ai": {
                    "enabled": True,
                    "positive_threshold": 0.4,
                    "negative_threshold": -0.4,
                    "cooldown_seconds": 7200,
                },
                # 旧格式兼容
                "price_break": {
                    "enabled": False,
                    "thresholds": {}
                },
                "volume_alert": {
                    "enabled": False,
                    "volume_ratio_threshold": 2.0,
                },
                # RSI/MACD/布林带 (已禁用)
                "rsi": {"enabled": False},
                "macd": {"enabled": False},
                "bollinger": {"enabled": False},
                "confluence": {"enabled": False},
                # 融合策略
                "fusion_v2": {
                    "enabled": True,
                    "technical_weight": center.get('strategy.fusion.technical_weight', 0.5),
                    "sentiment_weight": center.get('strategy.fusion.sentiment_weight', 0.5),
                    "min_confidence": risk_limits.get('min_confidence', 0.35),
                },
            }
        except Exception as e:
            print(f"[警告] 从配置中心加载策略配置失败：{e}")

    #  fallback 到默认值
    return {
        "confluence_v2": {"enabled": True, "min_factors": 3},
        "supertrend": {"enabled": True, "atr_period": 10, "multiplier": 2.5},
        "sentiment_ai": {"enabled": True},
        "price_break": {"enabled": False},
        "volume_alert": {"enabled": False},
        "rsi": {"enabled": False},
        "macd": {"enabled": False},
        "bollinger": {"enabled": False},
        "confluence": {"enabled": False},
        "fusion_v2": {"enabled": True},
    }

STRATEGIES = _get_strategy_config()

# ============================================================
#  交易配置
# ============================================================
HK_LOT_SIZES = {
    # 核心科技
    'HK.00700': 100,    # 腾讯
    'HK.09988': 100,    # 阿里巴巴
    'HK.03690': 100,    # 美团
    'HK.09618': 500,    # 京东物流
    'HK.01024': 100,    # 快手
    'HK.09888': 100,    # 百度
    'HK.00981': 500,    # 中芯国际
    'HK.01347': 1000,   # 华虹半导体
    'HK.09868': 100,    # 小鹏汽车
    'HK.02015': 100,    # 理想汽车
    # 金融/地产
    'HK.00388': 100,    # 港交所
    'HK.00939': 1000,   # 建设银行
    'HK.02318': 500,    # 中国平安
    'HK.01299': 200,    # 友邦保险
    'HK.06618': 100,    # 京东健康
    # 制造/消费
    'HK.02382': 200,    # 舜宇光学
    'HK.01211': 500,    # 比亚迪
    'HK.01810': 200,    # 小米
    'HK.02020': 100,    # 安踏体育
    'HK.09926': 200,    # 泡泡玛特
    'HK.09992': 200,    # 名创优品
    'HK.00883': 1000,   # 中海油
    'HK.00857': 2000,   # 中石油
    'HK.01378': 500,    # 中国有色矿业
    # 医药/生物
    'HK.02269': 500,    # 药明生物
    'HK.01801': 500,    # 信达生物
    'HK.01876': 500,    # 百威亚太
    # 其他
    'HK.00772': 100,    # 阅文集团
    'HK.09961': 100,    # 携程
    'HK.00708': 5000,   # 恒大汽车
}

DEFAULT_ORDER_QTY_BY_CODE = HK_LOT_SIZES.copy()
DEFAULT_ORDER_QTY_HK = int(os.getenv("DEFAULT_ORDER_QTY_HK", "100"))
DEFAULT_ORDER_QTY_US = int(os.getenv("DEFAULT_ORDER_QTY_US", "1"))
DEFAULT_ORDER_QTY = int(os.getenv("DEFAULT_ORDER_QTY", "100"))

def get_lot_size(code: str) -> int:
    """获取股票每手股数"""
    if code in HK_LOT_SIZES:
        return HK_LOT_SIZES[code]
    return 100

DEFAULT_ORDER_TYPE = os.getenv("DEFAULT_ORDER_TYPE", "MARKET")
MAX_ORDER_AMOUNT = float(os.getenv("MAX_ORDER_AMOUNT", "120000.0"))

# ============================================================
#  风控配置 (兼容旧接口)
# ============================================================
def _get_risk_config():
    """获取风控配置"""
    center = get_center()
    if center:
        try:
            risk_limits = center.get('risk_limits', {})
            return {
                'max_drawdown': risk_limits.get('max_drawdown', 0.15),
                'max_daily_trades': risk_limits.get('max_daily_trades', 10),
                'max_single_position': risk_limits.get('max_single_position', 0.20),
                'max_total_position': risk_limits.get('max_total_position', 0.80),
                'min_confidence': risk_limits.get('min_confidence', 0.60),
                'consecutive_loss_limit': risk_limits.get('consecutive_loss_limit', 5),
            }
        except Exception:
            pass

    return {
        'max_drawdown': 0.15,
        'max_daily_trades': 10,
        'max_single_position': 0.20,
        'max_total_position': 0.80,
        'min_confidence': 0.60,
        'consecutive_loss_limit': 5,
    }

RISK_CONFIG = _get_risk_config()

# 共识引擎配置
CONSENSUS_WINDOW = int(os.getenv("CONSENSUS_WINDOW", "60"))
CONSENSUS_THRESHOLD = float(os.getenv("CONSENSUS_THRESHOLD", "0.6"))
CONSENSUS_ADX_TREND = float(os.getenv("CONSENSUS_ADX_TREND", "25"))
CONSENSUS_ADX_RANGE = float(os.getenv("CONSENSUS_ADX_RANGE", "20"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "120"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "4"))
SHORT_SELLING_ENABLED = os.getenv('SHORT_SELLING_ENABLED', 'false').lower() in ('true', '1', 'yes')

# ============================================================
#  日志配置
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ============================================================
#  兼容函数
# ============================================================

def get_config_value(key: str, default: Any = None) -> Any:
    """
    兼容函数：获取配置值

    支持的路径:
    - trading.mode
    - risk_limits.max_drawdown
    - strategy.weights
    """
    return _get_value(key, default)


def reload_config() -> bool:
    """重新加载配置"""
    center = get_center()
    if center:
        return center.reload_if_changed()
    return False


def get_config_center_instance() -> Optional[ConfigCenter]:
    """获取配置中心实例"""
    return get_center()


# ============================================================
#  启动时输出配置状态
# ============================================================
def _print_config_status():
    """打印配置加载状态"""
    center = get_center()
    if center:
        print(f"\n{'='*60}")
        print("配置中心状态")
        print(f"{'='*60}")
        print(f"配置目录：{center.config_dir}")
        print(f"校验结果：{center.get_validation_summary()}")

        warnings_list = center.get_validation_warnings()
        if warnings_list:
            print(f"\n警告 ({len(warnings_list)}):")
            for w in warnings_list[:5]:
                print(f"  - {w}")

        print(f"{'='*60}\n")

try:
    _print_config_status()
except Exception:
    pass
