"""RFC-210 B1: 补录 regime 继承指令真值 (开关默认关 + fail-safe) 契约测试。"""
import json
import sqlite3

import app.core.position_sync as ps
import app.playbook.config as pconfig
import pytest
from app.storage.trade_store import TradeStore as RealTradeStore
from tests.playbook.test_exit_compensation import compensation_case


class _FakeConfig:
    def __init__(self, enabled):
        self._enabled = enabled

    def get(self, key, default=None):
        if key == "backfill_inherit_instruction_regime":
            return self._enabled
        return default


def _inst():
    return object.__new__(ps.USPositionSync)


def _db(tmp_path, regime="cautious", iid=10):
    db_path = tmp_path / "ftseek_test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER, market_regime TEXT)")
    if regime is not None:
        conn.execute("INSERT INTO playbook_instructions VALUES (?,?)", (iid, regime))
    conn.commit()
    conn.close()
    return str(db_path)


def test_switch_off_keeps_range(tmp_path, monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(False))
    regime, source = _inst()._resolve_backfill_regime(10, _db(tmp_path))
    assert regime == "RANGE" and source == "hardcoded_range"


def test_switch_on_inherits_instruction_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    regime, source = _inst()._resolve_backfill_regime(10, _db(tmp_path, "cautious"))
    assert regime == "cautious" and source == "instruction"


def test_switch_on_missing_instruction_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    regime, source = _inst()._resolve_backfill_regime(999, _db(tmp_path, "cautious", iid=10))
    assert regime == "UNKNOWN" and source == "unknown_fallback"


def test_switch_on_no_instruction_id_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    regime, source = _inst()._resolve_backfill_regime(None, _db(tmp_path))
    assert regime == "UNKNOWN" and source == "unknown_fallback"


def test_switch_on_db_error_failsafe_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    bad = tmp_path / "empty.db"
    sqlite3.connect(bad).close()
    regime, source = _inst()._resolve_backfill_regime(10, str(bad))
    assert regime == "UNKNOWN" and source == "unknown_failsafe"


def test_config_error_defaults_off(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(pconfig, "get_playbook_config", _boom)
    regime, source = _inst()._resolve_backfill_regime(10, _db(tmp_path))
    assert regime == "RANGE" and source == "hardcoded_range"


@pytest.fixture
def sealed_backfill(compensation_case, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules['app.storage.trade_store'], 'TradeStore', RealTradeStore)


def test_full_backfill_writes_inherited_regime_when_on(tmp_path, monkeypatch, sealed_backfill):
    db_path = tmp_path / "ftseek_test.db"
    RealTradeStore(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER, market_regime TEXT)")
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?)", (77, "risk_off"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(ps, "_get_db_path", lambda: str(db_path))
    monkeypatch.setattr(ps, "_fetch_entry_vix_for_backfill", lambda: None)
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(True))
    inst = _inst()
    ok = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.TEST", direction="LONG", qty=100, entry=50.0, sl=48.0, tp=55.0,
        trace_id="test-regime", broker_env="sim", futu_order_id="REG-1",
        order_evidence={'confirmed': True, 'order_id': 'REG-1', 'dealt_qty': 100, 'price': 50.0},
        broker_position={'qty': 100, 'direction': 'LONG'}, instruction_id=77)
    assert ok is True
    conn = sqlite3.connect(db_path)
    try:
        regime, meta = conn.execute(
            "SELECT market_regime, metadata_json FROM trade_records").fetchone()
    finally:
        conn.close()
    assert regime == "risk_off"
    assert json.loads(meta)["regime_source"] == "instruction"


def test_full_backfill_keeps_range_when_off(tmp_path, monkeypatch, sealed_backfill):
    db_path = tmp_path / "ftseek_test.db"
    RealTradeStore(str(db_path))
    monkeypatch.setattr(ps, "_get_db_path", lambda: str(db_path))
    monkeypatch.setattr(ps, "_fetch_entry_vix_for_backfill", lambda: None)
    monkeypatch.setattr(pconfig, "get_playbook_config", lambda: _FakeConfig(False))
    inst = _inst()
    ok = ps.USPositionSync._backfill_playbook_trade_record(
        inst, symbol="US.TEST", direction="LONG", qty=100, entry=50.0, sl=48.0, tp=55.0,
        trace_id="test-range", broker_env="sim", futu_order_id="REG-2",
        order_evidence={'confirmed': True, 'order_id': 'REG-2', 'dealt_qty': 100, 'price': 50.0},
        broker_position={'qty': 100, 'direction': 'LONG'}, instruction_id=77)
    assert ok is True
    conn = sqlite3.connect(db_path)
    try:
        regime, meta = conn.execute(
            "SELECT market_regime, metadata_json FROM trade_records").fetchone()
    finally:
        conn.close()
    assert regime == "RANGE"
    assert json.loads(meta)["regime_source"] == "hardcoded_range"
