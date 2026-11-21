"""Focused final-quantity service and EXT wiring, with sealed SDK/temp evidence."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.risk import sim_entry_budget as budget
from tests.risk.test_sim_entry_budget import (
    sealed_budget_io, admission, add_book, preflight, receipt,
)
from tests.playbook.test_exit_compensation import compensation_case


def test_service_final_post_gap_quantity_guard_send_and_book(compensation_case,monkeypatch,tmp_path):
    case=compensation_case
    from app.playbook import playbook_service as service
    monkeypatch.setattr(budget.core_paths,"get_data_dir",lambda:tmp_path)
    case.failure="none"
    from app.playbook import config
    config_values={"symbol_daily_loss_gate_enabled":False,"duplicate_position_guard_enabled":False,
        "pretrade_quote_gate_enabled":False,"entry_freshness_gate_enabled":False,
        "gap_dive_halve_enabled":True,"gap_dive_halve_pct":-3,"entry_confirm_max_wait_s":0}
    monkeypatch.setattr(config,"get_playbook_config",lambda:config_values)
    monkeypatch.setattr(service,"_private_router_quote",lambda symbol:{
        "price":100,"bid":99.99,"ask":100.01,"prev_close":100,"open":95})
    seen=[]
    def check(owner,**kwargs):
        seen.append(kwargs)
        assert kwargs["quantity"] == 5
        row=case.conn.execute("SELECT execution_state FROM playbook_instructions WHERE id=?",(case.iid,)).fetchone()
        assert row[0] == "PENDING"
        return {"allowed":True,"quantity":5,"account_id":"SIM-1","source":"playbook",
            "symbol":"US.COMP","signed_qty":5,"stop_loss":95,"reference_price":100}
    monkeypatch.setattr(budget,"sim_entry_preflight",check)
    def submit(order,**kwargs):
        assert budget._guard().marker()["signed_qty"] == order.quantity == 5
        return {"success":True,"submit_state":"ACCEPTED","futu_order_id":"ENTRY-17",
            "broker_order_status":"FILLED_ALL","filled_price":100.25,"filled_qty":5}
    case.broker.submit_order.side_effect=submit
    def finish(owner,**kwargs):
        row=case.conn.execute("SELECT execution_state FROM playbook_instructions WHERE id=?",(case.iid,)).fetchone()
        assert row[0] == "BOOKED"
        assert budget._guard().marker()["order_id"] == "ENTRY-17"
        budget._guard().clear_marker()
        return True
    monkeypatch.setattr(budget,"finish_sim_submission",finish)
    result=case.service.execute_instruction(case.iid,acknowledge_risk=True,target_env="sim")
    assert result["success"]
    assert len(seen) == 1
    case.broker.submit_order.assert_called_once()
    assert not (tmp_path/"locks"/"sim-entry-account.json").exists()


def test_service_budget_deferral_is_before_claim_no_terminal_poison(compensation_case,monkeypatch,tmp_path):
    case=compensation_case
    monkeypatch.setattr(budget.core_paths,"get_data_dir",lambda:tmp_path)
    monkeypatch.setattr(budget,"sim_entry_preflight",lambda *a,**k:budget.defer("insufficient"))
    result=case.service.execute_instruction(case.iid,acknowledge_risk=True,target_env="sim")
    assert result["deferred"]
    row=case.conn.execute("SELECT execution_state,executed,skip_reason FROM playbook_instructions WHERE id=?",(case.iid,)).fetchone()
    assert row[0] == "PENDING" and row[1] == 0 and not row[2]
    case.broker.submit_order.assert_not_called()


def test_service_live_does_not_use_sim_budget(compensation_case,monkeypatch,tmp_path):
    case=compensation_case
    monkeypatch.setattr(budget.core_paths,"get_data_dir",lambda:tmp_path)
    check=MagicMock(side_effect=AssertionError("LIVE must not use SIM checker"))
    monkeypatch.setattr(budget,"sim_entry_preflight",check)
    case.broker.submit_order.return_value["filled_qty"]=1
    case.service.execute_instruction(case.iid,acknowledge_risk=True,target_env="real")
    check.assert_not_called()
    case.broker.submit_order.assert_called_once()
    assert not (tmp_path/"locks").exists()


def ext_setup(monkeypatch,state):
    import scripts.extended_hours_entry_executor as ext
    monkeypatch.setattr(ext,"DB",str(state.path))
    monkeypatch.setattr(ext,"_mode",lambda:SimpleNamespace(is_live=False,
        account_id=lambda:"SIM-1",futu_trd_env=lambda:"sim"))
    monkeypatch.setattr(ext,"_reconcile_broker_entry_orders",lambda *a,**k:([],[]))
    monkeypatch.setattr(ext,"_probe_env_only",lambda:False)
    monkeypatch.setattr(ext,"_prior_move_context",lambda *a,**k:{})
    for name in ("_audit","_push","_push_filled"):
        monkeypatch.setattr(ext,name,lambda *a,**k:None)
    context=SimpleNamespace(close=MagicMock())
    def place(**kwargs):
        marker=budget._guard().marker()
        assert marker["signed_qty"] == kwargs["qty"]
        assert marker["source"] == "extended_hours_entry"
        state.sent=kwargs
        return 0,pd.DataFrame([{"order_id":"OID"}])
    context.place_order=MagicMock(side_effect=place)
    monkeypatch.setattr(ext,"_open_trade_context",lambda:context)
    def filled(*args):
        qty=state.sent["qty"]
        state.snapshot["orders"]=[receipt(qty=qty,dealt=qty)]
        return {"cancel_ok":True,"filled":True,"filled_qty":qty,"avg_fill_price":100.25,"status":"FILLED_ALL"}
    monkeypatch.setattr(ext,"wait_order_fill",filled)
    def write(symbol,qty,*args,**kwargs):
        add_book(state,qty=qty)
        return "position"
    monkeypatch.setattr(ext,"_write_open_position",write)
    return ext,context


def test_ext_full_send_fill_booking_uses_shared_marker(admission,monkeypatch):
    ext,context=ext_setup(monkeypatch,admission)
    result=ext._execute("US.A",10,100,"premarket",2,100000,{},False)
    assert result["ok"] and result["sim_budget_marker_cleared"]
    context.place_order.assert_called_once()
    assert admission.sent["remark"] == "ext_entry"
    assert result["initial_sl"] == round(100.25*(1+ext.STOP_PCT/100),2)
    context.close.assert_called_once()


def test_ext_daily_half_is_final_quantity_and_does_not_change_filled_stop(admission,monkeypatch):
    import sqlite3
    from app.utils.timezone import now_et
    with sqlite3.connect(admission.path) as conn:
        conn.execute("INSERT INTO trade_records(id,exit_time,exit_price,pnl_net) VALUES(9,?,100,-1000)",(now_et().isoformat(),))
    admission.snapshot["account"]["total_assets"]=100000
    ext,context=ext_setup(monkeypatch,admission)
    result=ext._execute("US.A",10,100,"premarket",2,100000,{},False)
    assert result["qty"] == admission.sent["qty"] == 5
    assert result["initial_sl"] == round(100.25*(1+ext.STOP_PCT/100),2)
    assert result["sim_budget_marker_cleared"]


def test_ext_insufficient_budget_defers_before_sdk_send(admission,monkeypatch):
    ext,context=ext_setup(monkeypatch,admission)
    admission.snapshot["account"]["total_assets"]=94999
    result=ext._execute("US.A",10,100,"premarket",2,100000,{},False)
    assert result["deferred"]
    context.place_order.assert_not_called()
    assert "skip_reason" not in result


def test_ext_sdk_error_no_id_is_not_definitive_rejection(admission,monkeypatch):
    ext,context=ext_setup(monkeypatch,admission)
    context.place_order.side_effect=lambda **kwargs:(-1,"SDK uncertain")
    result=ext._execute("US.A",10,100,"premarket",2,100000,{},False)
    assert not result["ok"]
    with budget.sim_entry_account_guard() as guard:
        assert guard.marker() is not None
        assert preflight(admission,"US.B")["reason"] == "submission_identity_unknown_manual_recovery"


def test_ext_process_failure_after_send_retains_account_slot(admission,monkeypatch):
    ext,context=ext_setup(monkeypatch,admission)
    def crash(*args,**kwargs):
        raise RuntimeError("simulated process death after SDK send")
    monkeypatch.setattr(ext,"wait_order_fill",crash)
    with pytest.raises(RuntimeError,match="process death"):
        ext._execute("US.A",10,100,"premarket",2,100000,{},False)
    with budget.sim_entry_account_guard() as guard:
        assert guard.marker()["order_id"] == "OID"
    context.close.assert_called_once()


@pytest.mark.parametrize("extra", [{},{"buy_order_id":"OID"},{"submit_state":"UNKNOWN"},
    {"filled_qty":1},{"filled_qty":float("nan")},{"submission_not_sent":False}])
def test_ext_only_explicit_unsent_defer_releases_current_attempt(admission,monkeypatch,extra):
    import json
    ext,_=ext_setup(monkeypatch,admission)
    monkeypatch.setattr(ext,"STATE",admission.path.parent/"entry-state.json")
    state={"attempts":{"key":{"status":"reserved"},"other":{"status":"opened"}},"opened_today":{"day":1}}
    results=[]
    result={**budget.defer("account_busy"),**extra}
    released=ext._release_sim_presend_attempt(state,state["attempts"],"key",result,results)
    assert released is (not extra)
    assert state["opened_today"] == {"day":1}
    assert state["attempts"]["other"] == {"status":"opened"}
    if released:
        assert "key" not in state["attempts"] and results == [result]
        assert json.loads(ext.STATE.read_text()) == state
    else:
        assert state["attempts"]["key"]["status"] == "reserved" and not results
        assert not ext.STATE.exists()


def test_ext_research_budget_defer_remains_retryable_on_next_collect(admission,monkeypatch):
    import json
    ext,context=ext_setup(monkeypatch,admission)
    monkeypatch.setattr(ext,"STATE",admission.path.parent/"entry-state.json")
    monkeypatch.setattr(ext,"_held_symbols",lambda:set())
    monkeypatch.setattr(ext,"_open_exposure_gate",lambda:None)
    monkeypatch.setattr(ext,"_research_downstream_gates",lambda *a,**k:(10,100,1000,"B",{},{}))
    monkeypatch.setattr(ext,"_record_research_funnel",lambda *a,**k:None)
    monkeypatch.setattr(ext,"_is_late_premarket",lambda *a:False)
    admission.snapshot["account"]["total_assets"]=94999
    state={"attempts":{},"opened_today":{}}
    pool=[{"symbol":"US.A","px":100,"chg":2,"vol":100000,"_source":"test"}]
    for _ in range(2):
        results=[]
        counters=ext._research_pass_real(pool,"premarket","2026-11-21",None,
            state,state["attempts"],state["opened_today"],"day",0,0,10,results,[],{})
        assert counters == (0,0)
        assert len(results) == 1 and results[0]["sim_entry_budget_deferred"]
        assert state["attempts"] == {} and state["opened_today"] == {}
    assert json.loads(ext.STATE.read_text()) == state
    context.place_order.assert_not_called()
    import inspect
    assert "_release_sim_presend_attempt(state, attempts, attempt_key, result, results)" in inspect.getsource(ext.collect)


def test_service_fsync_failure_defers_and_unlocks_proven_unsent_claim(compensation_case,monkeypatch,tmp_path):
    case=compensation_case
    monkeypatch.setattr(budget.core_paths,"get_data_dir",lambda:tmp_path)
    monkeypatch.setattr(budget,"sim_entry_preflight",lambda *a,**k:{"allowed":True,
        "account_id":"SIM-1","source":"playbook","symbol":"US.COMP","signed_qty":10,
        "stop_loss":95,"reference_price":100,"quantity":10})
    original=budget.SimAccountGuard._sync_directory
    calls=[]
    def first_fsync_fails(guard):
        calls.append(True)
        if len(calls)==1:
            raise OSError("presend fsync failed")
        return original(guard)
    monkeypatch.setattr(budget.SimAccountGuard,"_sync_directory",first_fsync_fails)
    result=case.service.execute_instruction(case.iid,acknowledge_risk=True,target_env="sim")
    assert result["sim_entry_budget_deferred"] and result["submission_not_sent"]
    case.broker.submit_order.assert_not_called()
    row=case.conn.execute("SELECT execution_state,executed,skip_reason FROM playbook_instructions WHERE id=?",(case.iid,)).fetchone()
    assert row[0] == "PENDING" and row[1] == 0 and not row[2]
    assert not (tmp_path/"locks"/"sim-entry-account.json").exists()
