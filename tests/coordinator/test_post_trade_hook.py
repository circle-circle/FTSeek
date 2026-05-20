"""PostTradeHook 测试 (2026-05-16)

验证:
- submit 主线 0 阻塞
- 单个 hook 失败不影响其他
- 顺序执行 (attribution → mirror → push → thesis)
- shutdown 等异步完成
"""
import os
import sys
import time
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.pipeline.post_trade_hook import submit, get_stats, shutdown


def test_submit_returns_immediately():
    """submit() 是 fire-and-forget, 主线不等"""
    called = []

    def _slow():
        time.sleep(0.3)
        called.append("done")

    t0 = time.time()
    submit("US.X", [("slow_hook", _slow)])
    elapsed = time.time() - t0
    assert elapsed < 0.05, f"submit 应立即返回, 实际 {elapsed:.3f}s"
    # 等异步完成
    time.sleep(0.5)
    assert called == ["done"]


def test_hooks_execute_in_order():
    """hooks 按 list 顺序串行执行"""
    log = []
    submit("US.X", [
        ("a", lambda: log.append("a")),
        ("b", lambda: log.append("b")),
        ("c", lambda: log.append("c")),
    ])
    time.sleep(0.3)
    assert log == ["a", "b", "c"]


def test_single_hook_failure_does_not_break_others():
    """单 hook 失败 → 后续仍执行"""
    log = []
    def _fail():
        raise RuntimeError("boom")
    submit("US.X", [
        ("ok1", lambda: log.append("ok1")),
        ("fail", _fail),
        ("ok2", lambda: log.append("ok2")),
    ])
    time.sleep(0.3)
    assert log == ["ok1", "ok2"]


def test_stats_tracks_completion():
    """stats 记录 submitted/completed/failed"""
    before = get_stats()

    def _fail():
        raise ValueError("boom")
    submit("US.S", [
        ("ok", lambda: None),
        ("bad", _fail),
    ])
    time.sleep(0.3)
    after = get_stats()
    # 至少完成 +1, 失败 +1
    assert after["completed"] >= before["completed"] + 1
    assert after["failed"] >= before["failed"] + 1
    assert after["submitted"] >= before["submitted"] + 1


def test_concurrent_submits_thread_safe():
    """并发 50 次 submit 不丢"""
    counter = {"n": 0}
    lock = threading.Lock()

    def _inc():
        with lock:
            counter["n"] += 1

    for _ in range(50):
        submit("US.C", [("inc", _inc)])
    # 等所有完成 (2 worker × 50 任务 < 1s)
    time.sleep(1.0)
    assert counter["n"] == 50


def test_on_position_opened_main_path_not_blocked(monkeypatch):
    """on_position_opened 入口主线不阻塞, 即使 hook 内部慢"""
    from app.pipeline.post_trade_hook import on_position_opened

    # 替换 4 个 hook 为 slow noop, 避免真实写 DB / 调 LLM
    import app.pipeline.post_trade_hook as pth
    real_submit = pth.submit
    called = []

    def _fake_submit(symbol, hooks):
        for name, fn in hooks:
            called.append(name)
        # 不真跑

    monkeypatch.setattr(pth, "submit", _fake_submit)

    class _Sig:
        direction = type("D", (), {"value": "LONG"})()
        symbol = "US.X"
        signal_id = "abc"
        trace_id = "US-T"
        strength = 60
        confidence = 0.7
        strategy_id = "voting_llm"
        metadata = {"_factor_detail": {"trend": {"raw": 5, "conf": 0.8}}}

    class _Pos:
        symbol = "US.X"
        owner_strategy = "main"
        size = 100
        avg_entry_price = 145.0
        entry_time = None
        position_id = "pid"
        direction = type("D", (), {"value": "LONG"})()
        market_regime = None

    t0 = time.time()
    on_position_opened(_Pos(), _Sig(), {"vix": 18.5}, scheduler=None)
    assert time.time() - t0 < 0.1
    # 4 hook 应都被 submit
    assert called == ["attribution", "mirror", "push", "thesis"]
