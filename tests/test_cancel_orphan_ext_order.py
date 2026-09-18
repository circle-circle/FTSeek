#!/usr/bin/env python3
"""人工确认撤销孤儿单工具的安全门与"不宣称成功"契约 (DEB-20260918 §23.3/§23.5)。

钉住四件事:
  ① **绝不碰别人的单** —— `remark` 非 `ext_exit_*` 一律拒绝（`ext_entry`/`pbe_*`/手动单）;
  ② 终态单 / 太新的单 / 符号不符 一律拒绝（防抢自己正常的退出单）;
  ③ 默认 dry-run：不带 `--apply` 时**一次券商调用都不发**;
  ④ 撤单 API 成功 ≠ 订单已撤 —— 回查仍是活单时报失败(退出码 2), 不写"已确认撤销"审计。
"""
import json

import pytest

import scripts.cancel_orphan_ext_order as tool
import scripts.extended_hours_entry_executor as entry_executor


class FakeTradeContext:
    def __init__(self, orders=None):
        self.orders = list(orders or [])
        self.cancels = []

    def close(self):
        pass


def _order(**kw):
    base = {
        "order_id": "8185508", "symbol": "US.CRM", "status": "SUBMITTED",
        "remark": "ext_exit_tighten", "create_time": "2026-09-17 05:50:13",
        "qty": 23, "filled_qty": 0, "avg_fill_price": 0.0, "price": 251.27,
    }
    base.update(kw)
    return base


NOW = __import__("datetime").datetime(2026, 9, 17, 10, 30, 0)  # 4h40m after create


# ---- decide(): 纯判定 ------------------------------------------------------

def test_eligible_order_passes():
    ok, reason, age = tool.decide(_order(), now=NOW)
    assert ok is True and reason == "eligible"
    assert age == pytest.approx(280.0, abs=1.0)


@pytest.mark.parametrize("kwargs,expect", [
    ({"remark": "ext_entry"}, "not_our_exit_order"),
    ({"remark": "pbe_d45bcb"}, "not_our_exit_order"),
    ({"remark": ""}, "not_our_exit_order"),
    ({"status": "CANCELLED_ALL"}, "already_terminal"),
    ({"status": "FILLED_ALL"}, "already_terminal"),
    ({"create_time": "2026-09-17 10:25:00"}, "too_fresh"),      # 5 min < 15 min
    ({"create_time": ""}, "create_time_unparseable"),
    ({"filled_qty": 5, "avg_fill_price": 0.0}, "partial_fill_without_avg_price"),
])
def test_gates_reject(kwargs, expect):
    ok, reason, _ = tool.decide(_order(**kwargs), now=NOW)
    assert ok is False
    assert reason.startswith(expect)


def test_symbol_mismatch_and_missing_order_rejected():
    assert tool.decide(_order(), now=NOW, symbol="US.NVDA")[1].startswith("symbol_mismatch")
    assert tool.decide(None, now=NOW)[1] == "order_not_found"


# ---- clear_attempt(): 默认只报不删 ----------------------------------------

def test_clear_attempt_is_dry_by_default(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"attempts": {"2026-09-17:US.CRM": {"status": "failed"}}}))

    changed, msg = tool.clear_attempt("US.CRM", today_et="2026-09-17", state_path=state, apply=False)

    assert changed is False and "[dry-run]" in msg
    assert "2026-09-17:US.CRM" in json.loads(state.read_text())["attempts"]


def test_clear_attempt_applies_and_backs_up(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"attempts": {"2026-09-17:US.CRM": {"status": "failed"},
                                              "2026-09-17:US.NVDA": {"status": "opened"}}}))

    changed, msg = tool.clear_attempt("US.CRM", today_et="2026-09-17", state_path=state, apply=True)

    assert changed is True
    attempts = json.loads(state.read_text())["attempts"]
    assert "2026-09-17:US.CRM" not in attempts
    assert "2026-09-17:US.NVDA" in attempts          # 只删该标的
    assert list(tmp_path.glob("state.json.bak_*")), "必须留备份"


# ---- main(): 端到端（券商全替身） -----------------------------------------

@pytest.fixture()
def harness(tmp_path, monkeypatch):
    audits = []
    monkeypatch.setattr(tool, "AUDIT", tmp_path / "orphan.jsonl")
    monkeypatch.setattr(tool, "_audit", lambda payload, path=None: audits.append(payload))
    monkeypatch.setattr(tool, "STATE", tmp_path / "state.json")
    return audits


def _arm(monkeypatch, order, after_status="CANCELLED_ALL", cancel_ok=True):
    ctx = FakeTradeContext()
    monkeypatch.setattr(entry_executor, "_open_trade_context", lambda: ctx)
    # 第一次 query_order = 判定前(活单); 第二次 = 撤单后回查(after_status, "SAME" 表示仍是活单)
    after = order if after_status == "SAME" else {**order, "status": after_status}
    seq = [order, after]
    calls = []
    monkeypatch.setattr(tool, "query_order",
                        lambda *a, **kw: seq.pop(0) if seq else None)
    monkeypatch.setattr(tool, "cancel_order",
                        lambda *a, **kw: (calls.append(a), (cancel_ok, "stub"))[1])
    monkeypatch.setattr(tool, "query_order_rows", lambda *a, **kw: [order])
    return calls


def test_dry_run_never_calls_broker(harness, monkeypatch):
    calls = _arm(monkeypatch, _order())

    rc = tool.main(["--order-id", "8185508"])

    assert rc == 0 and calls == [], "dry-run 不得撤单"


def test_apply_cancels_and_confirms_terminal(harness, monkeypatch):
    calls = _arm(monkeypatch, _order(), after_status="CANCELLED_ALL")

    rc = tool.main(["--order-id", "8185508", "--apply"])

    assert rc == 0 and len(calls) == 1
    assert harness[-1]["action"] == "cancelled_by_operator"


def test_apply_reports_failure_when_still_active(harness, monkeypatch):
    """撤单 API 成功但回查仍是活单 ⇒ 退出码 2, 不得写"已撤销"审计 (历史 #185 教训)。"""
    _arm(monkeypatch, _order(), after_status="SAME", cancel_ok=True)

    rc = tool.main(["--order-id", "8185508", "--apply"])

    assert rc == 2
    assert harness[-1]["action"] == "cancel_unconfirmed"


def test_foreign_or_fresh_order_refused_before_any_broker_call(harness, monkeypatch):
    calls = _arm(monkeypatch, _order(remark="ext_entry"))

    rc = tool.main(["--order-id", "8185508", "--apply"])

    assert rc == 1 and calls == []
    assert harness[-1]["action"] == "refused"
    assert "not_our_exit_order" in harness[-1]["reason"]