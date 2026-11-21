"""SIM finite-budget and crash-serialization contracts: temp files, no SDK/network."""
import copy
import json
import multiprocessing
from pathlib import Path
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from app.risk import sim_entry_budget as budget
from app.playbook import entry_data_fetch

_REAL_BUDGET_FETCH = entry_data_fetch.fetch_entry_sim_budget_snapshot


@pytest.fixture(autouse=True)
def sealed_budget_io(monkeypatch, tmp_path):
    original = sqlite3.connect

    def connect(path, *args, **kwargs):
        raw = str(path).removeprefix("file:").split("?", 1)[0]
        assert raw == ":memory:" or Path(raw).resolve().is_relative_to(tmp_path)
        return original(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network/SDK is forbidden")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import futu
    monkeypatch.setattr(futu, "OpenQuoteContext", forbidden)
    monkeypatch.setattr(futu, "OpenSecTradeContext", forbidden)
    monkeypatch.setattr(budget.core_paths, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(budget.core_paths, "get_trade_records_db_for_arm", lambda env: tmp_path / "books.sqlite")
    from app.playbook import config
    monkeypatch.setattr(config, "get_playbook_config", lambda: SimpleNamespace(get=lambda key, default=None: default))
    from app.risk import account_circuit_breaker
    monkeypatch.setattr(account_circuit_breaker, "get_account_circuit_breaker", lambda: SimpleNamespace(
        reduce_threshold=.01, halt_threshold=.02, liquidate_threshold=.03))
    monkeypatch.setattr(account_circuit_breaker, "get_peak_drawdown_threshold", lambda env: .05)
    return tmp_path


def candidate(symbol="US.A", qty=10, direction="LONG", entry=100, stop=95):
    return {"symbol": symbol, "quantity": qty, "direction": direction,
            "entry_price": entry, "metadata_json": {"stop_loss": stop}}


def quote(bid=99.9, ask=100.1):
    return {"bid": bid, "ask": ask, "timestamp": time.time(), "source": "futu_snapshot"}


def evaluate(trades=None, item=None, equity=96894.392, peak=99999.496, **kwargs):
    trades = trades or []
    item = item or candidate()
    quotes = {row["symbol"]: quote() for row in trades + [item]}
    return budget.evaluate_budget(equity=equity, peak=peak, threshold=.05,
        trades=trades, candidate=item, quotes=kwargs.pop("quotes", quotes), **kwargs)


def seed_db(path, peak=99999.496):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE capital_state(peak_equity REAL,trade_env INTEGER);
            CREATE TABLE trade_records(id INTEGER PRIMARY KEY,symbol TEXT,direction TEXT,
              quantity REAL,entry_price REAL,source_type TEXT,metadata_json TEXT,
              futu_order_id TEXT,exit_time TEXT,exit_price REAL,pnl_net REAL,realized_pnl REAL);
            CREATE TABLE open_positions(symbol TEXT,direction TEXT,size REAL,account_type TEXT);
            CREATE TABLE playbook_instructions(id INTEGER PRIMARY KEY,symbol TEXT,
              execution_state TEXT,broker_env TEXT,broker_order_id TEXT);
        """)
        conn.execute("INSERT INTO capital_state VALUES(?,1)", (peak,))
    conn.close()


def snapshot(positions=None, orders=None, equity=96894.392):
    return {"account": {"_target_env": "sim", "currency": "USD", "total_assets": equity},
            "account_id": "SIM-1", "positions": {"success": True, "positions": positions or []},
            "orders": orders or [], "quotes": {"US.A": quote(), "US.B": quote()}, "read_at": time.time()}


@pytest.fixture
def admission(monkeypatch, tmp_path):
    path = tmp_path / "books.sqlite"
    seed_db(path)
    state = SimpleNamespace(snapshot=snapshot(), owner=SimpleNamespace(), path=path)
    monkeypatch.setattr(entry_data_fetch, "fetch_entry_sim_budget_snapshot", lambda *a, **k: state.snapshot)
    return state


def preflight(state, symbol="US.A", **kwargs):
    state.snapshot["read_at"] = time.time()
    return budget.sim_entry_preflight(state.owner, source="playbook", symbol=symbol,
        quantity=kwargs.pop("quantity", 10), entry_price=100, stop_loss=95,
        direction="LONG", db_path=state.path, **kwargs)


def add_book(state, qty=10, side="LONG", order_id="OID"):
    with sqlite3.connect(state.path) as conn:
        conn.execute("INSERT INTO trade_records VALUES(1,'US.A',?,?,100,'PLAYBOOK_SIM',? ,?,NULL,NULL,NULL,NULL)",
                     (side, qty, json.dumps({"stop_loss": 95}), order_id))
        conn.execute("INSERT INTO open_positions VALUES('US.A',?,?,'simulate')", (side, qty))
    state.snapshot["positions"]["positions"] = [{"code": "US.A", "qty": qty if side == "LONG" else -qty}]


def receipt(status="FILLED_ALL", qty=10, dealt=10, oid="OID"):
    return {"order_id": oid, "code": "US.A", "qty": qty, "trd_side": "BUY",
            "order_status": status, "dealt_qty": dealt, "remark": "pb_1_sim_1"}


def test_true_historical_peak_formula_1894_8708():
    result = evaluate()
    assert result["remaining_budget_usd"] == pytest.approx(1894.8708)
    assert result["allowed"]
    assert result["required_risk_usd"] > 50


@pytest.mark.parametrize("direction,current,stop,expected", [("LONG", 120, 98, 24), ("SHORT", 80, 102, 24)])
def test_giveback_uses_initial_cooldown_envelope_not_stale_effective_stop(direction,current,stop,expected):
    row = candidate(qty=1, direction=direction, stop=stop)
    row["metadata_json"]["_effective_sl_observed"] = current
    result = evaluate(trades=[row], item=candidate("US.B", 1),
        quotes={"US.A": quote(current, current), "US.B": quote()}, cushion_pct=0)
    assert result["open_risk_usd"] >= expected


@pytest.mark.parametrize("direction,bid,ask,stop,distance", [
    ("LONG", 90, 110, 90, 20),
    ("LONG", 89, 91, 90, 1),
    ("LONG", 88, 90, 90, 0),
    ("LONG", 88, 89, 90, 0),
    ("SHORT", 90, 110, 110, 20),
    ("SHORT", 109, 111, 110, 1),
    ("SHORT", 110, 112, 110, 0),
    ("SHORT", 111, 112, 110, 0),
])
def test_open_risk_reserves_full_quote_interval_mark_to_stop(direction, bid, ask, stop, distance):
    row = candidate(qty=1, direction=direction, stop=stop)
    result = evaluate(trades=[row], item=candidate("US.B", 1),
        quotes={"US.A": quote(bid, ask), "US.B": quote()}, cushion_pct=0)
    fee = budget.estimate_fee(1, stop, "SELL" if direction == "LONG" else "BUY")["total"]
    assert result["open_risk_usd"] == pytest.approx(distance + fee)
    for mark in (bid, (bid + ask) / 2, ask):
        mark_distance = max(mark - stop if direction == "LONG" else stop - mark, 0)
        assert result["open_risk_usd"] >= mark_distance + fee


@pytest.mark.parametrize("direction,bid,ask,current", [
    ("LONG", 90, 110, 110), ("SHORT", 90, 110, 90),
    ("LONG", 88, 89, 100), ("SHORT", 111, 112, 100),
])
def test_candidate_reserves_entry_side_and_order_price_envelope(direction, bid, ask, current):
    stop = 95 if direction == "LONG" else 105
    result = evaluate(item=candidate(qty=1, direction=direction, stop=stop),
        quotes={"US.A": quote(bid, ask)})
    distance = max(current - stop if direction == "LONG" else stop - current, 0)
    fee = budget.estimate_round_trip(1, current, stop, direction)["total"]
    assert result["candidate_risk_usd"] == pytest.approx(distance + current * .002 + fee)


@pytest.mark.parametrize("field,value", [("quantity",0),("quantity",float("nan")),
    ("entry_price",float("inf")),("entry_price",0),("quantity",1.5)])
def test_missing_nan_or_fractional_candidate_fails_closed(field,value):
    row = candidate()
    row[field] = value
    with pytest.raises(budget.BudgetUnavailable):
        evaluate(item=row)


def test_missing_stop_even_if_observed_effective_stop_fails_closed():
    row = candidate()
    row["metadata_json"] = {"_effective_sl_observed":99}
    with pytest.raises(budget.BudgetUnavailable, match="initial_stop"):
        evaluate(trades=[row])


@pytest.mark.parametrize("change", [{"timestamp":None},{"timestamp":float("nan")},
    {"timestamp":time.time()-120},{"timestamp":time.time()+100},
    {"bid":float("nan")},{"ask":0},{"source":"finnhub"}])
def test_missing_invalid_stale_or_delayed_quotes_fail_closed(change):
    bad = {**quote(), **change}
    with pytest.raises(budget.BudgetUnavailable):
        evaluate(quotes={"US.A":bad})


def test_one_share_cannot_be_forced_with_insufficient_remaining_budget():
    result = evaluate(item=candidate(qty=1), equity=95000, peak=100000)
    assert not result["allowed"]
    assert result["candidate_risk_usd"] > 0


def test_long_short_risks_are_not_netted():
    rows = [candidate("US.A",1,"LONG"),candidate("US.B",1,"SHORT",stop=105)]
    result = evaluate(trades=rows,item=candidate("US.C",1),quotes={s:quote() for s in ("US.A","US.B","US.C")})
    assert result["open_risk_usd"] > 10


def test_new_high_equity_uses_high_not_reset_lower_stored_peak():
    result = evaluate(equity=110000,peak=100000)
    assert result["peak_equity"] == 110000
    assert result["remaining_budget_usd"] == 5500


def test_without_account_guard_preflight_defers_without_send_or_marker(admission):
    result = preflight(admission)
    assert result["deferred"] and "account_lock_not_held" in result["reason"]


def test_golden_preflight_is_read_only_and_final_quantity_preserved(admission):
    before = admission.path.read_bytes()
    with budget.sim_entry_account_guard() as guard:
        result = preflight(admission,quantity=3)
        assert result["allowed"] and result["quantity"] == 3
        assert guard.marker() is None
    assert admission.path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["equity_nan","env_real","missing_equity","stale_account","position_mismatch","unknown_order"])
def test_account_truth_failures_are_transient_before_claim(admission,mutation):
    state = admission.snapshot
    if mutation == "equity_nan": state["account"]["total_assets"] = float("nan")
    elif mutation == "env_real": state["account"]["_target_env"] = "real"
    elif mutation == "missing_equity": state["account"].pop("total_assets")
    elif mutation == "position_mismatch": state["positions"]["positions"] = [{"code":"US.B","qty":1}]
    elif mutation == "unknown_order": state["orders"] = [{"order_status":"SUBMITTED"}]
    with budget.sim_entry_account_guard():
        if mutation == "stale_account":
            state["read_at"] = time.time()-10
            result = budget.sim_entry_preflight(admission.owner,source="playbook",symbol="US.A",
                quantity=10,entry_price=100,stop_loss=95,direction="LONG",db_path=admission.path)
        else: result = preflight(admission)
        assert result["deferred"]
        assert "skip_reason" not in result


@pytest.mark.parametrize("state", ["UNKNOWN","SUBMITTED","FILLED_UNBOOKED","LEGACY_LOCKED"])
def test_any_other_symbol_unresolved_entry_blocks_account(admission,state):
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO playbook_instructions VALUES(7,'US.B',?,'sim','')",(state,))
    with budget.sim_entry_account_guard():
        assert preflight(admission)["reason"] == "account_unresolved_entry"


def test_live_unresolved_entry_not_used_as_sim_gate(admission):
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO playbook_instructions VALUES(7,'US.B','UNKNOWN','real','')")
    with budget.sim_entry_account_guard():
        assert preflight(admission)["allowed"]


@pytest.mark.parametrize("pnl", [-1000,-2000,-3000])
def test_original_daily_reduce_halt_liquidate_thresholds(admission,pnl):
    from app.utils.timezone import now_et
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO trade_records(id,exit_time,exit_price,pnl_net) VALUES(9,?,100,?)",
                     (now_et().isoformat(),pnl))
    admission.snapshot["account"]["total_assets"] = 100000
    with budget.sim_entry_account_guard():
        result = preflight(admission,apply_daily_reduce=True)
        if pnl == -1000:
            assert result["allowed"] and result["quantity"] == 5
        else:
            assert result["reason"] == "account_daily_halt"


def test_daily_reduce_never_forces_one_share(admission):
    from app.utils.timezone import now_et
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO trade_records(id,exit_time,exit_price,pnl_net) VALUES(9,?,100,-1000)",
                     (now_et().isoformat(),))
    admission.snapshot["account"]["total_assets"] = 100000
    with budget.sim_entry_account_guard():
        assert preflight(admission,quantity=1,apply_daily_reduce=True)["reason"] == "daily_reduce_below_one_share"
        assert preflight(admission,quantity=10)["reason"] == "daily_reduce_changed_retry"
        assert preflight(admission,quantity=5,reduce_applied=True)["quantity"] == 5


def test_cross_symbol_threads_only_one_account_owner(admission):
    result=[]
    with budget.sim_entry_account_guard() as guard:
        assert guard.acquired
        def compete():
            with budget.sim_entry_account_guard() as other:
                result.append(other.acquired)
        worker=threading.Thread(target=compete)
        worker.start(); worker.join()
    assert result == [False]


def _process_compete(directory,connection):
    budget.core_paths.get_data_dir=lambda:Path(directory)
    # Spawn starts a separate unlocked Python process, exercising actual flock.
    with budget.sim_entry_account_guard() as guard:
        connection.send(guard.acquired)
    connection.close()


def test_account_flock_serializes_different_processes(admission):
    ctx=multiprocessing.get_context("spawn")
    parent,child=ctx.Pipe()
    with budget.sim_entry_account_guard():
        process=ctx.Process(target=_process_compete,args=(str(admission.path.parent),child))
        process.start()
        assert parent.poll(10)
        assert parent.recv() is False
        process.join(10)
        assert process.exitcode == 0


def test_crash_unknown_no_id_marker_retains_until_identity_evidence(admission):
    with budget.sim_entry_account_guard() as guard:
        result=preflight(admission)
        budget.begin_sim_submission(result)
        marker=guard.marker()
        assert marker["env"] == "sim" and marker["signed_qty"] == 10
    with budget.sim_entry_account_guard() as guard:
        admission.snapshot["orders"]=[]
        result=preflight(admission,"US.B")
        assert result["reason"] == "submission_identity_unknown_manual_recovery"
        assert guard.marker()["identity"] == marker["identity"]


def test_definitive_zero_rejection_only_release(admission):
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission))
        budget.reject_sim_submission(submit_state="UNKNOWN",dealt_qty=0)
        assert guard.marker() is not None
        budget.reject_sim_submission(submit_state="DEFINITIVE_REJECT",dealt_qty=1)
        assert guard.marker() is not None
        budget.reject_sim_submission(submit_state="DEFINITIVE_REJECT",dealt_qty=0)
        assert guard.marker() is None


@pytest.mark.parametrize("status,dealt,book", [("SUBMITTED",0,False),("FILLED_PART",3,True),
    ("FILLED_ALL",10,False),("CANCELLED_PART",3,False)])
def test_accepted_partial_unbooked_marker_never_releases(status,dealt,book,admission):
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission))
        budget.observe_sim_submission("OID")
        admission.snapshot["orders"]=[receipt(status,dealt=dealt)]
        if book: add_book(admission,qty=dealt)
        assert not budget.finish_sim_submission(admission.owner,db_path=admission.path)
        assert guard.marker() is not None


@pytest.mark.parametrize("status,dealt", [("FILLED_ALL",10),("CANCELLED_PART",3)])
def test_exact_terminal_fill_and_booking_clear_marker(admission,status,dealt):
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission),client_order_id="pb_1_sim_1")
        budget.observe_sim_submission("OID")
        add_book(admission,qty=dealt)
        admission.snapshot["orders"]=[receipt(status,dealt=dealt)]
        assert budget.finish_sim_submission(admission.owner,db_path=admission.path)
        assert guard.marker() is None


def test_exact_terminal_zero_receipt_clears_and_no_missing_order_inference(admission):
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission))
        budget.observe_sim_submission("OID")
        assert not budget.finish_sim_submission(admission.owner,db_path=admission.path)
        admission.snapshot["orders"]=[receipt("CANCELLED_ALL",dealt=0)]
        assert budget.finish_sim_submission(admission.owner,db_path=admission.path)
        assert guard.marker() is None


def test_booking_wrong_signed_quantity_retains_marker(admission):
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission))
        budget.observe_sim_submission("OID")
        add_book(admission,qty=9)
        admission.snapshot["orders"]=[receipt()]
        assert not budget.finish_sim_submission(admission.owner,db_path=admission.path)
        assert guard.marker() is not None


def test_late_fixed_factory_does_not_write_marker_or_accept_result(monkeypatch,admission):
    done=threading.Event(); release=threading.Event()
    def read(symbols,expected_account_id=None,marker_receipt=None):
        release.wait(2); done.set(); return snapshot()
    monkeypatch.setattr(budget,"read_sim_budget_snapshot",read)
    owner=SimpleNamespace()
    with pytest.raises(entry_data_fetch.EntryDataUnavailable,match="timeout"):
        entry_data_fetch.fetch_entry_data(owner,"sim_entry_budget",None,
            entry_data_fetch._SIM_BUDGET_FACTORY_METHOD,["US.A"],timeout_s=.01)
    with pytest.raises(entry_data_fetch.EntryDataUnavailable,match="busy"):
        entry_data_fetch.fetch_entry_data(SimpleNamespace(),"sim_entry_budget",None,
            entry_data_fetch._SIM_BUDGET_FACTORY_METHOD,["US.B"],timeout_s=.01)
    flight=entry_data_fetch._SIM_BUDGET_QUERY_STATE.flight
    release.set(); assert done.wait(2); assert flight.done.wait(2)
    assert not (admission.path.parent/"locks"/"sim-entry-account.json").exists()


def test_live_and_exit_do_not_take_sim_account_lock(admission):
    with budget.sim_entry_account_guard():
        with budget.sim_entry_account_guard("real") as live:
            assert live is None
    import inspect
    from app.execution.dual_broker import DualBrokerAdapter
    assert "sim_entry_budget" not in inspect.getsource(DualBrokerAdapter.close_position)


def test_path_configuration_failure_is_transient_and_releases_thread_lock(monkeypatch):
    def broken_path():
        raise ValueError("unavailable arm directory")
    monkeypatch.setattr(budget.core_paths,"get_data_dir",broken_path)
    for _ in range(2):
        with budget.sim_entry_account_guard() as guard:
            assert not guard.acquired and guard.reason == "account_lock_unavailable"
    assert not budget._THREAD_LOCK.locked()


def test_failed_begin_and_reject_cannot_erase_previous_marker(admission):
    with budget.sim_entry_account_guard() as guard:
        verdict=preflight(admission)
        budget.begin_sim_submission(verdict)
        identity=guard.marker()["identity"]
    with budget.sim_entry_account_guard() as guard:
        with pytest.raises(budget.BudgetUnavailable,match="already_exists"):
            budget.begin_sim_submission(verdict)
        budget.reject_sim_submission(submit_state="DEFINITIVE_REJECT",dealt_qty=0)
        assert guard.marker()["identity"] == identity


def test_pre_send_directory_fsync_failure_releases_only_own_marker(admission,monkeypatch):
    with budget.sim_entry_account_guard() as guard:
        original=guard._sync_directory
        def broken_fsync():
            raise OSError("fsync failure after replace")
        monkeypatch.setattr(guard,"_sync_directory",broken_fsync)
        with pytest.raises(OSError,match="fsync"):
            budget.begin_sim_submission(preflight(admission))
        assert guard.marker()["identity"] == guard.submission_identity
        monkeypatch.setattr(guard,"_sync_directory",original)
        budget.reject_sim_submission(submit_state="DEFINITIVE_REJECT",dealt_qty=0)
        assert guard.marker() is None


@pytest.mark.parametrize("state", ["BOOKED","FILLED_UNBOOKED","PENDING","MISSING"])
def test_closed_trade_receipt_requires_exact_booked_instruction_cas(admission,state):
    from app.utils.timezone import now_et
    with budget.sim_entry_account_guard() as guard:
        budget.begin_sim_submission(preflight(admission),instruction_id=1)
        budget.observe_sim_submission("OID")
        add_book(admission)
        with sqlite3.connect(admission.path) as conn:
            if state != "MISSING":
                conn.execute("INSERT INTO playbook_instructions VALUES(1,'US.A',?,'sim','OID')",(state,))
            conn.execute("UPDATE trade_records SET exit_time=?,exit_price=100,pnl_net=0 WHERE id=1",(now_et().isoformat(),))
            conn.execute("DELETE FROM open_positions")
        admission.snapshot["positions"]["positions"]=[]
        admission.snapshot["orders"]=[receipt()]
        cleared=budget.finish_sim_submission(admission.owner,db_path=admission.path)
        assert cleared is (state == "BOOKED")
        assert (guard.marker() is None) is (state == "BOOKED")


@pytest.mark.parametrize("threshold", [.01,.02,.03])
def test_daily_exact_usd_boundary_is_not_division_rounded(admission,threshold):
    from app.utils.timezone import now_et
    equity=123456.789
    admission.snapshot["account"]["total_assets"]=equity
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO trade_records(id,exit_time,exit_price,pnl_net) VALUES(9,?,100,?)",
                     (now_et().isoformat(),-equity*threshold))
    with budget.sim_entry_account_guard():
        result=preflight(admission,apply_daily_reduce=True)
    if threshold == .01:
        assert result["allowed"] and result["quantity"] == 5
    else:
        assert result["reason"] == "account_daily_halt"


@pytest.mark.parametrize("changed_stop", [True,False])
def test_local_risk_mutation_during_read_defers_but_observation_does_not(admission,monkeypatch,changed_stop):
    add_book(admission)
    def read(*args,**kwargs):
        with sqlite3.connect(admission.path) as conn:
            metadata={"stop_loss":94 if changed_stop else 95,"_effective_sl_observed":110}
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=1",(json.dumps(metadata),))
        return admission.snapshot
    monkeypatch.setattr(entry_data_fetch,"fetch_entry_sim_budget_snapshot",read)
    with budget.sim_entry_account_guard():
        result=preflight(admission,"US.B")
    if changed_stop:
        assert result["reason"] == "local_portfolio_changed_during_read"
    else:
        assert result["allowed"]


def sdk_snapshot_setup(monkeypatch,ready=0,orders=None,history=None,history_ret=0):
    from unittest.mock import MagicMock
    import pandas as pd
    import futu
    from app.execution import dual_broker
    ctx=SimpleNamespace(order_list_query=MagicMock(return_value=(0,pd.DataFrame(orders or []))),
        history_order_list_query=MagicMock(return_value=(history_ret,pd.DataFrame(history or []))))
    dual=SimpleNamespace(_ctx_and_acc=MagicMock(return_value=(ctx,"42","sim",threading.Lock())),
        get_account_info=MagicMock(return_value=snapshot()["account"]),
        query_positions=MagicMock(return_value={"success":True,"positions":[]}))
    get_dual=MagicMock(return_value=dual)
    monkeypatch.setattr(dual_broker,"get_dual_broker",get_dual)
    events=[]
    qctx=SimpleNamespace(set_sync_query_connect_timeout=MagicMock(side_effect=lambda seconds:events.append(("timeout",seconds))),
        get_global_state=MagicMock(side_effect=lambda:(events.append(("ready",ready)) or (ready,{}))),
        get_market_snapshot=MagicMock(side_effect=lambda symbols:(events.append(("snapshot",symbols)) or
            (0,pd.DataFrame([{"code":"US.A","bid_price":99.9,"ask_price":100.1,
                              "update_time":"2026-11-20 15:00:00"}])))),close=MagicMock())
    constructor=MagicMock(return_value=qctx)
    monkeypatch.setattr(futu,"OpenQuoteContext",constructor)
    monkeypatch.setenv("FUTU_OPEND_HOST","sealed-opend")
    monkeypatch.setenv("FUTU_OPEND_PORT","12345")
    return SimpleNamespace(ctx=ctx,dual=dual,get_dual=get_dual,qctx=qctx,constructor=constructor,events=events)


@pytest.mark.parametrize("ready", [0,-1])
def test_fixed_quote_factory_uses_correct_env_async_ready_and_closes(monkeypatch,ready):
    sdk=sdk_snapshot_setup(monkeypatch,ready=ready)
    if ready == 0:
        result=budget.read_sim_budget_snapshot(["US.A"])
        assert result["quotes"]["US.A"]["source_asof"] == "2026-11-20 15:00:00"
        assert sdk.events == [("timeout",3.0),("ready",0),("snapshot",["US.A"])]
    else:
        with pytest.raises(budget.BudgetUnavailable,match="not_ready"):
            budget.read_sim_budget_snapshot(["US.A"])
        sdk.qctx.get_market_snapshot.assert_not_called()
    sdk.constructor.assert_called_once_with(host="sealed-opend",port=12345,is_async_connect=True)
    sdk.dual.get_account_info.assert_called_once_with(target_env="sim")
    sdk.dual.query_positions.assert_called_once_with(target_env="sim",refresh_cache=True)
    sdk.qctx.close.assert_called_once()


@pytest.mark.parametrize("has_oid,history_ret", [(True,0),(False,0),(True,-1)])
def test_history_fallback_is_exact_identity_account_code_and_et_date(monkeypatch,has_oid,history_ret):
    sdk=sdk_snapshot_setup(monkeypatch,history=[receipt(),receipt(oid="OTHER")],history_ret=history_ret)
    marker={"env":"sim","account_id":"42","order_id":"OID" if has_oid else "",
        "client_order_id":"pb_1_sim_1" if not has_oid else None,"symbol":"US.A",
        "created_at":"2026-11-20T23:00:00+00:00"}
    if not has_oid:
        sdk.ctx.history_order_list_query.return_value[1].loc[1,"remark"]="pb_other"
    if history_ret == 0:
        result=budget.read_sim_budget_snapshot(["US.A"],marker_receipt=marker)
        assert [row["order_id"] for row in result["orders"]] == ["OID"]
    else:
        with pytest.raises(budget.BudgetUnavailable,match="history_unavailable"):
            budget.read_sim_budget_snapshot(["US.A"],marker_receipt=marker)
        sdk.constructor.assert_not_called()
    sdk.ctx.history_order_list_query.assert_called_once_with(code="US.A",start="2026-11-20",
        end="2026-11-20",trd_env="sim",acc_id=42)


def test_lazy_broker_constructor_timeout_keeps_global_flight_across_owners(monkeypatch):
    from app.execution import dual_broker
    sdk=sdk_snapshot_setup(monkeypatch)
    entered=threading.Event(); release=threading.Event()
    def lazy_broker():
        entered.set()
        release.wait(2)
        return sdk.dual
    monkeypatch.setattr(dual_broker,"get_dual_broker",lazy_broker)
    assert entry_data_fetch.DEFAULT_ENTRY_DATA_TIMEOUT_S == 3.0
    flight=None
    try:
        with pytest.raises(entry_data_fetch.EntryDataUnavailable,match="timeout"):
            _REAL_BUDGET_FETCH(SimpleNamespace(),["US.A"],timeout_s=.01)
        assert entered.is_set()
        flight=entry_data_fetch._SIM_BUDGET_QUERY_STATE.flight
        assert flight is not None and flight.abandoned
        with pytest.raises(entry_data_fetch.EntryDataUnavailable,match="busy"):
            _REAL_BUDGET_FETCH(SimpleNamespace(),["US.B"],timeout_s=.01)
        sdk.constructor.assert_not_called()
    finally:
        release.set()
        if flight is not None:
            assert flight.done.wait(2)
    assert flight.value is None and flight.error is None
    assert entry_data_fetch._SIM_BUDGET_QUERY_STATE.flight is None
    sdk.qctx.close.assert_called_once()
