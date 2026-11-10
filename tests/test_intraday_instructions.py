from __future__ import annotations

from app.event_stream.instructions import (
    build_intraday_instruction_card,
    compact_symbol_event_section,
    compare_instruction_cards,
)

VALID_UNTIL = "2026-11-05T16:00:00-05:00"


def make_brief():
    return {
        "brief_id": "brief-1", "as_of": "2026-11-05T13:35:00+00:00",
        "facts": [{"event_id": "event-1", "member_event_ids": [], "event_time": "2026-11-05T10:00:00+00:00",
                   "first_seen_at": "2026-11-05T11:00:00+00:00", "headline": "AI split"}],
        "transmissions": [{"event_ids": ["event-1"], "theme": "AI split", "sign": "MIXED",
                           "price_in": "UNKNOWN", "invalidation": "broad rally", "causal_chain": "policy to split",
                           "counter_evidence": "rebound", "beneficiary_symbols": ["GOOG"],
                           "loser_symbols": ["NVDA"]}],
        "opposition": "NONE", "fatal_flaw": None, "arbitration_required": False,
    }


def make_result(direction="BUY"):
    return {
        "direction": direction, "score": 7, "event_role": "PRIMARY", "event_ids": ["event-1"],
        "price_in_status": "PARTIAL", "price_in_evidence": "premarket -3% on 2x volume",
        "valid_until": VALID_UNTIL, "skip_reason": "no edge" if direction == "SKIP" else "",
        "execution_plan": {
            "entry_archetype": "PULLBACK_LIMIT", "entry_price": 100,
            "stop_loss_price": 95 if direction == "BUY" else 105,
            "take_profit_price": 110 if direction == "BUY" else 90,
            "confirmation": {"price_level": 100, "side": "below"},
            "invalidation": "new evidence invalidates the thesis",
        },
    }


def make_card(**overrides):
    result = make_result(overrides.get("direction", "BUY"))
    result.update(overrides)
    return build_intraday_instruction_card(
        symbol="US.NVDA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=result, challenger_raw={"opposition": "NONE", "fatal_flaw": None,
                                       "independent_direction": "BUY"},
        brief=make_brief(), arm="treatment",
    )


def test_valid_intraday_card_has_executable_geometry_and_expiry():
    card = make_card()
    assert card["status"] == "VALID"
    assert card["direction"] == "BUY"
    assert card["entry_trigger"]["price"] == 100
    assert card["stop_loss"] == 95
    assert card["target"] == 110
    assert card["valid_until"] == VALID_UNTIL
    assert card["production_write"] is False


def test_ungrounded_event_and_missing_expiry_are_rejected():
    card = make_card(event_ids=["future"], valid_until="tomorrow")
    assert card["status"] == "DEGRADED"
    assert "ungrounded_event:future" in card["validation_errors"]
    assert "valid_until_mismatch" in card["validation_errors"]


def test_missing_event_contract_fields_are_not_silently_defaulted():
    result = make_result()
    del result["event_role"]
    del result["event_ids"]
    card = build_intraday_instruction_card(
        symbol="US.NVDA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=result, challenger_raw={}, brief=make_brief(), arm="treatment",
    )
    assert "missing_event_role" in card["validation_errors"]
    assert "missing_event_ids" in card["validation_errors"]
    assert "challenger_missing_opposition" in card["validation_errors"]
    assert "challenger_missing_fatal_flaw" in card["validation_errors"]


def test_invalid_buy_geometry_is_rejected():
    card = make_card(execution_plan={"entry_price": 100, "stop_loss_price": 101,
                                     "take_profit_price": 110, "invalidation": "x"})
    assert "buy_price_geometry_invalid" in card["validation_errors"]


def test_short_geometry_is_checked_in_opposite_direction():
    card = make_card(direction="SELL_SHORT")
    assert card["status"] == "VALID"
    assert card["stop_loss"] == 105
    assert card["target"] == 90


def test_skip_requires_reason_but_not_numeric_levels():
    card = make_card(direction="SKIP", execution_plan={})
    assert card["status"] == "VALID"
    assert card["skip_reason"] == "no edge"


def test_event_role_none_cannot_claim_shared_event():
    card = make_card(event_role="NONE")
    assert "event_ids_with_none_role" in card["validation_errors"]


def test_price_in_requires_local_evidence():
    card = make_card(price_in_evidence="")
    assert "price_in_requires_evidence" in card["validation_errors"]


def test_fatal_flaw_requires_grounded_evidence():
    card = build_intraday_instruction_card(
        symbol="US.NVDA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=make_result(),
        challenger_raw={"opposition": "OPPOSE", "independent_direction": "SELL_SHORT",
                        "fatal_flaw": {"detail": "bad chain", "evidence_ids": ["invented"]}},
        brief=make_brief(), arm="treatment",
    )
    assert card["arbitration_required"] is True
    assert "ungrounded_fatal_flaw_event:invented" in card["validation_errors"]


def test_control_without_brief_cannot_claim_event_attribution():
    result = make_result()
    card = build_intraday_instruction_card(
        symbol="US.NVDA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=result, challenger_raw={"opposition": "NONE", "fatal_flaw": None}, brief=None, arm="control",
    )
    assert "ungrounded_event:event-1" in card["validation_errors"]


def test_event_grounding_is_limited_to_the_symbol_visible_payload():
    card = build_intraday_instruction_card(
        symbol="US.HCA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=make_result(), challenger_raw={"opposition": "NONE", "fatal_flaw": None},
        brief=make_brief(), arm="treatment", allowed_event_ids=set(),
    )
    assert "ungrounded_event:event-1" in card["validation_errors"]


def test_unrelated_global_fatal_does_not_block_symbol_card():
    brief = make_brief()
    brief["fatal_flaw"] = {"detail": "oil chain wrong", "evidence_ids": ["oil-event"]}
    brief["arbitration_required"] = True
    card = build_intraday_instruction_card(
        symbol="US.NVDA", session_date="2026-11-05", valid_until=VALID_UNTIL,
        result=make_result(), challenger_raw={"opposition": "NONE", "fatal_flaw": None},
        brief=brief, arm="treatment",
    )
    assert card["global_fatal_flaw"] is None
    assert card["arbitration_required"] is False


def test_symbol_section_is_shared_except_brief_and_has_expiry():
    control = compact_symbol_event_section(None, valid_until=VALID_UNTIL)
    treatment = compact_symbol_event_section(make_brief(), valid_until=VALID_UNTIL)
    assert VALID_UNTIL in control and VALID_UNTIL in treatment
    assert "共享简报为空" in control
    assert "event-1" in treatment and "AI split" in treatment


def test_symbol_filter_excludes_unrelated_theme_and_global_fatal():
    brief = make_brief()
    brief["transmissions"].append({
        "event_ids": ["oil-event"], "theme": "oil and Iran", "sign": "NEGATIVE",
        "price_in": "UNKNOWN", "invalidation": "oil rally", "causal_chain": "election to oil",
        "counter_evidence": "AI angst does not make oil an AI trade", "beneficiary_symbols": [], "loser_symbols": [],
    })
    brief["facts"].append({"event_id": "oil-event", "member_event_ids": [], "headline": "Oil falls"})
    brief["fatal_flaw"] = {"detail": "oil chain wrong", "evidence_ids": ["oil-event"]}
    section = compact_symbol_event_section(
        brief, valid_until=VALID_UNTIL, symbol="US.NVDA",
        relevance_text="NVIDIA semiconductors AI chips data center",
    )
    assert "AI split" in section
    assert "oil chain wrong" not in section
    assert '"omitted_transmission_count":1' in section


def test_symbol_filter_understands_company_industry_without_market_wide_sector_text():
    section = compact_symbol_event_section(
        make_brief(), valid_until=VALID_UNTIL, symbol="US.AMD",
        relevance_text='{"name":"Advanced Micro Devices","industry":"Semiconductors"}',
    )
    assert "AI split" in section
    assert '"omitted_transmission_count":0' in section


def test_comparator_reports_direction_change_without_claiming_economic_value():
    control = make_card(event_role="NONE", event_ids=[])
    treatment = make_card(direction="SELL_SHORT")
    comparison = compare_instruction_cards(control, treatment)
    assert comparison["direction_changed"] is True
    assert comparison["economic_value_claimed"] is False
