"""
行业关联推理 (L11, 2026-05-14)

显式建模板块/相关标的的传导:
- TSMC (代工) drop → NVDA / AMD 跟跌
- Brent crude spike → XOM / CVX up, 航空 down
- 美联储鹰派 → 高 Beta growth 跌, 防御股涨

输入: 主导标的 (anchor) + 当前 watchlist
输出: 关联评估列表 (related_ticker, expected_direction, confidence, lag_minutes)

可控:
- 默认 disabled
- 仅当某 anchor 单股变动 > 2% 触发, 不主动遍历
- 输出 → 仅作为信号增强 (作为新闻/lifecycle 因子的 input), 不直接生成开仓信号
- 同 anchor 30min cooldown
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import parse_and_validate, clamp_numeric
from app.llm.features.llm_helper import call_llm

logger = logging.getLogger(__name__)

ALLOWED_DIRECTION = ["UP", "DOWN", "NEUTRAL"]


@dataclass
class CorrelationItem:
    related_ticker: str
    expected_direction: str
    confidence: float
    lag_minutes: int
    rationale: str


@dataclass
class CorrelationReasoning:
    anchor_ticker: str
    anchor_change_pct: float
    items: List[CorrelationItem]
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


def reason_about(
    anchor_ticker: str,
    anchor_change_pct: float,
    watchlist: List[str],
    context: Optional[Dict[str, Any]] = None,
) -> Optional[CorrelationReasoning]:
    """
    Args:
        anchor_ticker: 主导标的
        anchor_change_pct: anchor 当日变动 (例 -0.03 = -3%)
        watchlist: 系统当前关注标的列表 (filter 候选)
        context: optional {anchor_news, sector, peers}
    """
    if not is_enabled("sector_correlation"):
        return None

    cfg = get_feature_config("sector_correlation")
    threshold = float(cfg.get("anchor_move_threshold", 0.02))
    cooldown_sec = int(cfg.get("cooldown_sec", 1800))

    if abs(anchor_change_pct) < threshold:
        return None
    if _is_in_cooldown(anchor_ticker, cooldown_sec):
        return None

    # 仅取 watchlist 前 30 个 (控 prompt 大小)
    candidates = watchlist[:30]
    anchor_news = (context or {}).get("anchor_news", "")[:200] if context else ""
    sector = (context or {}).get("sector", "unknown")[:50] if context else "unknown"

    system = (
        "You are FTSeek's sector correlation reasoner. Given a primary ticker move, "
        "identify which watchlist tickers will likely follow and how. Output strict JSON only."
    )
    prompt = f"""Anchor: {anchor_ticker} {anchor_change_pct:+.2%}
Anchor sector: {sector}
Anchor news: {anchor_news}

Watchlist candidates:
{', '.join(candidates)}

Identify correlated reactions:
{{
  "items": [
    {{
      "related_ticker": "TICKER",
      "expected_direction": "UP|DOWN|NEUTRAL",
      "confidence": 0.0-1.0,
      "lag_minutes": 1-120,
      "rationale": "<= 80 chars"
    }}
  ]
}}

Rules:
- At most 8 items
- Only include tickers from the watchlist above
- Direction: UP if expecting positive move, DOWN if negative
- lag_minutes: typical cross-asset reaction time (chip peers ~5min, sector ETF ~10min, defensives ~30min)
- Skip tickers with confidence < 0.5"""

    response = call_llm(
        feature="sector_correlation",
        prompt=prompt,
        system=system,
        model=cfg.get("model", "qwen"),
        temperature=0.0,
        max_tokens=600,
        timeout=int(cfg.get("timeout_sec", 30)),
    )
    if response is None:
        return None

    obj = parse_and_validate(
        response,
        required={"items": list},
        feature="sector_correlation",
    )
    if obj is None:
        return None

    items: List[CorrelationItem] = []
    watchlist_set = {t.upper() for t in watchlist}
    for it in obj["items"][:8]:
        if not isinstance(it, dict):
            continue
        rt = str(it.get("related_ticker", "")).upper()
        if not rt or rt not in watchlist_set:
            continue
        d = it.get("expected_direction", "NEUTRAL")
        if d not in ALLOWED_DIRECTION:
            d = "NEUTRAL"
        c = clamp_numeric(it.get("confidence", 0.5), 0.0, 1.0)
        if c < 0.5:
            continue
        items.append(CorrelationItem(
            related_ticker=rt,
            expected_direction=d,
            confidence=c,
            lag_minutes=int(clamp_numeric(it.get("lag_minutes", 10), 1, 120)),
            rationale=str(it.get("rationale", ""))[:160],
        ))

    if not items:
        return None

    out = CorrelationReasoning(
        anchor_ticker=anchor_ticker,
        anchor_change_pct=anchor_change_pct,
        items=items,
        timestamp=time.time(),
    )
    _mark(anchor_ticker)
    logger.info(
        f"[SectorCorrelation] {anchor_ticker} {anchor_change_pct:+.2%} → "
        f"{len(items)} 关联标的: {[(i.related_ticker, i.expected_direction) for i in items[:3]]}"
    )
    return out
