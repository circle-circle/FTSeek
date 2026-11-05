import json

from app.playbook.price_ledger import (
    build_price_ledger,
    calculate_risk_amount,
    calculate_risk_reward,
)


def test_directional_risk_reward_and_amount():
    assert calculate_risk_reward("BUY", 100, 95, 110) == 2.0
    assert calculate_risk_reward("SELL_SHORT", 100, 105, 90) == 2.0
    assert calculate_risk_reward("BUY", 100, 105, 110) is None
    assert calculate_risk_reward("SELL_SHORT", 100, 105, 110) is None
    assert calculate_risk_amount(100, 95, 7) == 35.0


def test_legacy_reprice_row_exposes_planned_effective_and_fill_views():
    instruction = {
        "symbol": "US.COIN",
        "direction": "SELL_SHORT",
        "entry_price": 149.0,
        "stop_loss": 157.19,
        "take_profit": 136.71,
        "position_size": 39,
        "risk_amount": 319.41,
        "risk_reward": 4.44,
        "execution_price": 149.085,
        "raw_instruction_json": json.dumps({
            "planned_entry": 145.0,
            "planned_stop_loss": 149.5,
            "planned_take_profit": 125.0,
        }),
        "metadata_json": json.dumps({
            "reprice_audit": {"audit_schema": "reprice_audit_v1"},
            "entry_fill": {"price": 149.085, "dealt_qty": 39},
        }),
    }

    ledger = build_price_ledger(instruction)

    assert ledger["planned"]["entry_price"] == 145.0
    assert ledger["planned_risk_reward"] == 4.44
    assert ledger["effective_risk_reward"] == 1.5006
    assert ledger["fill_risk_reward"] == 1.5268
    assert ledger["risk_amount_at_fill"] == 316.09


def test_v2_audit_preserves_plan_after_canonical_rr_becomes_effective():
    instruction = {
        "direction": "BUY",
        "entry_price": 102.0,
        "stop_loss": 97.0,
        "take_profit": 112.0,
        "position_size": 20,
        "risk_reward": 2.0,
        "raw_instruction_json": json.dumps({
            "planned_entry": 100.0,
            "planned_stop_loss": 95.0,
            "planned_take_profit": 120.0,
        }),
        "metadata_json": json.dumps({
            "reprice_audit": {
                "audit_schema": "reprice_audit_v2",
                "planned_risk_reward": 4.0,
            },
            "execution_risk": {
                "fill_price": 102.0,
                "fill_quantity": 10,
            },
        }),
    }

    ledger = build_price_ledger(instruction)

    assert ledger["planned_risk_reward"] == 4.0
    assert ledger["effective_risk_reward"] == 2.0
    assert ledger["effective"]["risk_amount"] == 100.0
    assert ledger["risk_amount_at_fill"] == 50.0


def test_skipped_da_row_infers_direction_from_price_geometry():
    instruction = {
        "direction": "SKIP",
        "entry_price": 609.65,
        "stop_loss": 645.01,
        "take_profit": 556.61,
        "risk_reward": 2.4,
        "raw_instruction_json": json.dumps({
            "planned_entry": 670.0,
            "planned_stop_loss": 695.0,
            "planned_take_profit": 610.0,
        }),
    }

    ledger = build_price_ledger(instruction)

    assert ledger["planned_risk_reward"] == 2.4
    assert ledger["effective_risk_reward"] == 1.5
