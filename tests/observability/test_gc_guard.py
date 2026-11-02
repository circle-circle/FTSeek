"""GC 护栏 (app/observability/gc_guard.py) 回归测试 — 2026-11-02 开盘冻结批。

守什么:
1. 阈值真的被改 (默认开); FTSEEK_GC_TUNE=0 时一个字节都不改 (回滚路径必须真实可用)。
2. `freeze_long_lived` 只冻结一次 (重复调用 = 空操作) — 运行时反复冻结会把
   后成环的垃圾钉成永不回收 (内存泄漏), 这是本模块唯一有持续副作用的动作。
3. 停顿计时: gen2 开始行 (冻结前最后一行日志 = 归因锚点) 与超阈值 WARNING 都要出,
   正常短停顿不得刷 WARNING (否则等于没告警)。
4. 钩子永不抛异常 (gc.callbacks 内抛异常会污染 GC 状态)。

全部用 monkeypatch 打桩, 不触碰生产库/网络/真实 1.4GB 堆。
"""
from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.observability import gc_guard  # noqa: E402


@pytest.fixture()
def fresh(monkeypatch):
    """重置模块全局态 + 保底还原进程 GC 配置 (别的用例/被测系统不受污染)。"""
    thresholds = gc.get_threshold()
    callbacks = list(gc.callbacks)
    monkeypatch.setattr(gc_guard, "_installed", False)
    monkeypatch.setattr(gc_guard, "_frozen", False)
    monkeypatch.setattr(gc_guard, "_stack", [])
    monkeypatch.setattr(gc_guard, "_window_max_ms", 0.0)
    monkeypatch.setattr(gc_guard, "_window_max_gen", -1)
    monkeypatch.setattr(gc_guard, "_window_pauses", 0)
    monkeypatch.setattr(gc_guard, "_total_pauses", 0)
    for name in ("FTSEEK_GC_TUNE", "FTSEEK_GC_MONITOR", "FTSEEK_GC_GEN0",
                 "FTSEEK_GC_GEN1", "FTSEEK_GC_GEN2", "FTSEEK_GC_REPORT_S",
                 "FTSEEK_GC_PAUSE_WARN_MS"):
        monkeypatch.delenv(name, raising=False)
    yield
    gc.set_threshold(*thresholds)
    gc.callbacks[:] = callbacks


def test_thresholds_applied_and_callback_installed(fresh, monkeypatch):
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")  # 不起后台线程
    assert gc_guard.install_gc_guard() is True
    assert gc.get_threshold() == (gc_guard._DEFAULT_GEN0, gc_guard._DEFAULT_GEN1,
                                  gc_guard._DEFAULT_GEN2)
    assert gc_guard._on_gc in gc.callbacks


def test_tune_disabled_changes_nothing(fresh, monkeypatch):
    monkeypatch.setenv("FTSEEK_GC_TUNE", "0")
    monkeypatch.setenv("FTSEEK_GC_MONITOR", "0")
    before = gc.get_threshold()
    gc_guard.install_gc_guard()
    assert gc.get_threshold() == before           # 回滚路径: 阈值原样
    assert gc_guard._on_gc not in gc.callbacks     # 连钩子都不挂
    assert gc_guard.freeze_long_lived("test") is False
    assert gc_guard._frozen is False


def test_freeze_only_once(fresh, monkeypatch, caplog):
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")
    gc_guard.install_gc_guard()
    calls = []
    monkeypatch.setattr(gc, "freeze", lambda: calls.append(1))
    assert gc_guard.freeze_long_lived("startup") is True
    assert gc_guard.freeze_long_lived("again") is True   # 幂等: 第二次直接返回
    assert len(calls) == 1


def test_gen2_start_line_and_long_pause_warning(fresh, monkeypatch, caplog):
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")
    monkeypatch.setenv("FTSEEK_GC_PAUSE_WARN_MS", "500")
    gc_guard.install_gc_guard()
    clock = iter([100.0, 101.2])                  # 1.2s = 超阈值
    monkeypatch.setattr(gc_guard.time, "perf_counter", lambda: next(clock))
    caplog.set_level("INFO")
    gc_guard._on_gc("start", {"generation": 2, "collected": 0})
    gc_guard._on_gc("stop", {"generation": 2, "collected": 1234})
    text = caplog.text
    assert "gen2 全堆扫描开始" in text            # 归因锚点
    assert "停顿 1200ms" in text and "gen=2" in text
    assert gc_guard.get_status()["window_max_ms"] == pytest.approx(1200.0, abs=0.5)


def test_short_pause_is_silent(fresh, monkeypatch, caplog):
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")
    monkeypatch.setenv("FTSEEK_GC_PAUSE_WARN_MS", "500")
    gc_guard.install_gc_guard()
    clock = iter([100.0, 100.05])
    monkeypatch.setattr(gc_guard.time, "perf_counter", lambda: next(clock))
    caplog.set_level("INFO")
    gc_guard._on_gc("start", {"generation": 0, "collected": 0})
    gc_guard._on_gc("stop", {"generation": 0, "collected": 3})
    assert "停顿" not in caplog.text              # 短停顿不刷日志
    assert gc_guard.get_status()["total_pauses"] == 1


def test_nested_collection_pairs_by_stack(fresh, monkeypatch, caplog):
    """gen0 内触发 gen2: 配对必须按栈, 不能把 gen2 的时长记到 gen0 上。"""
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")
    monkeypatch.setenv("FTSEEK_GC_PAUSE_WARN_MS", "900")
    gc_guard.install_gc_guard()
    clock = iter([0.0, 0.5, 3.0])                 # gen0@0s → gen2@0.5s, 到 3.0s 收尾
    monkeypatch.setattr(gc_guard.time, "perf_counter", lambda: next(clock))
    caplog.set_level("INFO")
    gc_guard._on_gc("start", {"generation": 0, "collected": 0})
    gc_guard._on_gc("start", {"generation": 2, "collected": 0})
    gc_guard._on_gc("stop", {"generation": 2, "collected": 9})
    st = gc_guard.get_status()
    assert st["window_max_gen"] == 2
    assert st["window_max_ms"] == pytest.approx(2500.0, abs=1.0)


def test_stall_probe_reports_gap_and_gc_delta(fresh, monkeypatch, caplog):
    """停摆探针: 采样间隔超阈值 → WARNING 里必须同时给出 秒数 与 各代 GC 次数增量。

    这两个数就是"到底是不是 GC 独占 GIL"的唯一判决依据 (gen 全 0 ⇒ 不是 GC)。
    """
    sleeps = {"n": 0}

    def fake_sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise SystemExit("stop loop")   # BaseException: 不被 except Exception 吞

    real_stats = [{"collections": 10, "collected": 100, "uncollectable": 0},
                  {"collections": 1, "collected": 5, "uncollectable": 0},
                  {"collections": 4, "collected": 9, "uncollectable": 0}]
    after_stats = [{"collections": 30, "collected": 200, "uncollectable": 0},
                   {"collections": 1, "collected": 5, "uncollectable": 0},
                   {"collections": 7, "collected": 9, "uncollectable": 0}]
    seq = iter([real_stats, after_stats])
    monkeypatch.setattr(gc_guard.gc, "get_stats", lambda: next(seq))
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(gc_guard.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(gc_guard.time, "sleep", fake_sleep)
    monkeypatch.setattr(gc_guard, "_rss_mb", lambda: 1400.0)
    caplog.set_level("WARNING")
    with pytest.raises(SystemExit):
        gc_guard._stall_probe_loop(5.0, 15.0)
    text = caplog.text
    assert "进程整体停摆" in text and "100" in text
    assert "gen0=+20 gen1=+0 gen2=+3" in text


def test_hook_never_raises(fresh, monkeypatch):
    monkeypatch.setenv("FTSEEK_GC_REPORT_S", "0")
    gc_guard.install_gc_guard()
    gc_guard._on_gc("stop", {"generation": "bad"})    # 脏输入
    gc_guard._on_gc("start", {})                       # 缺字段
    assert isinstance(gc_guard.get_status(), dict)