"""
Position Lifecycle Engine

Generates actionable recommendations for each position based on existing data.
No complex auto-generation — just clear, rule-based suggestions.

Data sources (all already available):
  - stop_distances: SL proximity, TP proximity
  - positions: PnL%, direction, size, entry price
  - earnings: upcoming events
  - lifecycle DB fields: thesis, invalidation_price, expected_hold_days
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Sector peer mapping (from sector_leader.py)
SECTOR_MAPPING = {
    "Technology": ["NVDA", "AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMD", "INTC", "CRM", "ORCL"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "TSM", "AVGO", "QCOM", "MU", "TXN", "ASML"],
    "Consumer": ["AMZN", "TSLA", "NKE", "MCD", "SBUX", "HD", "WMT", "COST"],
    "Financials": ["JPM", "BAC", "GS", "MS", "V", "MA", "BRK-B", "C"],
    "Healthcare": ["JNJ", "UNH", "PFE", "MRK", "ABBV", "LLY", "BMY"],
    "Energy": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC"],
    "China_ADR": ["BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI", "TME", "BILI"],
    "Communication": ["GOOG", "GOOGL", "META", "NFLX", "DIS", "T", "VZ"],
}

# Build reverse index: ticker -> list of (sector, peers)
_TICKER_SECTORS: Dict[str, List[Tuple[str, List[str]]]] = {}
for _sector, _tickers in SECTOR_MAPPING.items():
    for _t in _tickers:
        if _t not in _TICKER_SECTORS:
            _TICKER_SECTORS[_t] = []
        _TICKER_SECTORS[_t].append((_sector, [x for x in _tickers if x != _t]))


def get_sector_peers(symbol: str) -> List[dict]:
    """Get sector peers for a symbol.

    Returns list of {sector, peers: [ticker, ...]} for each sector the symbol belongs to.
    A stock can be in multiple sectors (e.g. NVDA in Technology + Semiconductors).
    """
    short = symbol.replace("US.", "")
    entries = _TICKER_SECTORS.get(short, [])
    return [{"sector": s, "peers": p} for s, p in entries]


def collect_all_peer_symbols(lifecycle_results: List[dict], max_per_position: int = 4) -> Set[str]:
    """Collect all unique peer symbols across all positions (for batch quote fetch)."""
    held_symbols = {r["symbol"].replace("US.", "") for r in lifecycle_results}
    all_peers: Set[str] = set()
    for r in lifecycle_results:
        for group in get_sector_peers(r["symbol"]):
            for peer in group["peers"][:max_per_position]:
                if peer not in held_symbols:
                    all_peers.add(peer)
    return all_peers


# ═══════════════════════════════════════════════════════════
#  Recommendation Engine
# ═══════════════════════════════════════════════════════════

def generate_recommendations(
    positions: List[dict],
    stop_distances: List[dict],
    earnings: List[dict],
    lifecycle_map: Dict[str, dict],
) -> List[dict]:
    """
    Generate lifecycle cards for ALL positions.
    
    Returns list of dicts, one per position:
    {
        "symbol": "US.NVDA",
        "thesis": "...",
        "thesis_valid": True/False/None,
        "recommendations": ["...", "..."],
        "alert_level": "normal" | "warning" | "danger",
        "hold_days": 3,
        "expected_hold_days": 5,
        "invalidation_price": 138.0,
        "entry_context": {...},
    }
    """
    sl_map = {}
    for s in stop_distances:
        key = (s.get("symbol", ""), s.get("direction", ""))
        sl_map[key] = s

    # earnings by symbol (strip US. prefix)
    # earnings can be a dict {"today": [...], "upcoming": [...], "has_events": bool}
    # or a flat list of {"symbol": ..., "date": ...}
    earn_map = {}
    if isinstance(earnings, dict):
        all_events = earnings.get("today", []) + earnings.get("upcoming", [])
        for e in all_events:
            sym = e.get("ticker", "") or e.get("symbol", "").replace("US.", "")
            earn_map[sym] = e
    elif isinstance(earnings, list):
        for e in earnings:
            sym = e.get("symbol", "")
            earn_map[sym] = e

    results = []
    for pos in positions:
        sym = pos.get("code", pos.get("symbol", ""))
        direction = pos.get("direction", "LONG")
        cost = pos.get("cost_price", 0)
        cur = pos.get("cur_price", 0)
        pnl_pct = pos.get("pnl_pct", 0)

        key = (sym, direction)
        sl_data = sl_map.get(key, {})
        lc = lifecycle_map.get(sym, {})

        thesis = lc.get("thesis", "")
        invalidation = lc.get("invalidation_price", 0)
        expected_days = lc.get("expected_hold_days", 5)
        entry_time = lc.get("entry_time", "")
        entry_context = lc.get("entry_context", {})

        # Calculate hold days
        hold_days = None
        if entry_time:
            try:
                et = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                hold_days = (datetime.now(et.tzinfo) - et).days
            except Exception:
                pass

        recs = []
        alert_level = "normal"

        # ── Rule 1: Stop loss proximity ──
        sl_dist = sl_data.get("sl_distance_pct", 99)
        if sl_dist < 0:
            # Price has broken BELOW the stop loss
            recs.append(f"🔴 已跌破估算止损位 {abs(sl_dist):.1f}%，建议立即评估是否退出")
            alert_level = "danger"
        elif sl_data.get("danger") or sl_dist < 2:
            recs.append(f"⚠️ 距止损仅 {sl_dist:.1f}%，准备退出或调整止损位")
            alert_level = "danger"
        elif sl_dist < 5:
            recs.append(f"距止损 {sl_dist:.1f}%，留意走势")
            if alert_level == "normal":
                alert_level = "warning"

        # ── Rule 2: Large unrealized gain ──
        if pnl_pct > 10:
            # Suggest specific trailing stop level based on ATR
            sl_price = sl_data.get("stop_loss", 0)
            if sl_price and sl_price > 0 and cur > 0:
                atr_est = cur * 0.02
                if direction == "LONG":
                    trail_level = cur - 2 * atr_est
                    recs.append(f"浮盈 +{pnl_pct:.1f}%，建议设置追踪止盈 ${trail_level:.2f} (现价 -2×ATR)")
                else:
                    trail_level = cur + 2 * atr_est
                    recs.append(f"浮盈 +{pnl_pct:.1f}%，建议设置追踪止盈 ${trail_level:.2f} (现价 +2×ATR)")
            else:
                recs.append(f"浮盈 +{pnl_pct:.1f}%，建议设置追踪止盈保护利润")
            if alert_level == "normal":
                alert_level = "warning"
        elif pnl_pct > 5:
            tp_dist = sl_data.get("tp_distance_pct", 99)
            if tp_dist < 3:
                recs.append(f"接近止盈目标 (距离{tp_dist:.1f}%)，可考虑部分止盈")
                if alert_level == "normal":
                    alert_level = "warning"

        # ── Rule 3: Invalidation check ──
        if invalidation and invalidation > 0 and cur > 0:
            if direction == "LONG" and cur <= invalidation:
                recs.append("🔴 价格已跌破失效价位，thesis不再成立")
                alert_level = "danger"
            elif direction == "SHORT" and cur >= invalidation:
                recs.append("🔴 价格已突破失效价位，thesis不再成立")
                alert_level = "danger"

        # ── Rule 4: Hold time exceeded ──
        if hold_days is not None and hold_days > expected_days:
            recs.append(f"已持有 {hold_days} 天，超出预期 {expected_days} 天，评估是否继续持有")
            if alert_level == "normal":
                alert_level = "warning"

        # ── Rule 5: Upcoming earnings ──
        sym_short = sym.replace("US.", "")
        earn = earn_map.get(sym_short, {})
        if earn:
            days_until = earn.get("days_until", 999)
            if 0 <= days_until <= 3:
                report_date = earn.get("report_date", "")
                recs.append(f"📅 {days_until}天后财报({report_date})，考虑减仓降低事件风险")
                if alert_level == "normal":
                    alert_level = "warning"

        # ── Rule 6: No thesis set ──
        if not thesis:
            recs.append("💡 建议填写进场理由，便于后续跟踪")

        # Thesis validity
        thesis_valid = None  # None = not evaluated
        if invalidation and invalidation > 0 and cur > 0:
            if direction == "LONG":
                thesis_valid = cur > invalidation
            else:
                thesis_valid = cur < invalidation

        results.append({
            "symbol": sym,
            "direction": direction,
            "qty": pos.get("qty", 0),
            "cost_price": cost,
            "cur_price": cur,
            "pnl_pct": pnl_pct,
            "unrealized_pnl": pos.get("unrealized_pnl", 0),
            "thesis": thesis,
            "thesis_valid": thesis_valid,
            "invalidation_price": invalidation,
            "expected_hold_days": expected_days,
            "hold_days": hold_days,
            "entry_context": entry_context,
            "recommendations": recs,
            "alert_level": alert_level,
            "sl_distance_pct": sl_dist,
            "stop_loss": sl_data.get("stop_loss", 0),
            "take_profit": sl_data.get("take_profit", 0),
            "sector_peers": get_sector_peers(sym),
        })

    # Sort: danger first, then warning, then normal
    priority = {"danger": 0, "warning": 1, "normal": 2}
    results.sort(key=lambda x: priority.get(x["alert_level"], 9))
    return results


# ═══════════════════════════════════════════════════════════
#  DB Operations
# ═══════════════════════════════════════════════════════════

async def get_lifecycle_map(ds) -> Dict[str, dict]:
    """Read lifecycle fields from open_positions table.
    
    Returns: {symbol: {thesis, invalidation_price, expected_hold_days, entry_time, entry_context}}
    """
    try:
        rows = await ds.db_query("""
            SELECT symbol, thesis, invalidation_price, expected_hold_days,
                   entry_time, entry_context_json
            FROM open_positions
            WHERE sync_status IS NULL OR sync_status NOT IN (
                'closed_by_sync','closed_by_scheduler','closed_by_daemon',
                'closed_emergency','stale_not_in_broker'
            )
        """)
        result = {}
        for r in rows:
            sym = r["symbol"]
            ctx = {}
            try:
                ctx = json.loads(r.get("entry_context_json") or "{}")
            except Exception:
                pass
            result[sym] = {
                "thesis": r.get("thesis") or "",
                "invalidation_price": float(r.get("invalidation_price") or 0),
                "expected_hold_days": int(r.get("expected_hold_days") or 5),
                "entry_time": r.get("entry_time") or "",
                "entry_context": ctx,
            }
        return result
    except Exception as e:
        logger.error(f"[lifecycle] Failed to read lifecycle data: {e}")
        return {}


async def update_thesis(ds, symbol: str, thesis: Optional[str] = None,
                        invalidation_price: Optional[float] = None,
                        expected_hold_days: Optional[int] = None) -> dict:
    """Update thesis fields for a position. Only updates provided fields."""
    updates = []
    params = []

    if thesis is not None:
        updates.append("thesis = ?")
        params.append(thesis)
    if invalidation_price is not None:
        updates.append("invalidation_price = ?")
        params.append(invalidation_price)
    if expected_hold_days is not None:
        updates.append("expected_hold_days = ?")
        params.append(expected_hold_days)

    if not updates:
        return {"status": "no_change"}

    params.append(symbol)
    sql = f"UPDATE open_positions SET {', '.join(updates)} WHERE symbol = ?"

    try:
        await ds.db_execute(sql, params)
        return {"status": "ok", "symbol": symbol, "updated": [u.split("=")[0].strip() for u in updates]}
    except Exception as e:
        logger.error(f"[lifecycle] thesis update failed: {e}")
        return {"status": "error", "error": str(e)}


async def set_entry_context(ds, symbol: str, context: dict) -> dict:
    """Set entry context JSON for a position."""
    try:
        await ds.db_execute(
            "UPDATE open_positions SET entry_context_json = ? WHERE symbol = ?",
            [json.dumps(context, ensure_ascii=False), symbol]
        )
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[lifecycle] entry context update failed: {e}")
        return {"status": "error", "error": str(e)}
