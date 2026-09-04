"""
分析师修正动量信号 — 纯函数策略 (ADR-007: 2026-06-16)

学术依据:
  - PEAD (Post Earnings Announcement Drift): Ball & Brown 1968
  - 分析师上调 EPS 估计 → 机构跟进买入 → 持续数周漂移
  - IC 预期: 0.15-0.25 (有文献支持)

信号条件:
  1. 近30天 ≥2 次分析师升级 (action="up" or "main")
  2. 当前季度 EPS 增长预期 > 0
  3. 分析师覆盖 ≥15 人 (足够共识有意义)
  4. 综合修正分数 > 0.15 (正向修正)

策略输出:
  - 方向: 永远 LONG (正向修正 = 看涨)
  - strength: 基于修正幅度 + EPS 增长
  - confidence: 基于分析师覆盖 + 共识一致性
"""

from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register


@register("earnings_revision")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    分析师修正动量信号。

    config:
        enabled: bool
        min_upgrades: int           # 最低30天升级数 (默认 2)
        min_revision_score: float   # 最低综合修正分 (默认 0.15)
        min_analysts: int           # 最低分析师覆盖 (默认 15)
        min_eps_growth: float       # 最低EPS增长预期 (默认 0.05 = 5%)
        min_strength: int           # 最低信号强度 (默认 45)
    """
    if not config.get("enabled", True):
        return None

    symbol = ctx.symbol
    min_strength = config.get("min_strength", 45)
    min_revision_score = config.get("min_revision_score", 0.15)

    # 调用分析师数据提供商
    try:
        from app.data.analyst_revision_provider import get_analyst_provider
        provider = get_analyst_provider()
        revision = provider.compute_revision_score(symbol)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"[EarningsRev] {symbol} fetch error: {e}")
        return None

    if revision is None:
        return None

    # ── 门槛检查 ──
    min_upgrades = config.get("min_upgrades", 2)
    min_analysts = config.get("min_analysts", 15)
    min_eps_growth = config.get("min_eps_growth", 0.05)

    if revision.recent_upgrades < min_upgrades:
        return None

    if revision.revision_score < min_revision_score:
        return None

    # 检查当前季度 EPS 增长和分析师覆盖
    current_q = None
    for est in revision.estimates:
        if est.period == "0q":
            current_q = est
            break

    if current_q is None:
        return None

    if current_q.num_analysts < min_analysts:
        return None

    if current_q.growth < min_eps_growth:
        return None

    # ── 评分 ──
    # strength: 45 base + revision_score bonus (0-30) + eps_growth bonus (0-25)
    revision_bonus = min(30, int(revision.revision_score * 50))  # 0.6 → +30
    growth_bonus = min(25, int(current_q.growth * 40))  # 62.5% growth → +25
    strength = min(100, 45 + revision_bonus + growth_bonus)

    if strength < min_strength:
        return None

    # confidence: 基于分析师数量 + 净修正 + 共识一致性
    analyst_factor = min(0.20, current_q.num_analysts / 200)  # 100 analysts → 0.20
    # DEB-20260904 #S5: 净修正/共识漂移是有符号量, abs() 会把强负修正也算成
    # LONG 信号的高置信 — 负向贡献应归零而非取绝对值
    net_factor = min(0.15, max(0, revision.net_revisions) * 0.03)  # 5 net → 0.15
    consensus_factor = min(0.15, max(0, revision.consensus_shift) * 3)  # 0.05 shift → 0.15
    confidence = min(0.85, 0.45 + analyst_factor + net_factor + consensus_factor)

    # Rationale
    ticker = symbol.replace("US.", "")
    rationale = (
        f"{ticker}: {revision.recent_upgrades}up/{revision.recent_downgrades}dn in 30d | "
        f"EPS g={current_q.growth*100:.1f}% | "
        f"{current_q.num_analysts} analysts | "
        f"score={revision.revision_score:.2f}"
    )

    return SignalCandidate(
        symbol=symbol,
        direction="LONG",
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="EARNINGS_REVISION_MOMENTUM",
        source="earnings_revision",
        rationale=rationale,
        context=ctx,
        metadata={
            "revision_score": revision.revision_score,
            "recent_upgrades": revision.recent_upgrades,
            "recent_downgrades": revision.recent_downgrades,
            "net_revisions": revision.net_revisions,
            "consensus_shift": revision.consensus_shift,
            "eps_growth": current_q.growth,
            "num_analysts": current_q.num_analysts,
            "eps_estimate": current_q.eps_avg,
        },
    )
