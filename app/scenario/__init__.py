"""
FTSeek 场景识别与路由系统

场景识别 → 策略路由 → 共识融合 → 风控执行 → 绩效反馈

Phase 2 新增:
- 多周期特征提取器 (Multi-Timeframe Feature Extractor)
- 跨周期趋势确认
- 多周期共振检测
"""
from .classifier import ScenarioClassifier, ScenarioOutput, ScenarioType, get_scenario_classifier
from .router import ScenarioRouter, RouteResult, get_scenario_router
from .features import FeatureExtractor, Features, get_feature_extractor
from .memory import ScenarioMemory, ScenarioPhase, ScenarioUpdateResult, get_scenario_memory
from .multi_timeframe import (
    MultiTimeframeFeatureExtractor,
    MultiTimeframeFeatures,
    TimeframeFeatures,
    get_multi_timeframe_extractor,
)
from .log import (
    ScenarioLogManager,
    get_scenario_log_manager,
)
from .config import (
    CORE_SCENARIO_ROUTING,
    DEFAULT_WEIGHT_CONFIG,
    SCENARIO_CLASSIFIER_CONFIG,
    get_scenario_config,
    get_all_scenarios,
    is_scenario_configured,
)

__all__ = [
    # 核心类
    "ScenarioClassifier",
    "ScenarioOutput",
    "ScenarioType",
    "ScenarioRouter",
    "RouteResult",
    "FeatureExtractor",
    "Features",
    "ScenarioMemory",
    "ScenarioPhase",
    "ScenarioUpdateResult",
    # Phase 2 新增
    "MultiTimeframeFeatureExtractor",
    "MultiTimeframeFeatures",
    "TimeframeFeatures",
    "ScenarioLogManager",
    # 单例获取函数
    "get_scenario_classifier",
    "get_scenario_router",
    "get_scenario_memory",
    "get_feature_extractor",
    "get_multi_timeframe_extractor",
    "get_scenario_log_manager",
    # 配置
    "CORE_SCENARIO_ROUTING",
    "DEFAULT_WEIGHT_CONFIG",
    "SCENARIO_CLASSIFIER_CONFIG",
    "get_scenario_config",
    "get_all_scenarios",
    "is_scenario_configured",
]
