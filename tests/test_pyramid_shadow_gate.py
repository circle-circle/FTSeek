# -*- coding: utf-8 -*-
"""pyramid_shadow 停采门契约 (2026-10-22, 用户裁 FAIL 停采).

钉住:
  1. 门控键路径 = playbook.execution_parameters.pyramid_shadow_enabled (execution_parameters 块内,
     曾误写 playbook.* 顶层 — 回归钉);
  2. false ⇒ observe 零写入 (停采);
  3. 配置读取异常 ⇒ fail-open 维持采集 (停采是治理动作, 读故障不静默扩大影响面);
  4. 门开时行为不变 (arm 行照常落盘)。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.playbook import pyramid_shadow as ps  # noqa: E402


@pytest.fixture()
def shadow_tmp(tmp_path, monkeypatch):
    p = tmp_path / "trail_lock_pyramid.jsonl"
    monkeypatch.setattr(ps, "_PATH", p)
    return p


def _observe():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ps.observe(symbol="US.TESTX", direction="LONG", entry_price=100.0,
               current_price=101.0, qty=10, entry_time_str="2026-10-22T10:00:00-04:00",
               be_armed=True, peak_price=101.0, meta={}, exiting=False,
               now_et=datetime(2026, 10, 22, 15, 52, tzinfo=ZoneInfo("America/New_York")))


def test_gate_off_stops_collection(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.core.config_container.get_config",
                        lambda: type("C", (), {"get_dotted": lambda self, k, d=None: False})())
    _observe()
    assert not shadow_tmp.exists(), "门关 ⇒ 零写入 (停采)"


def test_gate_on_collects(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.core.config_container.get_config",
                        lambda: type("C", (), {"get_dotted": lambda self, k, d=None: True})())
    _observe()
    rows = [json.loads(l) for l in shadow_tmp.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["t"] == "arm", "门开 ⇒ arm 行照常落盘"


def test_config_failure_fail_open(shadow_tmp, monkeypatch):
    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr("app.core.config_container.get_config", _boom)
    _observe()
    assert shadow_tmp.exists(), "配置读失败 ⇒ fail-open 维持采集"


def test_gate_reads_execution_parameters_block():
    """钉键路径: 必须是 playbook.execution_parameters.pyramid_shadow_enabled。"""
    import inspect
    src = inspect.getsource(ps._collection_enabled)
    assert "playbook.execution_parameters.pyramid_shadow_enabled" in src
