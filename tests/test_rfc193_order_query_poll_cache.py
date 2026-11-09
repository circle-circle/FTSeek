"""RFC-193 (2026-11-09): EXT 订单查询轮询口径 — 缓存优先 + 刷新节流 + 研究臂限频自退避。

病灶: `order_list_query` 的「同一账户每 30 秒最多 10 次」**只对 refresh_cache=True 生效**
(Futu 官方文档), 而 `wait_order_fill` 旧实现在 90s × 3s 的轮询里每次都硬写 refresh_cache=True
⇒ 单笔等成交 = 30 次刷新 ≈ 60 次/分钟, 把 entry/exit 其它查询打成
`查询未完成订单频率太高，请求失败，每30秒最多10次。` (2026-11-09 US.TSM 研究臂实例)。

本文件钉四件事:
  A. 轮询默认走缓存 (`refresh_cache=False`), 刷新被节流到同一 (env, acc) 6s 一次;
  B. 刷新撞限频时退回缓存读, 不把可观测性读取升级成异常;
  C. `wait_order_fill` 全程刷新次数有上限 (≤2), 与轮询次数解耦;
  D. 研究臂遇限频 ⇒ 不打 `❌ 执行器异常` 推送 + 本轮剩余候选让位; 非限频异常仍推送。
"""
import pytest

import scripts.extended_hours_entry_executor as executor
import scripts.extended_hours_order_lifecycle as lifecycle

ENV = "SIMULATE"
ACC = 19930823
THROTTLE_MSG = "查询未完成订单频率太高，请求失败，每30秒最多10次。"


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


def _row(order_id="9", status="SUBMITTED", dealt_qty=0.0, dealt_avg=0.0):
    return {
        "order_id": order_id, "code": "US.AAA", "trd_side": "BUY",
        "order_status": status, "qty": "10", "dealt_qty": str(dealt_qty),
        "dealt_avg_price": str(dealt_avg), "price": "10.0",
        "order_type": "NORMAL", "session": "ETH", "remark": "ext_entry",
        "create_time": "2026-11-09 08:05:00", "update_time": "2026-11-09 08:05:01",
    }


class RecordingCtx:
    """记录每次 order_list_query 的 kwargs; 可让「刷新」调用撞限频。"""

    def __init__(self, rows=None, refresh_error=None, cache_error=None,
                 mutate_on_cancel=None):
        self.calls = []
        self.rows = list(rows or [])
        self.refresh_error = refresh_error
        self.cache_error = cache_error
        self.mutate_on_cancel = mutate_on_cancel
        self.cancelled = []

    def order_list_query(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("refresh_cache") and self.refresh_error:
            return -1, self.refresh_error
        if not kwargs.get("refresh_cache") and self.cache_error:
            return -1, self.cache_error
        order_id = kwargs.get("order_id")
        rows = self.rows
        if order_id:
            rows = [r for r in rows if str(r.get("order_id")) == str(order_id)][-1:]
        return 0, FakeFrame(rows)

    def modify_order(self, op, **kwargs):
        self.cancelled.append(kwargs)
        if self.mutate_on_cancel:
            self.rows = [self.mutate_on_cancel]
        return 0, "ok"

    def close(self):
        pass

    # 断言助手
    def refreshes(self):
        return [c for c in self.calls if c.get("refresh_cache")]

    def cache_reads(self):
        return [c for c in self.calls if not c.get("refresh_cache")]


@pytest.fixture(autouse=True)
def _fresh_gate():
    lifecycle.reset_refresh_state()
    yield
    lifecycle.reset_refresh_state()


# ── A. 轮询缓存优先 + 刷新节流 ────────────────────────────────────────────────


def test_query_order_rows_refreshes_at_most_once_per_window():
    ctx = RecordingCtx(rows=[_row()])

    first = lifecycle.query_order_rows(ctx, ENV, ACC)
    second = lifecycle.query_order_rows(ctx, ENV, ACC)

    assert [c["refresh_cache"] for c in ctx.calls] == [True, False]
    assert len(first) == len(second) == 1          # 缓存读与刷新读同一份行
    assert lifecycle.refresh_stats["suppressed"] == 1


def test_query_order_default_is_cache_read():
    ctx = RecordingCtx(rows=[_row()])

    lifecycle.query_order(ctx, ENV, ACC, "9")

    assert ctx.calls[0]["refresh_cache"] is False


def test_refresh_gate_is_per_account():
    ctx = RecordingCtx(rows=[_row()])

    lifecycle.query_order_rows(ctx, ENV, ACC)
    lifecycle.query_order_rows(ctx, ENV, ACC + 1)   # 另一个账户 = 另一份额度

    assert [c["refresh_cache"] for c in ctx.calls] == [True, True]


# ── B. 刷新撞限频 → 缓存兜底 ──────────────────────────────────────────────────


def test_refresh_throttled_falls_back_to_cache_instead_of_raising():
    ctx = RecordingCtx(rows=[_row()], refresh_error=THROTTLE_MSG)

    rows = lifecycle.query_order_rows(ctx, ENV, ACC)   # 旧实现这里抛 RuntimeError

    assert len(rows) == 1
    assert [c["refresh_cache"] for c in ctx.calls] == [True, False]
    assert lifecycle.refresh_stats["throttle_fallback"] == 1


def test_throttle_without_cache_body_still_raises():
    """刷新撞限频 + 缓存读也失败 (如连接已断) ⇒ 仍按原语义抛错, 不静默放行。"""
    ctx = RecordingCtx(rows=[_row()], refresh_error=THROTTLE_MSG,
                       cache_error="网络断开")

    with pytest.raises(RuntimeError):
        lifecycle.query_order_rows(ctx, ENV, ACC)


# ── C. wait_order_fill: 轮询走缓存, 刷新有硬上限 ──────────────────────────────


def test_wait_order_fill_polls_cache_and_caps_refreshes():
    """未成交路径: N 次缓存轮询 + 撤单前刷新 1 次 + 终态复核 1 次 = 刷新 2 次。"""
    ctx = RecordingCtx(rows=[_row(status="SUBMITTED")])

    result = lifecycle.wait_order_fill(
        ctx, ENV, ACC, "9", 10, 10.0,
        timeout_s=0.3, poll_s=0.05, cancel_timeout_s=0.2,
    )

    assert result["filled"] is False
    assert result["status"] == "SUBMITTED"
    assert len(ctx.cache_reads()) >= 5            # 轮询都在缓存上
    assert len(ctx.refreshes()) == 2              # 旧实现 = 每次轮询都刷新 (≥8)
    assert len(ctx.cancelled) == 1


def test_wait_order_fill_pre_cancel_refresh_catches_real_fill():
    """撤单前那次刷新若看到 FILLED_ALL ⇒ 不撤单, 按成交返回 (旧实现会报 cancel 失败)。"""

    class _FilledOnRefresh(RecordingCtx):
        def order_list_query(self, **kwargs):
            self.calls.append(dict(kwargs))
            if kwargs.get("refresh_cache"):
                return 0, FakeFrame([_row(status="FILLED_ALL", dealt_qty=10, dealt_avg=10.0)])
            return 0, FakeFrame([_row(status="SUBMITTED")])

    ctx = _FilledOnRefresh()

    result = lifecycle.wait_order_fill(
        ctx, ENV, ACC, "9", 10, 10.0, timeout_s=0.1, poll_s=0.05, cancel_timeout_s=0.1,
    )

    assert result["filled"] is True
    assert result["filled_qty"] == 10
    assert ctx.cancelled == []


def test_wait_order_fill_cache_miss_escalates_once():
    """缓存里两轮看不到该单 ⇒ 一次强制刷新兜底 (不重复烧额度)。"""
    ctx = RecordingCtx(rows=[])                   # 缓存永远空

    lifecycle.wait_order_fill(
        ctx, ENV, ACC, "9", 10, 10.0, timeout_s=0.2, poll_s=0.05, cancel_timeout_s=0,
    )

    assert len(ctx.refreshes()) <= 3


# ── D. 研究臂限频自退避 ──────────────────────────────────────────────────────


def _pool(*symbols):
    return [
        {"symbol": s, "px": 10.0, "chg": 5.0, "vol": 1_000_000.0,
         "prev_close": 9.5, "_source": "watchlist"}
        for s in symbols
    ]


def _run_research_pass(monkeypatch, tmp_path, execute_impl, symbols=("US.AAA", "US.BBB")):
    pushes, audited, executed = [], [], []

    def _push(text, *a, **k):
        pushes.append(text)
        return True

    def _execute(symbol, qty, price, session, chg, vol, earn, dry, **kwargs):
        executed.append(symbol)
        return execute_impl(symbol)

    monkeypatch.setattr(executor, "_push", _push)
    monkeypatch.setattr(executor, "_execute", _execute)
    monkeypatch.setattr(executor, "_audit", lambda rec: audited.append(dict(rec)))
    monkeypatch.setattr(executor, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(executor, "RESEARCH_SOURCES_ON", False)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: None)
    monkeypatch.setattr(executor, "_is_late_premarket", lambda session: False)
    monkeypatch.setattr(executor, "_save_state", lambda state, *a, **k: None)
    monkeypatch.setattr(
        executor, "_research_downstream_gates",
        lambda item, session, today, qctx, counts, rejects:
            (10, 10.0, 1_000.0, "B", {}, {"passed": True}),
    )
    monkeypatch.setattr(executor, "MAX_PER_DAY", 3)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 3)

    attempts, results, counts, rejects = {}, [], {}, []
    today = "2026-11-09"
    state = {"attempts": attempts}
    total, session_today = executor._research_pass_real(
        _pool(*symbols), "premarket", today, None, state, attempts,
        {}, f"{today}:premarket", 0, 0, 3, results, rejects, counts,
    )
    return {
        "pushes": pushes, "audited": audited, "executed": executed,
        "attempts": attempts, "results": results, "counts": counts,
        "today": today, "total": total, "session_today": session_today,
    }


def test_research_arm_throttle_backs_off_without_push(tmp_path, monkeypatch):
    def _boom(symbol):
        raise RuntimeError(f"order_list_query failed: {THROTTLE_MSG}")

    out = _run_research_pass(monkeypatch, tmp_path, _boom)

    assert out["pushes"] == []                                   # 不再报"执行器异常"
    assert out["executed"] == ["US.AAA"]                         # 第二只候选让位 = 自退避
    assert out["results"][0]["skip"] == "throttled_order_query"
    assert out["results"][0]["throttled"] is True
    assert out["audited"][0]["skip"] == "throttled_order_query"
    assert out["counts"]["research_throttled_backoff"] == 1
    # failed 不占配额 (attempt 落 failed, 允许下一轮重试)
    assert out["attempts"][f"{out['today']}:US.AAA"]["status"] == "failed"
    assert out["total"] == 0 and out["session_today"] == 0


def test_research_arm_non_throttle_exception_still_pushes(tmp_path, monkeypatch):
    def _boom(symbol):
        raise ValueError("boom-order-rejected")

    out = _run_research_pass(monkeypatch, tmp_path, _boom)

    assert len(out["pushes"]) == 2                               # 逐候选照旧告警
    assert "执行器异常" in out["pushes"][0]
    assert out["results"][0]["throttled"] is False
    assert "research_throttled_backoff" not in out["counts"]


def test_throttle_classifier_markers():
    assert executor._is_throttle_error(RuntimeError(THROTTLE_MSG)) is True
    assert executor._is_throttle_error(RuntimeError("too frequent requests")) is True
    assert executor._is_throttle_error(RuntimeError("order query failed for 9: 网络断开")) is False


# ── 源码契约: 轮询里不得再硬写 refresh_cache=True ────────────────────────────


def test_source_contract_no_hardcoded_refresh_in_poll_path():
    src = (executor.ROOT / "scripts" / "extended_hours_order_lifecycle.py").read_text()
    assert "refresh_cache=True, trd_env=env, acc_id=acc_id" not in src
    assert "def query_order(ctx, env, acc_id, order_id, ret_ok=0, refresh=False" in src