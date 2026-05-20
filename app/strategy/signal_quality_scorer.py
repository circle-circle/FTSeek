"""
统一信号质量评分系统
5维度加权评分 → 质量等级(A/B/C) → 仓位系数

维度权重（默认）:
  consensus          30% — 共识度（分数/阈值比值 + 策略一致性）
  mtf_alignment      20% — 多周期对齐度
  news_sentiment     15% — 新闻/情绪评分
  market_regime      15% — 市场状态匹配度（策略类型与当前状态契合度）
  historical_accuracy 20% — 历史准确率（策略自适应权重系统中的加权胜率）

质量等级:
  A (composite >= 0.75) → position_factor 1.3  优质信号，仓位 +30%
  B (composite >= 0.55) → position_factor 1.0  标准信号
  C (composite  < 0.55) → position_factor 0.7  弱信号，仓位 -30%

所有评分方法均有 defensive 默认值：字段缺失时返回 0.5（中性分）。
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认权重
DEFAULT_WEIGHTS: Dict[str, float] = {
    'consensus':           0.30,
    'mtf_alignment':       0.20,
    'news_sentiment':      0.15,
    'market_regime':       0.15,
    'historical_accuracy': 0.20,
}

# 等级 → 最低分 & 仓位系数
TIER_CONFIG = {
    'A': {'min_score': 0.75, 'position_factor': 1.3},
    'B': {'min_score': 0.55, 'position_factor': 1.0},
    'C': {'min_score': 0.0,  'position_factor': 0.7},
}

# 适合趋势行情的策略集合（与 market_regime 维度匹配逻辑相关）
# 实际策略名来自 factor_votes.py + 独立策略类
TREND_STRATEGIES = {'supertrend', 'st_factor', 'macd', 'macd_factor', 'ma_cross_factor', 'confluence_v2'}
RANGE_STRATEGIES = {'rsi', 'rsi_factor', 'bollinger', 'bb_factor'}
SENTIMENT_STRATEGIES = {'sentiment', 'sentiment_ai', 'news_sentiment', 'event_driven'}


@dataclass
class SignalQualityResult:
    """质量评分结果（只读视图）"""
    scores: Dict[str, float]        # 各维度得分 (0-1)
    composite: float                # 综合分 (0-1)
    tier: str                       # 等级 A / B / C
    position_factor: float          # 仓位系数
    details: Dict[str, str] = field(default_factory=dict)  # 调试说明

    def to_dict(self) -> dict:
        return {
            'scores': dict(self.scores),
            'composite': self.composite,
            'tier': self.tier,
            'position_factor': self.position_factor,
            'details': dict(self.details),
        }


class SignalQualityScorer:
    """
    统一信号质量评分系统

    用法::

        scorer = SignalQualityScorer()          # 使用默认权重
        scorer = SignalQualityScorer(weights={  # 使用自定义权重
            'consensus': 0.40, ...
        })

        # signal_context 由 ConsensusEngine._evaluate 构建，包含：
        #   consensus_result  — ConsensusResult 对象
        #   mtf_cache         — Dict {"buy": float, "sell": float}（可选）
        #   news_factor       — Dict {"sentiment": float, "confidence": float}（可选）
        #   quality_score     — float 历史质量分（可选，来自 _quality_score 缓存）
        #   aw_win_rates      — Dict {strategy: shrunk_rate}（可选，来自 AdaptiveWeightManager）

        result = scorer.compute(signal_context)
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        raw = weights or DEFAULT_WEIGHTS.copy()
        # 归一化权重，防止权重之和不等于 1.0 时评分偏差
        total = sum(raw.values())
        if total <= 0:
            total = 1.0
        self.weights: Dict[str, float] = {k: v / total for k, v in raw.items()}

    # ------------------------------------------------------------------
    #  公开 API
    # ------------------------------------------------------------------

    def compute(self, signal_context: dict) -> SignalQualityResult:
        """
        根据 signal_context 计算 5 维度综合质量分。

        Args:
            signal_context: 上下文字典，字段详见类注释。

        Returns:
            SignalQualityResult
        """
        try:
            scores: Dict[str, float] = {}
            details: Dict[str, str] = {}

            scores['consensus'],           details['consensus']           = self._score_consensus(signal_context)
            scores['mtf_alignment'],       details['mtf_alignment']       = self._score_mtf(signal_context)
            scores['news_sentiment'],      details['news_sentiment']      = self._score_sentiment(signal_context)
            scores['market_regime'],       details['market_regime']       = self._score_regime(signal_context)
            scores['historical_accuracy'], details['historical_accuracy'] = self._score_accuracy(signal_context)

            # 综合加权分
            composite = sum(scores[dim] * self.weights.get(dim, 0.0) for dim in scores)
            composite = round(max(0.0, min(1.0, composite)), 4)

            tier, factor = self._tier_by_score(composite)

            result = SignalQualityResult(
                scores=scores,
                composite=composite,
                tier=tier,
                position_factor=factor,
                details=details,
            )

            logger.debug(
                f"[SignalQualityScorer] 综合分={composite:.3f} 等级={tier} 仓位系数={factor} | "
                f"各维度: {', '.join(f'{k}={v:.2f}' for k, v in scores.items())}"
            )
            return result

        except Exception as exc:
            logger.warning(f"[SignalQualityScorer] 评分异常，返回默认中性结果: {exc}")
            return self._neutral_result()

    # ------------------------------------------------------------------
    #  维度评分方法（每个均有 defensive 默认值 0.5）
    # ------------------------------------------------------------------

    def _score_consensus(self, ctx: dict) -> Tuple[float, str]:
        """
        共识度评分（权重 30%）

        指标:
          - score_ratio: abs(consensus_score) / threshold，越大越强
          - agree_ratio: 同向票数 / 全部票数，越高越一致

        分数 = score_ratio * 0.6 + agree_ratio * 0.4
        """
        try:
            cr = ctx.get('consensus_result')
            if cr is None:
                return 0.5, 'no consensus_result'

            threshold = float(getattr(cr, 'threshold', 1.5) or 1.5)
            score_val = float(getattr(cr, 'score', 0.0) or 0.0)
            score_ratio = min(1.0, abs(score_val) / threshold) if threshold > 0 else 0.5

            agree = tuple(getattr(cr, 'agree_strategies', ()) or ())
            oppose = tuple(getattr(cr, 'oppose_strategies', ()) or ())
            total_votes = len(agree) + len(oppose)
            agree_ratio = len(agree) / max(1, total_votes)

            # 分歧度惩罚：divergence 越高越差
            divergence = float(getattr(cr, 'divergence', 0.0) or 0.0)
            divergence_penalty = divergence * 0.2

            raw = score_ratio * 0.6 + agree_ratio * 0.4 - divergence_penalty
            val = round(max(0.0, min(1.0, raw)), 4)
            detail = f"score_ratio={score_ratio:.2f} agree_ratio={agree_ratio:.2f} div_penalty={divergence_penalty:.2f}"
            return val, detail
        except Exception as e:
            logger.debug(f"[SQ/consensus] 评分失败: {e}")
            return 0.5, 'error→default'

    def _score_mtf(self, ctx: dict) -> Tuple[float, str]:
        """
        多周期对齐度评分（权重 20%）

        来源: ctx['mtf_cache'] = {"buy": float, "sell": float}
        或 consensus_result 的 action 字段判断方向。
        """
        try:
            mtf = ctx.get('mtf_cache')
            if not mtf:
                return 0.5, 'no mtf_cache→default'

            cr = ctx.get('consensus_result')
            action_val = ''
            if cr is not None:
                action = getattr(cr, 'action', None)
                action_val = action.value if hasattr(action, 'value') else str(action)

            if action_val == 'BUY':
                score = float(mtf.get('buy', 0.5))
            elif action_val == 'SELL':
                score = float(mtf.get('sell', 0.5))
            else:
                # 取两个方向平均值（信号方向未知时）
                score = (float(mtf.get('buy', 0.5)) + float(mtf.get('sell', 0.5))) / 2.0

            val = round(max(0.0, min(1.0, score)), 4)
            return val, f"mtf_{action_val.lower() or 'avg'}={val:.2f}"
        except Exception as e:
            logger.debug(f"[SQ/mtf] 评分失败: {e}")
            return 0.5, 'error→default'

    def _score_sentiment(self, ctx: dict) -> Tuple[float, str]:
        """
        情绪评分（权重 15%）

        来源优先级:
          1. ctx['news_factor']['sentiment'] × news_factor['confidence']
          2. consensus_result.confidence（作为宽泛代理）
          3. 默认 0.5

        sentiment 取值: -1~1，归一化为 0~1
        """
        try:
            nf = ctx.get('news_factor')
            if nf and isinstance(nf, dict):
                sentiment = float(nf.get('sentiment', 0.0))
                confidence = float(nf.get('confidence', 0.5))
                # 将 -1~1 → 0~1（中性=0→0.5，利好=1→1.0，利空=-1→0.0）
                normalized = (sentiment + 1.0) / 2.0
                # 置信度加权：低置信度时向 0.5 回归
                val = confidence * normalized + (1.0 - confidence) * 0.5
                val = round(max(0.0, min(1.0, val)), 4)
                return val, f"nf_sentiment={sentiment:.2f} conf={confidence:.2f}→{val:.2f}"

            # fallback: consensus_result 的 confidence 作为代理
            cr = ctx.get('consensus_result')
            if cr is not None:
                conf = float(getattr(cr, 'confidence', 0.5) or 0.5)
                val = round(max(0.0, min(1.0, conf)), 4)
                return val, f"cr.confidence={val:.2f}(proxy)"

            return 0.5, 'no sentiment data→default'
        except Exception as e:
            logger.debug(f"[SQ/sentiment] 评分失败: {e}")
            return 0.5, 'error→default'

    def _score_regime(self, ctx: dict) -> Tuple[float, str]:
        """
        市场状态匹配度（权重 15%）

        逻辑:
          - 提取当前 market_regime（trending/ranging/neutral）
          - 提取参与共识的策略集合
          - 判断策略类型与市场状态是否匹配:
              trending + 趋势策略 → 高分
              ranging  + 震荡策略 → 高分
              neutral            → 中性分 0.5
              不匹配              → 低分 0.3
        """
        try:
            cr = ctx.get('consensus_result')
            if cr is None:
                return 0.5, 'no consensus_result→default'

            regime = str(getattr(cr, 'market_regime', 'neutral') or 'neutral').lower()
            agree_strategies = set(getattr(cr, 'agree_strategies', ()) or ())

            if regime == 'neutral':
                return 0.5, 'regime=neutral→default'

            if regime == 'trending':
                trend_count = len(agree_strategies & TREND_STRATEGIES)
                total = max(1, len(agree_strategies))
                match_ratio = trend_count / total
                val = round(0.3 + 0.7 * match_ratio, 4)
                return val, f"trending match={trend_count}/{total}→{val:.2f}"

            if regime == 'ranging':
                range_count = len(agree_strategies & RANGE_STRATEGIES)
                total = max(1, len(agree_strategies))
                match_ratio = range_count / total
                val = round(0.3 + 0.7 * match_ratio, 4)
                return val, f"ranging match={range_count}/{total}→{val:.2f}"

            return 0.5, f'regime={regime}→default'
        except Exception as e:
            logger.debug(f"[SQ/regime] 评分失败: {e}")
            return 0.5, 'error→default'

    def _score_accuracy(self, ctx: dict) -> Tuple[float, str]:
        """
        历史准确率（权重 20%）

        来源优先级:
          1. ctx['aw_win_rates'] — AdaptiveWeightManager 提供的 {strategy: shrunk_rate}
             取参与共识策略的平均 shrunk_rate
          2. ctx['quality_score'] — consensus_engine 维护的旧版质量分 (0.3~1.5)，
             归一化到 0~1: (quality_score - 0.3) / (1.5 - 0.3)
          3. 默认 0.5
        """
        try:
            cr = ctx.get('consensus_result')
            agree_strategies = []
            if cr is not None:
                agree_strategies = list(getattr(cr, 'agree_strategies', ()) or ())

            aw_win_rates = ctx.get('aw_win_rates')
            if aw_win_rates and isinstance(aw_win_rates, dict) and agree_strategies:
                rates = [float(aw_win_rates[s]) for s in agree_strategies if s in aw_win_rates]
                if rates:
                    avg_rate = sum(rates) / len(rates)
                    val = round(max(0.0, min(1.0, avg_rate)), 4)
                    return val, f"aw_avg_wr={avg_rate:.2f}(n={len(rates)})"

            # fallback: 旧质量分归一化
            quality_score = ctx.get('quality_score')
            if quality_score is not None:
                qs = float(quality_score)
                # 0.3~1.5 → 0.0~1.0
                val = round(max(0.0, min(1.0, (qs - 0.3) / 1.2)), 4)
                return val, f"quality_score={qs:.2f}→{val:.2f}"

            return 0.5, 'no accuracy data→default'
        except Exception as e:
            logger.debug(f"[SQ/accuracy] 评分失败: {e}")
            return 0.5, 'error→default'

    # ------------------------------------------------------------------
    #  辅助方法
    # ------------------------------------------------------------------

    def _tier_by_score(self, score: float) -> Tuple[str, float]:
        """根据综合分确定质量等级和仓位系数"""
        if score >= TIER_CONFIG['A']['min_score']:
            return 'A', TIER_CONFIG['A']['position_factor']
        elif score >= TIER_CONFIG['B']['min_score']:
            return 'B', TIER_CONFIG['B']['position_factor']
        else:
            return 'C', TIER_CONFIG['C']['position_factor']

    def _neutral_result(self) -> SignalQualityResult:
        """评分异常时返回中性默认结果"""
        scores = {k: 0.5 for k in DEFAULT_WEIGHTS}
        composite = round(sum(scores[k] * self.weights.get(k, 0.0) for k in scores), 4)
        tier, factor = self._tier_by_score(composite)
        return SignalQualityResult(
            scores=scores,
            composite=composite,
            tier=tier,
            position_factor=factor,
            details={'error': 'fallback neutral result'},
        )


# ------------------------------------------------------------------
#  模块级单例（可被 ConsensusEngine 惰性初始化后复用）
# ------------------------------------------------------------------
_default_scorer: Optional[SignalQualityScorer] = None


def get_default_scorer(weights: Optional[Dict[str, float]] = None) -> SignalQualityScorer:
    """
    获取模块级默认评分器（首次调用时初始化，之后复用同一实例）。

    如需使用自定义权重，请直接实例化 SignalQualityScorer(weights=...)。
    """
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = SignalQualityScorer(weights=weights)
    return _default_scorer
