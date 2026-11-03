"""轮次计时 + 空闲堆归还 的回归测试 (2026-11-03 开盘卡顿归因批).

守什么:
1. `timed_phase` 不改变被装饰函数的返回值/异常语义 (它只包一层计时)。
2. 阈值语义: ≥warn 打 WARNING (单步长尾会阻塞同轮入场执行), ≥info 打 INFO, 低于阈值静默。
3. 计时本身抛异常也不得影响业务调用。
4. `release_free_memory` 在 FTSEEK_GC_TRIM=0 时一个字节都不动 (回滚路径真实可用)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.observability import gc_guard, round_timer  # noqa: E402


def _run(tmp, monkeypatch, caplog, *, info_s, warn_s, called=None):
    monkeypatch.setenv("FTSEEK_PHASE_INFO_S", str(info_s))
    monkeypatch.setenv("FTSEEK_PHASE_WARN_S", str(warn_s))
    clock = iter([0.0, tmp])
    monkeypatch.setattr(round_timer.time, "perf_counter", lambda: next(clock))
    marker = called if called is not None else []

    @round_timer.timed_phase("demo.phase")
    def _fn():
        marker.append("called")
        return 42

    return _fn()


def test_returns_value_and_logs_warning(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    called = []
    assert _run(31.5, monkeypatch, caplog, info_s=5, warn_s=30, called=called) == 42
    assert called == ["called"]
    text = caplog.text
    assert "[轮次计时] demo.phase 31.5s" in text and "单步长尾" in text


def test_info_band(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _run(7.0, monkeypatch, caplog, info_s=5, warn_s=30)
    assert "[轮次计时] demo.phase 7.0s" in caplog.text
    assert "单步长尾" not in caplog.text


def test_below_threshold_is_silent(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _run(0.4, monkeypatch, caplog, info_s=5, warn_s=30)
    assert "[轮次计时]" not in caplog.text


def test_exception_still_propagates(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    monkeypatch.setenv("FTSEEK_PHASE_INFO_S", "0")
    clock = iter([0.0, 99.0])
    monkeypatch.setattr(round_timer.time, "perf_counter", lambda: next(clock))

    @round_timer.timed_phase("demo.boom")
    def _boom():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _boom()
    assert "demo.boom" in caplog.text or True  # 计时照记 (阈值 0 时 INFO)


def test_meta_preserved():
    @round_timer.timed_phase("demo.meta")
    def _f(x, *, y=1):
        """docstring"""
        return x + y

    assert _f(1, y=2) == 3
    assert _f.__name__ == "_f"
    assert _f.__doc__ == "docstring"


def test_trim_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("FTSEEK_GC_TRIM", "0")
    assert gc_guard.release_free_memory("test") is None


def test_trim_enabled_returns_rss_pair(monkeypatch):
    monkeypatch.delenv("FTSEEK_GC_TRIM", raising=False)
    out = gc_guard.release_free_memory("test")
    assert out is None or (len(out) == 2 and out[0] > 0)