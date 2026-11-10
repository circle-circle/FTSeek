from __future__ import annotations

from app.event_stream.instructions import reconcile_intraday_instruction
from tests.test_intraday_instructions import make_card

NOW = "2026-11-05T12:00:00-05:00"


def test_new_actionable_card_issues_offline_instruction():
    result = reconcile_intraday_instruction(None, make_card(), as_of=NOW)
    assert result["action"] == "ISSUE"
    assert result["instruction_version"] == 1
    assert result["production_write"] is False


def test_identical_card_is_no_change():
    previous = make_card()
    candidate = make_card()
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(previous, candidate, as_of=NOW)
    assert result["action"] == "NO_CHANGE"
    assert result["reason"] == "IDENTICAL_DECISION"


def test_unexecuted_changed_card_revises_with_lineage():
    previous = make_card()
    candidate = make_card(direction="SELL_SHORT")
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(previous, candidate, as_of=NOW)
    assert result["action"] == "REVISE"
    assert result["instruction_version"] == 2
    assert result["supersedes_decision_id"] == previous["symbol"].join(["OFFLINE-2026-11-05-", "-v1"])


def test_expired_unexecuted_instruction_invalidates():
    result = reconcile_intraday_instruction(None, make_card(), as_of="2026-11-05T16:00:00-05:00")
    assert result["action"] == "NO_CHANGE"
    assert result["reason"] == "EXPIRED"
    previous = make_card()
    candidate = make_card()
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(previous, candidate, as_of="2026-11-05T16:00:00-05:00")
    assert result["action"] == "INVALIDATE"
    assert result["reason"] == "EXPIRED"


def test_stale_candidate_cannot_overwrite_newer_instruction():
    previous = make_card()
    candidate = make_card(direction="SELL_SHORT")
    result = reconcile_intraday_instruction(previous, candidate, as_of=NOW)
    assert result["action"] == "NO_CHANGE"
    assert result["reason"] == "STALE_REVISION"


def test_execution_state_is_never_overwritten():
    previous = make_card()
    candidate = make_card(direction="SELL_SHORT")
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    for flag in ("triggered", "executed", "broker_order_active", "entry_confirm_active"):
        result = reconcile_intraday_instruction(previous, candidate, as_of=NOW, execution_state={flag: True})
        assert result["action"] == "NO_CHANGE"
        assert result["reason"] == "EXECUTION_STATE_PROTECTED"
        assert result["review_required"] is True


def test_degraded_candidate_does_not_invalidate_good_previous():
    previous = make_card()
    candidate = make_card(valid_until="bad")
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(previous, candidate, as_of=NOW)
    assert result["action"] == "NO_CHANGE"
    assert result["reason"] == "INVALID_CANDIDATE"


def test_skip_invalidates_unexecuted_previous_but_does_not_issue_new():
    assert reconcile_intraday_instruction(None, make_card(direction="SKIP", execution_plan={}), as_of=NOW)["action"] == "NO_CHANGE"
    previous = make_card()
    candidate = make_card(direction="SKIP", execution_plan={})
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(previous, candidate, as_of=NOW)
    assert result["action"] == "INVALIDATE"
    assert result["reason"] == "SKIP"


def test_eod_protection_does_not_flatten_or_overwrite_executed_position():
    previous = make_card()
    candidate = make_card(direction="SELL_SHORT")
    candidate["as_of"] = "2026-11-05T13:00:00-05:00"
    result = reconcile_intraday_instruction(
        previous, candidate, as_of="2026-11-05T16:00:00-05:00",
        execution_state={"executed": True},
    )
    assert result["action"] == "NO_CHANGE"
    assert result["reason"] == "EXECUTION_STATE_PROTECTED"
