from .base import BaseStrategy, Signal, SignalAction
from .price_break import PriceBreakStrategy
from .ma_cross import MACrossStrategy
from .volume_alert import VolumeAlertStrategy
from .rsi_strategy import RSIStrategy
from .macd_strategy import MACDStrategy
from .bollinger_strategy import BollingerStrategy
from .supertrend_strategy import SuperTrendStrategy
from .confluence_strategy import ConfluenceStrategy
from .tradingview_webhook import parse_tradingview_webhook, normalize_ticker
from .market_regime import MarketRegimeDetector, market_regime_detector, MarketRegime
from .confluence_v2 import ConfluenceStrategyV2

# 策略注册表：name -> class
# fusion_v2 延迟导入避免循环依赖
STRATEGY_REGISTRY = {
    "price_break": PriceBreakStrategy,
    "ma_cross": MACrossStrategy,
    "volume_alert": VolumeAlertStrategy,
    "rsi": RSIStrategy,
    "macd": MACDStrategy,
    "bollinger": BollingerStrategy,
    "supertrend": SuperTrendStrategy,
    "confluence": ConfluenceStrategy,
    "confluence_v2": ConfluenceStrategyV2,  # 市场状态增强版
}

# 延迟加载 fusion_v2
def _get_fusion_v2_class():
    """延迟导入 fusion_v2 避免循环依赖"""
    try:
        from .fusion_v2 import FusionV2Strategy
        return FusionV2Strategy
    except Exception as e:
        return None

# 尝试注册 fusion_v2
_fusion_cls = _get_fusion_v2_class()
if _fusion_cls:
    STRATEGY_REGISTRY['fusion_v2'] = _fusion_cls


def create_strategy(name: str, cfg: dict = None) -> BaseStrategy:
    """根据名称创建策略实例"""
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略：{name}, 可用：{list(STRATEGY_REGISTRY.keys())}")
    return cls(cfg)


def list_strategies() -> list:
    """列出所有可用策略"""
    return list(STRATEGY_REGISTRY.keys())
