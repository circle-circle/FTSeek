"""P0+P1 推送治理契约 (2026-10-18).

覆盖:
  ① 通道心跳跨进程共享证据 + 全局 ping 时隙;
  ② 复盘推送聚合 (orchestrator 不再重复推日报/周报);
  ③ 次日提示内容指纹去重;
  ④ ReviewEffectTracker 不再让失败/不可回滚动作进入 pending;
  ⑤ daily_alert_summary 可逐通道关闭 (Hermes 同通道重复);
  ⑥ 低频日报降频的配置面契约。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


class _RecordingChannel:
    name = "stub_dingtalk"
    priority = 1

    def __init__(self, ok: bool = True):
        self.sent = []
        self._ok = ok
        self._available = True

    def is_available(self) -> bool:
        return self._available

    def send(self, msg) -> bool:
        self.sent.append(msg)
        return self._ok


# ══════════ ① 通道心跳跨进程共享 ══════════
def test_shared_business_evidence_suppresses_other_process(tmp_path):
    from app.push.health import HealthMonitor

    state = str(tmp_path / "push_health_state.json")
    ch1 = _RecordingChannel()
    hm1 = HealthMonitor([ch1], suppress_window_sec=14400, shared_state_path=state)
    hm1.note_success(ch1.name)  # 进程 A 的业务推送成功

    ch2 = _RecordingChannel()
    hm2 = HealthMonitor([ch2], suppress_window_sec=14400, shared_state_path=state)
    hm2._ping_all()  # 进程 B 应看到共享证据, 跳过 ping

    assert ch2.sent == [], "跨进程业务成功证据未生效 ⇒ cron 仍会重复通道心跳"
    row = hm2.get_status()["channels"][0]
    assert row["ping_suppressed"] == 1


def test_global_ping_slot_allows_one_process_per_interval(tmp_path):
    from app.push.health import HealthMonitor

    state = str(tmp_path / "push_health_state.json")
    ch1 = _RecordingChannel()
    hm1 = HealthMonitor([ch1], interval_sec=3600, shared_state_path=state)
    hm1._ping_all()

    ch2 = _RecordingChannel()
    hm2 = HealthMonitor([ch2], interval_sec=3600, shared_state_path=state)
    hm2._ping_all()

    assert len(ch1.sent) == 1
    assert ch2.sent == [], "同一 interval 内第二个进程仍 ping = 全局时隙失效"
    assert hm2.get_status()["ping_slot_skipped"] == 1


def test_shared_state_path_empty_keeps_legacy_behavior():
    from app.push.health import HealthMonitor

    ch = _RecordingChannel()
    hm = HealthMonitor([ch], interval_sec=3600, shared_state_path="")
    hm._ping_all()
    hm._ping_all()
    assert len(ch.sent) == 2, "空路径必须回到进程内原行为 (单测/回退)"


# ══════════ ② 复盘推送聚合 ══════════
class _Notifier:
    def __init__(self):
        self.sent = []

    def send_text(self, text):
        self.sent.append(text)


def _stub_orchestrator(monkeypatch, notifier):
    from app.review.review_orchestrator import ReviewOrchestrator
    import app.core.memory_manager as mm

    class _Memory:
        def update_from_attribution(self, doc):
            return None

    monkeypatch.setattr(mm, "get_memory_manager", lambda: _Memory())
    orch = ReviewOrchestrator(notifier=notifier)
    orch.collector.collect_daily = lambda date: {"closed_trades": []}
    orch.analyst.analyze = lambda data: {}
    orch.renderer.render_daily = lambda data, analysis: "REPORT"
    orch._extract_lessons = lambda analysis, data: 0
    return orch


def test_orchestrator_daily_push_report_false_suppresses(monkeypatch):
    n = _Notifier()
    orch = _stub_orchestrator(monkeypatch, n)
    out = orch.run_daily("2026-10-18", push_report=False)
    assert n.sent == [], "push_report=False 仍推送 ⇒ 日报集群重复"
    assert out["report_text"] == "REPORT"


def test_orchestrator_daily_default_still_pushes(monkeypatch):
    n = _Notifier()
    orch = _stub_orchestrator(monkeypatch, n)
    orch.run_daily("2026-10-18")
    assert n.sent == ["REPORT"], "手动调用默认行为不得改变"


def test_scheduler_triggers_disable_orchestrator_push():
    import inspect
    from app.core import scheduler_triggers as st

    daily = inspect.getsource(st.check_daily_review)
    weekly = inspect.getsource(st.check_weekly_review)
    assert "push_report=False" in daily, "日复盘未抑制 orchestrator 重复推送"
    assert "push_report=False" in weekly, "周复盘未抑制 orchestrator 重复推送"


# ══════════ ③ 次日提示内容指纹去重 ══════════
def test_hint_fingerprint_ignores_order_and_lesson_count(monkeypatch, tmp_path):
    from app.core import scheduler_triggers as st

    monkeypatch.setattr(st, "_REVIEW_HINT_STATE", tmp_path / "hint.json")
    h1 = {"top_risk": "R", "avoid_sessions": ["a", "b"], "caution_symbols": ["X", "Y"]}
    h2 = {"top_risk": "R", "avoid_sessions": ["b", "a"], "caution_symbols": ["Y", "X"]}
    fp1 = st._daily_hint_fingerprint(h1)
    assert fp1 == st._daily_hint_fingerprint(h2), "顺序变化不应触发重推"

    assert st._review_hint_push_needed(fp1) is True
    st._mark_review_hint_pushed(fp1)
    assert st._review_hint_push_needed(fp1) is False, "同内容重复推送"

    h3 = dict(h1, top_risk="R2")
    assert st._review_hint_push_needed(st._daily_hint_fingerprint(h3)) is True


def test_non_actionable_hint_not_pushed():
    from app.core.scheduler_triggers import _has_actionable_review_hint

    assert _has_actionable_review_hint({}) is False
    assert _has_actionable_review_hint({"top_risk": "x"}) is True
    assert _has_actionable_review_hint({"avoid_sessions": ["a"]}) is True
    assert _has_actionable_review_hint({"caution_symbols": ["X"]}) is True


# ══════════ ④ ReviewEffectTracker pending 判据 ══════════
def _result(issue_id, *, success, rollback_version=""):
    from app.core.review_loop import ActionResult
    return ActionResult(
        issue_id=issue_id,
        action_taken="test",
        success=success,
        rollback_version=rollback_version,
    )


def test_pending_checks_exclude_failed_and_non_rollback_actions(tmp_path):
    from app.core.review_loop import ReviewEffectTracker

    t = ReviewEffectTracker(tracker_path=str(tmp_path / "effects.json"))
    past = (datetime.now() - timedelta(days=1)).isoformat()

    t.record_action(_result("failed", success=False, rollback_version="v1"), {})
    t.record_action(_result("no_version", success=True), {})
    t.record_action(_result("ok", success=True, rollback_version="v2"), {})
    for a in t._effects["actions"]:
        a["check_due"] = past

    pending = t.get_pending_checks()
    assert [p["issue_id"] for p in pending] == ["ok"], (
        "失败/不可回滚动作进入 pending ⇒ 每天重复「自动回滚失败」告警"
    )


def test_record_action_dedupes_unchecked_same_issue(tmp_path):
    from app.core.review_loop import ReviewEffectTracker

    t = ReviewEffectTracker(tracker_path=str(tmp_path / "effects.json"))
    t.record_action(_result("same", success=True, rollback_version="v1"), {})
    t.record_action(_result("same", success=True, rollback_version="v2"), {})
    rows = [a for a in t._effects["actions"] if a["issue_id"] == "same"]
    assert len(rows) == 1
    assert rows[0]["rollback_version"] == "v2", "新成功动作应刷新基线/版本"


def test_pending_checks_dedupe_legacy_duplicates(tmp_path):
    from app.core.review_loop import ReviewEffectTracker

    t = ReviewEffectTracker(tracker_path=str(tmp_path / "effects.json"))
    past = (datetime.now() - timedelta(days=1)).isoformat()
    for _ in range(3):
        t.record_action(_result("legacy", success=True, rollback_version="v1"), {})
    # 绕过 record_action 去重, 模拟历史脏数据
    base = t._effects["actions"][0]
    t._effects["actions"].append(dict(base))
    t._effects["actions"].append(dict(base))
    for a in t._effects["actions"]:
        a["check_due"] = past
    assert len(t.get_pending_checks()) == 1


# ══════════ ⑤ daily_alert_summary 逐通道开关 ══════════
def test_daily_alert_summary_push_report_flags(monkeypatch):
    import scripts.daily_alert_summary as das

    calls = []
    monkeypatch.setattr(das, "push_to_dingtalk", lambda t, c: calls.append("ding") or True)
    monkeypatch.setattr(das, "push_to_feishu", lambda t, c: calls.append("fs") or True)

    das.push_report("t", "c")
    assert calls == ["ding", "fs"]

    calls.clear()
    das.push_report("t", "c", feishu=False)
    assert calls == ["ding"], "Hermes 场景必须能关闭脚本内飞书"

    calls.clear()
    das.push_report("t", "c", dingtalk=False, feishu=False)
    assert calls == []


def test_daily_alert_summary_cli_has_no_feishu():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "daily_alert_summary.py").read_text(
        encoding="utf-8")
    assert "--no-feishu" in src
    hermes_jobs = Path("/root/.hermes/cron/jobs.json")
    if not hermes_jobs.exists():
        pytest.skip("Hermes jobs.json 不在本机 (仓外载体)")
    assert "daily_alert_summary.py --no-feishu" in hermes_jobs.read_text(
        encoding="utf-8"
    ), "Hermes 盘后综合复盘未改用 --no-feishu ⇒ 飞书同通道双推仍在"


# ══════════ ⑥ 配置面契约 ══════════
def test_config_declares_push_governance_keys():
    from pathlib import Path

    cfg = (Path(__file__).resolve().parents[1] / "config" / "unified_config.yaml").read_text(
        encoding="utf-8")
    assert "intraday_heartbeat_interval_min: 360" in cfg
    assert "health_check_interval_sec" in cfg
    assert "health_check_shared_state" in cfg


def test_get_push_hub_wires_shared_health_state():
    import inspect
    from app.push import hub as hub_mod

    src = inspect.getsource(hub_mod.get_push_hub)
    assert "push.health_check_shared_state" in src
    assert "health_shared_state_path=health_shared_path" in src
    assert "push_health_state.json" in src


def test_main_wires_heartbeat_interval_from_config():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "push.intraday_heartbeat_interval_min" in src
    assert "HeartbeatLoop(interval_min=_hb_interval_min" in src
