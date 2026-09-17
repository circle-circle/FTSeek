import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.risk.capital_safety as capital_safety_module
from app.risk.capital_safety import CapitalSafetySystem


def test_load_state_filters_by_trade_env(tmp_path):
    db_path = str(tmp_path / "capital_state.db")

    sim = CapitalSafetySystem(
        db_path=db_path, initial_capital=1000.0, trade_env=1
    )
    sim._state.current_equity = 1100.0
    sim._state.peak_equity = 1200.0
    sim._state.is_locked = True
    sim._state.lock_reason = "sim_lock"
    sim._save_state()

    live = CapitalSafetySystem(
        db_path=db_path, initial_capital=5000.0, trade_env=0
    )
    live._state.current_equity = 4900.0
    live._state.peak_equity = 5100.0
    live._save_state()

    reloaded_live = CapitalSafetySystem(
        db_path=db_path, initial_capital=1.0, trade_env=0
    )
    assert reloaded_live._state.initial_capital == 5000.0
    assert reloaded_live._state.peak_equity == 5100.0
    assert reloaded_live._peak_initialized is True

    reloaded_sim = CapitalSafetySystem(
        db_path=db_path, initial_capital=1.0, trade_env=1
    )
    assert reloaded_sim._state.initial_capital == 1000.0
    assert reloaded_sim._state.peak_equity == 1200.0
    assert reloaded_sim._state.is_locked is True
    assert reloaded_sim._state.lock_reason == "sim_lock"


def test_reset_capital_none_persists_uninitialized_state(tmp_path):
    system = CapitalSafetySystem(
        db_path=str(tmp_path / "capital_reset.db"),
        initial_capital=1000.0,
        trade_env=1,
    )
    system._state.current_equity = 900.0
    system._state.peak_equity = 1200.0
    system._state.is_locked = True
    system._state.lock_reason = "old_lock"
    system._save_state()

    system.reset_capital(None)

    assert system._state.peak_equity == 0.0
    assert system._state.is_locked is False
    assert system._peak_initialized is False

    reloaded = CapitalSafetySystem(
        db_path=str(tmp_path / "capital_reset.db"),
        initial_capital=1.0,
        trade_env=1,
    )
    assert reloaded._state.peak_equity == 0.0
    assert reloaded._state.is_locked is False
    assert reloaded._peak_initialized is False


def test_singleton_rejects_trade_env_mismatch(monkeypatch):
    existing = SimpleNamespace(trade_env=1)
    monkeypatch.setattr(capital_safety_module, "_capital_instance", existing)

    assert capital_safety_module.get_capital_safety_system(trade_env=1) is existing
    with pytest.raises(RuntimeError, match="trade_env 不一致"):
        capital_safety_module.get_capital_safety_system(trade_env=0)
