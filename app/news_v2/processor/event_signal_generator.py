"""
新闻事件 → 信号生成器 (L9, 2026-05-14)

依赖 L1 NewsClassification. 严重事件 → 生成 owner_strategy="llm_news" 的 SignalScore.

筛选规则 (硬过滤, 非 LLM):
- credibility >= 0.6 (拒谣言)
- |sentiment| >= 0.5 (拒中性)
- action_hint in {BUY, SELL} (拒 WAIT/OBSERVE/HEDGE)
- 同 ticker 5min cooldown
- 仅 primary_tickers + impacted_tickers (confidence >= 0.7)

可控:
- 默认 disabled (llm_features.news_signal_gen.enabled=false)
- 信号必经 SignalArbitrator + RiskGateway
- position_pct hard cap by config
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config

logger = logging.getLogger(__name__)


@dataclass
class _CooldownState:
    last_signal_ts: Dict[str, float]  # symbol -> ts


_state = _CooldownState(last_signal_ts={})
_lock = Lock()


def _is_in_cooldown(symbol: str, cooldown_sec: int) -> bool:
    with _lock:
        last = _state.last_signal_ts.get(symbol, 0)
    return time.time() - last < cooldown_sec


def _mark_signal(symbol: str):
    with _lock:
        _state.last_signal_ts[symbol] = time.time()


def generate_signals_from_classification(classification) -> List[Dict[str, Any]]:
    """
    从 L1 分类结果生成信号 list.

    Returns: list of signal dicts (不直接构造 SignalScore — 让上游决定)
        [{
            "symbol": "NVDA",
            "direction": "LONG"|"SHORT",
            "strength": 50-90,
            "confidence": 0.0-1.0,
            "owner_strategy": "llm_news",
            "source_event": "earnings",
            "rationale": "...",
            "horizon_min": int,
            "news_id": "...",
        }]
    """
    if not is_enabled("news_signal_gen"):
        return []
    if classification is None:
        return []

    cfg = get_feature_config("news_signal_gen")
    min_credibility = float(cfg.get("min_credibility", 0.6))
    min_abs_sentiment = float(cfg.get("min_abs_sentiment", 0.5))
    cooldown_sec = int(cfg.get("cooldown_sec", 300))
    max_position_pct = float(cfg.get("max_position_pct", 0.05))
    impacted_min_conf = float(cfg.get("impacted_min_confidence", 0.7))

    # 硬过滤
    if classification.credibility < min_credibility:
        return []
    if abs(classification.sentiment) < min_abs_sentiment:
        return []
    if classification.action_hint not in ("BUY", "SELL"):
        return []

    direction_from_hint = "LONG" if classification.action_hint == "BUY" else "SHORT"

    # 候选标的
    candidates: List[Dict[str, Any]] = []
    for ticker in classification.primary_tickers:
        if not ticker:
            continue
        candidates.append({
            "symbol": ticker,
            "direction": direction_from_hint,
            "ticker_confidence": 1.0,
            "is_primary": True,
        })
    for it in classification.impacted_tickers:
        if it.get("confidence", 0) < impacted_min_conf:
            continue
        d = it.get("direction", "NEUTRAL")
        if d == "NEUTRAL":
            continue
        candidates.append({
            "symbol": it["ticker"],
            "direction": "LONG" if d == "UP" else "SHORT",
            "ticker_confidence": it["confidence"],
            "is_primary": False,
        })

    # 生成 signal dicts
    signals = []
    for c in candidates:
        symbol = c["symbol"]
        if not symbol:
            continue
        if _is_in_cooldown(symbol, cooldown_sec):
            logger.debug(f"[EventSignalGen] {symbol} cooldown, skip")
            continue
        # strength: 综合 credibility + |sentiment| + ticker_confidence
        strength = int(50 + 30 * classification.credibility * abs(classification.sentiment) * c["ticker_confidence"])
        strength = max(50, min(90, strength))
        # confidence: credibility * abs(sentiment) * ticker_conf
        confidence = round(
            classification.credibility * abs(classification.sentiment) * c["ticker_confidence"],
            3,
        )
        confidence = max(0.0, min(1.0, confidence))

        signal = {
            "symbol": symbol,
            "direction": c["direction"],
            "strength": strength,
            "confidence": confidence,
            "owner_strategy": "llm_news",
            "source_event": classification.event_type,
            "rationale": classification.rationale,
            "horizon_min": classification.time_horizon_minutes,
            "news_id": classification.news_id,
            "is_primary_ticker": c["is_primary"],
            "max_position_pct": max_position_pct,
        }
        signals.append(signal)
        _mark_signal(symbol)
        logger.info(
            f"[EventSignalGen] {symbol} {c['direction']} strength={strength} "
            f"confidence={confidence:.2f} event={classification.event_type} "
            f"horizon={classification.time_horizon_minutes}min"
        )

    return signals
