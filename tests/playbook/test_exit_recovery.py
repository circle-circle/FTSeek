"""Real exit-ledger/broker/pipeline recovery with pre-import sandbox boundaries.

Run with --noconftest. The reused entry fixture installs I/O guards before any
application implementation is loaded; this module has no application imports.
"""

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.playbook.test_entry_revalidation import env as entry_env


ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc).timestamp()

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Table:
    """Only the SDK DataFrame surface consumed by the real exit lifecycle."""

    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)

    def to_dict(self, orient):
        assert orient == "records"
        return copy.deepcopy(self.rows)


class ScopedContext:
    def __init__(self, account, environment):
        self.account = account
        self.environment = environment
        self.positions = {}
        self.orders = []
        self.rows = {}
        self.calls = []
        self.cancels = []
        self.fill_market = False
        self.fill_price = 105.0

    def scope(self, kwargs):
        assert kwargs["acc_id"] == self.account
        assert kwargs["trd_env"] == self.environment
        self.calls.append(copy.deepcopy(kwargs))

    def position_list_query(self, **kwargs):
        self.scope(kwargs)
        return 0, Table([{"code": symbol, "qty": qty,
                          "position_side": "LONG" if qty > 0 else "SHORT"}
                         for symbol, qty in self.positions.items()])

    def place_order(self, **kwargs):
        self.scope(kwargs)
        self.orders.append(copy.deepcopy(kwargs))
        order_id = f"{self.account}-{len(self.orders)}"
        row = dict(kwargs, order_id=order_id, order_status="SUBMITTED",
                   dealt_qty=0, dealt_avg_price=0)
        if kwargs["order_type"] == "MARKET" and self.fill_market:
            row.update(order_status="FILLED_ALL", dealt_qty=kwargs["qty"],
                       dealt_avg_price=self.fill_price)
        self.rows[order_id] = row
        return 0, Table([row])

    def order_list_query(self, order_id=None, **kwargs):
        self.scope(kwargs)
        return 0, Table([row for key, row in self.rows.items() if not order_id or key == order_id])

    def history_order_list_query(self, **kwargs):
        self.scope(kwargs)
        return 0, Table([])

    def modify_order(self, order_id, **kwargs):
        self.scope(kwargs)
        self.cancels.append(order_id)
        return -1, "cancel unconfirmed"

    def fill(self, order_id=None, price=105.0):
        order_id = order_id or next(reversed(self.rows))
        self.rows[order_id].update(order_status="FILLED_ALL",
                                   dealt_qty=self.rows[order_id]["qty"], dealt_avg_price=price)


@pytest.fixture
def recovery(entry_env, tmp_path, monkeypatch):
    """Reuse pre-import guards; replace only reviewed dependencies with real code."""
    pipeline = entry_env.pipeline
    source = tmp_path / "trades.sqlite"
    ledger_path = tmp_path / "exit-ledger.sqlite"
    clock = Clock()
    phase = SimpleNamespace(value="TRADING")
    callbacks = {}
    callback_receipts = []
    callback_failures = []

    def module(name, **attrs):
        fake = ModuleType(name)
        fake.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, fake)
        return fake

    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        loaded = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, loaded)
        spec.loader.exec_module(loaded)
        loaded.__file__ = str(tmp_path / relative)
        return loaded

    def connect():
        conn = sqlite3.connect(source)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry_price REAL,
                entry_time TEXT, quantity INTEGER, source_type TEXT, metadata_json TEXT,
                futu_order_id TEXT, decision_id TEXT, exit_price REAL, exit_time TEXT,
                exit_reason TEXT, realized_pnl REAL, pnl_pct REAL, holding_hours REAL,
                close_futu_order_id TEXT, mfe_pct REAL, mae_pct REAL, max_drawdown REAL,
                fees_est REAL, pnl_net REAL, fees_source TEXT);
            CREATE TABLE playbook_instructions (
                id INTEGER PRIMARY KEY, outcome TEXT, exit_price REAL, exit_time TEXT,
                exit_reason TEXT, pnl REAL, pnl_pct REAL, holding_period_hours REAL,
                max_favorable_excursion REAL, max_adverse_excursion REAL,
                executed INTEGER DEFAULT 0, execution_state TEXT DEFAULT 'SUBMITTED',
                execution_attempt INTEGER DEFAULT 3, execution_owner TEXT DEFAULT 'entry-worker');
            CREATE TABLE open_positions (
                symbol TEXT PRIMARY KEY, sync_status TEXT, direction TEXT, size REAL,
                avg_entry_price REAL, entry_time TEXT, sl_broker_order_id TEXT);
        """)

    def callback(name, result=None):
        def record(*args, **kwargs):
            # A separate reader must already see the committed receipt whenever
            # a bookkeeping callback fires. This also detects duplicate replay.
            with connect() as conn:
                rows = conn.execute("SELECT metadata_json FROM trade_records WHERE exit_time IS NOT NULL").fetchall()
            if not rows or not all(json.loads(row[0]).get("exit_intent_id") for row in rows):
                callback_failures.append(name)
                raise AssertionError(f"callback {name} ran before committed receipt")
            callback_receipts.append(name)
            return result
        callbacks[name] = Mock(side_effect=record)
        return callbacks[name]

    module("app.core.paths", get_trade_records_db=lambda: source,
           get_exit_ledger_db=lambda: ledger_path)
    module("app.core.scheduler_triggers", ET=timezone.utc,
           MarketPhase=SimpleNamespace(TRADING="TRADING", CLOSED="CLOSED"),
           get_market_phase=lambda: phase.value)
    sync = SimpleNamespace(mark_closed_tombstone=callback("tombstone"),
                           confirm_tombstone=callback("confirm"), request_immediate_sync=callback("sync"))
    module("app.core.position_sync", get_us_position_sync=lambda: sync)
    module("app.playbook.exit_shadow", finalize_exit_shadow=callback("shadow"))
    module("app.playbook.reentry_tp_shadow", settle_on_close=callback("reentry"))
    module("app.playbook.overnight_hold_shadow", emit=callback("overnight"))
    acb = SimpleNamespace(record_trade_result=callback("acb"))
    pdt = SimpleNamespace(record_close=callback("pdt"))
    module("app.risk.absolute_circuit_breaker", get_absolute_circuit_breaker=lambda: acb)
    module("app.risk.pdt_guard", get_pdt_guard=lambda: pdt)
    module("app.risk.position_exit_monitor", get_market_hours_us=lambda *args: 0.1)
    module("app.coordinator", __path__=[])
    module("app.coordinator.manual_hold_registry", manual_hold_registry=SimpleNamespace(
        get_hold_info=lambda symbol: {}, unregister=callback("unregister")))
    module("app.analysis", __path__=[])
    module("app.analysis.trade_attribution", compute_attribution=callback("attribution", SimpleNamespace(
        summary="isolated", improvement="isolated", to_dict=lambda: {"isolated": True})))
    push = SimpleNamespace(push=callback("push"), push_text=callback("push_text"))
    module("app.push", get_push_hub=lambda: push, trade_exit=lambda **kwargs: kwargs)
    module("app.push.message", Level=SimpleNamespace(HIGH="HIGH"))
    entry_env.cfg["sl_burst_alert_enabled"] = False
    entry_env.cfg["portfolio_backstop"] = {"enabled": False}
    futu = sys.modules["futu"]
    for name, value in {
        "TrdEnv": SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        "TrdSide": SimpleNamespace(BUY="BUY", SELL="SELL"),
        "OrderType": SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET"),
        "ModifyOrderOp": SimpleNamespace(CANCEL="CANCEL"), "RET_OK": 0,
    }.items():
        monkeypatch.setattr(futu, name, value, raising=False)
    lifecycle = load("app.execution.exit_lifecycle", "app/execution/exit_lifecycle.py")
    broker_module = load("app.execution.dual_broker", "app/execution/dual_broker.py")
    fees = load("app.execution.fee_model", "app/execution/fee_model.py")
    sim = ScopedContext(42, "SIMULATE")
    real = ScopedContext(99, "REAL")
    holder = SimpleNamespace()

    def rebuild():
        broker = broker_module.DualBrokerAdapter.__new__(broker_module.DualBrokerAdapter)
        broker._exit_ledger_path = ledger_path
        broker._exit_clock = clock
        broker._sim_ctx, broker._real_ctx = sim, real
        broker.sim_account_id, broker.real_account_id = "42", "99"
        broker._sim_lock, broker._real_lock = threading.Lock(), threading.Lock()
        holder.broker = broker
        return broker

    rebuild()
    monkeypatch.setattr(broker_module, "get_dual_broker", lambda: holder.broker)
    alerts = Mock()
    monkeypatch.setattr(pipeline, "_alert_close_failure", alerts)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now, tz)

    monkeypatch.setattr(pipeline, "datetime", FrozenDateTime)
    scheduler = SimpleNamespace(data_router=None)

    def rows(table="trade_records"):
        with connect() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def add_trade(iid=1, symbol="US.TEST_RECOVERY", direction="LONG", target_env="sim",
                  order_id=None, entry_time=None, metadata=None):
        order_id = order_id or f"entry-{iid}"
        entry_time = entry_time or datetime.fromtimestamp(clock.now - 60, timezone.utc).isoformat()
        meta = {"playbook_instruction_id": iid, "stop_loss": 95.0, "take_profit": 110.0,
                "peak_price": 106.0 if direction == "LONG" else 94.0,
                "trough_price": 98.0 if direction == "LONG" else 102.0}
        meta.update(metadata or {})
        source_type = "PLAYBOOK_SIM" if target_env == "sim" else "PLAYBOOK_LIVE"
        with connect() as conn:
            conn.execute("INSERT INTO trade_records (id,symbol,direction,entry_price,entry_time,"
                         "quantity,source_type,metadata_json,futu_order_id,decision_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (iid, symbol, direction, 100.0, entry_time, 10, source_type,
                          json.dumps(meta), order_id, f"test-{iid}"))
            conn.execute("INSERT OR IGNORE INTO playbook_instructions(id,outcome) VALUES(?, 'holding')", (iid,))
            conn.execute("INSERT OR REPLACE INTO open_positions VALUES(?, 'synced', ?, 10, 100, ?, ?)",
                         (symbol, direction, entry_time, order_id))
        context = sim if target_env == "sim" else real
        context.positions[symbol] = 10 if direction == "LONG" else -10
        row = next(row for row in rows() if row["id"] == iid)
        return pipeline._playbook_exit_owner(source, row, meta)

    def start(owner, *, reason="SL_INITIAL", limit_price=0.0):
        target_env = "real" if owner["source_type"] == "PLAYBOOK_LIVE" else "sim"
        decision = pipeline._playbook_exit_decision(reason, 100.0, {
            "stop_loss": 95.0, "playbook_instruction_id": owner["instruction_id"],
            "_log_tag": "do-not-persist", "_symbol": owner["symbol"]}, 0.1)
        return holder.broker.close_position(owner["symbol"], owner["quantity"], owner["direction"],
            target_env=target_env, limit_price=limit_price, limit_timeout_s=0,
            exit_owner=owner, exit_decision=decision)

    def monitor():
        return pipeline.monitor_playbook_sim_exits(scheduler)

    def eod():
        return pipeline._force_close_orphaned_sim_positions(scheduler)

    yield SimpleNamespace(pipeline=pipeline, broker=holder, sim=sim, real=real,
                          clock=clock, phase=phase, callbacks=callbacks,
                          callback_receipts=callback_receipts, alerts=alerts,
                          ledger=lambda: lifecycle.ExitLedger(ledger_path), source=source,
                          ledger_path=ledger_path, connect=connect, rows=rows,
                          add_trade=add_trade, start=start, monitor=monitor, eod=eod,
                          rebuild=rebuild, scheduler=scheduler, fees=fees, load=load)
    assert callback_failures == [], "a production exception handler swallowed a precommit callback"
    assert entry_env.violations == []


def assert_booked(env, intent_id, *, trade_id=1, price=105.0):
    row = next(row for row in env.rows() if row["id"] == trade_id)
    assert row["exit_price"] == price
    assert row["close_futu_order_id"]
    assert row["exit_time"]
    assert json.loads(row["metadata_json"])["exit_intent_id"] == intent_id
    assert env.ledger().get(intent_id)["state"] == "BOOKED"
    assert env.ledger().get(intent_id)["receipt"]["trade_id"] == trade_id
    return row


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize("target_env", ["sim", "real"])
def test_pending_market_survives_rounds_and_adapter_rebuild(recovery, direction, target_env):
    env = recovery
    owner = env.add_trade(direction=direction, target_env=target_env)
    context = env.sim if target_env == "sim" else env.real
    pending = env.start(owner)
    intent_id = pending["exit_intent_id"]
    assert not pending["success"]
    deadline = env.ledger().legs(intent_id)[0]["deadline"]
    assert env.monitor() == 0
    env.rebuild()
    assert env.monitor() == 0
    assert len(context.orders) == 1
    assert env.ledger().legs(intent_id)[0]["deadline"] == deadline
    context.fill(price=105.0 if direction == "LONG" else 95.0)
    assert env.monitor() == 1
    row = assert_booked(env, intent_id, price=105.0 if direction == "LONG" else 95.0)
    assert row["realized_pnl"] == 50.0 and row["pnl_pct"] == 0.05
    assert row["fees_est"] > 0 and row["pnl_net"] == round(50.0 - row["fees_est"], 2)
    assert env.rows("playbook_instructions")[0]["pnl"] == row["pnl_net"]
    assert env.rows("open_positions") == []
    callbacks = list(env.callback_receipts)
    assert env.monitor() == 0
    assert env.callback_receipts == callbacks
    assert len(context.orders) == 1


def test_filled_unbooked_database_failure_retries_only_atomic_booking(recovery):
    env = recovery
    env.sim.fill_market = True
    result = env.start(env.add_trade())
    intent_id = result["exit_intent_id"]
    assert result["success"] and result["exit_state"] == "FILLED_UNBOOKED"
    with env.connect() as conn:
        conn.execute("CREATE TRIGGER fail_close BEFORE UPDATE OF exit_price ON trade_records "
                     "BEGIN SELECT RAISE(ABORT, 'test bookkeeping unavailable'); END")
    assert env.monitor() == 0
    row = env.rows()[0]
    assert row["exit_time"] is None
    assert "exit_intent_id" not in json.loads(row["metadata_json"])
    assert env.ledger().get(intent_id)["state"] == "FILLED_UNBOOKED"
    assert env.callback_receipts == []
    assert len(env.sim.orders) == 1
    with env.connect() as conn:
        conn.execute("DROP TRIGGER fail_close")
    rebuilt = env.rebuild()
    rebuilt._sim_ctx = None  # Receipt recovery must not need broker or quotes.
    assert env.monitor() == 1
    assert_booked(env, intent_id)
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("emit_overnight", [False, True], ids=["ordinary-close", "overnight-shadow"])
def test_commit_before_ack_replay_acknowledges_without_duplicate_callbacks(
        recovery, monkeypatch, emit_overnight):
    env = recovery
    owner = env.add_trade()
    decision = env.pipeline._playbook_exit_decision(
        "TIME_EXIT", 100.0, {"playbook_instruction_id": owner["instruction_id"]}, 0.1)
    decision.update(emit_overnight_shadow=emit_overnight, decision_id="overnight-replay-1")
    env.sim.fill_market = True
    result = env.broker.broker.close_position(
        owner["symbol"], owner["quantity"], owner["direction"],
        target_env="sim", exit_owner=owner, exit_decision=decision)
    intent_id = result["exit_intent_id"]
    assert result["success"] and result["exit_state"] == "FILLED_UNBOOKED"
    assert env.ledger().get(intent_id)["decision"] == decision
    assert "sl_fallback_attribution" not in decision
    env.callbacks["overnight"].assert_not_called()
    acknowledge = Mock(return_value=False)
    monkeypatch.setattr(env.broker.broker, "acknowledge_close", acknowledge)
    assert env.monitor() == 1
    acknowledge.assert_called_once()
    assert acknowledge.call_args.args[0] == intent_id
    assert env.ledger().get(intent_id)["state"] == "FILLED_UNBOOKED"
    row = env.rows()[0]
    assert row["exit_time"] is not None
    assert row["exit_reason"] == "TIME_EXIT"
    assert json.loads(row["metadata_json"])["exit_intent_id"] == intent_id
    if emit_overnight:
        env.callbacks["overnight"].assert_called_once_with(
            owner["symbol"], "LONG", "TIME_EXIT", 105.0, 100.0, 50.0,
            notional=1050.0, decision_id="overnight-replay-1")
        assert env.callback_receipts.count("overnight") == 1
    else:
        env.callbacks["overnight"].assert_not_called()
    before = copy.deepcopy(env.rows())
    callbacks = list(env.callback_receipts)
    env.rebuild()._sim_ctx = None
    assert env.monitor() == 0
    assert_booked(env, intent_id)
    assert env.monitor() == 0
    assert env.rows() == before
    assert env.callback_receipts == callbacks
    assert env.callbacks["overnight"].call_count == int(emit_overnight)
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("entrypoint", ["monitor", "eod"])
def test_closed_session_queries_existing_leg_without_cancel_or_replacement(recovery, entrypoint):
    env = recovery
    result = env.start(env.add_trade(), limit_price=100.0)
    intent_id = result["exit_intent_id"]
    env.phase.value = "CLOSED"
    cancels = list(env.sim.cancels)
    env.sim.rows["42-1"].update(order_status="CANCELLED_ALL", dealt_qty=0)
    assert getattr(env, entrypoint)() == 0
    assert len(env.sim.orders) == 1 and env.sim.cancels == cancels
    assert env.ledger().get(intent_id)["state"] != "BOOKED"
    env.phase.value = "TRADING"
    env.sim.fill_market = True
    assert getattr(env, entrypoint)() == 1
    assert [order["qty"] for order in env.sim.orders] == [10, 10]
    assert_booked(env, intent_id)


@pytest.mark.parametrize("entrypoint", ["monitor", "eod"])
def test_pending_fill_precedes_grace_eod_cap_and_missing_quotes(recovery, entrypoint):
    env = recovery
    owner = env.add_trade(metadata={"eod_close_fail_count": 3, "cohort": "B"})
    result = env.start(owner)
    env.sim.fill(price=103.0)
    env.scheduler.data_router = SimpleNamespace(
        get_batch_quotes=Mock(side_effect=AssertionError("quotes must not gate recovery")),
        get_quote=Mock(side_effect=AssertionError("quotes must not gate recovery")))
    assert getattr(env, entrypoint)() == 1
    assert_booked(env, result["exit_intent_id"], price=103.0)
    env.scheduler.data_router.get_batch_quotes.assert_not_called()
    env.scheduler.data_router.get_quote.assert_not_called()
    assert len(env.sim.orders) == 1


def compensation_owner(env, *, symbol="US.TEST_COMPENSATION", entry_order_id="comp-entry"):
    return {"kind": "compensation", "close_key": f"entry:{entry_order_id}",
            "db_path": str(env.source), "instruction_id": 77, "execution_attempt": 3,
            "entry_order_id": entry_order_id,
            "entry_time": datetime.fromtimestamp(env.clock.now - 60, timezone.utc).isoformat(),
            "entry_price": 100.0, "quantity": 10, "source_type": "PLAYBOOK_SIM",
            "symbol": symbol, "direction": "LONG"}


@pytest.mark.parametrize("entrypoint", ["monitor", "eod"])
def test_compensation_without_trade_rows_has_independent_receipt(recovery, entrypoint):
    env = recovery
    owner = compensation_owner(env)
    env.sim.positions[owner["symbol"]] = 10
    with env.connect() as conn:
        conn.execute("INSERT INTO playbook_instructions(id,outcome) VALUES(77, 'holding')")
    instruction_before = env.rows("playbook_instructions")
    result = env.start(owner, reason="ENTRY_BOOKKEEPING_FAILED")
    assert env.rows() == []
    env.sim.fill()
    assert getattr(env, entrypoint)() == 0  # No fictional trade row or closed count.
    intent = env.ledger().get(result["exit_intent_id"])
    assert intent["state"] == "BOOKED"
    assert intent["receipt"]["kind"] == "compensation"
    assert intent["receipt"]["instruction_id"] == 77
    assert intent["receipt"]["execution_attempt"] == 3
    assert intent["receipt"]["entry_order_id"] == "comp-entry"
    assert env.rows() == []
    assert env.rows("playbook_instructions") == instruction_before
    assert env.callback_receipts == []
    assert len(env.sim.orders) == 1


def test_compensation_closes_only_exact_partially_persisted_entry(recovery):
    env = recovery
    owner = compensation_owner(env)
    env.add_trade(iid=77, symbol=owner["symbol"], order_id=owner["entry_order_id"],
                  entry_time=owner["entry_time"])
    replacement_time = datetime.fromtimestamp(env.clock.now - 30, timezone.utc).isoformat()
    env.add_trade(iid=78, symbol=owner["symbol"], order_id="different-entry", entry_time=replacement_time,
                  metadata={"playbook_instruction_id": 77})
    env.sim.fill_market = True
    result = env.start(owner, reason="ENTRY_BOOKKEEPING_FAILED")
    assert env.monitor() == 1
    assert_booked(env, result["exit_intent_id"], trade_id=77)
    new = next(row for row in env.rows() if row["id"] == 78)
    assert new["exit_time"] is None
    assert env.rows("open_positions")[0]["sl_broker_order_id"] == "different-entry"
    env.callbacks["tombstone"].assert_not_called()
    assert len(env.sim.orders) == 1


def test_new_same_symbol_entry_cannot_consume_old_filled_result(recovery):
    env = recovery
    env.sim.fill_market = True
    result = env.start(env.add_trade())
    intent_id = result["exit_intent_id"]
    with env.connect() as conn:
        # Even equal price/time/quantity is a new entry when its broker ID changes.
        conn.execute("UPDATE trade_records SET futu_order_id='replacement-entry' WHERE id=1")
        conn.execute("UPDATE open_positions SET sl_broker_order_id='replacement-entry'")
    assert env.monitor() == 0
    assert env.rows()[0]["exit_time"] is None
    assert env.rows("open_positions")[0]["sl_broker_order_id"] == "replacement-entry"
    assert env.ledger().get(intent_id)["state"] == "FILLED_UNBOOKED"
    assert env.callback_receipts == []
    env.alerts.assert_called()
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("bad_json", ["{", "[]"], ids=["invalid-json", "non-object-result"])
def test_malformed_intent_does_not_block_other_recoverable_position(recovery, bad_json):
    env = recovery
    env.sim.fill_market = True
    bad = env.start(env.add_trade(iid=1, symbol="US.TEST_BAD"))
    good = env.start(env.add_trade(iid=2, symbol="US.TEST_GOOD"))
    with sqlite3.connect(env.ledger_path) as conn:
        conn.execute("UPDATE exit_intents SET result_json=? WHERE intent_id=?",
                     (bad_json, bad["exit_intent_id"]))
    assert env.monitor() == 1
    assert_booked(env, good["exit_intent_id"], trade_id=2)
    assert next(row for row in env.rows() if row["id"] == 1)["exit_time"] is None
    assert len(env.sim.orders) == 2


def test_limit_fill_preserves_breakeven_limit_telemetry(recovery):
    env = recovery
    owner = env.add_trade()
    decision = env.pipeline._playbook_exit_decision("SL_BREAKEVEN", 100.0, {
        "_be_close_telemetry": {"limit_px": 100.0}, "playbook_instruction_id": 1})
    result = env.broker.broker.close_position(owner["symbol"], 10, "LONG", limit_price=100.0,
        limit_timeout_s=0, exit_owner=owner, exit_decision=decision)
    env.sim.fill(price=100.01)
    assert env.monitor() == 1
    row = assert_booked(env, result["exit_intent_id"], price=100.01)
    meta = json.loads(row["metadata_json"])
    assert meta["exit_price_source"] == "broker_limit_deal"
    assert meta["_be_close_telemetry"]["outcome"] == "limit_filled"
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
@pytest.mark.parametrize(
    "overnight,gap,quote_loss,fill_loss,later_overnight,later_gap,initial_reason,final_reason",
    [
        (True, 3.0, 2.0, 4.0, True, 8.0, "SL_INITIAL", "OVERNIGHT_GAP_STOP"),
        (True, 3.0, 2.0, 4.0, False, 3.0, "SL_INITIAL", "OVERNIGHT_GAP_STOP"),
        (False, 3.0, 2.0, 4.0, True, 3.0, "SL_INITIAL", "SL_INITIAL"),
        (True, 3.0, 4.0, 2.0, True, 3.0, "OVERNIGHT_GAP_STOP", "SL_INITIAL"),
    ],
    ids=["frozen-gap-threshold", "frozen-overnight-true", "frozen-overnight-false",
         "confirmed-fill-recovers-above-gap"],
)
def test_sl_fallback_recovery_uses_confirmed_fill_and_persisted_attribution(
        recovery, monkeypatch, direction, overnight, gap, quote_loss, fill_loss,
        later_overnight, later_gap, initial_reason, final_reason):
    env = recovery
    sign = -1 if direction == "LONG" else 1
    quote_price = 100.0 + sign * quote_loss
    fill_price = 100.0 + sign * fill_loss
    owner = env.add_trade(
        direction=direction,
        entry_time=datetime.fromtimestamp(env.clock.now - 86400, timezone.utc).isoformat(),
        metadata={"stop_loss": quote_price, "horizon": "short"})
    overnight_source = Mock(return_value=overnight)
    horizon_source = Mock(return_value={"overnight_gap_stop_pct": gap})
    monkeypatch.setattr(env.pipeline, "_is_overnight_position", overnight_source)
    monkeypatch.setattr(env.pipeline, "_load_horizon_config", horizon_source)
    env.scheduler.data_router = SimpleNamespace(
        get_batch_quotes=Mock(return_value={owner["symbol"]: {"last": quote_price}}),
        get_quote=Mock(side_effect=AssertionError("batch quote must suffice")))

    # The real monitor must freeze attribution when creating the durable intent,
    # not pre-book the trigger quote or rely on the eventual recovery environment.
    assert env.monitor() == 0
    intents = env.ledger().list_unsettled()
    assert len(intents) == 1
    intent_id = intents[0]["intent_id"]
    decision = intents[0]["decision"]
    assert decision["reason"] == initial_reason
    assert decision["reference_price"] == quote_price
    assert decision["sl_fallback_attribution"] == {"overnight": overnight, "gap_stop_pct": gap}
    assert env.rows()[0]["exit_time"] is None
    assert env.callback_receipts == []
    overnight_source.assert_called_once_with(owner["entry_time"])
    horizon_source.assert_called_once_with("short")
    env.scheduler.data_router.get_quote.assert_not_called()

    # Configuration/day rollover after submission cannot rewrite the snapshot.
    overnight_source.reset_mock()
    horizon_source.reset_mock()
    overnight_source.return_value = later_overnight
    horizon_source.return_value = {"overnight_gap_stop_pct": later_gap}
    env.clock.now += 86400
    env.rebuild()
    env.scheduler.data_router = None
    env.sim.fill(price=fill_price)
    assert env.monitor() == 1
    row = assert_booked(env, intent_id, price=fill_price)
    assert row["exit_reason"] == final_reason
    assert row["realized_pnl"] == pytest.approx(-fill_loss * 10)
    assert row["pnl_pct"] == pytest.approx(-fill_loss / 100)
    assert env.rows("playbook_instructions")[0]["exit_reason"] == final_reason
    assert env.ledger().get(intent_id)["decision"] == decision
    overnight_source.assert_not_called()
    horizon_source.assert_not_called()
    callbacks = list(env.callback_receipts)
    assert env.monitor() == 0
    assert env.callback_receipts == callbacks
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("reason", ["SL_INITIAL", "TIME_EXIT"])
def test_non_fallback_decision_without_attribution_preserves_original_reason(
        recovery, monkeypatch, reason):
    env = recovery
    result = env.start(env.add_trade(), reason=reason)
    intent_id = result["exit_intent_id"]
    assert "sl_fallback_attribution" not in env.ledger().get(intent_id)["decision"]
    overnight_source = Mock(return_value=True)
    horizon_source = Mock(return_value={"overnight_gap_stop_pct": 3.0})
    monkeypatch.setattr(env.pipeline, "_is_overnight_position", overnight_source)
    monkeypatch.setattr(env.pipeline, "_load_horizon_config", horizon_source)
    env.rebuild()
    env.sim.fill(price=95.0)
    assert env.monitor() == 1
    row = assert_booked(env, intent_id, price=95.0)
    assert row["exit_reason"] == reason
    assert env.rows("playbook_instructions")[0]["exit_reason"] == reason
    overnight_source.assert_not_called()
    horizon_source.assert_not_called()
    assert len(env.sim.orders) == 1


@pytest.mark.parametrize("bad_trade_id", [3, 2, 1], ids=["first-row", "middle-row", "last-row"])
@pytest.mark.parametrize("fault", ["metadata", "entry-price", "broker", "bookkeeping"])
def test_eod_row_failure_preserves_other_closes_and_recoverable_evidence(
        recovery, monkeypatch, bad_trade_id, fault):
    env = recovery
    # EOD imports these pure helpers even for cohort A. Load their real code
    # beneath the existing pre-import guards; only exemption policy is a stub.
    storage = ModuleType("app.storage")
    storage.__path__ = []
    monkeypatch.setitem(sys.modules, "app.storage", storage)
    for name, path in (
        ("app.utils.timezone", "app/utils/timezone.py"),
        ("app.storage.trade_store", "app/storage/trade_store.py"),
        ("app.playbook.overnight_decision", "app/playbook/overnight_decision.py"),
        ("app.playbook.exit_engine", "app/playbook/exit_engine.py"),
    ):
        env.load(name, path)
    monkeypatch.setattr(sys.modules["app.playbook.overnight_decision"],
                        "overnight_eod_decision", Mock(return_value=(False, None)))
    env.sim.fill_market = True
    env.scheduler.data_router = SimpleNamespace(get_quote=Mock(return_value={"last": 105.0}))
    owners = {iid: env.add_trade(iid=iid, symbol=f"US.TEST_EOD_{iid}") for iid in (1, 2, 3)}
    bad_symbol = owners[bad_trade_id]["symbol"]
    healthy_ids = [iid for iid in (1, 2, 3) if iid != bad_trade_id]
    if fault in ("metadata", "entry-price"):
        with env.connect() as conn:
            if fault == "metadata":
                conn.execute("UPDATE trade_records SET metadata_json='[]' WHERE id=?", (bad_trade_id,))
            else:
                conn.execute("UPDATE trade_records SET entry_price='invalid-price' WHERE id=?", (bad_trade_id,))
    bad_before = next(row for row in env.rows() if row["id"] == bad_trade_id)

    real_close = env.broker.broker.close_position

    def close_with_fault(symbol, *args, **kwargs):
        if fault == "broker" and symbol == bad_symbol:
            raise RuntimeError("test broker close failure")
        return real_close(symbol, *args, **kwargs)

    close_calls = Mock(side_effect=close_with_fault)
    monkeypatch.setattr(env.broker.broker, "close_position", close_calls)
    real_book = env.pipeline._close_trade_record
    booking_connections = []
    booking_transaction_states = []
    dirty_writes = []
    dirty_metadata = None
    if fault == "bookkeeping":
        dirty_meta = json.loads(bad_before["metadata_json"])
        dirty_meta["_eod_uncommitted_test"] = True
        dirty_metadata = json.dumps(dirty_meta)

        def book_with_fault(conn, trade_id, *args, **kwargs):
            booking_connections.append(conn)
            booking_transaction_states.append(conn.in_transaction)
            if trade_id == bad_trade_id:
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (dirty_metadata, trade_id))
                dirty_writes.append((conn.in_transaction, conn.execute(
                    "SELECT metadata_json FROM trade_records WHERE id=?", (trade_id,)).fetchone()[0]))
                raise RuntimeError("test bookkeeping failure after uncommitted write")
            return real_book(conn, trade_id, *args, **kwargs)

        monkeypatch.setattr(env.pipeline, "_close_trade_record", book_with_fault)

    # ORDER BY id DESC makes the failed row first, middle, or last. The real
    # loop must keep both prior successes and later progress, not return zero.
    assert env.eod() == 2
    expected_close_ids = [iid for iid in (3, 2, 1)
                          if iid != bad_trade_id or fault in ("broker", "bookkeeping")]
    assert [call.args[0] for call in close_calls.call_args_list] == [
        owners[iid]["symbol"] for iid in expected_close_ids]
    assert len(env.sim.orders) == (3 if fault == "bookkeeping" else 2)
    rows = {row["id"]: row for row in env.rows()}
    for iid in healthy_ids:
        intent_id = json.loads(rows[iid]["metadata_json"])["exit_intent_id"]
        row = assert_booked(env, intent_id, trade_id=iid)
        assert row["exit_reason"] == "EOD_FORCE_CLOSE"
        assert row["realized_pnl"] == 50.0
    assert rows[bad_trade_id]["exit_time"] is None
    assert rows[bad_trade_id]["exit_price"] is None
    assert [row["symbol"] for row in env.rows("open_positions")] == [bad_symbol]
    env.alerts.assert_called_once()
    assert env.alerts.call_args.args[:3] == (bad_symbol, "sim", "EXIT_ROW_ERROR")
    assert f"trade_id={bad_trade_id}" in env.alerts.call_args.args[3]
    if fault == "broker":
        bad_meta = json.loads(rows[bad_trade_id]["metadata_json"])
        assert bad_meta["eod_close_fail_count"] == 1
        assert bad_meta["eod_close_fail_date"] == env.pipeline._today_et_str()
        assert all("eod_close_fail_count" not in json.loads(rows[iid]["metadata_json"])
                   for iid in healthy_ids)
    else:
        assert rows[bad_trade_id]["metadata_json"] == bad_before["metadata_json"]

    pending = env.ledger().list_unsettled()
    if fault == "bookkeeping":
        # The same connection must be clean again before another row books.
        assert len(booking_connections) == 3
        assert len({id(conn) for conn in booking_connections}) == 1
        assert booking_transaction_states == [False, False, False]
        assert dirty_writes == [(True, dirty_metadata)]
        assert len(pending) == 1
        intent = pending[0]
        assert intent["owner"]["trade_id"] == bad_trade_id
        assert intent["state"] == "FILLED_UNBOOKED"
        assert intent["result"]["order_filled"] is True
        assert intent["result"]["filled_price"] == 105.0
        assert intent["result"]["dealt_qty"] == 10
        assert intent["receipt"] is None
        healthy_before = {iid: copy.deepcopy(rows[iid]) for iid in healthy_ids}
        orders_before = copy.deepcopy(env.sim.orders)
        calls_before = close_calls.call_count
        monkeypatch.setattr(env.pipeline, "_close_trade_record", real_book)
        assert env.eod() == 1
        assert_booked(env, intent["intent_id"], trade_id=bad_trade_id)
        assert {row["id"]: row for row in env.rows() if row["id"] in healthy_ids} == healthy_before
        assert env.sim.orders == orders_before
        assert close_calls.call_count == calls_before
        assert env.ledger().list_unsettled() == []
        callbacks = list(env.callback_receipts)
        assert env.eod() == 0
        assert env.callback_receipts == callbacks
        assert env.sim.orders == orders_before
    else:
        assert pending == []
