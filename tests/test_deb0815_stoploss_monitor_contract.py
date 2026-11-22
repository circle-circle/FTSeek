"""DEB-20261122 F1 test-only 契约: 累计止损 tripped 后不重写配置/状态。

隔离 tmp DB/CFG/STATE，不触生产库/配置/state；不改监控脚本行为。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MONITOR = REPO / "ops" / "hermes" / "deb0815_stoploss_monitor.py"


def _load_monitor(monkeypatch, tmp_path, net):
    spec = importlib.util.spec_from_file_location("deb0815_stoploss_monitor", MONITOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = tmp_path / "ftseek_us.db"
    cfg = tmp_path / "unified_config.yaml"
    state = tmp_path / "deb0815_stoploss_state.json"
    monkeypatch.setattr(module, "DB", db)
    monkeypatch.setattr(module, "CFG", cfg)
    monkeypatch.setattr(module, "STATE", state)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trade_records (strategy_id TEXT, pnl_net REAL)")
    con.execute("INSERT INTO trade_records VALUES ('playbook_sim', ?)", (net,))
    con.commit()
    con.close()
    cfg.write_text("playbook:\n  auto_execute: true\n", encoding="utf-8")
    return module, cfg, state


def test_tripped_early_return_does_not_rewrite_config_or_state(monkeypatch, tmp_path, capsys):
    module, cfg, state = _load_monitor(monkeypatch, tmp_path, net=-5000.0)
    state.write_text(json.dumps({"tripped": True}), encoding="utf-8")
    before_cfg = cfg.read_bytes()
    before_state = state.read_bytes()
    module.main()
    assert cfg.read_bytes() == before_cfg
    assert state.read_bytes() == before_state
    assert capsys.readouterr().out == ""


def test_no_state_and_above_stop_line_is_silent(monkeypatch, tmp_path, capsys):
    module, cfg, state = _load_monitor(monkeypatch, tmp_path, net=-1000.0)
    before_cfg = cfg.read_bytes()
    module.main()
    assert cfg.read_bytes() == before_cfg
    assert not state.exists()
    assert capsys.readouterr().out == ""


def test_trigger_path_writes_only_in_isolated_tmp(monkeypatch, tmp_path):
    module, cfg, state = _load_monitor(monkeypatch, tmp_path, net=-5000.0)
    module.main()
    assert "auto_execute: false" in cfg.read_text(encoding="utf-8")
    assert json.loads(state.read_text(encoding="utf-8"))["tripped"] is True
    assert module.STOP_LINE == -4840.06