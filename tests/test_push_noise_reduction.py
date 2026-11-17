"""2026-11-14 推送降噪批契约：低信息量静默，安全告警语义不变。"""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

from app.core import scheduler_triggers as st


REPO = Path(__file__).resolve().parents[1]


def test_config_noise_switches_and_intervals():
    cfg = (REPO / "config" / "unified_config.yaml").read_text(encoding="utf-8")
    assert "lifecycle_notifications: false" in cfg
    assert "intraday_heartbeat_interval_min: 360" in cfg
    assert "health_check_interval_sec: 21600" in cfg


def test_lifecycle_notifications_default_off_and_opt_in(monkeypatch):
    import main
    from app.core import config_container

    class _Cfg:
        def __init__(self, value):
            self.value = value

        def get_dotted(self, key, default=None):
            assert key == "push.lifecycle_notifications"
            return self.value

    monkeypatch.setattr(config_container, "get_config", lambda: _Cfg(False))
    assert main._lifecycle_notifications_enabled() is False
    monkeypatch.setattr(config_container, "get_config", lambda: _Cfg(True))
    assert main._lifecycle_notifications_enabled() is True


def test_weekly_review_state_survives_process_restart(tmp_path, monkeypatch):
    state = tmp_path / "weekly.json"
    monkeypatch.setattr(st, "_WEEKLY_REVIEW_PUSH_STATE", state)
    assert st._weekly_review_already_sent("2026-11-13") is False
    st._weekly_review_mark_sent("2026-11-13")
    assert st._weekly_review_already_sent("2026-11-13") is True
    assert st._weekly_review_already_sent("2026-11-20") is False
    assert json.loads(state.read_text())["week_end"] == "2026-11-13"


def test_weekly_review_corrupt_state_fails_open(tmp_path, monkeypatch):
    state = tmp_path / "weekly.json"
    state.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(st, "_WEEKLY_REVIEW_PUSH_STATE", state)
    assert st._weekly_review_already_sent("2026-11-13") is False


def _trend_module():
    path = REPO / "ops" / "hermes" / "premarket_trend_verify.py"
    spec = importlib.util.spec_from_file_location("premarket_trend_verify_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_killed_trend_collects_daily_but_reports_weekly_only():
    mod = _trend_module()
    assert mod.should_deliver_report(datetime(2026, 11, 13)) is False  # 周五采集日
    assert mod.should_deliver_report(datetime(2026, 11, 14)) is True   # 周六汇总
    assert mod.should_deliver_report(datetime(2026, 11, 13), "2026-11-01") is True


def test_small_negative_pnl_is_not_a_large_loss_alert():
    src = (REPO / "ops" / "hermes" / "intraday_diagnose_late.py").read_text(encoding="utf-8")
    assert "PNL_CONCERN_USD = 100.0" in src
    assert "if total_pnl + overnight_net <= -PNL_CONCERN_USD:" in src
    assert "if total_pnl + overnight_net < 0:" not in src


# ── 盘中心跳窗口口径 (2026-11-14 降噪批收尾) ────────────────────────────────
# 间隔 60→360min 后, 卡内三处查询窗若仍写死 30min, 则一张卡只描述 6h 里的 30min
# 却读作"当前状态" ⇒ 口径失真。以下钉住"窗口跟随间隔"且"标签==实查窗口"。

def _hb_module():
    from app.observability import intraday_heartbeat as hb
    return hb


def test_resolve_window_min_clamps_and_preserves_legacy_default():
    hb = _hb_module()
    assert hb._resolve_window_min(None) == 30      # 旧语义: 未传 ⇒ 30
    assert hb._resolve_window_min("x") == 30       # 非法值 fail-safe 到 30, 不抛
    assert hb._resolve_window_min(5) == 30         # 下限
    assert hb._resolve_window_min(360) == 360      # 生产值原样通过
    assert hb._resolve_window_min(1440) == 390     # 上限 = 6.5h 盘中, 防跨日卡


def _stub_queries(monkeypatch, hb, seen):
    """把六个查询换成定值, 只记录三个带窗口的查询实际收到的 window_min。"""
    monkeypatch.setattr(hb, "_query_today_trades", lambda: {
        "today_trades": 0, "today_pnl": 0.0, "today_wins": 0, "positions": []})
    monkeypatch.setattr(hb, "_query_risk_state", lambda window_hours=8: {
        "breakers": 0, "flat_all": 0, "window_hours": window_hours})
    monkeypatch.setattr(hb, "_query_service_health", lambda scheduler=None: {
        "uptime_sec": 60, "thread_ok": True, "broker_ok": True})

    def _rec(name, ret):
        def _fn(window_min=30):
            seen[name] = window_min
            return ret
        return _fn

    monkeypatch.setattr(hb, "_query_signal_funnel", _rec("funnel", {
        "window_min": 0, "entries": 0, "rejects": 0, "top_reasons": []}))
    monkeypatch.setattr(hb, "_query_llm_health", _rec("llm", {
        "calls": 0, "success_pct": 0, "pre_gate_calls": 0, "pre_gate_blocked": 0}))
    monkeypatch.setattr(hb, "_query_alert_counts", _rec("alerts", {
        "errors": 0, "warnings": 0, "top_warning": "", "truncated": False}))


def test_card_queries_and_labels_both_follow_interval(monkeypatch):
    hb = _hb_module()
    seen = {}
    _stub_queries(monkeypatch, hb, seen)
    text = hb._format_heartbeat(None, window_min=360)
    assert seen == {"funnel": 360, "llm": 360, "alerts": 360}
    assert text.count("近360min") == 3
    assert "近30min" not in text


def test_card_window_default_stays_30min(monkeypatch):
    """未传 window_min 的调用方逐字保持旧行为 (回滚面)。"""
    hb = _hb_module()
    seen = {}
    _stub_queries(monkeypatch, hb, seen)
    text = hb._format_heartbeat(None)
    assert seen == {"funnel": 30, "llm": 30, "alerts": 30}
    assert text.count("近30min") == 3


def test_alert_log_truncation_is_surfaced_not_hidden(monkeypatch):
    """尾扫 5MB 截断时不得呈现为"0 ERROR = 干净"。"""
    hb = _hb_module()
    seen = {}
    _stub_queries(monkeypatch, hb, seen)
    monkeypatch.setattr(hb, "_query_alert_counts", lambda window_min=30: {
        "errors": 0, "warnings": 0, "top_warning": "", "truncated": True})
    assert "日志尾扫截断" in hb._format_heartbeat(None, window_min=360)


def test_heartbeat_loop_carries_interval_into_window():
    """生产接线: HeartbeatLoop 必须把自身 interval_min 传给 push_heartbeat。"""
    hb = _hb_module()
    assert hb.HeartbeatLoop(interval_min=360).interval_min == 360
    src = (REPO / "app" / "observability" / "intraday_heartbeat.py").read_text(encoding="utf-8")
    assert src.count("push_heartbeat(self.scheduler, force=False, "
                     "window_min=self.interval_min)") == 2
    assert "push_heartbeat(self.scheduler, force=False)\n" not in src

