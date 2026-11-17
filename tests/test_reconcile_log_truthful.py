"""契约: 对账日志报实际纳管数 (2026-11-17 展示级修复, 一次一变量)。

根因: 旧日志 "+N 新增" 用过滤前差集 (broker - memory), playbook 自管/手动/结算窗/
外部仓保护跳过的候选全被算成 "新增", 与 "内存共 0 个" 矛盾 (收盘核查实证每 5min
"+8 新增, 内存共 0" 刷屏误导排障)。本套件钉住: 纳管数 == PositionStore.add 实际
成功数; 跳过数 == 差集 - 纳管; 过滤/纳管语义一字未动。
"""
import logging
import types

import pytest

from app.core.scheduler_core import PositionStore
from app.pipeline import position_monitor as pm


class _Syncer:
    def __init__(self, positions):
        self._p = positions

    def query_broker_positions(self):
        return self._p


class _Scheduler:
    def __init__(self):
        self.position_store = PositionStore()


def _bp(direction="LONG", qty=10, cost=100.0):
    return {"direction": direction, "qty": qty, "cost": cost}


@pytest.fixture
def env(monkeypatch, tmp_path):
    import app.core.position_sync as ps
    import app.playbook.pipeline as pbp
    import app.core.db_utils as dbu
    import app.core.paths as paths
    import app.core.config_container as cc
    import app.utils.daily_active_pool as dap
    import app.risk.capital_safety as cs

    monkeypatch.setattr(dbu, "get_main_db", lambda: tmp_path / "main.db")
    monkeypatch.setattr(paths, "get_trade_records_db", lambda: tmp_path / "trades.db")
    monkeypatch.setattr(pm, "_load_price_fail_closed_set", lambda: set())
    monkeypatch.setattr(pm, "_is_manual_position_for_reconcile", lambda s: False)
    monkeypatch.setattr(pm, "_reconcile_is_system_owned", lambda s: True)
    monkeypatch.setattr(pm, "_send_reconcile_alert", lambda *a, **k: None)
    monkeypatch.setattr(pbp, "load_playbook_open_symbols", lambda: {"US.AAA", "US.BBB"})
    monkeypatch.setattr(pbp, "playbook_open_check", lambda s, syms: s in syms)
    monkeypatch.setattr(
        dap, "get_daily_active_pool",
        lambda **kw: types.SimpleNamespace(add_position=lambda *a, **k: None))
    monkeypatch.setattr(
        cc, "get_config",
        lambda: types.SimpleNamespace(get_dotted=lambda k, d=None: d))

    def _boom():
        raise RuntimeError("test: no capital safety side effects")

    monkeypatch.setattr(cs, "get_capital_safety_system", _boom)

    def _setup(broker):
        monkeypatch.setattr(ps, "get_us_position_sync", lambda: _Syncer(broker))

    return _setup


def _done_lines(caplog):
    return [r.getMessage() for r in caplog.records if "[持仓对账] 完成" in r.getMessage()]


def test_all_playbook_managed_reports_zero_added(env, caplog):
    env({"US.AAA": _bp(), "US.BBB": _bp(qty=5, cost=50.0)})
    sched = _Scheduler()
    with caplog.at_level(logging.INFO):
        pm._reconcile_position_store(sched)
    assert sched.position_store.count() == 0
    lines = _done_lines(caplog)
    assert len(lines) == 1
    assert "+0 纳管, 2 跳过" in lines[0]
    assert "内存共 0 个" in lines[0]


def test_mixed_reports_actual_added(env, caplog):
    env({"US.AAA": _bp(), "US.CCC": _bp(qty=7, cost=200.0)})
    sched = _Scheduler()
    with caplog.at_level(logging.INFO):
        pm._reconcile_position_store(sched)
    assert sched.position_store.count() == 1
    assert sched.position_store.get("US.CCC") is not None
    lines = _done_lines(caplog)
    assert len(lines) == 1
    assert "+1 纳管, 1 跳过" in lines[0]
    assert "内存共 1 个" in lines[0]


def test_no_candidates_no_completion_log(env, caplog):
    env({})
    sched = _Scheduler()
    with caplog.at_level(logging.INFO):
        pm._reconcile_position_store(sched)
    assert _done_lines(caplog) == []