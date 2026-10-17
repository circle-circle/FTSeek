"""盘后 EOD 孤儿平仓顺序契约 (2026-10-17 审计).

无指令/全终态/已追踪早退时, force_close_orphaned_sim_positions 必须仍执行,
否则遗留 SIM 仓无人兜底。
"""
import logging

import app.playbook.pipeline_parts.tracking_flow as tf


class _Scheduler:
    pass


def _run(monkeypatch, instructions, *, trading_day=True, force=False,
         tracked_date=None):
    class FakeDB:
        def get_instructions_by_date(self, today):
            return instructions

    monkeypatch.setattr("app.playbook.db.PlaybookDB", FakeDB)
    calls = []

    def force_close(scheduler):
        calls.append("force_close")
        return 1

    def ic_backfill(scheduler, today):
        calls.append("ic")

    scheduler = _Scheduler()
    if tracked_date:
        scheduler._playbook_tracked_date = tracked_date

    result = tf.run_playbook_track(
        scheduler,
        force=force,
        today_provider=lambda: "2026-10-17",
        playbook_enabled=lambda: True,
        trading_day_today=lambda: trading_day,
        run_ic_backfill_only=ic_backfill,
        force_close_orphaned_sim_positions=force_close,
        now_et_provider=lambda: None,
        module_file=__file__,
        logger=logging.getLogger("test_tracking_flow"),
    )
    return calls, result, scheduler


def test_no_instructions_still_runs_orphan_close(monkeypatch):
    calls, result, scheduler = _run(monkeypatch, [])
    assert calls == ["force_close", "ic"]
    assert result is None
    assert scheduler._playbook_tracked_date == "2026-10-17"


def test_all_terminal_instructions_still_runs_orphan_close(monkeypatch):
    calls, _, _ = _run(monkeypatch, [{"outcome": "win"}, {"outcome": "void"}])
    assert calls == ["force_close", "ic"]


def test_already_tracked_still_runs_orphan_close(monkeypatch):
    calls, _, _ = _run(monkeypatch, [], tracked_date="2026-10-17")
    assert calls == ["force_close", "ic"]


def test_non_trading_day_skips_orphan_close(monkeypatch):
    calls, result, _ = _run(monkeypatch, [], trading_day=False)
    assert calls == []
    assert result is None
