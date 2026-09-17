import json
import sqlite3
from datetime import datetime, timezone

import pytest

import scripts.extended_hours_entry_executor as entry_executor
import scripts.extended_hours_exit_executor as exit_executor
import scripts.extended_hours_order_lifecycle as lifecycle


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def iterrows(self):
        return iter(enumerate(self.rows))

    @property
    def iloc(self):
        return self

    def __getitem__(self, index):
        return self.rows[index]


class FakeOrderResponse:
    def __init__(self, order_id):
        self._order_id = FakeSeries(order_id)

    def __getitem__(self, key):
        if key != "order_id":
            raise KeyError(key)
        return self._order_id


class FakeSeries:
    def __init__(self, value):
        self.values = (value,)


class FakeTradeContext:
    def __init__(self, rows=None, place_order_id="900001", modify_ok=True):
        self.rows = rows or []
        self.place_order_id = place_order_id
        self.modify_ok = modify_ok
        self.placed = []

    def order_list_query(self, **kwargs):
        order_id = kwargs.get("order_id")
        rows = self.rows
        if order_id:
            rows = [row for row in rows if str(row.get("order_id")) == str(order_id)]
            rows = rows[-1:]
        return 0, FakeFrame(rows)

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return 0, FakeOrderResponse(self.place_order_id)

    def modify_order(self, *args, **kwargs):
        return (0 if self.modify_ok else -1), "ok" if self.modify_ok else "cancel failed"

    def close(self):
        pass


def _create_db(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, position_id TEXT, direction TEXT, size REAL,
            avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT,
            owner_strategy TEXT, owner TEXT, mode TEXT, account_type TEXT,
            trader_type TEXT, is_filled INTEGER, version INTEGER, updated_at TEXT,
            metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL,
            stop_loss_price REAL
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT,
            direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL,
            exit_price REAL, quantity REAL, realized_pnl REAL, pnl_pct REAL,
            exit_reason TEXT, holding_hours REAL, source_type TEXT,
            trade_mode TEXT, mode TEXT, owner_strategy TEXT, strategy_id TEXT,
            futu_order_id TEXT, close_futu_order_id TEXT, metadata_json TEXT,
            created_at TEXT
        );
        """
    )
    connection.commit()
    connection.close()


@pytest.fixture()
def entry_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "entry.db"
    _create_db(db_path)
    monkeypatch.setattr(entry_executor, "DB", db_path)
    monkeypatch.setattr(entry_executor, "AUDIT", tmp_path / "entry-audit.jsonl")
    monkeypatch.setattr(entry_executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(entry_executor, "LOCK", tmp_path / "state.lock")
    monkeypatch.setattr(entry_executor, "_push", lambda text: None)
    monkeypatch.setattr(
        entry_executor, "_now_et",
        lambda: datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(entry_executor, "_open_trade_context", lambda: FakeTradeContext())
    monkeypatch.setattr(
        entry_executor, "_reconcile_broker_entry_orders", lambda *args, **kwargs: ([], [])
    )
    return db_path


def test_entry_full_fill_writes_position_and_trade_record(
    entry_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        entry_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 100, "avg_fill_price": 10.0,
            "status": "FILLED_ALL", "remaining_qty": 0,
            "cancel_ok": True, "error": "",
        },
    )

    result = entry_executor._execute(
        "US.TEST", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is True
    connection = sqlite3.connect(entry_environment)
    position = connection.execute(
        "SELECT position_id, size, avg_entry_price, initial_sl FROM open_positions"
    ).fetchone()
    trade = connection.execute(
        "SELECT quantity, entry_price, futu_order_id, exit_price FROM trade_records"
    ).fetchone()
    connection.close()
    assert position[1:] == (100, 10.0, 9.5)
    assert trade == (100, 10.0, "900001", 0)


def test_entry_partial_fill_writes_actual_filled_quantity(
    entry_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        entry_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 40, "avg_fill_price": 10.2,
            "status": "CANCELLED_PART", "remaining_qty": 60,
            "cancel_ok": True, "error": "",
        },
    )

    result = entry_executor._execute(
        "US.TEST", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is True
    connection = sqlite3.connect(entry_environment)
    size = connection.execute("SELECT size FROM open_positions").fetchone()[0]
    connection.close()
    assert size == 40


def test_entry_db_write_is_idempotent_and_never_replaces_position(
    entry_environment,
):
    first_id = entry_executor._write_open_position(
        "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900001"
    )
    repeat_id = entry_executor._write_open_position(
        "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900001"
    )
    assert repeat_id == first_id

    with pytest.raises(RuntimeError, match="already exists"):
        entry_executor._write_open_position(
            "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900002"
        )


def test_wait_fill_cancels_partial_and_confirms_terminal_state():
    context = FakeTradeContext(
        rows=[{"order_id": "900001", "code": "US.TEST", "order_status": "FILLED_PART",
               "qty": 100, "dealt_qty": 40, "dealt_avg_price": 10.2, "price": 10.0}]
    )
    context.rows.append(
        {"order_id": "900001", "code": "US.TEST", "order_status": "CANCELLED_PART",
         "qty": 100, "dealt_qty": 40, "dealt_avg_price": 10.2, "price": 10.0}
    )

    result = lifecycle.wait_order_fill(
        context, "SIMULATE", 19930823, "900001", 100, 10.0,
        timeout_s=0.1, poll_s=0,
    )

    assert result["filled"] is True
    assert result["filled_qty"] == 40
    assert result["cancel_ok"] is True
    assert result["status"] == "CANCELLED_PART"


def test_wait_fill_reports_cancel_failure():
    context = FakeTradeContext(
        rows=[{"order_id": "900001", "code": "US.TEST", "order_status": "SUBMITTED",
               "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0}],
        modify_ok=False,
    )

    result = lifecycle.wait_order_fill(
        context, "SIMULATE", 19930823, "900001", 100, 10.0,
        timeout_s=0, poll_s=0,
    )

    assert result["filled"] is False
    assert result["cancel_ok"] is False
    assert "cancel_request_failed" in result["error"]


def test_entry_reconciliation_restores_attempt_state_from_active_broker_order():
    context = FakeTradeContext(
        rows=[{"order_id": "8185070", "code": "US.TEST", "order_status": "SUBMITTED",
               "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0,
               "remark": "ext_entry", "create_time": "2026-09-17 08:00:00"}]
    )
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    active_orders, conflicts = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert conflicts == []
    assert active_orders[0]["order_id"] == "8185070"
    attempt = state["attempts"]["2026-09-17:US.TEST"]
    assert attempt["status"] == "broker_active"
    assert attempt["order_id"] == "8185070"


@pytest.fixture()
def exit_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "exit.db"
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO open_positions (symbol, position_id, direction, size, "
        "avg_entry_price, entry_time, strategy_id, source, owner_strategy, owner, "
        "mode, account_type, trader_type, is_filled, version, updated_at, "
        "sync_status) VALUES ('US.TEST', 'EXT-1', 'LONG', 100, 10.0, "
        "'2026-09-17T08:00:00-04:00', 'extended_hours_entry', 'test', 'test', "
        "'test', 'normal', 'simulate', 'AUTO', 1, 1, '2026-09-17', 'pending')"
    )
    connection.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, "
        "entry_price, quantity, source_type, trade_mode, mode, owner_strategy, "
        "strategy_id, futu_order_id, created_at) VALUES ('2026-09-17', 'US.TEST', "
        "'LONG', '2026-09-17T08:00:00-04:00', 10.0, 100, 'PLAYBOOK_SIM', "
        "'simulate', 'futu_simulate', 'extended_hours_entry', "
        "'extended_hours_entry', '900001', '2026-09-17')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(exit_executor, "DB", db_path)
    monkeypatch.setattr(exit_executor, "_AUDIT_LOG", tmp_path / "exit-audit.jsonl")
    monkeypatch.setattr(exit_executor, "_push", lambda text: None)
    monkeypatch.setattr(exit_executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(exit_executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(exit_executor, "_open_trade_context", lambda: FakeTradeContext())
    return db_path


def test_exit_full_fill_updates_records_and_removes_position(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        exit_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 100, "avg_fill_price": 11.0,
            "status": "FILLED_ALL", "remaining_qty": 0,
            "cancel_ok": True, "error": "",
        },
    )

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is True
    assert result["fully_filled"] is True
    connection = sqlite3.connect(exit_environment)
    positions = connection.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0]
    trade = connection.execute(
        "SELECT exit_price, exit_reason, close_futu_order_id, realized_pnl "
        "FROM trade_records WHERE futu_order_id='900001'"
    ).fetchone()
    connection.close()
    assert positions == 0
    assert trade == (11.0, "lock_in", "900001", 100.0)
    with exit_executor._AUDIT_LOG.open() as handle:
        audit = json.loads(handle.read().splitlines()[-1])
    assert audit["fully_filled"] is True
    assert audit["db_updated"] is True


def test_exit_partial_fill_reduces_position_and_records_partial_close(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        exit_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 40, "avg_fill_price": 11.0,
            "status": "CANCELLED_PART", "remaining_qty": 60,
            "cancel_ok": True, "error": "",
        },
    )

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is True
    assert result["fully_filled"] is False
    assert result["remaining_qty"] == 60
    connection = sqlite3.connect(exit_environment)
    position_size = connection.execute("SELECT size FROM open_positions").fetchone()[0]
    open_quantity = connection.execute(
        "SELECT quantity FROM trade_records WHERE futu_order_id='900001'"
    ).fetchone()[0]
    partial = connection.execute(
        "SELECT quantity, exit_price, exit_reason, close_futu_order_id "
        "FROM trade_records WHERE close_futu_order_id='900001'"
    ).fetchone()
    connection.close()
    assert position_size == 60
    assert open_quantity == 60
    assert partial == (40, 11.0, "lock_in_partial", "900001")


def test_exit_blocks_when_broker_exit_order_is_active(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    context = FakeTradeContext(
        rows=[{"order_id": "900002", "code": "US.TEST", "order_status": "SUBMITTED",
               "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 11.0,
               "remark": "ext_exit_lock_in"}]
    )
    monkeypatch.setattr(exit_executor, "_open_trade_context", lambda: context)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["skip"] == "active_ext_exit_order"
    assert context.placed == []
