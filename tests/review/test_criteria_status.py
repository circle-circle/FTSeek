from pathlib import Path

from app.review.criteria_status import (
    ESCALATION_OVERDUE_DAYS,
    escalation_candidates,
    guard_escalation,
    pending_adjudications,
    update_ready_state,
)


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
    # 与真实 dataclass 对齐 (RFC-092 + §10.3): 缺失会让 pending_adjudications 直接 AttributeError
    no_adjudication_reason = ""


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
            # 样本量达标但**判据从未读过** ⇒ 这正是"待裁面" (RFC-092 §10.3 语义):
            "adjudicated_at": "",
            "recheck_after": "",
            "needs_adjudication": True,
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


# ── 逾期升级双门槛 (DEB-20260919 §13.4 / §20.2④) ─────────────────────────────


def test_escalation_candidates_filters_overdue_and_sorts_oldest_first():
    rows = [
        {"id": "recent", "ready": True},
        {"id": "old-a", "ready": True},
        {"id": "old-b", "ready": True},
        {"id": "not-ready", "ready": False},
    ]
    ready_state = {
        "recent": {"ready_since": "2026-09-20"},     # 4 days (<14)
        "old-a": {"ready_since": "2026-09-01"},      # 23 days (>14)
        "old-b": {"ready_since": "2026-09-05"},      # 19 days (>14)
    }
    result = escalation_candidates(rows, ready_state, "2026-09-24")
    assert [r["id"] for r in result] == ["old-a", "old-b"]
    assert result[0]["days_ready"] == 23
    assert result[1]["days_ready"] == 19


def test_escalation_candidates_excludes_exactly_14_days():
    """门槛是 >14 (严格大于), 恰好 14 天不升级 (保守: 门槛日不算逾期)."""
    rows = [{"id": "boundary", "ready": True}]
    ready_state = {"boundary": {"ready_since": "2026-09-10"}}
    result = escalation_candidates(rows, ready_state, "2026-09-24")
    assert result == []


def test_escalation_candidates_handles_missing_ready_since():
    """ready_since 缺失 = 保守不升级 (不得因为 state 损坏误催)."""
    rows = [{"id": "no-since", "ready": True}]
    ready_state = {}
    result = escalation_candidates(rows, ready_state, "2026-09-24")
    assert result == []


def test_guard_escalation_picks_most_overdue_and_writes_state(tmp_path):
    state_path = tmp_path / "esc_state.json"
    lock_path = tmp_path / "esc_state.lock"
    candidates = [
        {"id": "old-a", "ready": True, "ready_since": "2026-09-01", "days_ready": 23},
        {"id": "old-b", "ready": True, "ready_since": "2026-09-05", "days_ready": 19},
    ]
    pick = guard_escalation(candidates, state_path, lock_path, "2026-09-24")
    assert pick is not None
    assert pick["id"] == "old-a"
    assert pick["days_ready"] == 23
    # 状态文件记录了今天的催办
    import json
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_escalated_at"] == "2026-09-24"
    assert state["last_escalated_id"] == "old-a"


def test_guard_escalation_blocks_second_call_same_day(tmp_path):
    """同一天第二次调用 → None (每天最多 1 条)."""
    state_path = tmp_path / "esc_state.json"
    lock_path = tmp_path / "esc_state.lock"
    candidates = [
        {"id": "old-a", "ready": True, "ready_since": "2026-09-01", "days_ready": 23},
    ]
    first = guard_escalation(candidates, state_path, lock_path, "2026-09-24")
    second = guard_escalation(candidates, state_path, lock_path, "2026-09-24")
    assert first is not None and first["id"] == "old-a"
    assert second is None


def test_guard_escalation_returns_none_when_no_candidates(tmp_path):
    state_path = tmp_path / "esc_state.json"
    lock_path = tmp_path / "esc_state.lock"
    pick = guard_escalation([], state_path, lock_path, "2026-09-24")
    assert pick is None
    assert not state_path.exists(), "无候选时不应写状态文件"


def test_guard_escalation_resets_next_day(tmp_path):
    """次日重置: 新的一天可以再催一条."""
    state_path = tmp_path / "esc_state.json"
    lock_path = tmp_path / "esc_state.lock"
    candidates_day1 = [
        {"id": "old-a", "ready": True, "ready_since": "2026-09-01", "days_ready": 23},
    ]
    candidates_day2 = [
        {"id": "old-b", "ready": True, "ready_since": "2026-09-05", "days_ready": 20},
    ]
    pick1 = guard_escalation(candidates_day1, state_path, lock_path, "2026-09-24")
    pick2 = guard_escalation(candidates_day2, state_path, lock_path, "2026-09-25")
    assert pick1 is not None and pick1["id"] == "old-a"
    assert pick2 is not None and pick2["id"] == "old-b"

