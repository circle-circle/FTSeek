"""B1-lite Playbook broker execution-state safety contracts."""
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.core.position_sync import USPositionSync
from app.execution.dual_broker import DualBrokerAdapter
from app.playbook.db import PlaybookDB
from tests.playbook.test_exit_compensation import compensation_case


@pytest.fixture(autouse=True)
def sealed_execution_state(compensation_case):
    return compensation_case


def _new_db(conn=None):
    conn = conn or sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    return db, conn


def _instruction(db, symbol="US.B1"):
    return db.save_instruction({
        "date": "2026-09-10",
        "symbol": symbol,
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 10,
        "confidence": 70,
    })


def _row(conn, iid):
    return conn.execute(
        "SELECT * FROM playbook_instructions WHERE id=?", (iid,)
    ).fetchone()


def test_new_schema_and_safe_legacy_backfill():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            triggered INTEGER DEFAULT 0, executed INTEGER DEFAULT 0,
            execution_price REAL, execution_time TEXT, outcome TEXT,
            created_at TEXT NOT NULL, updated_at TEXT,
            UNIQUE(date, symbol)
        )
    """)
    conn.execute(
        "INSERT INTO playbook_instructions(date,symbol,direction,executed,created_at) "
        "VALUES('2026-09-09','US.OLD_UNKNOWN','BUY',1,'now')"
    )
    conn.execute(
        "INSERT INTO playbook_instructions(date,symbol,direction,executed,execution_price,outcome,created_at) "
        "VALUES('2026-09-09','US.OLD_BOOKED','BUY',1,101.5,'holding','now')"
    )
    conn.execute(
        "INSERT INTO playbook_instructions(date,symbol,direction,executed,created_at) "
        "VALUES('2026-09-09','US.OLD_PENDING','BUY',0,'now')"
    )
    conn.commit()

    db, _ = _new_db(conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)")}
    assert {"execution_state", "execution_attempt", "broker_order_id",
            "broker_env", "broker_order_status"} <= columns
    rebuilt_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='playbook_instructions'"
    ).fetchone()[0]
    assert "UNIQUE(date, symbol)" not in rebuilt_sql
    states = dict(conn.execute(
        "SELECT symbol, execution_state FROM playbook_instructions"
    ).fetchall())
    assert states == {
        "US.OLD_UNKNOWN": "LEGACY_LOCKED",
        "US.OLD_BOOKED": "BOOKED",
        "US.OLD_PENDING": "PENDING",
    }
    assert db.try_claim_execution(1) is None


def test_execution_safety_column_migration_failure_blocks_startup():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            triggered INTEGER DEFAULT 0, executed INTEGER DEFAULT 0,
            execution_price REAL, execution_time TEXT, outcome TEXT,
            created_at TEXT NOT NULL, updated_at TEXT
        )
    """)
    conn.set_authorizer(
        lambda action, *_: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK
    )

    with pytest.raises(RuntimeError, match="execution_state"):
        PlaybookDB(conn=conn).ensure_tables()


def test_claim_is_single_winner_and_returns_attempt(tmp_path):
    path = tmp_path / "b1.sqlite"
    seed = sqlite3.connect(path)
    seed.row_factory = sqlite3.Row
    db = PlaybookDB(conn=seed)
    db.ensure_tables()
    iid = _instruction(db)
    seed.close()

    barrier = threading.Barrier(2)
    results = []

    def claim():
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        local = PlaybookDB(conn=conn)
        barrier.wait()
        results.append(local.try_claim_execution(iid))
        conn.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results, key=lambda value: value is None) == [1, None]
    check = sqlite3.connect(path)
    row = check.execute(
        "SELECT execution_state, execution_attempt, executed FROM playbook_instructions WHERE id=?",
        (iid,),
    ).fetchone()
    assert row == ("UNKNOWN", 1, 1)
    check.close()


def test_only_explicit_zero_fill_terminal_unlocks_and_attempt_is_cas():
    db, conn = _new_db()
    iid = _instruction(db)
    attempt1 = db.try_claim_execution(iid)
    assert attempt1 == 1
    assert not db.unlock_execution_if_zero_fill_terminal(iid, attempt1, "QUERY_ERROR", 0)
    assert not db.unlock_execution_if_zero_fill_terminal(iid, attempt1, "QUERY_TIMEOUT", 0)
    assert not db.unlock_execution_if_zero_fill_terminal(iid, attempt1, "CANCELLED_PART", 0)
    assert not db.unlock_execution_if_zero_fill_terminal(iid, attempt1, "CANCELLED_ALL", 1)
    assert db.mark_execution_submitted(iid, attempt1, "OID-1", "sim")
    assert not db.mark_execution_submitted(iid, attempt1, "OID-OTHER", "sim")
    assert not db.mark_execution_submitted(iid, attempt1, "OID-1", "real")
    assert not db.mark_execution_unknown(
        iid, attempt1, broker_order_id="OID-OTHER", broker_env="sim",
        broker_order_status="LATE_RESPONSE",
    )
    assert _row(conn, iid)["broker_order_id"] == "OID-1"
    assert db.unlock_execution_if_zero_fill_terminal(iid, attempt1, "CANCELLED_ALL", 0)

    attempt2 = db.try_claim_execution(iid)
    assert attempt2 == 2
    assert not db.mark_execution_submitted(iid, attempt1, "LATE", "sim")
    assert db.mark_execution_unknown(iid, attempt2, broker_env="sim", broker_order_status="QUERY_TIMEOUT")
    row = _row(conn, iid)
    assert row["execution_state"] == "UNKNOWN"
    assert row["execution_attempt"] == 2
    assert row["broker_order_id"] is None


def test_submitted_partial_missing_price_and_bookkeeping_states():
    db, conn = _new_db()
    iid = _instruction(db)
    attempt = db.try_claim_execution(iid)
    assert db.mark_execution_submitted(iid, attempt, "OID-2", "real", "ACCEPTED")
    assert db.mark_execution_filled_unbooked(iid, attempt, "FILLED_PART")
    row = _row(conn, iid)
    assert row["execution_state"] == "FILLED_UNBOOKED"
    assert row["broker_order_id"] == "OID-2"
    assert row["broker_env"] == "real"
    assert not db.unlock_execution_if_zero_fill_terminal(iid, attempt, "CANCELLED_PART", 2)

    # Missing price / bookkeeping failures remain non-retryable until backfill succeeds.
    assert db.mark_execution_filled_unbooked(iid, attempt, "FILLED_NO_PRICE")
    assert db.mark_execution_filled_unbooked(iid, attempt, "BOOKKEEPING_FAILED")
    assert not db.mark_execution_booked(iid, attempt, 101.25)
    assert db.observe_execution_order(iid, attempt, "OID-2", "real", {
        "order_id": "OID-2", "code": "US.B1", "qty": 10,
        "order_status": "CANCELLED_PART", "dealt_qty": 2, "dealt_avg_price": 101.25,
    })["confirmed"]
    assert db.mark_execution_booked(iid, attempt, 101.25, "2026-09-10T10:00:00", "holding")
    row = _row(conn, iid)
    assert row["execution_state"] == "BOOKED"
    assert row["execution_price"] == 101.25
    assert row["executed"] == 1


def test_position_sync_conservative_reconciliation(tmp_path):
    path = tmp_path / "sync.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()

    submitted_id = _instruction(db, "US.SUBMITTED")
    submitted_attempt = db.try_claim_execution(submitted_id)
    db.mark_execution_submitted(submitted_id, submitted_attempt, "OID-S", "sim")

    unbooked_id = _instruction(db, "US.UNBOOKED")
    unbooked_attempt = db.try_claim_execution(unbooked_id)
    db.mark_execution_submitted(unbooked_id, unbooked_attempt, "OID-U", "sim")
    db.mark_execution_filled_unbooked(unbooked_id, unbooked_attempt, "FILLED_ALL")
    conn.close()

    sync = USPositionSync.__new__(USPositionSync)
    sync._backfill_playbook_trade_record = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.order_list_query.side_effect = lambda order_id, **kwargs: (0, pd.DataFrame([{
        "order_id": order_id, "code": "US.SUBMITTED" if order_id == "OID-S" else "US.UNBOOKED",
        "qty": 10, "order_status": "FILLED_ALL", "dealt_qty": 10, "dealt_avg_price": 100.0,
    }]))
    positions = {"US.UNBOOKED": {"qty": 10, "cost": 100.0, "direction": "LONG"}}
    with patch("app.core.position_sync._get_db_path", return_value=str(path)):
        sync._reconcile_playbook_execution_states(ctx, "SIMULATE", "sim", positions)

    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    submitted = _row(check, submitted_id)
    unbooked = _row(check, unbooked_id)
    assert submitted["execution_state"] == "BOOKED"
    assert submitted["broker_order_status"] == "FILLED_ALL"
    assert unbooked["execution_state"] == "BOOKED"
    assert sync._backfill_playbook_trade_record.call_count == 2
    assert ctx.order_list_query.call_count == 2
    check.close()


def test_position_sync_recovers_missing_order_id_by_attempt_remark(tmp_path):
    path = tmp_path / "remark-sync.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    iid = _instruction(db, "US.REMARK")
    attempt = db.try_claim_execution(iid)
    conn.close()

    sync = USPositionSync.__new__(USPositionSync)
    sync._backfill_playbook_trade_record = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, pd.DataFrame([{
        "order_id": "OID-R", "remark": f"pb_{iid}_sim_{attempt}", "code": "US.REMARK", "qty": 4,
        "order_status": "FILLED_ALL", "dealt_qty": 4, "dealt_avg_price": 99.5,
    }]))
    positions = {"US.REMARK": {"qty": 4, "cost": 99.5, "direction": "LONG"}}

    with patch("app.core.position_sync._get_db_path", return_value=str(path)):
        sync._reconcile_playbook_execution_states(ctx, "SIMULATE", "sim", positions)

    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    row = _row(check, iid)
    assert row["execution_state"] == "BOOKED"
    assert row["broker_order_id"] == "OID-R"
    assert row["execution_price"] == 99.5
    ctx.order_list_query.assert_called_once_with(trd_env="SIMULATE", refresh_cache=True)
    check.close()


def test_position_sync_does_not_adopt_order_without_matching_remark(tmp_path):
    path = tmp_path / "no-remark-sync.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    iid = _instruction(db, "US.NO_REMARK")
    db.try_claim_execution(iid)
    conn.close()

    sync = USPositionSync.__new__(USPositionSync)
    sync._backfill_playbook_trade_record = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.order_list_query.return_value = (0, pd.DataFrame([{
        "order_id": "UNRELATED", "order_status": "FILLED_ALL",
        "dealt_qty": 10, "dealt_avg_price": 100.0,
    }]))

    with patch("app.core.position_sync._get_db_path", return_value=str(path)):
        sync._reconcile_playbook_execution_states(ctx, "SIMULATE", "sim", {})

    check = sqlite3.connect(path)
    check.row_factory = sqlite3.Row
    row = _row(check, iid)
    assert row["execution_state"] == "UNKNOWN"
    assert row["broker_order_id"] is None
    assert row["executed"] == 1
    sync._backfill_playbook_trade_record.assert_not_called()
    check.close()


def _adapter_with_ctx(place_result):
    adapter = DualBrokerAdapter.__new__(DualBrokerAdapter)
    ctx = MagicMock()
    ctx.place_order.return_value = place_result
    adapter._ctx_and_acc = MagicMock(return_value=(ctx, "1", "SIMULATE", threading.Lock()))
    adapter._notional_sanity_check = MagicMock(return_value=True)
    return adapter, ctx


def _order():
    from app.core.models import Direction, OrderType
    return SimpleNamespace(
        symbol="US.B1", direction=Direction.LONG, quantity=10, price=0.0,
        order_type=OrderType.MARKET, metadata={}, client_order_id="pb_1_sim_1",
    )


def test_dual_broker_explicit_reject_is_definitive():
    adapter, _ = _adapter_with_ctx((1, "rejected"))
    result = adapter.submit_order(_order(), "sim")
    assert result["success"] is False
    assert result["submit_state"] == "DEFINITIVE_REJECT"
    assert result["futu_order_id"] == ""


def test_dual_broker_non_success_with_order_id_is_accepted():
    adapter, _ = _adapter_with_ctx((1, pd.DataFrame([{
        "order_id": "OID-WARN", "order_status": "SUBMITTING",
    }])))
    adapter._query_fill = MagicMock(return_value=(0.0, 0.0))

    result = adapter.submit_order(_order(), "sim")

    assert result["success"] is True
    assert result["submit_state"] == "ACCEPTED"
    assert result["futu_order_id"] == "OID-WARN"
    assert result["broker_order_status"] == "SUBMITTING"


def test_dual_broker_keeps_order_id_when_fill_query_raises():
    adapter, _ = _adapter_with_ctx((0, pd.DataFrame([{"order_id": "OID-3"}])))
    adapter._query_fill = MagicMock(side_effect=TimeoutError("fill query timeout"))
    result = adapter.submit_order(_order(), "sim")
    assert result["success"] is True
    assert result["submit_state"] == "ACCEPTED"
    assert result["futu_order_id"] == "OID-3"
    assert result["filled_price"] == 0.0


def test_order_status_query_refreshes_broker_identity_and_cumulative_receipt():
    adapter, ctx = _adapter_with_ctx((0, None))
    ctx.order_list_query.return_value = (0, pd.DataFrame([{
        'order_id': 'OID-Q', 'code': 'US.B1', 'qty': 10,
        'order_status': 'CANCELLED_PART', 'dealt_qty': 4, 'dealt_avg_price': 101.5,
    }]))
    result = adapter.query_order_status('OID-Q', 'sim')
    assert result['success'] and result['dealt_qty'] == 4 and result['dealt_avg_price'] == 101.5
    ctx.order_list_query.assert_called_once_with(order_id='OID-Q', trd_env='SIMULATE', refresh_cache=True)
