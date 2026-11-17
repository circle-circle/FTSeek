from scripts.utils.da_sample import (
    ARTIFACT_ENTRY_REASONS,
    is_recovery_artifact,
    split_frequency_population,
    split_signal_artifact,
)


def test_artifact_reasons_are_classified():
    for reason in ("sync_recovery_backfill", "playbook_entry_bookkeeping_compensation", "adopt_to_playbook"):
        assert is_recovery_artifact({"entry_reason": reason}) is True


def test_signal_and_missing_are_not_artifact():
    assert is_recovery_artifact({"entry_reason": "playbook_futu_simulate"}) is False
    assert is_recovery_artifact({"entry_reason": None}) is False
    assert is_recovery_artifact({}) is False


def test_split_preserves_order_and_partitions():
    rows = [
        {"id": 1, "entry_reason": "playbook_futu_simulate"},
        {"id": 2, "entry_reason": "sync_recovery_backfill"},
        {"id": 3, "entry_reason": "playbook_futu_simulate"},
        {"id": 4, "entry_reason": "adopt_to_playbook"},
        {"id": 5},
    ]
    signal, artifact = split_signal_artifact(rows)
    assert [r["id"] for r in signal] == [1, 3, 5]
    assert [r["id"] for r in artifact] == [2, 4]
    assert len(signal) + len(artifact) == len(rows)


def test_artifact_set_is_frozen():
    assert isinstance(ARTIFACT_ENTRY_REASONS, frozenset)
    assert "sync_recovery_backfill" in ARTIFACT_ENTRY_REASONS


def test_frequency_population_preserves_real_recovery_pnl_in_original_order():
    rows = [
        {"id": 1, "entry_reason": "playbook_futu_simulate"},
        {"id": 2, "entry_reason": "sync_recovery_backfill"},
        {"id": 3, "entry_reason": "playbook_entry_bookkeeping_compensation"},
        {"id": 4, "entry_reason": "adopt_to_playbook"},
        {"id": 5},
    ]
    included, excluded = split_frequency_population(rows)
    assert [r["id"] for r in included] == [1, 2, 5]
    assert [r["id"] for r in excluded] == [3, 4]
