"""
LLM 投票策略 — 多模型共识信号生成

输入: SymbolContext + 投票结果 (通过 metadata 传入, 由 VotingCoordinator 产生)
输出: SignalCandidate 或 None

纯数据转换 — 不调用 LLM API (VotingCoordinator 负责 LLM 调用),
本函数只做投票结果 → SignalCandidate 的映射。
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register


# 市场状态名称规范化映射
_REGIME_NORMALIZE = {
    "正常": "RANGE", "neutral": "RANGE", "NORMAL": "RANGE",
    "上涨": "TREND_UP", "bullish": "TREND_UP",
    "下跌": "TREND_DOWN", "bearish": "TREND_DOWN",
    "高波动": "HIGH_VOL", "high_volatility": "HIGH_VOL",
    "低波动": "LOW_VOL", "low_volatility": "LOW_VOL",
}


@register("llm_vote")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    从 LLM 投票结果构建 SignalCandidate。

    config:
        min_consensus: float = 0.30       # 最低共识度
        min_confidence: float = 0.70      # 2026-06-13 fail-safe 统一

    ctx.metadata 中传入:
        ctx.metadata["vote_result"] = {
            "direction": "LONG",
            "final_strength": 63,
            "final_confidence": 0.55,
            "consensus_level": 0.40,
            "std_dev": 15,
            "model_scores": [...],
            "decision_id": "DEC-...",
        }
    """
    vote = ctx.metadata.get("vote_result")
    if not vote or not isinstance(vote, dict):
        return None

    direction_str = str(vote.get("direction", "NEUTRAL")).upper()
    if direction_str not in ("LONG", "SHORT"):
        return None

    consensus = float(vote.get("consensus_level", 0))
    min_consensus = config.get("min_consensus", 0.30)
    if consensus < min_consensus:
        return None

    confidence = float(vote.get("final_confidence", 0.5))
    min_conf = config.get("min_confidence", 0.70)
    if confidence < min_conf:
        return None

    strength = int(vote.get("final_strength", 50))
    std_dev = float(vote.get("std_dev", 0))

    # 高分歧时降低置信度
    if std_dev > 20:
        confidence *= 0.85

    rationale = (
        f"LLM 投票 | 共识={consensus:.0%} | "
        f"评分={strength} | 标准差={std_dev:.0f}"
    )

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction_str,
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="LLM_VOTE",
        source="llm_vote",
        rationale=rationale,
        context=ctx,
        metadata={
            "consensus_level": consensus,
            "std_dev": std_dev,
            "model_scores": vote.get("model_scores", []),
            "decision_id": vote.get("decision_id"),
            "news_sentiment": ctx.news_sentiment,
            "vix": ctx.vix,
        },
    )
