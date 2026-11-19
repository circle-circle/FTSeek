"""RFC-214 出场提交结果契约：明确拒单 vs UNKNOWN，含 fail-closed 反向钉。"""
from __future__ import annotations

import pandas as pd
import pytest

import scripts.rth_zero_fill_autorelease as rz
from app.execution import exit_lifecycle as lc
from tests.execution.test_exit_lifecycle import Context, adapter, close, owner

# autouse fixture 会把 lc._push_submit_rejected 换成 no-op，故在 import 期捕获真身，
# 供「推送面不得对 payload 求 bool」那条钉子直接调用。
_REAL_PUSH_SUBMIT_REJECTED = lc._push_submit_rejected


class RejectContext(Context):
    def __init__(self, *, ret=-1, payload=None, position_qty=10):
        super().__init__()
        self.ret = ret
        self.payload = payload if payload is not None else "integrated_us_order_new_err"
        self.position_qty = position_qty

    def place_order(self, **kwargs):
        if self.lose_submit:
            raise TimeoutError("response lost")
        self.orders.append(kwargs)
        return self.ret, self.payload


@pytest.fixture(autouse=True)
def _block_notification(monkeypatch):
    monkeypatch.setattr(lc, "_push_submit_rejected", lambda *args: None)


def _prepared(broker, *, quantity=10):
    ledger = broker._exit_lifecycle().ledger
    intent, blocked = ledger.request(
        account_id="42", target_env="sim", symbol="US.TEST", direction="LONG",
        quantity=quantity, owner=owner(), decision={"reason": "SL", "metadata": {}},
        limit_price=0, limit_timeout_s=30, now=1800000000.0,
    )
    assert blocked is None
    leg = ledger.reserve_leg(intent, "MARKET", quantity, 0, 1800000030, 1800000000)
    return intent, leg


def _send_prepared(broker):
    lifecycle = broker._exit_lifecycle()
    intent, leg = _prepared(broker)
    lifecycle.send(intent, leg, broker._sim_ctx,
                   {"trd_env": "SIMULATE", "acc_id": 42}, broker._sim_lock)
    return lifecycle.ledger.legs(intent["intent_id"])[0]


def test_send_classifies_definitive_reject_and_marks_leg_terminal(tmp_path):
    broker = adapter(tmp_path, RejectContext())
    result = close(broker)
    assert not result["success"]
    intent = broker.pending_closes()[0]
    leg = broker._exit_lifecycle().ledger.legs(intent["intent_id"])[0]
    assert leg["state"] == "TERMINAL"
    assert leg["broker_status"] == "FAILED"
    assert leg["dealt_qty"] == leg["dealt_amount"] == 0
    assert "integrated_us_order_new_err" in leg["last_error"]


def test_send_keeps_claim_on_unknown_exception(tmp_path):
    ctx = RejectContext()
    ctx.lose_submit = True
    broker = adapter(tmp_path, ctx)
    close(broker)
    leg = broker._exit_lifecycle().ledger.legs(broker.pending_closes()[0]["intent_id"])[0]
    assert leg["state"] == "PREPARED" and leg["version"] == 0


def test_send_keeps_claim_when_ret_ok_but_no_order_id(tmp_path):
    broker = adapter(tmp_path, RejectContext(ret=0, payload=pd.DataFrame()))
    close(broker)
    leg = broker._exit_lifecycle().ledger.legs(broker.pending_closes()[0]["intent_id"])[0]
    assert leg["state"] == "PREPARED" and leg["version"] == 0


def test_send_attaches_when_rejected_response_still_carries_order_id(tmp_path):
    payload = pd.DataFrame([{
        "code": "US.TEST", "order_id": "9001", "trd_side": "SELL", "qty": 10,
        "order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0,
    }])
    broker = adapter(tmp_path, RejectContext(payload=payload))
    close(broker)
    leg = broker._exit_lifecycle().ledger.legs(broker.pending_closes()[0]["intent_id"])[0]
    assert leg["state"] == "ACTIVE" and leg["order_id"] == "9001"
    assert leg["broker_status"] != "FAILED"


def test_send_accepts_first_valid_order_id_in_multirow_response(tmp_path):
    payload = pd.DataFrame([
        {"code": "US.TEST", "order_id": "9001", "trd_side": "SELL", "qty": 10,
         "order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0},
        {"code": "US.OTHER", "order_id": "9002", "trd_side": "SELL", "qty": 99,
         "order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0},
    ])
    broker = adapter(tmp_path, RejectContext(payload=payload))
    close(broker)
    leg = broker._exit_lifecycle().ledger.legs(broker.pending_closes()[0]["intent_id"])[0]
    assert leg["state"] == "ACTIVE" and leg["order_id"] == "9001"
    assert leg["broker_status"] != "FAILED"


def test_mark_submit_rejected_refuses_non_prepared_leg(tmp_path):
    broker = adapter(tmp_path)
    intent, leg = _prepared(broker)
    broker._exit_lifecycle().ledger.attach_order(leg, "9001", 1800000001)
    leg = broker._exit_lifecycle().ledger.legs(intent["intent_id"])[0]
    assert not broker._exit_lifecycle().ledger.mark_submit_rejected(leg, 1800000002, error="x")


def test_mark_submit_rejected_refuses_when_order_id_present(tmp_path):
    broker = adapter(tmp_path)
    intent, leg = _prepared(broker)
    broker._exit_lifecycle().ledger.attach_order(leg, "9001", 1800000001)
    leg = broker._exit_lifecycle().ledger.legs(intent["intent_id"])[0]
    assert not broker._exit_lifecycle().ledger.mark_submit_rejected(leg, 1800000002, error="x")


def test_mark_submit_rejected_refuses_when_dealt_qty_known(tmp_path):
    broker = adapter(tmp_path)
    _intent, leg = _prepared(broker)
    with broker._exit_lifecycle().ledger.transaction() as db:
        db.execute("UPDATE exit_legs SET dealt_qty=1.0 WHERE intent_id=? AND sequence=?",
                   (leg["intent_id"], leg["sequence"]))
    leg = broker._exit_lifecycle().ledger.legs(_intent["intent_id"])[0]
    assert not broker._exit_lifecycle().ledger.mark_submit_rejected(leg, 1800000002, error="x")


def test_mark_submit_rejected_is_version_cas(tmp_path):
    broker = adapter(tmp_path)
    _intent, leg = _prepared(broker)
    leg["version"] += 1
    assert not broker._exit_lifecycle().ledger.mark_submit_rejected(leg, 1800000002, error="x")


def test_reject_downgrades_to_unknown_when_broker_qty_dropped(tmp_path):
    broker = adapter(tmp_path, RejectContext(position_qty=0))
    leg = _send_prepared(broker)
    assert leg["state"] == "PREPARED" and leg["dealt_qty"] is None


def test_reject_fail_closed_when_position_query_fails(tmp_path):
    ctx = RejectContext()
    ctx.position_list_query = lambda **kwargs: (-1, None)
    broker = adapter(tmp_path, ctx)
    leg = _send_prepared(broker)
    assert leg["state"] == "PREPARED" and leg["dealt_qty"] is None


def test_observe_cannot_act_on_null_order_id_leg(tmp_path):
    broker = adapter(tmp_path)
    _intent, leg = _prepared(broker)
    with pytest.raises(ValueError, match="exit_order_id_conflict"):
        broker._exit_lifecycle().ledger.observe(
            leg, {"order_status": "CANCELLED_ALL", "dealt_qty": 0}, 1800000002)


def test_rfc157_verdict_reaches_release_after_v1(tmp_path):
    broker = adapter(tmp_path, RejectContext())
    close(broker)
    intent = broker.pending_closes()[0]
    leg = broker._exit_lifecycle().ledger.legs(intent["intent_id"])[0]
    action, reason, _ = rz.verdict(
        intent, [leg], now=intent["created_at"] + 3600, position_open=True)
    assert (action, reason) == ("release", "all_legs_terminal_zero_fill")


def test_terminal_statuses_already_contains_failed_and_disabled():
    assert "FAILED" in lc.TERMINAL_STATUSES and "DISABLED" in lc.TERMINAL_STATUSES


def test_exit_submit_reject_emits_push_not_silent(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(lc, "_push_submit_rejected", lambda intent, error: pushed.append(error))
    broker = adapter(tmp_path, RejectContext())
    close(broker)
    assert pushed == ["integrated_us_order_new_err"]


def test_position_evidence_conflict_is_fail_closed(tmp_path):
    ctx = RejectContext()
    ctx.position_list_query = lambda **kwargs: (0, pd.DataFrame([
        {"code": "US.TEST", "qty": 10, "position_side": "SHORT"},
    ]))
    broker = adapter(tmp_path, ctx)
    leg = _send_prepared(broker)
    assert leg["state"] == "PREPARED" and leg["dealt_qty"] is None


def test_error_text_never_evaluates_truthiness_of_payload():
    """`error or ''` 对 DataFrame 抛 "truth value is ambiguous"。

    券商失败路径今日返回 str（futu open_trade_context.py:617 `return RET_ERROR, msg`），
    但成功路径返回 DataFrame；一旦 SDK 在 RET_ERROR 下也回 DataFrame，旧写法会在
    **拒单处置内部**二次抛错 ⇒ 腿退回永久 PREPARED，正是 RFC-214 要消除的死锁。
    """
    frame = pd.DataFrame([{"order_id": None, "last_err_msg": "boom"}])
    assert "boom" in lc._error_text(frame, 500)
    assert lc._error_text(None, 500) == ""
    assert lc._error_text("integrated_us_order_new_err", 500) == "integrated_us_order_new_err"
    assert lc._error_text("x" * 900, 500) == "x" * 500


def test_send_terminalizes_reject_when_payload_is_dataframe(tmp_path):
    """端到端钉：拒单分支拿到 DataFrame 也必须正常终态化，不得抛错退回 PREPARED。"""
    payload = pd.DataFrame([{
        "code": "US.TEST", "order_id": None, "trd_side": "SELL", "qty": 10,
        "last_err_msg": "integrated_us_order_new_err",
    }])
    broker = adapter(tmp_path, RejectContext(payload=payload))
    leg = _send_prepared(broker)
    assert leg["state"] == "TERMINAL" and leg["broker_status"] == "FAILED"
    assert leg["dealt_qty"] == leg["dealt_amount"] == 0
    assert "integrated_us_order_new_err" in leg["last_error"]


def test_push_submit_rejected_survives_dataframe_payload(tmp_path, monkeypatch):
    """推送面同样不得对 payload 求 bool；否则 T14「不静默」会被 except 吞成静默。"""
    sent = []

    class _Hub:
        def push_text(self, text, **kwargs):
            sent.append(text)

    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _Hub(), raising=False)
    _REAL_PUSH_SUBMIT_REJECTED(
        {"symbol": "US.TEST", "direction": "LONG", "quantity": 10, "intent_id": "i1"},
        pd.DataFrame([{"last_err_msg": "boom"}]))
    assert len(sent) == 1 and "boom" in sent[0] and "US.TEST" in sent[0]

