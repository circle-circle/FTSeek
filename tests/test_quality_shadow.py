"""record-only 指令质量影子标签契约 (2026-10-18)."""
from app.playbook.quality_shadow import attach_quality_shadow, build_quality_shadow


def _inst(**over):
    base = {
        "date": "2026-10-18",
        "symbol": "US.AMD",
        "direction": "BUY",
        "confidence": 70,
        "decision_id": "DA-20261018-US.AMD-1",
        "raw_instruction": {
            "source": "deep_analysis",
            "score": 8.0,
            "score_band": "HIGH",
            "entry_archetype": "PULLBACK_LIMIT",
            "score_lowered_by_challenger": True,
            "evidence_gaps": ["gap1", "gap2"],
            "challenger_score_shadow": 6.0,
        },
    }
    base.update(over)
    return base


def test_high_score_long_with_challenger_disagreement_is_downgraded():
    tags = build_quality_shadow(_inst())
    assert tags["direction"] == "LONG"
    assert tags["score_band"] == "HIGH"
    assert tags["would_downgrade_high_score_long"] is True
    assert tags["evidence_gaps"] == 2


def test_high_score_long_without_warning_is_not_downgraded():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "HIGH",
        "score_lowered_by_challenger": False,
        "evidence_gaps": [],
        "corroborated_by_market": True,
        "self_tier": "HARD_EVENT",
        "entry_archetype": "PULLBACK_LIMIT",
    }
    tags = build_quality_shadow(inst)
    assert tags["would_downgrade_high_score_long"] is False


def test_medium_pullback_is_preferred():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "entry_archetype": "PULLBACK_LIMIT",
    }
    tags = build_quality_shadow(inst)
    assert tags["would_prefer_medium_pullback"] is True


def test_thesis_only_long_requires_market_confirmation():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "origin": {"self_tier": "THESIS_ONLY"},
    }
    tags = build_quality_shadow(inst)
    assert tags["evidence_tier"] == "THESIS_ONLY"
    assert tags["would_require_market_confirmation"] is True


def test_confidence_basis_maps_market_confirmation():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "ue_confidence_basis": "MARKET_CONFIRMED",
    }
    tags = build_quality_shadow(inst)
    assert tags["confidence_basis"] == "MARKET_CONFIRMED"
    assert tags["market_corroborated"] is True
    assert tags["would_require_market_confirmation"] is False


def test_transmission_basis_requires_market_confirmation():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "ue_confidence_basis": "TRANSMISSION_ASSUMED",
    }
    tags = build_quality_shadow(inst)
    assert tags["market_corroborated"] is False
    assert tags["would_require_market_confirmation"] is True


def test_short_unknown_borrow_is_tagged():
    inst = _inst(direction="SELL_SHORT", decision_id="universe_expansion:2026-10-18:ERAS")
    inst["raw_instruction"] = {
        "origin": {"short_check": {"borrow_data_basis": "UNKNOWN"}},
    }
    tags = build_quality_shadow(inst)
    assert tags["direction"] == "SHORT"
    assert tags["borrow_data_basis"] == "UNKNOWN"
    assert tags["would_allow_unknown_short_small"] is True


def test_event_reaction_fields_propagate():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "ue_event_reaction": {
            "age_h": 23.2,
            "post_event_move_pct": 3.77,
            "reaction_basis": "same_day_close",
            "source": "futu_daily_kline",
            "reason": None,
        },
    }
    tags = build_quality_shadow(inst)
    assert tags["event_age_h"] == 23.2
    assert tags["post_event_move_pct"] == 3.77
    assert tags["reaction_basis"] == "same_day_close"
    assert tags["reaction_source"] == "futu_daily_kline"
    assert tags["reaction_reason"] is None


def test_quality_report_supports_event_reaction_dimensions():
    from scripts.quality_shadow_report import DIMENSIONS, _dim_value

    assert {"reaction_basis", "reaction_source", "event_age_bucket"} <= set(DIMENSIONS)
    assert _dim_value({"event_age_h": 23.2}, "event_age_bucket") == "12-24h"
    assert _dim_value({"event_age_h": 2.0}, "event_age_bucket") == "<=6h"
    assert _dim_value({"event_age_h": None}, "event_age_bucket") == "UNKNOWN"
    assert "plan_gap_bucket" in DIMENSIONS
    assert _dim_value({"plan_gap_pct": 2.5}, "plan_gap_bucket") == ">=1%"
    assert _dim_value({"plan_gap_pct": -1.2}, "plan_gap_bucket") == "<=-1%"
    assert _dim_value({"plan_gap_pct": 0.2}, "plan_gap_bucket") == "-1..1%"


def test_plan_gap_fields_propagate():
    inst = _inst()
    inst["entry_price"] = 61.74
    inst["raw_instruction"] = {
        "score_band": "MEDIUM",
        "entry_archetype": "PULLBACK_LIMIT",
        "planned_entry": 60.2,
        "confirmation": {"price_level": 60.2, "side": "below"},
    }
    tags = build_quality_shadow(inst)
    assert tags["planned_entry"] == 60.2
    assert tags["plan_gap_pct"] == 2.558


def test_skip_has_no_quality_tags():
    assert build_quality_shadow({"direction": "SKIP"}) == {}


def test_attach_merges_metadata_and_preserves_existing_keys():
    inst = _inst(metadata={"entry_confirm": {"status": "CONFIRMED"}})
    tags = attach_quality_shadow(inst)
    assert tags["schema"] == "quality_shadow_v2"
    assert inst["metadata"]["entry_confirm"] == {"status": "CONFIRMED"}
    assert inst["metadata"]["quality_shadow"]["score_band"] == "HIGH"


def test_source_event_debate_attribution_fields():
    inst = _inst()
    inst["raw_instruction"] = {
        "score_band": "HIGH",
        "source": "universe_expansion",
        "origin": {
            "info_channel": "news_headline",
            "first_news_id": "news-1",
            "evidence_ref": "event_id=abc123def456 | family=filing_8k",
        },
        "debate_outcome": "⚠️ 方向一致但挑战者有质疑(BUY)",
        "challenger_self_confidence": "MEDIUM",
        "challenger_direction": "BUY",
        "critique": {"verdict": "DOWNGRADE", "weakest_link": "证据缺口"},
    }
    tags = build_quality_shadow(inst)
    assert tags["source"] == "universe_expansion"
    assert tags["info_channel"] == "news_headline"
    assert tags["first_news_id"] == "news-1"
    assert tags["event_ids"] == ["abc123def456"]
    assert "挑战者" in (tags["debate_outcome"] or "")
    assert tags["challenger_self_confidence"] == "MEDIUM"
    assert tags["critique_verdict"] == "DOWNGRADE"


def test_raw_instruction_json_string_is_parsed():
    inst = _inst(raw_instruction='{"score_band": "MEDIUM", "entry_archetype": "PULLBACK_LIMIT"}')
    tags = build_quality_shadow(inst)
    assert tags["score_band"] == "MEDIUM"
    assert tags["would_prefer_medium_pullback"] is True
