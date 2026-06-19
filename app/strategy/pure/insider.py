"""
内部人集群买入信号 — 纯函数策略 (ADR-007: 2026-06-16)

学术依据:
  - 内部人集群买入 (≥2位不同内部人30天内净买入) IC=0.20-0.35
  - 内部人了解自己的公司价值, 集群买入是最强信号之一
  - 低频信号 (每月1-3个), 但每个都是高置信度

信号条件:
  1. SEC Form 4 中 30天内 ≥2 位不同内部人净买入
  2. 每位买入金额 > $50K (排除象征性买入)
  3. transactionCode = "P" (公开市场购买, 排除期权行权/赠与)

策略输出:
  - 方向: 永远 LONG (内部人买入 = 看涨)
  - strength: 基于集群规模 + 买入金额
  - confidence: 基于内部人角色 (C-suite > 普通员工)
"""

from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register


@register("insider")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    内部人集群买入信号。

    config:
        enabled: bool
        min_buy_amount: int         # 最低单人买入金额 (默认 $50K)
        min_buyers: int             # 最低不同内部人数 (默认 2)
        min_strength: int           # 最低信号强度 (默认 50)
        window_days: int            # 回溯窗口 (默认 30)
    """
    if not config.get("enabled", True):
        return None

    symbol = ctx.symbol
    min_strength = config.get("min_strength", 50)

    # 调用 SEC Form 4 数据提供商检测集群买入
    try:
        from app.data.sec_form4_provider import get_sec_form4_provider
        provider = get_sec_form4_provider()
        cluster = provider.detect_cluster_buys(
            symbol,
            window_days=config.get("window_days", 30),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"[Insider] {symbol} fetch error: {e}")
        return None

    if cluster is None:
        return None

    # ── 检查最低门槛 ──
    min_buyers = config.get("min_buyers", 2)
    min_buy_amount = config.get("min_buy_amount", 50_000)

    if cluster.unique_buyers < min_buyers:
        return None
    if cluster.total_amount < min_buy_amount * min_buyers:
        return None

    # ── 评分 ──
    # strength: 50 base + buyer_count bonus (0-25) + amount bonus (0-25)
    buyer_bonus = min(25, (cluster.unique_buyers - 1) * 8)  # 2 buyers=+8, 3=+16, 4+=+24
    amount_bonus = min(25, int(cluster.total_amount / 100_000 * 5))  # $100K→+5, $500K→+25
    strength = min(100, 50 + buyer_bonus + amount_bonus)

    if strength < min_strength:
        return None

    # confidence: 基于 C-suite 占比 + 集群紧密度
    c_suite_count = sum(
        1 for b in cluster.buyers
        if any(title in (b.get("title", "") or "").upper()
               for title in ["CEO", "CFO", "COO", "CTO", "PRESIDENT", "CHIEF"])
    )
    c_suite_ratio = c_suite_count / cluster.unique_buyers if cluster.unique_buyers > 0 else 0
    tightness = 1.0 - (cluster.days_span / 30.0)  # 更紧密 = 更高
    confidence = min(0.90, 0.50 + c_suite_ratio * 0.20 + tightness * 0.15 + cluster.score * 0.10)

    return SignalCandidate(
        symbol=symbol,
        direction="LONG",
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="INSIDER_CLUSTER_BUY",
        source="insider",
        rationale=cluster.rationale,
        context=ctx,
        metadata={
            "insider_buyers": cluster.unique_buyers,
            "insider_total_amount": cluster.total_amount,
            "insider_score": cluster.score,
            "insider_details": cluster.buyers,
        },
    )
