"""盘中"执行优先"开关 + 自动执行门的回归测试 (2026-11-03 开盘卡顿批).

守什么:
1. `FTSEEK_EXEC_FIRST=0/false` 必须真的关掉 (回滚路径真实可用), 不设时读 YAML 默认 true。
2. `_maybe_auto_execute` 的 interval 门: 间隔内不重复调用, 到点才调用 (防执行优先把
   TRADING 轮次变成每轮都执行 = 放大下单频率)。
3. 执行抛异常必须被隔离, 且**不得**更新 last_run (否则一次异常会静默压掉后续 180s)。
4. 抽方法前后语义一致: 两个调用点共用同一份门逻辑 (源码契约: 内联块不得再出现)。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.scheduler_core import TradingScheduler  # noqa: E402


def _stub():
    return SimpleNamespace(
        PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC=180,
        _playbook_auto_execute_last_run=None,
    )


def test_exec_first_env_override(monkeypatch):
    stub = _stub()
    for off in ("0", "false", "False", "no", "off"):
        monkeypatch.setenv("FTSEEK_EXEC_FIRST", off)
        assert TradingScheduler._exec_first_enabled(stub) is False, off
    monkeypatch.setenv("FTSEEK_EXEC_FIRST", "1")
    assert TradingScheduler._exec_first_enabled(stub) is True


def test_exec_first_defaults_to_yaml(monkeypatch):
    monkeypatch.delenv("FTSEEK_EXEC_FIRST", raising=False)
    stub = _stub()
    cfg_cls = SimpleNamespace(get_dotted=lambda k, d=None: True)
    import app.core.config_container as cc
    monkeypatch.setattr(cc, "get_config", lambda: cfg_cls)
    assert TradingScheduler._exec_first_enabled(stub) is True


def test_interval_gate_blocks_second_call(monkeypatch):
    stub = _stub()
    calls = []
    import app.playbook.pipeline as pl
    monkeypatch.setattr(pl, "run_playbook_auto_execute", lambda s: calls.append(1))

    t0 = datetime(2026, 11, 3, 10, 0, 0)
    assert TradingScheduler._maybe_auto_execute(stub, t0) is True
    assert TradingScheduler._maybe_auto_execute(stub, t0 + timedelta(seconds=60)) is False
    assert TradingScheduler._maybe_auto_execute(stub, t0 + timedelta(seconds=181)) is True
    assert len(calls) == 2


def test_exception_isolated_and_no_last_run_update(monkeypatch):
    stub = _stub()
    import app.playbook.pipeline as pl

    def _boom(_s):
        raise RuntimeError("boom")

    monkeypatch.setattr(pl, "run_playbook_auto_execute", _boom)
    t0 = datetime(2026, 11, 3, 10, 0, 0)
    assert TradingScheduler._maybe_auto_execute(stub, t0) is False
    assert stub._playbook_auto_execute_last_run is None   # 异常不得吃掉 180s 窗口


def test_no_inline_duplicate_gate_in_source():
    """源码契约: interval 门只允许存在于 _maybe_auto_execute 内 (防两处漂移)。"""
    src = (PROJECT_ROOT / "app/core/scheduler_core.py").read_text(encoding="utf-8")
    assert src.count("_playbook_auto_execute_last_run") <= 4, "内联门疑似被复制回来 (方法内合法用法是 4 处)"
    assert "self._maybe_auto_execute(now)" in src
    assert src.count("self._maybe_auto_execute(now)") >= 2, "两个调用点缺一"