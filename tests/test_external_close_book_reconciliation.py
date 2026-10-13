import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.position_sync import upsert_external_close_trade_record


def _create_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, "
        "direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL, "
        "quantity INTEGER, realized_pnl REAL, pnl_pct REAL, exit_reason TEXT, source_type TEXT, "
        "mode TEXT, trade_mode TEXT, created_at TEXT, trace_id TEXT, signal_id TEXT, "
        # 2026-09-20: 补 config_version — helper INSERT 列表已含该列 (生产靠历史 ALTER,
        # TradeStore._init_db 走 :403 迁移), fixture 陈旧导致既有红灯, 与变种4批同车修复
        "futu_order_id TEXT, close_futu_order_id TEXT, config_version TEXT)"
    )
    return conn


def _book(conn):
    return upsert_external_close_trade_record(
        conn,
        symbol="US.GIPR",
        direction="LONG",
        entry_time="2026-09-17T04:15:19-04:00",
        entry_price=0.431,
        exit_price=0.424,
        quantity=11494,
        realized_pnl=-80.46,
        pnl_pct=-0.01624,
        trade_mode="simulate",
        created_at="2026-09-17T04:27:01-04:00",
        trace_id="trace-1",
        signal_id="signal-1",
        futu_order_id="8185457",
        close_order_id="8185461",
    )


def test_open_trade_is_updated_in_place():
    conn = _create_conn()
    conn.execute(
        "INSERT INTO trade_records (id, trade_date, symbol, direction, entry_time, exit_time, "
        "entry_price, exit_price, quantity, source_type, created_at, futu_order_id) VALUES "
        "(1522,'2026-09-17','US.GIPR','LONG','2026-09-17T04:15:19-04:00','',0.431,0,11494,"
        "'PLAYBOOK_SIM','2026-09-17T04:15:19-04:00','8185457')"
    )
    result = _book(conn)
    row = conn.execute(
        "SELECT id, exit_price, exit_time, exit_reason, close_futu_order_id, source_type "
        "FROM trade_records"
    ).fetchone()
    assert result == {"action": "updated_open_trade", "trade_id": 1522}
    assert row == (
        1522, 0.424, "2026-09-17T04:27:01-04:00", "external_close",
        "8185461", "PLAYBOOK_SIM",
    )
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1


def test_closed_trade_is_not_duplicated():
    conn = _create_conn()
    conn.execute(
        "INSERT INTO trade_records (id, trade_date, symbol, direction, entry_time, exit_time, "
        "entry_price, exit_price, quantity, source_type, created_at, close_futu_order_id) VALUES "
        "(1526,'2026-09-17','US.GIPR','LONG','2026-09-17T04:15:19-04:00',"
        "'2026-09-17T04:25:00-04:00',0.431,0.424,11494,'PLAYBOOK_SIM',"
        "'2026-09-17T04:15:19-04:00','8185461')"
    )
    result = _book(conn)
    assert result == {"action": "duplicate_skip", "trade_id": 1526}
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1


def test_second_round_external_close_is_booked():
    conn = _create_conn()
    conn.execute(
        "INSERT INTO trade_records (id, trade_date, symbol, direction, entry_time, exit_time, "
        "entry_price, exit_price, quantity, source_type, created_at, close_futu_order_id) VALUES "
        "(1526,'2026-09-17','US.GIPR','LONG','2026-09-17T04:15:19-04:00',"
        "'2026-09-17T04:25:00-04:00',0.431,0.424,11494,'PLAYBOOK_SIM',"
        "'2026-09-17T04:15:19-04:00','8185461')"
    )
    result = upsert_external_close_trade_record(
        conn,
        symbol="US.GIPR",
        direction="LONG",
        entry_time="2026-09-18T04:15:19-04:00",
        entry_price=0.500,
        exit_price=0.480,
        quantity=10000,
        realized_pnl=-200.0,
        pnl_pct=-0.04,
        trade_mode="simulate",
        created_at="2026-09-18T04:27:01-04:00",
        close_order_id="8199999",
    )
    assert result["action"] == "inserted"
    assert conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 2


def test_missing_trade_is_inserted_once():
    conn = _create_conn()
    result = _book(conn)
    row = conn.execute(
        "SELECT symbol, direction, exit_reason, source_type, close_futu_order_id "
        "FROM trade_records"
    ).fetchone()
    assert result["action"] == "inserted"
    assert row == ("US.GIPR", "LONG", "external_close", "POSITION_SYNC", "8185461")
