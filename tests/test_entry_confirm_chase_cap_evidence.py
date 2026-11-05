import importlib.util
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "entry_confirm_chase_cap_evidence.py"
    spec = importlib.util.spec_from_file_location("entry_confirm_chase_cap_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = _load_module()


def _row(**overrides):
    row = {
        "type": "entry_confirm",
        "outcome": "CONFIRMED",
        "decision_id": "DA-1",
        "date": "2026-11-05",
        "symbol": "US.TEST",
        "direction": "SHORT",
        "entry_archetype": "FADE_SHORT",
        "session_min": 10,
        "opp_cost_pct": 1.2,
    }
    row.update(overrides)
    return row


def test_cap_only_changes_otherwise_confirmed_rows():
    assert evidence.cap_hit(_row(), 1.0)
    assert not evidence.cap_hit(_row(outcome="ABANDONED"), 1.0)
    assert not evidence.cap_hit(_row(opp_cost_pct=0.99), 1.0)
    assert not evidence.cap_hit(_row(opp_cost_pct=None), 1.0)


def test_fade_cap_scope_is_exact():
    assert evidence.cap_hit(_row(), 1.0, fade_open_only=True)
    assert not evidence.cap_hit(_row(entry_archetype="PULLBACK_LIMIT"), 1.0, True)
    assert not evidence.cap_hit(_row(session_min=15), 1.0, True)
    assert not evidence.cap_hit(_row(entry_archetype=None), 1.0, True)


def test_archived_delta_does_not_double_count_opp_cost():
    assert evidence.archived_policy_delta(_row(), {"pnl_net": -150.0}, 1.0) == 150.0
    assert evidence.archived_policy_delta(_row(), {"pnl_net": 50.0}, 1.0) == -50.0
    assert evidence.archived_policy_delta(_row(outcome="ABANDONED"), {"pnl_net": -150.0}, 1.0) == 0.0


def test_missing_pnl_never_counts_as_evidence():
    result = evidence.evaluate_confirmed(
        [_row()], {"DA-1": {"pnl_net": None}}, 1.0, bootstrap=100
    )
    assert result["joined_n"] == 0
    assert result["unjoined_n"] == 1
    assert result["n_capped"] == 0
    assert result["pass"] is False


def test_forward_requires_all_gates_not_just_positive_sum():
    rows = [_row(decision_id=f"DA-{index}", date=f"2026-10-{day:02d}",
                 chase_shadow_would_abandon=True)
            for index, day in enumerate(range(1, 5))]
    trades = {f"DA-{index}": {"pnl_net": -150.0, "exit_reason": "SL_INITIAL"}
              for index in range(4)}
    result = evidence.evaluate_confirmed(rows, trades, 1.0, require_shadow_field=True, bootstrap=100)
    assert result["delta_sum_usd"] == pytest.approx(600.0)
    assert result["gates"]["sample"] is False
    assert result["pass"] is False


def test_post_watch_move_is_relative_to_abandon_price():
    assert evidence._directional_move_pct("SHORT", 100.0, 99.0) == 1.0
    assert evidence._directional_move_pct("LONG", 100.0, 99.0) == -1.0


def test_sign_flip_is_day_clustered_and_zero_safe():
    assert evidence._sign_flip_p_day_clustered({"2026-11-05": [0.0, 0.0]}, 100) == 1.0
    by_day = {f"2026-10-{day:02d}": [100.0, -100.0] for day in range(1, 5)}
    assert evidence._sign_flip_p_day_clustered(by_day, 100) == 1.0
