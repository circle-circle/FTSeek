"""
权重解析器 (Weight Resolver)

从 consensus_engine.py 提取的权重计算逻辑。
包含:
  - _calc_weight: 基础权重计算（市场状态调整 + 场景 + 新闻倍增）
  - _get_hybrid_weight: 混合权重引擎集成
  - 趋势/震荡策略分类常量

拆分自 consensus_engine.py (原行 1060-1187 + 691-853)
"""
import logging
from typing import Dict, Tuple, Optional
from config.config_center import get_config_center as _gcc_wr

logger = logging.getLogger("ftseek.strategy.weight_resolver")

_cc_wr = _gcc_wr()

# ── 策略分类常量 ────────────────────────────────────────────────────
TREND_STRATEGIES = {"macd", "macd_factor", "supertrend", "st_factor", "ma_cross_factor", "confluence_v2"}
OSCILLATOR_STRATEGIES = {"rsi", "rsi_factor", "bollinger", "bb_factor"}
MTF_STRATEGIES = {"enhanced_confluence", "layered_integration"}
SENTIMENT_STRATEGIES = {"sentiment", "sentiment_ai", "news_sentiment", "event_driven", "premarket_analyst"}

# 市场状态增益/衰减 — 2026-05-16: 迁移至 SSOT
TREND_BOOST = float(_cc_wr.get('strategy.consensus.trend_boost', 1.5))
TREND_DECAY = float(_cc_wr.get('strategy.consensus.trend_decay', 0.6))
RANGE_BOOST = float(_cc_wr.get('strategy.consensus.range_boost', 1.5))
RANGE_DECAY = float(_cc_wr.get('strategy.consensus.range_decay', 0.6))
MTF_TREND_BOOST = float(_cc_wr.get('strategy.consensus.mtf_trend_boost', 1.3))
MTF_RANGE_DECAY = float(_cc_wr.get('strategy.consensus.mtf_range_decay', 0.7))

# 突发新闻倍增 — 2026-05-16: 迁移至 SSOT
BREAKING_NEWS_MULTIPLIER = float(_cc_wr.get('strategy.consensus.breaking_news_multiplier', 2.0))
BREAKING_NEWS_LOOKBACK_HOURS = int(_cc_wr.get('strategy.consensus.breaking_news_lookback_hours', 2))

# 灰区防守 — 2026-05-16: 迁移至 SSOT
GRAY_ZONE_PENALTY = float(_cc_wr.get('strategy.consensus.gray_zone_penalty', -0.3))
DIVERGENCE_THRESHOLD = float(_cc_wr.get('strategy.consensus.divergence_threshold', 0.5))
DIVERGENCE_SCORE_DECAY = float(_cc_wr.get('strategy.consensus.divergence_score_decay', 0.5))
DIVERGENCE_GRAY_ZONE = _cc_wr.get('strategy.consensus.divergence_gray_zone', True)


class WeightResolver:
    """
    权重解析器

    计算策略在特定市场状态下的动态权重:
      final_weight = base × market_adjustment × news_multiplier + scenario_delta
    """

    def __init__(
        self,
        base_weights: Dict[str, float],
        weight_engine=None,
        hybrid_enabled: bool = True,
    ):
        self.base_weights = base_weights
        self._weight_engine = weight_engine
        self.hybrid_enabled = hybrid_enabled

    def calc_weight(
        self,
        strategy: str,
        regime: str,
        code: str = None,
        scenario_router=None,
        scenario_cache: Dict = None,
        scenario_phase_cache: Dict = None,
        scenario_tags_cache: Dict = None,
        breaking_news_cache: Dict = None,
        check_breaking_news_fn=None,
    ) -> Tuple[float, Dict]:
        """
        计算某策略在当前市场状态下的实际权重

        Args:
            strategy: 策略名称
            regime: 市场状态 ("trending" / "ranging" / "neutral")
            code: 股票代码
            scenario_router: 场景路由器
            scenario_cache: {code: scenario}
            scenario_phase_cache: {code: phase}
            scenario_tags_cache: {code: [tags]}
            breaking_news_cache: {code: (has_breaking, timestamp)}
            check_breaking_news_fn:  callable(code) -> bool

        Returns:
            (final_weight, weight_details)
        """
        # 使用混合权重引擎 (如果启用)
        if self.hybrid_enabled and self._weight_engine is not None:
            return self._get_hybrid_weight(
                strategy, regime, code,
                scenario_router, scenario_cache, scenario_phase_cache,
                scenario_tags_cache, breaking_news_cache, check_breaking_news_fn,
            )

        # 降级: 原有计算逻辑
        base = self.base_weights.get(strategy, 0.5)

        # 场景路由权重调整 (加法 delta)
        scenario_delta = 0.0
        if code and scenario_router is not None and scenario_cache is not None:
            try:
                current_scenario = scenario_cache.get(code)
                if current_scenario:
                    if getattr(current_scenario, 'value', '') == 'gray_zone':
                        scenario_delta = GRAY_ZONE_PENALTY
                        logger.debug(f"[灰区防守] {code} {strategy} delta={scenario_delta:+.2f}")
                    else:
                        route_result = scenario_router.route(current_scenario)
                        scenario_weight = getattr(route_result, 'active_strategies', {}).get(strategy, 1.0)
                        scenario_delta = (scenario_weight - 1.0) * 0.3

                    if scenario_phase_cache is not None:
                        phase = scenario_phase_cache.get(code)
                        if phase is not None and getattr(phase, 'value', '') == 'decaying':
                            scenario_delta *= 0.5
            except Exception as e:
                logger.debug(f"[场景权重] {code} {strategy} 计算异常: {e}")

        # 突发新闻权重倍增
        news_multiplier = 1.0
        if strategy in SENTIMENT_STRATEGIES and code and check_breaking_news_fn:
            has_breaking = check_breaking_news_fn(code)
            if has_breaking:
                news_multiplier = BREAKING_NEWS_MULTIPLIER
                logger.info(f"[突发新闻] {code} {strategy} 权重 ×{BREAKING_NEWS_MULTIPLIER:.1f}")

        # 市场状态调整
        market_adjustment = 1.0
        if regime == "trending":
            if strategy in TREND_STRATEGIES:
                market_adjustment = TREND_BOOST
            elif strategy in OSCILLATOR_STRATEGIES:
                market_adjustment = TREND_DECAY
            elif strategy in MTF_STRATEGIES:
                market_adjustment = MTF_TREND_BOOST
        elif regime == "ranging":
            if strategy in OSCILLATOR_STRATEGIES:
                market_adjustment = RANGE_BOOST
            elif strategy in TREND_STRATEGIES:
                market_adjustment = RANGE_DECAY
            elif strategy in MTF_STRATEGIES:
                market_adjustment = MTF_RANGE_DECAY

        final_weight = base * market_adjustment * news_multiplier + scenario_delta

        # 标签微调
        if code and scenario_tags_cache:
            tags = scenario_tags_cache.get(code, [])
            tag_delta = 0.0
            if "vol_expanding" in tags and strategy in TREND_STRATEGIES:
                tag_delta += 0.02
            if "reversal_risk" in tags and strategy in TREND_STRATEGIES:
                tag_delta -= 0.03
            if "mean_reversion" in tags and strategy in OSCILLATOR_STRATEGIES:
                tag_delta += 0.02
            if "mtf_divergent" in tags:
                tag_delta -= 0.02
            if tag_delta != 0.0:
                final_weight += tag_delta

        # 保护下界
        final_weight = max(0.1, final_weight)

        weight_details = {
            'base_weight': base,
            'market_adjustment': market_adjustment,
            'scenario_delta': scenario_delta,
            'news_multiplier': news_multiplier,
            'hybrid_adjustment': 1.0,
        }

        return final_weight, weight_details

    def _get_hybrid_weight(self, *args, **kwargs):
        """
        委托给 WeightEngine 的混合权重计算。
        如果 WeightEngine 不可用，降级到原有逻辑。
        """
        if self._weight_engine is not None:
            try:
                return self._weight_engine.calc_weight(*args, **kwargs)
            except Exception as e:
                logger.warning(f"[混合权重] 回退到原有逻辑: {e}")
        # 降级
        return self.calc_weight(*args, **kwargs)
