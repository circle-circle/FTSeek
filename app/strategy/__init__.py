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
from .sentiment_strategy import SentimentStrategy  # 新增：情绪策略 (2026-03-17)
from .market_aware import MarketAwareMixin  # 新增：市场状态感知 Mixin (2026-03-27)
from .open_close_effect import OpenCloseEffectStrategy  # 新增：开盘/收盘效应策略 (2026-04-05)
from .event_driven import EventDrivenStrategy  # 新增：事件驱动策略 (2026-04-09)
from .cross_market import CrossMarketArbitrage  # 新增：跨市场套利策略 (2026-04-09)
from .tech_enhancer import EnhancedConfluenceStrategy  # 新增：增强融合策略 (2026-04-09)

# 策略注册表：name -> class
# 优化于 2026-03-27: 去冗余，精简策略组合
# 更新于 2026-04-09: 新增三层策略架构 (事件驱动/跨市场套利/技术增强)
#
# 启用的策略 (推荐组合):
#   - confluence_v2: 主力融合策略 (RSI+MACD+BB+SuperTrend+MA+Volume)
#   - supertrend: 趋势跟踪主力
#   - sentiment_ai: AI 情绪辅助
#   - open_close_effect: 开盘/收盘效应 (增强信号频率)
#   - event_driven: 事件驱动 (突发新闻→LLM 分析→高优先级信号)
#   - cross_market_arbitrage: 跨市场套利 (美港股联动)
#   - enhanced_confluence: 技术增强 (多周期共振 + 积小胜仓位管理)
#
# 禁用的策略 (原因):
#   - rsi, macd, bollinger: 已包含在 confluence_v2 中，避免双重投票
#   - confluence (V1): 被 confluence_v2 替代
#   - price_break: 信号频率不匹配
#   - ma_cross, volume_alert: 非核心策略
#
STRATEGY_REGISTRY = {
    # === 启用策略 ===
    "confluence_v2": ConfluenceStrategyV2,  # 主力：多因子融合 + 市场状态感知
    "supertrend": SuperTrendStrategy,        # 趋势：趋势跟踪
    "sentiment_ai": SentimentStrategy,       # 情绪：AI 辅助确认
    "open_close_effect": OpenCloseEffectStrategy,  # 开盘/收盘效应
    "event_driven": EventDrivenStrategy,     # 事件驱动：突发新闻分析
    "cross_market_arbitrage": CrossMarketArbitrage,  # 跨市场：美港股套利
    "enhanced_confluence": EnhancedConfluenceStrategy,  # 技术增强：多周期共振 + 积小胜
    # === 禁用策略 (保留类定义，但不在注册表中) ===
    # "price_break": PriceBreakStrategy,      # 禁用：信号频率不匹配
    # "ma_cross": MACrossStrategy,            # 禁用：非核心
    # "volume_alert": VolumeAlertStrategy,    # 禁用：非核心
    # "rsi": RSIStrategy,                     # 禁用：已包含在 confluence_v2
    # "macd": MACDStrategy,                   # 禁用：已包含在 confluence_v2
    # "bollinger": BollingerStrategy,         # 禁用：已包含在 confluence_v2
    # "confluence": ConfluenceStrategy,       # 禁用：被 confluence_v2 替代
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


def register_strategy(name: str, strategy_class: type) -> None:
    """
    注册策略到注册表

    Args:
        name: 策略名称
        strategy_class: 策略类 (继承自 BaseStrategy)
    """
    if not issubclass(strategy_class, BaseStrategy):
        raise TypeError(f"策略类必须继承自 BaseStrategy")
    STRATEGY_REGISTRY[name] = strategy_class
    print(f"策略已注册：{name}")
