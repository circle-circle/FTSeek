"""2026-10-23: DA admission must be re-evaluated at execution size."""
from app.playbook.pipeline_parts import auto_execute_flow as aef


def _inst(decision_id="DA-20261023-US.ACI-1", size=1, entry=100.0):
    return {"symbol": "US.ACI", "direction": "BUY", "decision_id": decision_id,
            "entry_price": entry, "stop_loss": 95.0, "take_profit": 110.0,
            "position_size": size, "confidence": 50, "date": "2026-10-23"}


def test_da_small_notional_is_rejected_at_execution_size():
    res = aef.execution_admission_evaluate(_inst(size=1, entry=100.0))
    assert res["verdict"] == "REJECT"
    assert any(r.startswith("NOTIONAL_TOO_SMALL") for r in res["reasons"])
    assert res["metrics"]["notional"] == 100.0


def test_reprice_params_are_the_evaluated_size_and_price():
    res = aef.execution_admission_evaluate(
        _inst(size=1, entry=100.0),
        {"entry_price": 50.0, "position_size": 60})
    assert res["verdict"] == "PASS"
    assert res["metrics"]["notional"] == 3000.0


def test_non_da_instruction_is_not_double_gated():
    assert aef.execution_admission_evaluate(
        _inst(decision_id="TFB-US.ACI-1", size=1, entry=100.0)) is None


def test_switch_off_is_record_only(monkeypatch):
    monkeypatch.setattr(aef, "_admission_at_execution_enabled", lambda: False)
    assert aef.execution_admission_evaluate(_inst(size=1, entry=100.0)) is None
