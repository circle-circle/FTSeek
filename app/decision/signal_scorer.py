"""
信号评分器 - 优化版

将原始信号升级为完整评分 (0-100 分)
优化点：自适应权重 + 负面因子优化
"""
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.models import SignalScore, MarketRegime
from app.core.events import EventStore
from .cost_model import CostModel, CostModelConfig

logger = logging.getLogger(__name__)


class SignalScorer:
    """信号评分器 - 优化版

    2026-04-28 改造:
    - 接入 CostModel, 作为"净边际守门" 负面因子.
      若 expected_return - total_cost < net_edge_floor_bps, 直接扣至 max_penalty.
    - score_breakdown 内写入 cost_bps / net_edge_bps / cost_penalty 用于归因.
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        
        # 基础评分因子权重
        self.base_factor_weights = {
            "trend_confirm": 0.20,
            "volume_confirm": 0.15,
            "volatility_fit": 0.10,
            "market_align": 0.15,
            "history_performance": 0.10,
            "news_sentiment": 0.10,
            "regime_fit": 0.10,
            "risk_reward": 0.10,
        }
        
        # Task #4: 权重版本缓存
        self._weight_version_cache: Optional[str] = None
        
        # 负面因子 (优化版 - 分阶段扣分)
        self.negative_factors = {
            "major_event_upcoming": -10,  # 重大事件 (分阶段)
            "low_liquidity": -10,         # 流动性不足 (根据强度调整)
            "high_spread": -5,            # 价差过大 (降低扣分)
            "signal_conflict": -10,       # 信号冲突 (考虑共识)
            "net_edge_below_floor": -20,  # 净边际低于门槛
        }

        # 2026-04-28: 成本模型
        cost_cfg_raw = config.get("COST_MODEL", {}) or {}
        self.cost_enabled = bool(cost_cfg_raw.get("enabled", True))
        self.cost_model = CostModel(
            CostModelConfig(**{
                k: v for k, v in cost_cfg_raw.items()
                if k in CostModelConfig.__dataclass_fields__
            })
        ) if self.cost_enabled else None
    
    def score(
        self,
        signal: SignalScore,
        market_context: Dict[str, Any],
        history_stats: Optional[Dict[str, Any]] = None,
    ) -> SignalScore:
        """对信号进行综合评分 - 优化版

        T5 (2026-05-16): 双层去重 —
        如果 signal 已携带 IntradayScorer 的 `_factor_detail` (含 trend/volume 评分),
        本层 trend_confirm / volume_confirm 权重置 0, 避免同一因子被乘 2 次.
        其他 6 个因子 (volatility_fit/market_align/history_performance/news_sentiment/
        regime_fit/risk_reward) 是本层独有的, 不受影响.
        """
        base_strength = signal.strength
        score_adjustments = {}

        # 获取自适应权重
        market_regime = market_context.get("market_regime", "RANGE")
        weights = self._get_adaptive_weights(market_regime, signal.strategy_id)

        # T5: 检测上游 IntradayScorer 是否已算了 trend/volume
        # _factor_detail 由 IntradayScorer.to_vote_result() 在 metadata 中传入
        upstream_factors = {}
        try:
            _meta = getattr(signal, "metadata", {}) or {}
            upstream_factors = _meta.get("_factor_detail", {}) or _meta.get("factor_detail", {}) or {}
        except Exception:
            upstream_factors = {}
        _has_upstream_trend = "trend" in upstream_factors and upstream_factors["trend"].get("conf", 0) > 0
        _has_upstream_volume = "volume" in upstream_factors and upstream_factors["volume"].get("conf", 0) > 0
        if _has_upstream_trend:
            weights["trend_confirm"] = 0.0
        if _has_upstream_volume:
            weights["volume_confirm"] = 0.0
        if _has_upstream_trend or _has_upstream_volume:
            logger.debug(
                "[scorer·T5] %s upstream_factors=%s → 二层权重置 0: trend=%s volume=%s",
                signal.symbol,
                list(upstream_factors.keys()),
                _has_upstream_trend, _has_upstream_volume,
            )

        # 1. 趋势确认
        trend_score = self._score_trend_alignment(signal, market_context)
        score_adjustments["trend_confirm"] = trend_score

        # 2. 成交量确认
        volume_score = self._score_volume_confirmation(signal, market_context)
        score_adjustments["volume_confirm"] = volume_score
        
        # 3. 波动率适配
        vol_score = self._score_volatility_fit(signal, market_context)
        score_adjustments["volatility_fit"] = vol_score
        
        # 4. 大盘同向
        market_score = self._score_market_alignment(signal, market_context)
        score_adjustments["market_align"] = market_score
        
        # 5. 历史表现
        history_score = self._score_history_performance(signal, history_stats)
        score_adjustments["history_performance"] = history_score
        
        # 6. 新闻情绪
        news_score = self._score_news_sentiment(signal, market_context)
        score_adjustments["news_sentiment"] = news_score
        
        # 7. 市场状态适配
        regime_score = self._score_regime_fit(signal, market_context)
        score_adjustments["regime_fit"] = regime_score
        
        # 8. 风险收益比
        rr_score = self._score_risk_reward(signal)
        score_adjustments["risk_reward"] = rr_score
        
        # 计算加权调整 (使用自适应权重)
        total_adjustment = 0
        for factor, score in score_adjustments.items():
            weight = weights.get(factor, 0.1)
            total_adjustment += score * weight
        
        # 应用负面因子 (优化版)
        negative_adjustment = self._apply_negative_factors_optimized(signal, market_context)

        # 成本守门: 净边际低于门槛直接重扣, 并写入归因
        cost_penalty = 0
        if self.cost_enabled and self.cost_model is not None:
            try:
                cost_penalty = self.cost_model.score_penalty(signal, market_context)
                net_edge_bps = self.cost_model.net_edge_bps(signal, market_context)
                # 归因明细 (不参与加权, 仅记录)
                score_adjustments["cost_penalty"] = int(cost_penalty)
                score_adjustments["net_edge_bps"] = int(round(net_edge_bps))
            except Exception as e:
                logger.debug("[scorer] cost model failed: %s", e)
                cost_penalty = 0

        # 最终评分 (0-100)
        final_strength = max(
            0,
            min(100, base_strength + total_adjustment + negative_adjustment + cost_penalty),
        )

        # 更新信号
        signal.strength = int(final_strength)
        signal.score_breakdown = score_adjustments
        signal.volatility_fit = vol_score > 0
        
        # 更新置信度 (基于评分稳定性)
        signal.confidence = self._calculate_confidence(score_adjustments)

        logger.info(
            "信号评分：%s %s base=%s neg=%s cost=%s -> final=%.1f",
            signal.symbol, signal.direction.value,
            base_strength, negative_adjustment, cost_penalty, final_strength,
        )

        return signal
    
    def _get_adaptive_weights(self, market_regime: str, strategy_id: str) -> Dict[str, float]:
        """根据市场状态、策略类型和历史 IC 表现返回自适应权重
        
        IC 反馈闭环 (2026-06-16):
        1. regime-based 权重作为基底
        2. 查询历史因子 IC → 负 IC 因子置零, 高 IC 因子 boost
        3. 归一化
        """
        # Task #4: 使用版本化权重作为基底
        weights = self._get_versioned_weights().copy()
        
        # 趋势市：提高趋势确认权重
        if market_regime in ["TREND_UP", "TREND_DOWN"]:
            weights["trend_confirm"] = 0.30
            weights["market_align"] = 0.20
            weights["regime_fit"] = 0.05
        
        # 震荡市：提高成交量和技术权重
        elif market_regime == "RANGE":
            weights["trend_confirm"] = 0.10
            weights["volume_confirm"] = 0.20
            weights["regime_fit"] = 0.15
        
        # 高波动：提高风险收益比权重
        if market_regime == "HIGH_VOLATILITY":
            weights["risk_reward"] = 0.20
            weights["volatility_fit"] = 0.15
        
        # 策略特定调整
        if strategy_id == "price_break":
            weights["volume_confirm"] = 0.25
        elif strategy_id == "rsi_rebound":
            weights["volatility_fit"] = 0.20
        elif strategy_id == "ma_cross":
            weights["trend_confirm"] = 0.25
        
        # ── IC 反馈闭环 (2026-06-16): 历史表现自动调整权重 ──
        weights = self._apply_ic_adaptation(weights)
        
        # 归一化
        total = sum(weights.values())
        if total > 0:
            return {k: v/total for k, v in weights.items()}
        return weights
    
    def _apply_ic_adaptation(self, weights: Dict[str, float]) -> Dict[str, float]:
        """根据历史因子 IC 自动调整评分权重
        
        逻辑:
        - IC < -0.05 (负相关): 权重置零 (因子方向错误，不再贡献)
        - IC > 0.08 (正相关且显著): 权重 × 1.5 (boost 有效因子)
        - IC 0~0.08: 保持原权重
        - 数据不足 (<20 信号): 不调整 (保守，等积累)
        
        缓存: 5 分钟 TTL，避免每次评分都查 DB
        """
        import time as _time
        
        # 缓存检查 (5 min TTL)
        _now = _time.time()
        _cache = getattr(self, '_ic_adapt_cache', None)
        if _cache and (_now - _cache.get('ts', 0)) < 300:
            ic_map = _cache.get('ic_map', {})
        else:
            ic_map = self._fetch_factor_ic_map()
            self._ic_adapt_cache = {'ts': _now, 'ic_map': ic_map}
        
        if not ic_map:
            return weights  # 无 IC 数据，不调整
        
        adapted = weights.copy()
        adjustments_log = []
        
        for factor, ic_data in ic_map.items():
            ic = ic_data.get('ic', 0)
            n = ic_data.get('count', 0)
            
            if n < 20:
                continue  # 样本不足，不调整
            
            if ic < -0.05:
                # 负相关因子：置零 (方向错误)
                if adapted.get(factor, 0) > 0:
                    adapted[factor] = 0.0
                    adjustments_log.append(f"{factor}: IC={ic:.3f} → ZERO")
            elif ic > 0.08:
                # 强正相关：boost 1.5x
                old_w = adapted.get(factor, 0.1)
                adapted[factor] = old_w * 1.5
                adjustments_log.append(f"{factor}: IC={ic:.3f} → BOOST ×1.5")
        
        if adjustments_log:
            logger.info("[scorer·IC闭环] 权重自适应: %s", "; ".join(adjustments_log))
        
        return adapted
    
    def _fetch_factor_ic_map(self) -> Dict[str, Dict]:
        """从 scoring_audit 获取因子级 IC 数据
        
        Returns: {factor_name: {"ic": float, "count": int, "pval": float}}
        """
        try:
            from app.analysis.scoring_audit import ScoringAudit
            audit = ScoringAudit()
            factor_results = audit.audit_factor_ic(days_back=30)
            
            ic_map = {}
            for item in factor_results:
                factor = item.get("factor", "")
                if factor:
                    ic_map[factor] = {
                        "ic": item.get("ic", 0),
                        "count": item.get("count", 0),
                        "pval": item.get("pval", 1.0),
                        "recommendation": item.get("recommendation", "keep"),
                    }
            
            if ic_map:
                logger.debug("[scorer·IC闭环] 加载 %d 个因子 IC: %s",
                    len(ic_map),
                    {k: round(v['ic'], 3) for k, v in ic_map.items()}
                )
            return ic_map
        except Exception as e:
            logger.debug("[scorer·IC闭环] 因子 IC 查询失败 (静默): %s", e)
            return {}
    
    def _apply_negative_factors_optimized(self, signal: SignalScore, context: Dict) -> int:
        """应用负面因子 - 优化版 (分阶段扣分，不过度惩罚)"""
        adjustment = 0
        
        # 1. 重大事件：分阶段扣分
        event_hours = context.get("hours_to_event", 999)
        if event_hours < 2:
            adjustment -= 10  # 2 小时内扣 10 分
        elif event_hours < 24:
            adjustment -= 5   # 24 小时内扣 5 分
        
        # 2. 流动性不足：根据信号强度调整
        if context.get("low_liquidity", False):
            if signal.strength > 80:
                adjustment -= 5   # 强信号少扣
            else:
                adjustment -= 10  # 弱信号多扣
        
        # 3. 价差过大：降低扣分
        if context.get("high_spread", False):
            adjustment -= 5
        
        # 4. 信号冲突：如果有强共识则不扣
        if context.get("signal_conflict", False):
            consensus_level = context.get("consensus_level", 0)
            if consensus_level > 0.8:
                adjustment -= 0   # 高共识不扣
            else:
                adjustment -= 10  # 低共识扣 10 分
        
        return adjustment
    
    def _score_trend_alignment(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """趋势确认评分 (-20 to +20)"""
        trend_direction = context.get("trend_direction", "NEUTRAL")
        
        if signal.direction.value == "LONG" and trend_direction == "UP":
            return 20
        elif signal.direction.value == "SHORT" and trend_direction == "DOWN":
            return 20
        elif trend_direction == "NEUTRAL":
            return 0
        else:
            return -10
    
    def _score_volume_confirmation(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """成交量确认评分 (-15 to +15)"""
        volume_ratio = context.get("volume_ratio", 1.0)
        
        if volume_ratio > 2.0:
            return 15
        elif volume_ratio > 1.5:
            return 10
        elif volume_ratio > 1.0:
            return 5
        else:
            return -5
    
    def _score_volatility_fit(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """波动率适配评分 (-10 to +10)"""
        current_vol = context.get("volatility", 0)
        avg_vol = context.get("avg_volatility", 0)
        
        if avg_vol == 0:
            return 0
        
        vol_ratio = current_vol / avg_vol
        
        if 0.8 <= vol_ratio <= 1.5:
            return 10
        elif 0.5 <= vol_ratio < 0.8 or 1.5 < vol_ratio <= 2.0:
            return 0
        else:
            return -10
    
    def _score_market_alignment(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """大盘同向评分 (-15 to +15)"""
        spx_change = context.get("spx_change_percent", 0)
        qqq_change = context.get("qqq_change_percent", 0)
        
        market_direction = "UP" if (spx_change + qqq_change) > 0 else "DOWN"
        
        if signal.direction.value == "LONG" and market_direction == "UP":
            return 15
        elif signal.direction.value == "SHORT" and market_direction == "DOWN":
            return 15
        else:
            return -10
    
    def _score_history_performance(
        self,
        signal: SignalScore,
        history_stats: Optional[Dict[str, Any]],
    ) -> int:
        """历史表现评分 (-10 to +10)"""
        if not history_stats:
            return 0
        
        win_rate = history_stats.get("win_rate", 0.5)
        
        if win_rate > 0.6:
            return 10
        elif win_rate > 0.5:
            return 5
        elif win_rate < 0.4:
            return -5
        else:
            return 0
    
    def _score_news_sentiment(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """新闻情绪评分 (-10 to +10)"""
        sentiment = context.get("news_sentiment", 0)
        
        if sentiment > 0.5:
            return 10
        elif sentiment > 0:
            return 5
        elif sentiment < -0.5:
            return -10
        elif sentiment < 0:
            return -5
        else:
            return 0
    
    def _score_regime_fit(self, signal: SignalScore, context: Dict[str, Any]) -> int:
        """市场状态适配评分 (-10 to +10)"""
        regime = context.get("market_regime", "RANGE")
        
        # 趋势市 + 趋势策略 = 适配
        if regime in ["TREND_UP", "TREND_DOWN"]:
            if signal.strategy_id in ["ma_cross", "macd", "supertrend"]:
                return 10
            else:
                return 0
        # 震荡市 + 震荡策略 = 适配
        elif regime == "RANGE":
            if signal.strategy_id in ["rsi", "bollinger"]:
                return 10
            else:
                return 0
        # 高波动 + 风控策略 = 适配
        elif regime == "HIGH_VOLATILITY":
            if signal.strategy_id in ["supertrend"]:
                return 10
            else:
                return -5
        else:
            return 0
    
    def _score_risk_reward(self, signal: SignalScore) -> int:
        """风险收益比评分 (0 to +10)"""
        if signal.risk_reward_estimate >= 3.0:
            return 10
        elif signal.risk_reward_estimate >= 2.0:
            return 7
        elif signal.risk_reward_estimate >= 1.5:
            return 5
        elif signal.risk_reward_estimate >= 1.0:
            return 0
        else:
            return -5
    
    def _calculate_confidence(self, score_adjustments: Dict[str, int]) -> float:
        """计算置信度 (基于评分稳定性)"""
        if not score_adjustments:
            return 0.5
        
        scores = list(score_adjustments.values())
        avg = sum(scores) / len(scores)
        variance = sum((s - avg) ** 2 for s in scores) / len(scores)
        std_dev = variance ** 0.5
        
        # 标准差越小，置信度越高
        confidence = max(0.3, min(0.95, 1.0 - (std_dev / 40)))
        
        return round(confidence, 2)

    # ═══════════════════════════════════════════════════════════════════
    #  Task #4: 权重版本化能力
    # ═══════════════════════════════════════════════════════════════════

    def _get_weight_version(self) -> str:
        """获取当前使用的权重版本"""
        try:
            from app.core.feature_flags import FeatureFlags
            if FeatureFlags().is_enabled("weight_v2_active"):
                return "v2"
        except Exception:
            pass
        return "v1"

    def _get_versioned_weights(self) -> dict:
        """根据版本返回对应权重"""
        version = self._get_weight_version()
        if version == "v2":
            return self._get_v2_weights()
        return self.base_factor_weights

    def _get_v2_weights(self) -> dict:
        """v2 权重 - 基于审计结果修复反向因子"""
        # 从配置读取 v2 权重
        try:
            from app.core.config_container import get_config
            config = get_config()
            v2_weights = config.get("signal_scoring", {}).get("weights_v2", None)
            if v2_weights and isinstance(v2_weights, dict):
                return v2_weights
        except Exception:
            pass
        # fallback 到 v1
        return self.base_factor_weights
