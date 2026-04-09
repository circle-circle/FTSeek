"""
场景路由器 (Scenario Router)

根据场景分类结果，动态调整策略组合和权重。

核心功能:
1. 场景→策略映射表配置
2. 动态权重计算 (primary/secondary/suppressed)
3. 策略优先级调整
4. 风控模板切换

路由矩阵示例:
| 场景 | 主策略 (权重 1.5) | 辅助策略 (权重 1.0) | 抑制策略 (权重 0.3) |
|------|-----------------|-------------------|-------------------|
| TREND_STRONG | supertrend, confluence_v2 | enhanced_confluence | rsi, bollinger |
| RANGE_BOUND | rsi, bollinger | confluence_v2 | supertrend |
| GAP_EVENT | event_driven | sentiment_ai | 所有技术指标 |
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any
from enum import Enum

from .classifier import ScenarioType

logger = logging.getLogger("ftseek.scenario.router")
logger.propagate = False


class StrategyRole(Enum):
    """策略角色"""
    PRIMARY = "primary"       # 主策略 (权重 1.5x)
    SECONDARY = "secondary"   # 辅助策略 (权重 1.0x)
    SUPPRESSED = "suppressed" # 抑制策略 (权重 0.3x)


@dataclass
class RouteResult:
    """路由结果"""
    scenario: ScenarioType
    active_strategies: Dict[str, float]  # {策略名：权重}
    primary_strategies: List[str]
    secondary_strategies: List[str]
    suppressed_strategies: List[str]
    risk_template: str = "default"  # 风控模板
    priority_boost: Dict[str, float] = field(default_factory=dict)  # 优先级提升 {策略名：boost}
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "scenario": self.scenario.value,
            "active_strategies": {k: round(v, 2) for k, v in self.active_strategies.items()},
            "primary": self.primary_strategies,
            "secondary": self.secondary_strategies,
            "suppressed": self.suppressed_strategies,
            "risk_template": self.risk_template,
            "metadata": self.metadata,
        }


class ScenarioRouter:
    """
    场景路由器

    根据场景类型，返回对应的策略组合和权重配置。
    """

    # 默认场景→策略映射表
    # 更新于 2026-04-09: 仅使用已实现的策略
    DEFAULT_ROUTING_CONFIG: Dict[ScenarioType, Dict] = {
        # --- 趋势类场景 ---
        ScenarioType.TREND_STRONG: {
            "primary": ["supertrend", "confluence_v2"],
            "secondary": ["enhanced_confluence", "macd"],
            "suppressed": ["rsi", "bollinger"],
            "risk_template": "trend_following",
            "priority_boost": {"supertrend": 1.5},
        },
        ScenarioType.TREND_START: {
            # 123_rule → confluence_v2, turtle → supertrend
            "primary": ["confluence_v2", "supertrend"],
            "secondary": ["enhanced_confluence"],
            "suppressed": ["rsi"],
            "risk_template": "trend_following",
            "priority_boost": {"confluence_v2": 1.5},
        },
        ScenarioType.TREND_EXHAUST: {
            # td_range → confluence_v2, 123_rule → supertrend
            "primary": ["confluence_v2", "supertrend"],
            "secondary": ["enhanced_confluence"],
            "suppressed": ["rsi", "bollinger"],
            "risk_template": "reversal",
            "priority_boost": {"confluence_v2": 1.5},
        },

        # --- 震荡类场景 ---
        ScenarioType.RANGE_BOUND: {
            "primary": ["rsi", "bollinger"],
            "secondary": ["confluence_v2", "open_close_effect"],
            "suppressed": ["supertrend", "enhanced_confluence"],
            "risk_template": "mean_reversion",
            "priority_boost": {"rsi": 1.5, "bollinger": 1.5},
        },

        # --- 波动类场景 ---
        ScenarioType.VOL_EXPANSION: {
            # volatility_breakout → confluence_v2
            "primary": ["confluence_v2", "supertrend"],
            "secondary": ["event_driven", "enhanced_confluence"],
            "suppressed": ["rsi", "bollinger"],
            "risk_template": "breakout",
            "priority_boost": {"confluence_v2": 1.5},
        },
        ScenarioType.VOL_CONTRACTION: {
            # range_bound_strategy → rsi + bollinger
            "primary": ["bollinger", "rsi"],
            "secondary": ["confluence_v2"],
            "suppressed": ["supertrend"],
            "risk_template": "low_volatility",
            "priority_boost": {"bollinger": 1.3},
        },

        # --- 事件类场景 ---
        ScenarioType.GAP_EVENT: {
            # gap_trading → event_driven
            "primary": ["event_driven", "sentiment_ai"],
            "secondary": ["confluence_v2"],
            "suppressed": ["rsi", "bollinger", "macd"],
            "risk_template": "event_driven",
            "priority_boost": {"event_driven": 2.0},
        },
        ScenarioType.NEWS_DRIVEN: {
            "primary": ["sentiment_ai", "event_driven"],
            "secondary": ["confluence_v2"],
            "suppressed": ["rsi", "bollinger", "macd"],
            "risk_template": "event_driven",
            "priority_boost": {"sentiment_ai": 2.0},
        },

        # --- 反转/突破类场景 ---
        ScenarioType.REVERSAL_SETUP: {
            # 123_rule → supertrend, rsi_divergence → rsi
            "primary": ["supertrend", "rsi"],
            "secondary": ["confluence_v2", "sentiment_ai"],
            "suppressed": ["bollinger"],
            "risk_template": "reversal",
            "priority_boost": {"supertrend": 1.5},
        },
        ScenarioType.BREAKOUT_SETUP: {
            # turtle → supertrend, bollinger_breakout → bollinger
            "primary": ["supertrend", "bollinger"],
            "secondary": ["confluence_v2", "enhanced_confluence"],
            "suppressed": ["rsi"],
            "risk_template": "breakout",
            "priority_boost": {"supertrend": 1.5},
        },

        # --- 默认场景 ---
        ScenarioType.UNKNOWN: {
            "primary": ["confluence_v2"],
            "secondary": ["supertrend", "rsi"],
            "suppressed": [],
            "risk_template": "default",
            "priority_boost": {},
        },
    }

    # 默认权重倍数
    PRIMARY_WEIGHT = 1.5      # 主策略权重
    SECONDARY_WEIGHT = 1.0    # 辅助策略权重
    SUPPRESSED_WEIGHT = 0.3   # 抑制策略权重

    # 策略白名单 (系统中已实现的策略)
    # 更新于 2026-04-09: 仅包含实际实现的策略
    AVAILABLE_STRATEGIES = {
        # === 核心策略 ===
        "confluence_v2",       # 主力融合策略
        "supertrend",          # 趋势跟踪
        "sentiment_ai",        # AI 情绪
        "event_driven",        # 事件驱动
        "enhanced_confluence", # 技术增强

        # === 辅助策略 ===
        "rsi",                 # RSI (包含在 confluence_v2 中)
        "bollinger",           # 布林带 (包含在 confluence_v2 中)
        "macd",                # MACD (包含在 confluence_v2 中)
        "open_close_effect",   # 开盘/收盘效应

        # === 特殊标记占位符 (会在_expand_special_markers 中展开) ===
        # 注意：以下是占位符，不是实际策略
        "123_rule",            # → 占位符，实际使用 confluence_v2 替代
        "turtle",              # → 占位符，实际使用 supertrend 替代
        "td_range",            # → 占位符，实际使用 confluence_v2 替代
        "gap_trading",         # → 占位符，实际使用 event_driven 替代
        "volatility_breakout", # → 占位符，实际使用 confluence_v2 替代
        "bollinger_breakout",  # → 占位符，实际使用 bollinger 替代
        "volume_breakout",     # → 占位符，实际使用 enhanced_confluence 替代
        "range_bound_strategy",# → 占位符，实际使用 rsi/bollinger 替代
        "rsi_divergence",      # → 占位符，实际使用 rsi 替代
        "mean_reversion_strategies",  # → 特殊标记
        "trend_following_strategies", # → 特殊标记
        "breakout_strategies",        # → 特殊标记
        "all_technical_indicators",   # → 特殊标记
    }

    def __init__(self, config: Dict = None):
        """
        初始化场景路由器

        Args:
            config: 配置字典，可覆盖默认映射表
        """
        self.config = config or {}

        # 加载自定义配置 (如有)
        self.routing_config = self._load_custom_config(config)

        # 策略权重缓存
        self._weight_cache: Dict[str, float] = {}

        logger.info("场景路由器已初始化")
        logger.info(f"已配置 {len(self.routing_config)} 个场景的路由规则")

    def _load_custom_config(self, config: Dict = None) -> Dict[ScenarioType, Dict]:
        """加载自定义配置，覆盖默认值"""
        if not config or 'routing_config' not in config:
            return self.DEFAULT_ROUTING_CONFIG.copy()

        custom_config = config.get('routing_config', {})

        # 合并配置 (自定义配置覆盖默认值)
        merged_config = self.DEFAULT_ROUTING_CONFIG.copy()
        for scenario_str, rules in custom_config.items():
            try:
                scenario = ScenarioType(scenario_str)
                if scenario in merged_config:
                    merged_config[scenario].update(rules)
                else:
                    merged_config[scenario] = rules
            except ValueError:
                logger.warning(f"未知场景类型：{scenario_str}, 跳过")

        return merged_config

    def route(self, scenario: ScenarioType) -> RouteResult:
        """
        根据场景类型返回策略路由结果

        Args:
            scenario: 场景类型

        Returns:
            RouteResult: 路由结果
        """
        config = self.routing_config.get(scenario, self.routing_config.get(ScenarioType.UNKNOWN))

        if not config:
            logger.warning(f"场景 {scenario} 无路由配置，使用默认配置")
            config = self.routing_config[ScenarioType.UNKNOWN]

        # 解析策略列表
        primary = config.get('primary', [])
        secondary = config.get('secondary', [])
        suppressed = config.get('suppressed', [])

        # 过滤特殊标记 (如 "all_technical_indicators")
        primary = self._expand_special_markers(primary)
        secondary = self._expand_special_markers(secondary)
        suppressed = self._expand_special_markers(suppressed)

        # 去重
        primary = list(set(primary))
        secondary = list(set(secondary) - set(primary))
        suppressed = list(set(suppressed) - set(primary) - set(secondary))

        # 计算权重
        active_strategies = {}
        for s in primary:
            active_strategies[s] = self.PRIMARY_WEIGHT
        for s in secondary:
            active_strategies[s] = self.SECONDARY_WEIGHT
        for s in suppressed:
            active_strategies[s] = self.SUPPRESSED_WEIGHT

        # 获取风控模板
        risk_template = config.get('risk_template', 'default')

        # 获取优先级提升
        priority_boost = config.get('priority_boost', {})

        result = RouteResult(
            scenario=scenario,
            active_strategies=active_strategies,
            primary_strategies=primary,
            secondary_strategies=secondary,
            suppressed_strategies=suppressed,
            risk_template=risk_template,
            priority_boost=priority_boost,
            metadata={
                "config_source": "custom" if self.config.get('routing_config') else "default",
            }
        )

        logger.info(
            f"[路由] {scenario.value} → "
            f"主策略={primary} | "
            f"辅助={secondary} | "
            f"抑制={suppressed} | "
            f"风控={risk_template}"
        )

        return result

    def _expand_special_markers(self, strategies: List[str]) -> List[str]:
        """
        展开特殊标记和占位符策略

        Args:
            strategies: 策略列表 (可能包含特殊标记或占位符)

        Returns:
            展开后的策略列表

        占位符映射 (2026-04-09):
        - 123_rule → supertrend
        - turtle → supertrend
        - td_range → confluence_v2
        - gap_trading → event_driven
        - volatility_breakout → confluence_v2
        - bollinger_breakout → bollinger
        - volume_breakout → enhanced_confluence
        - range_bound_strategy → rsi
        - rsi_divergence → rsi
        """
        expanded = []

        # 占位符映射表 (未实现的策略→已实现的策略)
        placeholder_map = {
            "123_rule": "supertrend",
            "turtle": "supertrend",
            "td_range": "confluence_v2",
            "gap_trading": "event_driven",
            "volatility_breakout": "confluence_v2",
            "bollinger_breakout": "bollinger",
            "volume_breakout": "enhanced_confluence",
            "range_bound_strategy": "rsi",
            "rsi_divergence": "rsi",
        }

        # 特殊标记展开
        special_markers = {
            "all_technical_indicators": ["rsi", "bollinger", "macd", "supertrend"],
            "mean_reversion_strategies": ["rsi", "bollinger"],
            "trend_following_strategies": ["supertrend", "confluence_v2"],
            "breakout_strategies": ["supertrend", "confluence_v2", "bollinger"],
            "pure_technical_strategies": ["rsi", "bollinger", "macd", "supertrend"],
        }

        for s in strategies:
            # 1. 检查是否是占位符
            if s in placeholder_map:
                expanded.append(placeholder_map[s])
            # 2. 检查是否是特殊标记
            elif s in special_markers:
                expanded.extend(special_markers[s])
            # 3. 检查是否是已实现策略
            elif s in self.AVAILABLE_STRATEGIES:
                expanded.append(s)
            else:
                logger.debug(f"未知策略标记：{s}, 跳过")

        return expanded

    def get_strategy_weight(self, scenario: ScenarioType, strategy: str) -> float:
        """
        获取某策略在特定场景下的权重

        Args:
            scenario: 场景类型
            strategy: 策略名称

        Returns:
            权重值 (0 表示被抑制)
        """
        cache_key = f"{scenario.value}_{strategy}"
        if cache_key in self._weight_cache:
            return self._weight_cache[cache_key]

        result = self.route(scenario)
        weight = result.active_strategies.get(strategy, 0.0)

        self._weight_cache[cache_key] = weight
        return weight

    def is_strategy_active(self, scenario: ScenarioType, strategy: str,
                           min_weight: float = 0.5) -> bool:
        """
        判断某策略在特定场景下是否激活

        Args:
            scenario: 场景类型
            strategy: 策略名称
            min_weight: 最小权重阈值

        Returns:
            是否激活
        """
        weight = self.get_strategy_weight(scenario, strategy)
        return weight >= min_weight

    def get_risk_template(self, scenario: ScenarioType) -> str:
        """
        获取场景对应的风控模板

        Args:
            scenario: 场景类型

        Returns:
            风控模板名称
        """
        config = self.routing_config.get(scenario, self.routing_config.get(ScenarioType.UNKNOWN))
        return config.get('risk_template', 'default')

    def clear_cache(self):
        """清除权重缓存"""
        self._weight_cache.clear()


# 全局单例
_router: Optional[ScenarioRouter] = None


def get_scenario_router(config: Dict = None) -> ScenarioRouter:
    """获取场景路由器单例"""
    global _router
    if _router is None:
        _router = ScenarioRouter(config)
    return _router
