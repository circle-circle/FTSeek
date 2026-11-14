"""补偿退出后的入场重建与指令收口契约（离线临时 SQLite）。"""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.playbook import pipeline
from app.playbook.db import PlaybookDB
from app.storage.trade_store import TradeStore


@pytest.fixture
def repair_case(tmp_path, monkeypatch):
    push_hub = SimpleNamespace(push=MagicMock(), push_text=MagicMock())
    monkeypatch.setattr("app.push.get_push_hub", lambda: push_hub)
    monkeypatch.setattr("app.push.trade_exit", lambda **kwargs: kwargs)
    path = tmp_path / "compensation-repair.db"
    store = TradeStore(str(path))  # 创建完整 trade_records/open_positions schema
    store._get_conn().close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    iid = db.save_instruction({
        "date": "2026-11-13", "symbol": "US.XLE", "direction": "BUY",
        "entry_price": 64.32, "stop_loss": 62.3904, "take_profit": 0,
        "position_size": 76, "confidence": 100,
        "decision_id": "MOM-ETF-V1-20261113-US.XLE", "thesis": None,
    })
    attempt = db.try_claim_execution(iid)
    snapshot = {
        "success": True, "order_id": "8369338", "code": "US.XLE", "qty": 76,
        "order_status": "FILLED_ALL", "dealt_qty": 76, "dealt_avg_price": 64.5,
    }
    observation = db.observe_execution_order(iid, attempt, "8369338", "sim", snapshot)
    assert observation["confirmed"] is True
    assert db.mark_execution_filled_unbooked(iid, attempt, "FILLED_ALL")
    owner = {
        "kind": "compensation", "db_path": str(path),
        "close_key": "entry:8369338", "instruction_id": iid,
        "execution_attempt": attempt, "entry_order_id": "8369338",
        "entry_time": "2026-11-13T09:52:09.266814-05:00",
        "entry_price": 64.5, "quantity": 76, "symbol": "US.XLE",
        "direction": "LONG", "source_type": "PLAYBOOK_SIM",
    }
    result = {
        "success": True, "order_filled": True, "exit_state": "FILLED_UNBOOKED",
        "owner": owner,
        "decision": {
            "reason": "ENTRY_BOOKKEEPING_COMPENSATION", "reference_price": 64.5,
            "metadata": {"playbook_instruction_id": iid}, "holding_hours": 0.0,
        },
        "exit_intent_id": "21bd5b51a1c04eebbc5825e2ac93e1ae",
        "filled_price": 64.46, "dealt_qty": 76, "price_source": "broker_deal",
        "broker_qty_before": 76, "broker_qty_after": 0,
        "confirmed_at": 1794581559.465332, "futu_order_id": "8369340",
    }
    dual = SimpleNamespace(acknowledge_close=MagicMock(return_value=True))
    yield SimpleNamespace(path=path, conn=conn, db=db, iid=iid, result=result, dual=dual)
    conn.close()


def test_missing_entry_is_reconstructed_closed_and_instruction_booked(repair_case):
    case = repair_case
    assert pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn) == 1

    trade = case.conn.execute("SELECT * FROM trade_records").fetchone()
    assert trade["symbol"] == "US.XLE"
    assert trade["decision_id"] == "MOM-ETF-V1-20261113-US.XLE"
    assert trade["entry_price"] == 64.5
    assert trade["exit_price"] == 64.46
    assert trade["quantity"] == 76
    assert trade["realized_pnl"] == -3.04
    assert trade["fees_est"] > 0
    assert trade["pnl_net"] == round(trade["realized_pnl"] - trade["fees_est"], 2)
    assert trade["exit_reason"] == "ENTRY_BOOKKEEPING_COMPENSATION"
    metadata = json.loads(trade["metadata_json"])
    assert metadata["playbook_instruction_id"] == case.iid
    assert metadata["compensation_repair"] is True
    assert metadata["exit_intent_id"] == case.result["exit_intent_id"]

    inst = case.conn.execute(
        "SELECT execution_state, execution_price, execution_time, outcome, exit_price, "
        "exit_reason, pnl FROM playbook_instructions WHERE id=?", (case.iid,),
    ).fetchone()
    assert inst["execution_state"] == "BOOKED"
    assert inst["execution_price"] == 64.5
    assert inst["execution_time"] == case.result["owner"]["entry_time"]
    assert inst["outcome"] == "loss"
    assert inst["exit_price"] == 64.46
    assert inst["exit_reason"] == "ENTRY_BOOKKEEPING_COMPENSATION"
    assert inst["pnl"] == trade["pnl_net"]
    case.dual.acknowledge_close.assert_called_once()


def test_repair_uses_effective_reprice_stop_not_original_instruction_stop(repair_case):
    case = repair_case
    case.db.merge_instruction_metadata(case.iid, {
        "reprice_audit": {
            "reprice_stop_loss": 62.565,
            "reprice_take_profit": 0.0,
        }
    })
    assert pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn) == 1
    metadata = json.loads(case.conn.execute(
        "SELECT metadata_json FROM trade_records").fetchone()[0])
    assert metadata["stop_loss"] == 62.565
    assert metadata["take_profit"] == 0.0

    from app.playbook.pipeline_parts.compensation_repair import (
        align_existing_reprice_metadata)
    case.conn.execute(
        "UPDATE trade_records SET metadata_json=json_set(metadata_json,'$.stop_loss',62.3904)")
    case.conn.commit()
    trade_id = case.conn.execute("SELECT id FROM trade_records").fetchone()[0]
    assert align_existing_reprice_metadata(case.conn, case.result["owner"], trade_id) is True
    assert align_existing_reprice_metadata(case.conn, case.result["owner"], trade_id) is False


def test_repair_receipt_replay_is_idempotent(repair_case):
    case = repair_case
    assert pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn) == 1
    assert pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn) == 0
    assert case.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1
    assert case.conn.execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=?", (case.iid,),
    ).fetchone()[0] == "BOOKED"
    assert case.dual.acknowledge_close.call_count == 2


def test_close_failure_removes_unclosed_reconstruction(repair_case, monkeypatch):
    case = repair_case
    monkeypatch.setattr(
        pipeline, "_close_trade_record",
        MagicMock(side_effect=sqlite3.OperationalError("injected close failure")),
    )
    with pytest.raises(sqlite3.OperationalError, match="injected close failure"):
        pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn)
    assert case.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 0
    assert case.conn.execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=?", (case.iid,),
    ).fetchone()[0] == "FILLED_UNBOOKED"
    case.dual.acknowledge_close.assert_not_called()


def test_existing_entry_identity_conflict_is_fail_closed(repair_case):
    case = repair_case
    case.conn.execute(
        "INSERT INTO trade_records (trade_date,symbol,direction,entry_time,entry_price,"
        "quantity,source_type,futu_order_id,metadata_json,created_at) "
        "VALUES ('2026-11-13','US.XLE','LONG',?,64.5,75,'PLAYBOOK_SIM','8369338',?,?)",
        (case.result["owner"]["entry_time"],
         json.dumps({"playbook_instruction_id": case.iid}),
         "2026-11-13T09:52:09.266814-05:00"),
    )
    case.conn.commit()
    with pytest.raises(ValueError, match="identity conflict"):
        pipeline._book_playbook_exit_result(case.dual, case.result, conn=case.conn)
    assert case.conn.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0] == 1
    assert case.conn.execute(
        "SELECT execution_state FROM playbook_instructions WHERE id=?", (case.iid,),
    ).fetchone()[0] == "FILLED_UNBOOKED"
    case.dual.acknowledge_close.assert_not_called()
