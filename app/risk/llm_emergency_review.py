"""
LLM 紧急风控审查 (L2, 2026-05-14)

触发条件 (任一):
- 持仓单股 5min 内 -3%
- VIX 5min 内 +20%
- SystemCircuitBreaker 进入 DEGRADED
- 该 symbol 当日新闻 LLM 标 sentiment < -0.6

动作: 同步调 LLM (Qwen-Max, 30s 超时)
输出: HOLD | REDUCE_50 | FLAT_NOW

可控:
- 默认 disabled
- 同 symbol 1h cooldown
- LLM 决策 → ExitDispatcher 路径执行 (不直接 broker)
- 审计 emergency_reviews 表
"""
from __future__ import annotations
import logging
import sqlite3
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import parse_and_validate
from app.llm.features.llm_helper import call_llm

logger = logging.getLogger(__name__)

ALLOWED_DECISION = ["HOLD", "REDUCE_50", "FLAT_NOW"]


@dataclass
class EmergencyDecision:
    symbol: str
    decision: str        # HOLD | REDUCE_50 | FLAT_NOW
    reason: str
    confidence: float    # [0, 1]
    trigger: str         # price_drop / vix_spike / news_negative / system_degraded
    timestamp: float


_cooldown: Dict[str, float] = {}
_lock = Lock()


def _is_in_cooldown(symbol: str, cooldown_sec: int) -> bool:
    with _lock:
        last = _cooldown.get(symbol, 0)
    return time.time() - last < cooldown_sec


def _mark(symbol: str):
    with _lock:
        _cooldown[symbol] = time.time()


def _audit_db_path() -> str:
    from app.core.paths import get_data_dir
    return str(get_data_dir() / "emergency_reviews.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emergency_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            trigger TEXT,
            decision TEXT,
            reason TEXT,
            confidence REAL,
            position_pct REAL,
            unrealized_pnl_pct REAL,
            executed INTEGER DEFAULT 0,
            timestamp REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_em_symbol ON emergency_reviews(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_em_time ON emergency_reviews(timestamp)")


def _persist(d: EmergencyDecision, position_pct: float = 0.0, unrealized_pnl_pct: float = 0.0, executed: bool = False):
    try:
        conn = sqlite3.connect(_audit_db_path())
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO emergency_reviews "
            "(symbol, trigger, decision, reason, confidence, position_pct, "
            " unrealized_pnl_pct, executed, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d.symbol, d.trigger, d.decision, d.reason[:300], d.confidence,
             position_pct, unrealized_pnl_pct, int(executed), d.timestamp),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[EmergencyReview·persist] 失败: {e}")


def review(
    symbol: str,
    trigger: str,
    context: Dict[str, Any],
) -> Optional[EmergencyDecision]:
    """
    Args:
        symbol: 持仓 ticker
        trigger: price_drop / vix_spike / news_negative / system_degraded
        context: {direction, entry_price, current_price, position_pct,
                  unrealized_pnl_pct, recent_news, vix, ...}

    Returns: EmergencyDecision or None
    """
    if not is_enabled("emergency_review"):
        return None

    cfg = get_feature_config("emergency_review")
    cooldown_sec = int(cfg.get("cooldown_sec", 3600))
    if _is_in_cooldown(symbol, cooldown_sec):
        logger.debug(f"[EmergencyReview] {symbol} cooldown, skip")
        return None

    direction = context.get("direction", "LONG")
    entry = context.get("entry_price", 0)
    cur = context.get("current_price", 0)
    pos_pct = context.get("position_pct", 0)
    pnl_pct = context.get("unrealized_pnl_pct", 0)
    recent_news = context.get("recent_news", "无")[:300]
    vix = context.get("vix", 0)

    system = (
        "You are FTSeek's emergency risk reviewer. Given a position under stress, "
        "decide HOLD/REDUCE_50/FLAT_NOW. Output strict JSON, no extra text."
    )
    prompt = f"""Position under stress:
Symbol: {symbol}
Direction: {direction}
Entry: ${entry:.2f}, Current: ${cur:.2f}, PnL: {pnl_pct*100:+.2f}%
Position size: {pos_pct*100:.1f}% of equity
Trigger: {trigger}
Recent news (5min): {recent_news}
VIX: {vix:.1f}

Decide:
{{
  "decision": "HOLD|REDUCE_50|FLAT_NOW",
  "reason": "<= 100 chars",
  "confidence": 0.0-1.0
}}

Rules:
- HOLD if drop is normal noise + no news
- REDUCE_50 if drop is concerning but recoverable
- FLAT_NOW if material adverse event detected"""

    response = call_llm(
        feature="emergency_review",
        prompt=prompt,
        system=system,
        model=cfg.get("model", "qwen"),
        temperature=0.0,
        max_tokens=300,
        timeout=int(cfg.get("timeout_sec", 30)),
    )
    if response is None:
        return None

    obj = parse_and_validate(
        response,
        required={"decision": str, "reason": str, "confidence": float},
        enums={"decision": ALLOWED_DECISION},
        feature="emergency_review",
    )
    if obj is None:
        return None

    decision = EmergencyDecision(
        symbol=symbol,
        decision=obj["decision"],
        reason=obj["reason"][:200],
        confidence=max(0.0, min(1.0, float(obj["confidence"]))),
        trigger=trigger,
        timestamp=time.time(),
    )
    _mark(symbol)
    _persist(decision, position_pct=pos_pct, unrealized_pnl_pct=pnl_pct)
    logger.warning(
        f"[EmergencyReview] {symbol} trigger={trigger} → {decision.decision} "
        f"({decision.reason}, conf={decision.confidence:.2f})"
    )
    return decision


def should_trigger(symbol: str, context: Dict[str, Any]) -> Optional[str]:
    """
    检查触发条件, 返回触发理由 (None=不触发).

    用法: 在 PositionExitMonitor.scan_positions 内, 每次扫到一个仓位时调.
    """
    cfg = get_feature_config("emergency_review")

    # 单股价格闪跌
    pnl_5m = context.get("pnl_5m", 0)
    if pnl_5m <= -float(cfg.get("price_drop_5m_threshold", 0.03)):
        return "price_drop"

    # VIX 跳变
    vix_change_5m = context.get("vix_change_5m", 0)
    if vix_change_5m >= float(cfg.get("vix_spike_5m_threshold", 0.20)):
        return "vix_spike"

    # 系统熔断 DEGRADED
    if context.get("system_degraded", False):
        return "system_degraded"

    # 该 symbol 负面新闻
    news_sentiment = context.get("symbol_news_sentiment", 0)
    if news_sentiment <= -float(cfg.get("negative_news_threshold", 0.6)):
        return "news_negative"

    return None
