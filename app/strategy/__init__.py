"""
FTSeek 策略模块

策略注册机制 (2026-04-13 更新):
  1. 手动注册: 核心策略通过 import 显式注册
  2. 自动发现: 通过 discovery.discover_strategies() 自动发现新策略
  
新增策略只需:
  1. 在 app/strategy/ 目录下创建 .py 文件
  2. 定义继承自 BaseStrategy 的类 (带 name 属性)
  3. 在 config.yaml 的 strategy.enabled 中添加策略名
  无需修改本文件.
"""

# ============================================================
#  核心导入: 基础类和工具
# ============================================================
from .base import BaseStrategy, Signal, SignalAction
from .tradingview_webhook import parse_tradingview_webhook, normalize_ticker
from .market_regime import MarketRegimeDetector, market_regime_detector, MarketRegime
from .market_aware import MarketAwareMixin

# ============================================================
#  策略类导入 (向后兼容)
# ============================================================
# 基础策略
from .price_break import PriceBreakStrategy
from .ma_cross import MACrossStrategy
from .volume_alert import VolumeAlertStrategy
from .rsi_strategy import RSIStrategy
from .macd_strategy import MACDStrategy
from .bollinger_strategy import BollingerStrategy
from .supertrend_strategy import SuperTrendStrategy

# 透明化因子策略 (2026-05-03 新增)
from .factor_votes import (
    RSIFactorStrategy, MACDFactorStrategy, BBFactorStrategy,
    SuperTrendFactorStrategy, MACrossFactorStrategy, VolumeAmplifierStrategy
)

# 融合策略
# from .confluence_strategy import ConfluenceStrategy  # P0 Fix 2026-05-03: Removed missing V1 import
from .confluence_v2 import ConfluenceStrategyV2

# AI/情绪策略
from .sentiment_strategy import SentimentStrategy

# 高级策略
from .open_close_effect import OpenCloseEffectStrategy
from .event_driven import EventDrivenStrategy
from .cross_market import CrossMarketArbitrage
from .tech_enhancer import EnhancedConfluenceStrategy

# ============================================================
#  策略注册表
# ============================================================
# 启用的策略:
#   confluence_v2          - 主力融合策略 (RSI+MACD+BB+SuperTrend+MA+Volume)
#   sentiment_ai           - AI 情绪辅助
#   open_close_effect      - 开盘/收盘效应
#   event_driven           - 事件驱动 (突发新闻->LLM分析)
#   cross_market_arbitrage - 跨市场套利 (美港股联动)
#   enhanced_confluence    - 技术增强 (多周期共振 + 积小胜仓位)
#
# 禁用的策略 (保留类定义, 不注册):
#   rsi, macd, bollinger   - 已包含在 confluence_v2
#   supertrend             - 已包含在 confluence_v2
#   confluence (V1)        - 被 confluence_v2 替代
#   price_break            - 信号频率不匹配
#   ma_cross, volume_alert - 非核心策略

STRATEGY_REGISTRY = {
    # === 启用策略 (透明化因子共识架构) ===
    "rsi_factor": RSIFactorStrategy,
    "macd_factor": MACDFactorStrategy,
    "bb_factor": BBFactorStrategy,
    "st_factor": SuperTrendFactorStrategy,
    "ma_cross_factor": MACrossFactorStrategy,
    "vol_factor": VolumeAmplifierStrategy,
    # 辅助策略
    "sentiment_ai": SentimentStrategy,
    "open_close_effect": OpenCloseEffectStrategy,
    "event_driven": EventDrivenStrategy,
    "cross_market_arbitrage": CrossMarketArbitrage,
    # 废弃/降级策略
    # "confluence_v2": ConfluenceStrategyV2,  # P0 Fix 2026-05-03: 降级为透明化架构
}

# 延迟加载 fusion_v2 (避免循环依赖)
def _try_load_fusion_v2():
    try:
        from .fusion_v2 import FusionV2Strategy
        return FusionV2Strategy
    except Exception:
        return None

_fusion_cls = _try_load_fusion_v2()
if _fusion_cls:
    STRATEGY_REGISTRY['fusion_v2'] = _fusion_cls


# ============================================================
#  自动发现: 从目录动态发现新策略
# ============================================================
def auto_discover_strategies(config=None):
    """
    自动发现并注册新策略.
    
    扫描 app/strategy/ 目录, 找到所有继承自 BaseStrategy 的类,
    根据 config 中的 strategy.enabled/disabled 列表决定是否注册.
    
    调用时机: 在 main.py 启动时调用一次.
    
    Args:
        config: 配置对象 (config_center), 支持 get() 方法
    """
    try:
        from .discovery import discover_strategies
        discover_strategies(config=config, registry=STRATEGY_REGISTRY)
    except Exception as e:
        import logging
        logger = logging.getLogger("ftseek.strategy")
        logger.warning(f"策略自动发现失败 (使用手动注册): {e}")


# ============================================================
#  策略操作函数
# ============================================================

def create_strategy(name: str, cfg: dict = None) -> BaseStrategy:
    """根据名称创建策略实例"""
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"未知策略: {name}, 可用: {list(STRATEGY_REGISTRY.keys())}")
    return cls(cfg)


def list_strategies() -> list:
    """列出所有已注册策略"""
    return list(STRATEGY_REGISTRY.keys())


def register_strategy(name: str, strategy_class: type) -> None:
    """
    注册策略到注册表.
    
    Args:
        name: 策略名称
        strategy_class: 策略类 (继承自 BaseStrategy)
    """
    if not issubclass(strategy_class, BaseStrategy):
        raise TypeError(f"策略类必须继承自 BaseStrategy")
    STRATEGY_REGISTRY[name] = strategy_class


def get_strategy_info(name: str = None) -> dict:
    """
    获取策略信息.
    
    Args:
        name: 策略名, 为 None 时返回所有策略信息
    
    Returns:
        策略信息字典
    """
    try:
        from .discovery import get_strategy_info as _get_info
        return _get_info(name)
    except Exception:
        # Fallback: basic info from registry
        info = {}
        for n, cls in STRATEGY_REGISTRY.items():
            if name and n != name:
                continue
            info[n] = {
                'class': cls.__name__,
                'module': cls.__module__,
            }
        return info if name is None else info.get(name)
