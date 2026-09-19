from pathlib import Path

from app.review.criteria_status import pending_adjudications, update_ready_state


class FakeItem:
    id = "fixture-ready"
    name = "Fixture ready"
    status = "READY_TO_EVALUATE"
    current_n = 30
    min_n = 30
    graduation_criteria = "fixture criteria"
    data_path = "data/fixture.jsonl"
    eval_script = None
    count_mode = "lines"


def test_pending_adjudications_reads_registry_without_new_ledger(monkeypatch):
    from app.review import criteria_status

    monkeypatch.setattr(criteria_status, "REGISTRY", [FakeItem()])
    monkeypatch.setattr(criteria_status, "refresh_counts", lambda: [])

    rows = pending_adjudications()

    assert rows == [
        {
            "id": "fixture-ready",
            "status": "READY_TO_EVALUATE",
            "n": 30,
            "min_n": 30,
            "progress": "30/30",
            "ready": True,
            "criteria": "fixture criteria",
            "artifact": "data/fixture.jsonl",
            "eval_script": None,
            "count_mode": "lines",
            "evidence": {
                "kind": "artifact",
                "ref": "app/playbook/shadow_registry.py :: id=fixture-ready",
            },
        }
    ]


def test_ready_state_adds_new_and_removes_left_items(tmp_path):
    state_path = tmp_path / "criteria_ready_state.json"
    lock_path = tmp_path / "criteria_ready_state.lock"
    first = [
        {"id": "ready-a", "ready": True},
        {"id": "ready-b", "ready": True},
    ]
    second = [
        {"id": "ready-a", "ready": True},
        {"id": "ready-b", "ready": False},
    ]

    update_ready_state(first, state_path, lock_path, "2026-09-18")
    state = update_ready_state(second, state_path, lock_path, "2026-09-19")

    assert state == {"ready-a": {"ready_since": "2026-09-18"}}
    assert "ready-b" not in state
