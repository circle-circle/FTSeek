"""
财报分析师 (L4, 2026-05-14)

财报发布后 5 分钟内, LLM 分析:
- EPS beat/miss + magnitude
- Revenue beat/miss
- Guidance change (raise / cut / inline)
- Outlook tone
- Conference call key takeaways

输出 PostEarningsSignal: BUY/HOLD/SELL + confidence + horizon_hours.
仅在持仓 symbol 触发, 不主动遍历全市场.

可控:
- 默认 disabled
- 输入: 持仓 symbol 财报 (有 trigger 才调)
- 输出 → RiskKernel + thesis check, 不直接平仓
- 24h 后回看准确率, 写 earnings_attribution.db
"""
from __future__ import annotations
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.llm.features.feature_flags import is_enabled, get_feature_config
from app.llm.features.json_schema_guard import clamp_numeric
# 2026-05-17: 迁移到 model_call 统一入口 (盘前/盘后批, 非盘中热路径)

logger = logging.getLogger(__name__)

ALLOWED_DECISION = ["BUY_MORE", "HOLD", "TRIM_25", "TRIM_50", "FLAT"]
ALLOWED_GUIDANCE = ["raise", "cut", "inline", "withdrawn", "unknown"]


@dataclass
class PostEarningsSignal:
    symbol: str
    decision: str
    confidence: float
    horizon_hours: int
    eps_surprise_pct: Optional[float]
    revenue_surprise_pct: Optional[float]
    guidance: str
    summary: str
    timestamp: float = 0.0


def _db_path() -> str:
    from app.core.paths import get_data_dir
    return str(get_data_dir() / "earnings_attribution.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS earnings_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            decision TEXT,
            confidence REAL,
            horizon_hours INTEGER,
            eps_surprise_pct REAL,
            revenue_surprise_pct REAL,
            guidance TEXT,
            summary TEXT,
            timestamp REAL NOT NULL,
            -- T+ 实测
            price_at_signal REAL,
            price_t_1h REAL,
            price_t_24h REAL,
            actual_return_24h REAL,
            hit_24h INTEGER,
            evaluated_at REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_es_symbol ON earnings_signals(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_es_time ON earnings_signals(timestamp)")


def analyze_earnings(
    symbol: str,
    earnings_data: Dict[str, Any],
    position_context: Dict[str, Any],
) -> Optional[PostEarningsSignal]:
    """
    Args:
        earnings_data: {
            "eps_actual": 1.23, "eps_estimate": 1.10,
            "revenue_actual": 5.6e9, "revenue_estimate": 5.4e9,
            "guidance_text": "...", "call_transcript_excerpt": "...",
            "press_release_summary": "..."
        }
        position_context: {
            "direction": "LONG", "position_pct": 0.05,
            "entry_price": 100, "current_price": 102, ...
        }
    """
    if not is_enabled("earnings_analyst"):
        return None

    cfg = get_feature_config("earnings_analyst")

    eps_actual = earnings_data.get("eps_actual")
    eps_est = earnings_data.get("eps_estimate")
    rev_actual = earnings_data.get("revenue_actual")
    rev_est = earnings_data.get("revenue_estimate")
    guidance_text = (earnings_data.get("guidance_text") or "")[:500]
    pr_summary = (earnings_data.get("press_release_summary") or "")[:800]

    eps_surprise = None
    if eps_actual is not None and eps_est:
        try:
            eps_surprise = (eps_actual - eps_est) / abs(eps_est) if eps_est != 0 else None
        except Exception:
            pass
    rev_surprise = None
    if rev_actual is not None and rev_est:
        try:
            rev_surprise = (rev_actual - rev_est) / abs(rev_est) if rev_est != 0 else None
        except Exception:
            pass

    direction = position_context.get("direction", "LONG")
    pos_pct = position_context.get("position_pct", 0)
    pnl_pct = position_context.get("unrealized_pnl_pct", 0)

    system = (
        "You are FTSeek's post-earnings analyst. Given earnings results + a position, "
        "decide the best action. Output strict JSON only."
    )
    prompt = f"""Earnings Result:
Symbol: {symbol}
EPS: actual={eps_actual} vs estimate={eps_est} (surprise: {eps_surprise:+.2%} if applicable)
Revenue: actual={rev_actual} vs estimate={rev_est} (surprise: {rev_surprise:+.2%} if applicable)
Guidance: {guidance_text}
Press release: {pr_summary}

Current Position:
Direction: {direction}, size {pos_pct*100:.1f}% equity
Unrealized PnL: {pnl_pct*100:+.2f}%

Decide post-earnings action:
{{
  "decision": "BUY_MORE|HOLD|TRIM_25|TRIM_50|FLAT",
  "confidence": 0.0-1.0,
  "horizon_hours": 1-72,
  "guidance": "raise|cut|inline|withdrawn|unknown",
  "summary": "<= 150 chars"
}}

Rules:
- BUY_MORE only if strong beat AND raised guidance AND already aligned with direction
- TRIM_50 if mixed (beat EPS miss revenue, or raise then cut)
- FLAT if material miss or guidance cut against position direction
- HOLD if in-line / unclear"""

    # 2026-05-17: 走 model_call 统一入口 (盘前/盘后, 非盘中)
    # yaml 路由: {primary: qwen-plus, fallback: deepseek-pro}
    # 未来如启用 cross_check (大单+财报双验证): call_llm(..., cross_check=True)
    from app.llm.model_call import call_llm as _call_llm
    res = _call_llm(
        "earnings_analyst",
        prompt,
        system=system,
        temperature=0.0,
        max_tokens=400,
        schema_required={
            "decision": str,
            "confidence": float,
            "horizon_hours": int,
            "guidance": str,
            "summary": str,
        },
        schema_enums={
            "decision": ALLOWED_DECISION,
            "guidance": ALLOWED_GUIDANCE,
        },
    )
    if not res or not res.success or not isinstance(res.response, dict):
        return None
    obj = res.response

    sig = PostEarningsSignal(
        symbol=symbol,
        decision=obj["decision"],
        confidence=clamp_numeric(obj["confidence"], 0.0, 1.0),
        horizon_hours=int(clamp_numeric(obj["horizon_hours"], 1, 72)),
        eps_surprise_pct=eps_surprise,
        revenue_surprise_pct=rev_surprise,
        guidance=obj["guidance"],
        summary=obj["summary"][:300],
        timestamp=time.time(),
    )

    _persist(sig, position_context.get("current_price", 0))
    logger.info(
        f"[EarningsAnalyst] {symbol} {sig.decision} conf={sig.confidence:.2f} "
        f"horizon={sig.horizon_hours}h guidance={sig.guidance} "
        f"eps_surprise={'%.1f%%' % (eps_surprise*100) if eps_surprise else 'n/a'}"
    )
    return sig


def _persist(sig: PostEarningsSignal, price_at_signal: float):
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO earnings_signals "
            "(symbol, decision, confidence, horizon_hours, eps_surprise_pct, "
            " revenue_surprise_pct, guidance, summary, timestamp, price_at_signal) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sig.symbol, sig.decision, sig.confidence, sig.horizon_hours,
                sig.eps_surprise_pct, sig.revenue_surprise_pct,
                sig.guidance, sig.summary, sig.timestamp, price_at_signal,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[EarningsAnalyst·persist] 失败: {e}")
