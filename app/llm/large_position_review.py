"""
大仓位 LLM 二次确认 (L7, 2026-05-14)

position_pct > threshold (默认 0.05) 时, 同步 (60s 超时) 调 LLM:
- 检查是否有材料新闻未反映
- 检查关联标的反向移动
- 检查未来 48h 财报/事件

输出:
- GREEN_LIGHT: 放行
- YELLOW_REDUCE_TO_3PCT: 降仓位 (但不取消)
- RED_SKIP: 拒下单

可控:
- 默认 disabled
- 仅大头寸 (~5-10% 信号占比)
- LLM 只能"降仓"或"拒", 不能"加仓"
- 同 symbol 1h cooldown
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import parse_and_validate, clamp_numeric
from app.llm.features.llm_helper import call_llm

logger = logging.getLogger(__name__)

ALLOWED_DECISION = ["GREEN_LIGHT", "YELLOW_REDUCE_TO_3PCT", "YELLOW_REDUCE_TO_5PCT", "RED_SKIP"]


@dataclass
class LargePositionReview:
    decision: str
    rationale: str
    risk_factors: list
    suggested_position_pct: float
    timestamp: float


_cooldown: Dict[str, float] = {}
_lock = Lock()


def _is_in_cooldown(symbol: str, sec: int) -> bool:
    with _lock:
        last = _cooldown.get(symbol, 0)
    return time.time() - last < sec


def _mark(symbol: str):
    with _lock:
        _cooldown[symbol] = time.time()


def review_large_position(
    symbol: str,
    signal: Dict[str, Any],
    context: Dict[str, Any],
) -> Optional[LargePositionReview]:
    """
    Args:
        signal: {"direction", "position_pct", "confidence", ...}
        context: {recent_news, related_tickers_movement, upcoming_events, ...}

    Returns: LargePositionReview or None (None 表示走默认放行)
    """
    if not is_enabled("large_position_review"):
        return None

    cfg = get_feature_config("large_position_review")
    threshold = float(cfg.get("position_pct_threshold", 0.05))
    pos_pct = float(signal.get("position_pct", 0))

    if pos_pct < threshold:
        return None

    cooldown_sec = int(cfg.get("cooldown_sec", 3600))
    if _is_in_cooldown(symbol, cooldown_sec):
        return None

    direction = signal.get("direction", "LONG")
    confidence = signal.get("confidence", 0.5)

    recent_news = (context.get("recent_news") or "无")[:300]
    related = (context.get("related_tickers_movement") or "")[:200]
    upcoming = (context.get("upcoming_events_48h") or "")[:200]

    system = (
        "You are FTSeek's pre-trade risk reviewer for large positions. "
        "Detect anomalies before placing big orders. Output strict JSON only."
    )
    prompt = f"""Large Position Pre-Trade Check:
Symbol: {symbol} {direction}
Proposed size: {pos_pct*100:.1f}% of equity
Signal confidence: {confidence:.2f}

Recent news (last 4h): {recent_news}
Related tickers movement: {related}
Upcoming events (48h): {upcoming}

Decide:
{{
  "decision": "GREEN_LIGHT|YELLOW_REDUCE_TO_5PCT|YELLOW_REDUCE_TO_3PCT|RED_SKIP",
  "rationale": "<= 150 chars",
  "risk_factors": ["..."],
  "suggested_position_pct": 0.0-{threshold:.2f}
}}

Rules:
- GREEN_LIGHT: no anomaly detected
- YELLOW_REDUCE_TO_5PCT/3PCT: caution but tradable at smaller size
- RED_SKIP: material adverse event/risk detected, do not enter
- You can only REDUCE size, never increase"""

    response = call_llm(
        feature="large_position_review",
        prompt=prompt,
        system=system,
        model=cfg.get("model", "qwen"),
        temperature=0.0,
        max_tokens=300,
        timeout=int(cfg.get("timeout_sec", 60)),
    )
    if response is None:
        return None

    obj = parse_and_validate(
        response,
        required={
            "decision": str,
            "rationale": str,
            "risk_factors": list,
            "suggested_position_pct": float,
        },
        enums={"decision": ALLOWED_DECISION},
        feature="large_position_review",
    )
    if obj is None:
        return None

    suggested_pct = clamp_numeric(obj["suggested_position_pct"], 0.0, threshold)
    # LLM 只能降仓: 即使建议 > 当前 pos_pct, 也强制 ≤ pos_pct
    suggested_pct = min(suggested_pct, pos_pct)

    review = LargePositionReview(
        decision=obj["decision"],
        rationale=obj["rationale"][:200],
        risk_factors=[str(r)[:80] for r in obj["risk_factors"][:5]],
        suggested_position_pct=suggested_pct,
        timestamp=time.time(),
    )
    _mark(symbol)
    logger.info(
        f"[LargePositionReview] {symbol} {pos_pct*100:.1f}% → {review.decision} "
        f"(suggest={review.suggested_position_pct*100:.1f}%) | {review.rationale[:80]}"
    )
    return review
