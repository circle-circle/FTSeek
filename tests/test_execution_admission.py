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
    """reprice 后的 (价, 量) 才是被评估的口径。

    2026-11-03: 名义额须过**活**地板 (min_notional_usd 2500→5500, 2026-10-31 P3) ——
    原夹具 50 × 60 = $3,000 在当前口径下是 REJECT ⇒ 该用例自 10-31 起常驻红。
    夹具改为按活配置派生股数 (tests/helpers/notional.py)。
    """
    from tests.helpers.notional import qty_above_min_notional
    qty = qty_above_min_notional(50.0)
    # reprice 同时带 stop_loss: 原夹具的 SL=95 是按 entry=100 定的, reprice 到 $50 后
    # 止损反而高于入场 (|50-95|/50 = 90%) ⇒ 撞上 2026-10-31 P3 启用的 max_sl_pct=8%
    # (此前该判据为 0=关 ⇒ 老夹具恒 PASS)。reprice 参数本就支持带 stop_loss。
    res = aef.execution_admission_evaluate(
        _inst(size=1, entry=100.0),
        {"entry_price": 50.0, "position_size": qty, "stop_loss": 47.0, "take_profit": 55.0})
    assert res["verdict"] == "PASS"
    assert res["metrics"]["notional"] == 50.0 * qty


def test_non_da_instruction_is_not_double_gated():
    assert aef.execution_admission_evaluate(
        _inst(decision_id="TFB-US.ACI-1", size=1, entry=100.0)) is None


def test_switch_off_is_record_only(monkeypatch):
    monkeypatch.setattr(aef, "_admission_at_execution_enabled", lambda: False)
    assert aef.execution_admission_evaluate(_inst(size=1, entry=100.0)) is None
