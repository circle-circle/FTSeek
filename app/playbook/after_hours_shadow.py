"""盘后影子行情采集器 (仅观测, 零决策变更).

DEB-20260805 门E 数据基础 (2026-08-05 用户裁"是"):
16:00-20:00 ET 每 5 分钟对未平仓仓位采一次 Futu 快照,
落 data/after_hours_shadow.jsonl, 供"盘后平仓 vs 持夜"净期望检验。
不触碰任何交易/退出链路; 回滚 = config playbook.after_hours_shadow.observe → false。
"""
import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/after_hours_shadow.jsonl")
_COLLECT_INTERVAL_SEC = 300
_last_run_ts = 0.0


def _observe_enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted("playbook.after_hours_shadow.observe", False))
    except Exception:
        return False


def _load_open_positions():
    from app.core.paths import get_trade_records_db
    conn = sqlite3.connect(str(get_trade_records_db()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, source_type
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'ORB_ENGINE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()
    return [
        {
            "trade_id": r["id"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "entry_price": float(r["entry_price"] or 0),
            "quantity": r["quantity"] or 0,
            "source_type": r["source_type"],
        }
        for r in rows
        if (r["entry_price"] or 0) > 0 and (r["quantity"] or 0) > 0
    ]


def run_collect(scheduler=None) -> int:
    """采集一轮盘后影子快照, 返回落盘行数。5 分钟节流; 仅 AFTER_HOURS 相 + 开关开时生效。"""
    global _last_run_ts
    if not _observe_enabled():
        return 0

    from app.core.scheduler_triggers import MarketPhase, get_market_phase
    if get_market_phase() != MarketPhase.AFTER_HOURS:
        return 0

    now = time.time()
    if now - _last_run_ts < _COLLECT_INTERVAL_SEC:
        return 0

    positions = _load_open_positions()
    if not positions:
        _last_run_ts = now
        return 0

    router = None
    if scheduler is not None:
        router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    futu = getattr(router, "futu", None) if router is not None else None
    if futu is None:
        return 0

    symbols = sorted({p["symbol"] for p in positions})
    snap_rows = futu.get_snapshot_after_hours(symbols)
    if not snap_rows:
        return 0
    snap_by_symbol = {
        r["code"].replace("US.", "", 1): r for r in snap_rows if r.get("last_price", 0) > 0
    }

    import pytz
    from datetime import datetime
    now_et = datetime.now(pytz.timezone("America/New_York"))

    written = 0
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
        for p in positions:
            s = snap_by_symbol.get(p["symbol"])
            if not s:
                continue
            rec = {
                "ts_et": now_et.isoformat(timespec="seconds"),
                "date": now_et.strftime("%Y-%m-%d"),
                "session": "after_hours",
                **p,
                "last_price": s["last_price"],
                "prev_close": s["prev_close"],
                "bid": s["bid"],
                "ask": s["ask"],
                "volume": s["volume"],
                "turnover": s["turnover"],
                "quote_update_time": s["update_time"],
                "data_source": "futu_snapshot_eth",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1

    _last_run_ts = now
    if written:
        logger.info(f"[AH影子] 盘后快照落盘 {written} 行 → {_OUTPUT_PATH}")
    return written
