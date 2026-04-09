"""
因子库模块 - 另类因子 + 技术因子实现
"""
from .base import BaseFactor, FactorResult
from .config import FactorConfig, DEFAULT_FACTOR_WEIGHTS
from .capital_flow import CapitalFlowFactors
from .sentiment import SentimentFactors, sentiment_score
from .sentiment_ai import AISentimentFactor
from .market_prediction_ai import AIPredictionFactor
from .esg import ESGFactors
from .preprocessing import FactorPreprocessor
# Phase 3: 情绪衰减和新闻集成
from .sentiment_decay import SentimentDecay, DecayConfig, get_decay_calculator
from .news_integration import NewsSentimentAnalyzer, get_news_sentiment_analyzer

# 技术因子 (2026-04-06 新增)
from .technical import (
    # 基类
    FactorSignal,
    FactorDirection,

    # 技术因子
    RSIFactor,
    MACDFactor,
    BollingerFactor,
    SuperTrendFactor,
    MACrossFactor,
    VolumeFactor,
    ADXFilterFactor,

    # 工厂函数
    create_factor,
    list_factors,
    FACTOR_REGISTRY,
)

# 质量管理器 (2026-04-06 新增 - 巴菲特理念实现)
from .quality_scorer import (
    QualityScorer,
    QualityClass,
    QualityScore,
    get_quality_scorer,
    reset_quality_scorer,
)

# 持仓周期管理器
from .holding_period_manager import (
    HoldingPeriodManager,
    HoldingTier,
    HoldingPeriodConfig,
    get_holding_period_manager,
    reset_holding_period_manager,
)

# 资金水位管理器
from .water_level_manager import (
    WaterLevelManager,
    WaterLevel,
    WaterLevelConfig,
    WaterLevelState,
    get_water_level_manager,
    reset_water_level_manager,
)

# 止盈止损管理器
from .profit_loss_manager import (
    ProfitLossManager,
    ExitType,
    ExitSignal,
    PositionExitConfig,
    get_profit_loss_manager,
    reset_profit_loss_manager,
)

# 估值因子 (2026-04-06 新增 - 安全边际计算)
from .valuation import (
    ValuationCalculator,
    ValuationSignal,
    ValuationResult,
    get_valuation_calculator,
    reset_valuation_calculator,
)

__all__ = [
    # 原有因子
    'BaseFactor', 'FactorResult', 'FactorConfig',
    'CapitalFlowFactors', 'SentimentFactors', 'AISentimentFactor',
    'AIPredictionFactor', 'ESGFactors',
    'FactorPreprocessor', 'sentiment_score',
    'DEFAULT_FACTOR_WEIGHTS',
    # Phase 3 新增
    'SentimentDecay', 'DecayConfig', 'get_decay_calculator',
    'NewsSentimentAnalyzer', 'get_news_sentiment_analyzer',

    # 技术因子 (2026-04-06)
    'FactorSignal',
    'FactorDirection',
    'RSIFactor',
    'MACDFactor',
    'BollingerFactor',
    'SuperTrendFactor',
    'MACrossFactor',
    'VolumeFactor',
    'ADXFilterFactor',
    'create_factor',
    'list_factors',
    'FACTOR_REGISTRY',

    # 质量管理器 (2026-04-06 - 巴菲特理念实现)
    'QualityScorer',
    'QualityClass',
    'QualityScore',
    'get_quality_scorer',
    'reset_quality_scorer',
    'HoldingPeriodManager',
    'HoldingTier',
    'HoldingPeriodConfig',
    'get_holding_period_manager',
    'reset_holding_period_manager',
    'WaterLevelManager',
    'WaterLevel',
    'WaterLevelConfig',
    'WaterLevelState',
    'get_water_level_manager',
    'reset_water_level_manager',
    'ProfitLossManager',
    'ExitType',
    'ExitSignal',
    'PositionExitConfig',
    'get_profit_loss_manager',
    'reset_profit_loss_manager',

    # 估值因子 (2026-04-06 - 安全边际计算)
    'ValuationCalculator',
    'ValuationSignal',
    'ValuationResult',
    'get_valuation_calculator',
    'reset_valuation_calculator',
]
