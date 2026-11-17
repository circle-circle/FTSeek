"""VIX 级别告警跨重启冷却：跳变即时，同级每日一次，NORMAL 后重新武装。"""
import json

from app.core import scheduler_core as sc


def test_same_level_survives_restart_and_expires_after_one_day(tmp_path, monkeypatch):
    state = tmp_path / "vix.json"
    monkeypatch.setattr(sc, "_VIX_ALERT_STATE", state)
    now = 1_800_000_000.0
    assert sc._should_send_vix_level_alert("WARNING", now) is True
    sc._mark_vix_level_alert("WARNING", delivered=True, now_ts=now)
    assert sc._should_send_vix_level_alert("WARNING", now + 300) is False
    assert sc._should_send_vix_level_alert("WARNING", now + 86400) is True


def test_level_change_and_normal_rearm(tmp_path, monkeypatch):
    state = tmp_path / "vix.json"
    monkeypatch.setattr(sc, "_VIX_ALERT_STATE", state)
    now = 1_800_000_000.0
    sc._mark_vix_level_alert("WARNING", delivered=True, now_ts=now)
    assert sc._should_send_vix_level_alert("HIGH", now + 60) is True
    sc._mark_vix_level_alert("NORMAL", delivered=False, now_ts=now + 120)
    assert sc._should_send_vix_level_alert("WARNING", now + 180) is True


def test_normal_does_not_rewrite_or_delay_rearm(tmp_path, monkeypatch):
    state = tmp_path / "vix.json"
    monkeypatch.setattr(sc, "_VIX_ALERT_STATE", state)
    now = 1_800_000_000.0
    sc._mark_vix_level_alert("NORMAL", delivered=False, now_ts=now)
    first = json.loads(state.read_text())["pushed_at_epoch"]
    sc._mark_vix_level_alert("NORMAL", delivered=False, now_ts=now + 999)
    assert json.loads(state.read_text())["pushed_at_epoch"] == first


def test_corrupt_state_fails_open(tmp_path, monkeypatch):
    state = tmp_path / "vix.json"
    state.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(sc, "_VIX_ALERT_STATE", state)
    assert sc._should_send_vix_level_alert("WARNING", 1_800_000_000.0) is True
