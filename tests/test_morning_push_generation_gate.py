"""盘前推送顺序门: 必须等最终生成完成, 09:15 兜底放行 (2026-10-18)."""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import app.playbook.pipeline_parts.generation_flow as gf

ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("test.morning_push_gate")


class FakeDB:
    def __init__(self, instructions, completed):
        self.instructions = instructions
        self.completed = completed

    def get_instructions_by_date(self, date):
        return self.instructions

    def is_generate_completed(self, date):
        return self.completed


class FakePusher:
    calls = []

    def __init__(self, db):
        self.db = db

    def push_morning_instructions(self, trade_date, sync=False):
        FakePusher.calls.append((trade_date, sync))
        return True


def _setup(monkeypatch, now, *, completed=True, instructions=None):
    FakePusher.calls.clear()
    inst = instructions if instructions is not None else [
        {"id": 1, "symbol": "US.TEST", "direction": "BUY"}
    ]
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: now)
    monkeypatch.setattr("app.playbook.db.PlaybookDB", lambda: FakeDB(inst, completed))
    monkeypatch.setattr("app.playbook.feishu_push.PlaybookPusher", FakePusher)
    return inst


def _call(tmp_path):
    return gf.run_playbook_morning_push(
        None, state_path=str(tmp_path / "push_state.json"), logger=LOG
    )


def test_before_0900_waits_even_if_generation_marked_done(monkeypatch, tmp_path):
    _setup(monkeypatch, datetime(2026, 10, 16, 8, 47, tzinfo=ET), completed=True)
    assert _call(tmp_path) is None
    assert FakePusher.calls == []


def test_after_0900_waits_for_generation_completion(monkeypatch, tmp_path):
    _setup(monkeypatch, datetime(2026, 10, 16, 9, 5, tzinfo=ET), completed=False)
    assert _call(tmp_path) is None
    assert FakePusher.calls == []


def test_pushes_after_generation_completion(monkeypatch, tmp_path):
    _setup(monkeypatch, datetime(2026, 10, 16, 9, 5, tzinfo=ET), completed=True)
    assert _call(tmp_path) is True
    assert FakePusher.calls == [("2026-10-16", True)]
    state = json.loads((tmp_path / "push_state.json").read_text())
    assert state["morning"]["2026-10-16"] is True


def test_fallback_pushes_at_0915_when_generation_missing(monkeypatch, tmp_path, caplog):
    _setup(monkeypatch, datetime(2026, 10, 16, 9, 15, tzinfo=ET), completed=False)
    with caplog.at_level(logging.WARNING):
        assert _call(tmp_path) is True
    assert FakePusher.calls == [("2026-10-16", True)]
    assert any("09:15 兜底放行" in r.message for r in caplog.records)


def test_daily_idempotency_still_blocks_second_push(monkeypatch, tmp_path):
    _setup(monkeypatch, datetime(2026, 10, 16, 9, 5, tzinfo=ET), completed=True)
    assert _call(tmp_path) is True
    assert _call(tmp_path) is None
    assert FakePusher.calls == [("2026-10-16", True)]
