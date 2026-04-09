"""
FTSeek 场景识别与路由系统

场景识别 → 策略路由 → 共识融合 → 风控执行 → 绩效反馈
"""
from .classifier import ScenarioClassifier, ScenarioOutput, ScenarioType, get_scenario_classifier
from .router import ScenarioRouter, RouteResult, get_scenario_router
from .features import FeatureExtractor, Features, get_feature_extractor
from .memory import ScenarioMemory, get_scenario_memory
from .config import (
    CORE_SCENARIO_ROUTING,
    DEFAULT_WEIGHT_CONFIG,
    SCENARIO_CLASSIFIER_CONFIG,
    get_scenario_config,
    get_all_scenarios,
    is_scenario_configured,
)

__all__ = [
    "ScenarioClassifier",
    "ScenarioOutput",
    "ScenarioType",
    "ScenarioRouter",
    "RouteResult",
    "FeatureExtractor",
    "Features",
    "ScenarioMemory",
    # 单例获取函数
    "get_scenario_classifier",
    "get_scenario_router",
    "get_scenario_memory",
    "get_feature_extractor",
    # 配置
    "CORE_SCENARIO_ROUTING",
    "DEFAULT_WEIGHT_CONFIG",
    "SCENARIO_CLASSIFIER_CONFIG",
    "get_scenario_config",
    "get_all_scenarios",
    "is_scenario_configured",
]
