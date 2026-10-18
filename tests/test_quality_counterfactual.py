"""Quality counterfactual report tests (2026-10-18)."""
import json

from scripts.quality_counterfactual import evaluate, included


def _tags(**over):
    base = {
        "direction": "LONG", "score_band": "HIGH",
        "would_downgrade_high_score_long": False,
        "would_require_market_confirmation": False,
        "would_prefer_medium_pullback": False,
    }
    base.update(over)
    return base


def test_included_rules():
    high = _tags(score_band="HIGH")
    downgrade = _tags(would_downgrade_high_score_long=True)
    medium_pullback = _tags(score_band="MEDIUM", would_prefer_medium_pullback=True)
    short = _tags(direction="SHORT", score_band="HIGH")
    assert included(high, "all") is True
    assert included(high, "exclude_high_score_long") is False
    assert included(downgrade, "exclude_high_score_long_downgrade") is False
    assert included(medium_pullback, "prefer_medium_pullback") is True
    assert included(high, "prefer_medium_pullback") is False
    assert included(short, "exclude_high_score_long") is True
    assert included(short, "prefer_medium_pullback") is True


def _row(row_id, symbol, direction, fwd, raw):
    return {
        "id": row_id, "symbol": symbol, "direction": "BUY" if direction == "LONG" else "SELL_SHORT",
        "confidence": 70, "executed": 1, "fwd_return_t1_open": fwd,
        "metadata_json": None,
        "raw_instruction_json": json.dumps(raw),
    }


def test_evaluate_excludes_high_score_long_and_counts_missed():
    rows = [
        _row(1, "A", "LONG", 0.01, {
            "score_band": "HIGH", "score_lowered_by_challenger": True,
            "evidence_gaps": ["a", "b"]}),
        _row(2, "B", "LONG", 0.02, {"score_band": "MEDIUM",
                                    "entry_archetype": "PULLBACK_LIMIT"}),
        _row(3, "C", "SHORT", 0.03, {"score_band": "HIGH"}),
    ]
    trades = {
        1: {"net_bps": -10.0, "net": -100.0},
        2: {"net_bps": 20.0, "net": 100.0},
        3: {"net_bps": 30.0, "net": 50.0},
    }
    out = evaluate(rows, trades, "exclude_high_score_long_downgrade")
    assert out["n"] == 2
    assert out["exec_n"] == 2
    assert out["excluded_n"] == 1
    assert out["missed_fwd_n"] == 1
    assert out["missed_net_sum"] == 0.0
