"""trade → playbook_instructions 归属解析契约 (2026-11-12)."""
from __future__ import annotations

from app.playbook.instruction_attribution import (
    AMBIGUOUS,
    canonical_plan_direction,
    resolve_trade_instruction_ids,
)


def _instruction(**overrides):
    row = {
        "id": 1,
        "date": "2026-11-09",
        "symbol": "US.APP",
        "direction": "BUY",
        "decision_id": "DA-APP",
    }
    row.update(overrides)
    return row


def _trade(**overrides):
    row = {
        "trade_date": "2026-11-09",
        "symbol": "US.APP",
        "direction": "LONG",
        "decision_id": "DA-APP",
        "metadata_json": None,
    }
    row.update(overrides)
    return row


def test_canonical_plan_direction():
    assert canonical_plan_direction("LONG") == "BUY"
    assert canonical_plan_direction("BUY") == "BUY"
    assert canonical_plan_direction("SHORT") == "SELL_SHORT"
    assert canonical_plan_direction("SELL_SHORT") == "SELL_SHORT"
    assert canonical_plan_direction("SKIP") == "SKIP"


def test_unique_decision_id_resolves():
    assert resolve_trade_instruction_ids(
        [_trade()], [_instruction(id=7)]
    ) == [7]


def test_metadata_instruction_id_wins():
    trade = _trade(metadata_json='{"playbook_instruction_id": 2}')
    assert resolve_trade_instruction_ids(
        [trade],
        [_instruction(id=1), _instruction(id=2)],
    ) == [2]


def test_unknown_trade_is_none_not_ambiguous():
    trade = _trade(decision_id="EXT-APP")
    assert resolve_trade_instruction_ids(
        [trade], [_instruction(id=1)]
    ) == [None]


def test_cross_day_duplicate_decision_id_disambiguated_by_trade_context():
    instructions = [
        _instruction(id=1, date="2026-11-08", decision_id="DA-REUSED"),
        _instruction(id=2, date="2026-11-09", decision_id="DA-REUSED"),
    ]
    assert resolve_trade_instruction_ids(
        [_trade(trade_date="2026-11-09", decision_id="DA-REUSED")],
        instructions,
    ) == [2]
    assert resolve_trade_instruction_ids(
        [_trade(trade_date="2026-11-08", decision_id="DA-REUSED")],
        instructions,
    ) == [1]


def test_same_day_same_symbol_same_direction_duplicates_stay_ambiguous():
    instructions = [
        _instruction(id=1, decision_id="SHARED"),
        _instruction(id=2, decision_id="SHARED"),
    ]
    resolved = resolve_trade_instruction_ids(
        [_trade(decision_id="SHARED")], instructions
    )
    assert resolved == [AMBIGUOUS]
    assert resolved[0] is not None
    assert resolved[0] != 1


def test_symbol_and_direction_context_narrows_duplicate():
    instructions = [
        _instruction(id=1, symbol="US.APP", direction="BUY", decision_id="SHARED"),
        _instruction(id=2, symbol="US.TSLA", direction="BUY", decision_id="SHARED"),
    ]
    assert resolve_trade_instruction_ids(
        [_trade(symbol="US.TSLA", direction="LONG", decision_id="SHARED")],
        instructions,
    ) == [2]


def test_duplicate_without_context_fields_stays_ambiguous_not_wrong_id():
    # 旧 caller (只传 id/decision_id) 在重复 decision_id 时必须谨慎, 不得随手取第一行。
    instructions = [{"id": 1, "decision_id": "SHARED"}, {"id": 2, "decision_id": "SHARED"}]
    assert resolve_trade_instruction_ids(
        [_trade(decision_id="SHARED")], instructions
    ) == [AMBIGUOUS]