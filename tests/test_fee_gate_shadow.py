from app.playbook import shadow_registry
from scripts.fee_gate_shadow import (
    append_new_rows,
    build_shadow_row,
    gate_decisions,
    load_rows,
    summarize,
)


def _row(trade_id=1, entry_price=0.5, quantity=100, pnl_net=-10.0):
    return {
        "trade_id": trade_id,
        "trade_date": "2026-09-21",
        "symbol": "US.CTNT",
        "source_type": "PLAYBOOK_SIM",
        "direction": "LONG",
        "entry_time": "2026-09-21T09:30:00-04:00",
        "exit_time": "2026-09-21T10:00:00-04:00",
        "exit_reason": "SL_INITIAL",
        "entry_price": entry_price,
        "quantity": quantity,
        "realized_pnl": -5.0,
        "fees_est": 5.0,
        "pnl_net": pnl_net,
    }


def test_gate_decisions_cover_price_and_notional_candidates():
    decisions = gate_decisions(0.5, 5000.0)
    assert decisions["price_lt_1"] is True
    assert decisions["notional_lt_4000"] is False
    assert decisions["notional_lt_5000"] is False
    assert decisions["combo_price_1_notional_4000"] is True

    decisions = gate_decisions(2.0, 3500.0)
    assert decisions["combo_price_1_notional_4000"] is True
    decisions = gate_decisions(2.0, 4500.0)
    assert decisions["combo_price_1_notional_4000"] is False


def test_build_shadow_row_uses_net_caliber_and_real_trade_id():
    row = build_shadow_row(_row(), "2026-09-21T00:00:00+00:00")
    assert row["trade_id"] == 1
    assert row["notional"] == 50.0
    assert row["fee_bps"] == 1000.0
    assert row["pnl_caliber"] == "realized_pnl - fees_est"
    assert row["would_block"]["combo_price_1_notional_4000"] is True


def test_append_new_rows_is_idempotent_by_trade_id(tmp_path):
    path = tmp_path / "fee_gate_shadow.jsonl"
    rows = [
        build_shadow_row(_row(1), "2026-09-21T00:00:00+00:00"),
        build_shadow_row(_row(2), "2026-09-21T00:00:00+00:00"),
    ]
    assert append_new_rows(rows, path) == 2
    assert append_new_rows(rows, path) == 0
    assert (
        append_new_rows(
            [build_shadow_row(_row(1), "2026-09-22T00:00:00+00:00")], path
        )
        == 0
    )
    assert len(load_rows(path)) == 2
    assert {row["trade_id"] for row in load_rows(path)} == {1, 2}


def test_summarize_reports_blocked_kept_and_net_loss():
    rows = [
        build_shadow_row(_row(1, 0.5, 100, -10.0), "2026-09-21T00:00:00+00:00"),
        build_shadow_row(_row(2, 2.0, 1999, -20.0), "2026-09-21T00:00:00+00:00"),
        build_shadow_row(_row(3, 2.0, 3000, 30.0), "2026-09-21T00:00:00+00:00"),
        build_shadow_row(_row(4, 2.0, 1250, 15.0), "2026-09-21T00:00:00+00:00"),
    ]
    summary = summarize(rows, "2026-09-21T00:00:00+00:00")
    combo = summary["gates"]["combo_price_1_notional_4000"]
    assert combo["blocked"]["n"] == 3
    assert combo["kept"]["n"] == 1
    assert combo["kept"]["exp_per_trade"] == 30.0
    assert combo["blocked"]["exp_per_trade"] == -5.0
    assert combo["n_loss_pct"] == 75.0
    assert summary["baseline"]["n"] == 4


def test_summarize_carries_excluded_rows():
    summary = summarize([], "2026-09-21T00:00:00+00:00", excluded_rows=2)
    assert summary["n_total"] == 0
    assert summary["excluded_rows"] == 2


def test_fee_gate_shadow_registry_contract():
    items = {item.id: item for item in shadow_registry.REGISTRY}
    item = items["fee_gate_shadow"]
    assert item.data_path == "data/fee_gate_shadow.jsonl"
    assert item.count_mode == "distinct:trade_id"
    assert item.min_n == 30
    assert item.eval_script == "scripts/fee_gate_shadow.py"
    assert item.eval_on_demand is False
    assert item.intent == "fee_floor_cost_guard"
    warnings = shadow_registry.check_intent_uniqueness()
    assert not any("fee_floor_cost_guard" in warning for warning in warnings)
