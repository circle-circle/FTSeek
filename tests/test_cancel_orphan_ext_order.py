#!/usr/bin/env python3
"""人工确认撤销孤儿单工具的安全门与"不宣称成功"契约 (DEB-20260918 §23.3/§23.5)。

钉住四件事:
  ① **绝不碰别人的单** —— `remark` 非 `ext_exit_*` 一律拒绝（`ext_entry`/`pbe_*`/手动单）;
  ② 终态单 / 太新的单 / 符号不符 一律拒绝（防抢自己正常的退出单）;
  ③ 默认 dry-run：不带 `--apply` 时**一次券商调用都不发**;
  ④ 撤单 API 成功 ≠ 订单已撤 —— 轮询确认**撤销类终态**才算成功; 间隙成交(FILLED_ALL)
     单独报失败(退出码 3), 仍是活单/异常终态报失败(退出码 2), 不写"已撤销"审计。
"""
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

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
    assert not list(tmp_path.glob("*.tmp")), "原子写不得残留 tmp 文件"


def test_clear_attempt_fails_closed_when_state_locked(tmp_path):
    """执行器持锁期间, attempt 清理必须 fail-closed —— 不动文件、可重试。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"attempts": {"2026-09-17:US.CRM": {"status": "failed"}}}))
    lock_path = tmp_path / "state.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        changed, msg = tool.clear_attempt("US.CRM", today_et="2026-09-17",
                                          state_path=state, apply=True)

        assert changed is False and "锁" in msg
        attempts = json.loads(state.read_text())["attempts"]
        assert attempts["2026-09-17:US.CRM"]["status"] == "failed"


def test_clear_attempt_rereads_state_under_lock(tmp_path, monkeypatch):
    """持锁后必须重读 state: 初读快照看到的旧内容不得抹掉持锁后的真实键。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"attempts": {}}))
    real_read_text = Path.read_text
    read_calls = []

    def staged_read_text(path_self, *args, **kwargs):
        read_calls.append(path_self)
        if len(read_calls) == 1:
            return json.dumps({"attempts": {}})
        state.write_text(json.dumps({"attempts": {
            "2026-09-17:US.CRM": {"status": "broker_active"}}}))
        return real_read_text(path_self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", staged_read_text)
    changed, msg = tool.clear_attempt("US.CRM", today_et="2026-09-17",
                                      state_path=state, apply=True)

    assert changed is True, msg
    assert len(read_calls) == 2, "初读一次 + 持锁后必须重读一次"
    with open(state) as handle:
        assert "2026-09-17:US.CRM" not in json.load(handle)["attempts"]


# ---- main(): 端到端（券商全替身） -----------------------------------------

@pytest.fixture()
def harness(tmp_path, monkeypatch):
    audits = []
    monkeypatch.setattr(tool, "AUDIT", tmp_path / "orphan.jsonl")
    monkeypatch.setattr(tool, "_audit", lambda payload, path=None: audits.append(payload))
    monkeypatch.setattr(tool, "STATE", tmp_path / "state.json")
    return audits


def _arm(monkeypatch, order, after_statuses=("CANCELLED_ALL",), cancel_ok=True):
    ctx = FakeTradeContext()
    monkeypatch.setattr(entry_executor, "_open_trade_context", lambda: ctx)
    # 第一次 query_order = 判定前(活单); 之后 = 撤单后轮询序列("SAME" 表示仍是活单)
    after_seq = [order if st == "SAME" else {**order, "status": st} for st in after_statuses]
    seq = [order] + after_seq
    calls = []
    queries = []
    sleeps = []

    def fake_query(*args, **kwargs):
        queries.append(1)
        return seq.pop(0) if seq else None

    monkeypatch.setattr(tool, "query_order", fake_query)
    monkeypatch.setattr(tool, "cancel_order",
                        lambda *a, **kw: (calls.append(a), (cancel_ok, "stub"))[1])
    monkeypatch.setattr(tool, "_sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(tool, "_now_et", lambda: NOW)
    return SimpleNamespace(calls=calls, queries=queries, sleeps=sleeps)


def test_dry_run_never_calls_broker(harness, monkeypatch):
    res = _arm(monkeypatch, _order())

    rc = tool.main(["--order-id", "8185508"])

    assert rc == 0 and res.calls == [], "dry-run 不得撤单"


def test_apply_cancels_and_confirms_terminal(harness, monkeypatch):
    res = _arm(monkeypatch, _order(), after_statuses=("CANCELLED_ALL",))

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "0"])

    assert rc == 0 and len(res.calls) == 1
    assert harness[-1]["action"] == "cancelled_by_operator"


def test_apply_reports_failure_when_still_active(harness, monkeypatch):
    """撤单 API 成功但轮询到超时仍是活单 ⇒ 退出码 2, 不得写"已撤销"审计 (历史 #185 教训)。"""
    res = _arm(monkeypatch, _order(), after_statuses=("SAME",), cancel_ok=True)

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "0"])

    assert rc == 2
    assert harness[-1]["action"] == "cancel_unconfirmed"
    assert len(res.queries) == 2, "至少回查一次再判超时"


def test_cancel_polls_transient_cancelling_state(harness, monkeypatch):
    """撤单是异步的: CANCELLING_ALL 是过程态, 必须轮询到撤销类终态而非一次误报失败。"""
    res = _arm(monkeypatch, _order(),
               after_statuses=("CANCELLING_ALL", "CANCELLING_ALL", "CANCELLED_ALL"))

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "5"])

    assert rc == 0
    assert len(res.queries) == 4, "判定1次 + 轮询3次"
    assert res.sleeps, "轮询之间必须等待而非忙转"
    assert harness[-1]["action"] == "cancelled_by_operator"


def test_fill_during_cancel_window_is_not_reported_as_cancelled(harness, monkeypatch):
    """撤单间隙成交(FILLED_ALL)是终态但不是撤销 ⇒ 退出码 3 + 独立审计, 绝不写已撤销。"""
    res = _arm(monkeypatch, _order(), after_statuses=("FILLED_ALL",))

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "0"])

    assert rc == 3
    assert len(res.calls) == 1
    assert harness[-1]["action"] == "filled_during_cancel"
    assert harness[-1]["status_after"] == "FILLED_ALL"


def test_unexpected_terminal_is_not_reported_as_cancelled(harness, monkeypatch):
    """非撤销终态(如 FAILED)也不得宣称撤销成功。"""
    _arm(monkeypatch, _order(), after_statuses=("FAILED",))

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "0"])

    assert rc == 2
    assert harness[-1]["action"] == "terminal_unexpected"
    assert harness[-1]["status_after"] == "FAILED"


def test_external_cancel_after_failed_request_is_attributed_honestly(harness, monkeypatch):
    """撤单请求失败但订单确实已撤销 ⇒ 目标达成(rc=0), 但审计归因 cancelled_external。"""
    _arm(monkeypatch, _order(), after_statuses=("CANCELLED_ALL",), cancel_ok=False)

    rc = tool.main(["--order-id", "8185508", "--apply", "--confirm-timeout-s", "0"])

    assert rc == 0
    assert harness[-1]["action"] == "cancelled_external"
    assert harness[-1]["cancel_ok"] is False


def test_foreign_or_fresh_order_refused_before_any_broker_call(harness, monkeypatch):
    res = _arm(monkeypatch, _order(remark="ext_entry"))

    rc = tool.main(["--order-id", "8185508", "--apply"])

    assert rc == 1 and res.calls == []
    assert harness[-1]["action"] == "refused"
    assert "not_our_exit_order" in harness[-1]["reason"]
