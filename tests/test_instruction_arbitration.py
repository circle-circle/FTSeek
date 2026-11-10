from __future__ import annotations

from app.event_stream.instructions import arbitrate_intraday_card
from tests.test_intraday_instructions import make_card


def test_abstain_is_not_an_opposition_hard_block():
    card = make_card()
    card["opposition"] = "ABSTAIN"
    card["independent_direction"] = "SKIP"
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "BUY"
    assert result["changed"] is False


def test_evidenced_oppose_skip_blocks_in_r3_only():
    card = make_card()
    card["opposition"] = "OPPOSE"
    card["independent_direction"] = "SKIP"
    card["opposition_evidence"] = {"detail": "data contradicts", "event_ids": [], "data_evidence": ["price broke"]}
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "SKIP"
    assert result["reason"] == "EVIDENCED_OPPOSE_SKIP"


def test_true_reverse_blocks_unresolved_direction():
    card = make_card()
    card["opposition"] = "OPPOSE"
    card["independent_direction"] = "SELL_SHORT"
    card["opposition_evidence"] = {"detail": "data contradicts", "event_ids": [], "data_evidence": ["price broke"]}
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "SKIP"
    assert result["reason"] == "UNRESOLVED_TRUE_REVERSE"


def test_fatal_flaw_blocks_instead_of_being_appended_to_risks():
    card = make_card()
    card["fatal_flaw"] = {"detail": "unsupported", "evidence_ids": ["event-1"]}
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "SKIP"
    assert result["reason"] == "UNRESOLVED_FATAL_FLAW"
    assert result["repair_attempts"] == 0


def test_related_global_fatal_flaw_blocks_symbol_card():
    card = make_card()
    card["global_fatal_flaw"] = {"detail": "unsupported", "evidence_ids": ["event-1"]}
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "SKIP"
    assert result["reason"] == "UNRESOLVED_GLOBAL_FATAL_FLAW"


def test_same_direction_oppose_does_not_fake_reverse():
    card = make_card()
    card["opposition"] = "OPPOSE"
    card["independent_direction"] = "BUY"
    result = arbitrate_intraday_card(card)
    assert result["structured_direction"] == "BUY"
    assert result["changed"] is False
