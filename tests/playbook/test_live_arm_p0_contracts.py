"""P0 contracts for SIM/LIVE capital, risk, manual holds, and STOP protection."""

import inspect
import sqlite3
from pathlib import Path

import app.risk.capital_safety as capital_safety_module
from app.coordinator.manual_hold_registry import (
    ManualHoldRegistry,
    manual_hold_registry,
)
from app.playbook.live_arm_gate import (
    evaluate_live_arm_gate,
    halt_live_new_positions,
    read_live_arm_halt,
)
from app.playbook.playbook_service import PlaybookService


_ARM_CONFIG = {
    "enabled": True,
    "mode": "engineering_probe",
    "max_order_qty": 1,
    "max_open_positions": 1,
    "session": "RTH",
    "probe": {
        "authorized": True,
        "authorization_id": "TEST-AUTH",
        "direction": "LONG",
    },
}


def _gate_kwargs():
    return {
        "allow_real_trading": True,
        "real_account_id": "123456789",
        "lane_db_name": "ftseek_us_live",
        "instruction_direction": "BUY",
        "market_phase": "TRADING",
        "open_live_positions": 0,
    }


def test_capital_safety_live_arm_has_its_own_ledger(monkeypatch, tmp_path):
    live_ledger = tmp_path / "ftseek_us_live.db"
    created = []

    class FakeCapitalSafety:
        def __init__(self, db_path=None, initial_capital=None, trade_env=1):
            self.db_path = Path(db_path)
            self.trade_env = trade_env
            created.append(self)

    monkeypatch.setattr(capital_safety_module, "_capital_real_instance", None)
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm", lambda target_env: live_ledger
    )
    monkeypatch.setattr(capital_safety_module, "CapitalSafetySystem", FakeCapitalSafety)

    live = capital_safety_module.get_capital_safety_system_for_arm("real")
    same_live = capital_safety_module.get_capital_safety_system_for_arm("live")

    assert live is same_live
    assert live.db_path == live_ledger
    assert live.trade_env == 0
    assert len(created) == 1


def test_manual_hold_registry_keeps_same_symbol_in_both_arms():
    saved = dict(manual_hold_registry._holdings)
    manual_hold_registry._holdings.clear()
    try:
        manual_hold_registry.register("US.MU", "LONG", 1, "sim auto", account="sim")
        manual_hold_registry.register("US.MU", "LONG", 1, "real manual", account="real")

        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="sim") is True
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True

        manual_hold_registry.unregister("US.MU", account="sim")
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="sim") is False
        assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True
    finally:
        manual_hold_registry._holdings.clear()
        manual_hold_registry._holdings.update(saved)


def test_manual_hold_ssot_fallback_reads_live_ledger(monkeypatch, tmp_path):
    live_ledger = tmp_path / "ftseek_us_live.db"
    connection = sqlite3.connect(live_ledger)
    connection.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, owner TEXT, account_type TEXT, size REAL)"
    )
    connection.execute(
        "INSERT INTO open_positions VALUES ('US.MU', 'manual', 'real', 10)"
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        ManualHoldRegistry, "_ssot_cache", {"ts": 0.0, "account": "", "symbols": set()}
    )
    monkeypatch.setattr(
        "app.core.paths.get_trade_records_db_for_arm", lambda target_env: live_ledger
    )

    assert manual_hold_registry.is_auto_blocked("US.MU", target_env="real") is True


def test_halt_state_blocks_live_gate(monkeypatch, tmp_path):
    halt_file = tmp_path / "live_arm_halt.json"
    monkeypatch.setattr(
        "app.playbook.live_arm_gate._HALT_FILE", halt_file
    )

    state = halt_live_new_positions("broker STOP failed")
    assert state["halted"] is True
    assert read_live_arm_halt()["reason"] == "broker STOP failed"

    decision = evaluate_live_arm_gate(_ARM_CONFIG, halted=True, **_gate_kwargs())
    assert decision["allowed"] is False
    assert "LIVE_NEW_POSITIONS_HALTED" in {
        reason["code"] for reason in decision["reasons"]
    }


def test_live_stop_failure_is_fail_close_and_halt_new_live():
    source = inspect.getsource(PlaybookService._execute_via_dual_broker_inner)

    assert "_stop_result.get(\"success\")" in source
    assert "futu_order_id" in source
    assert "halt_live_new_positions" in source
    assert "LIVE_STOP_FAIL_CLOSE" in source
    assert "stop_protection_failed" in source
    assert "既有仓位不平仓" in source
