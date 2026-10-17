"""配置漂移回调接线 (2026-10-17 审计修复)

病形: `ConfigDriftDetector` 早就支持 `on_drift_detected`, 但 main.py 构造时没传 ⇒
盘外修改配置只落 ~/.hermes 本地日志 + WARNING, 推送侧零感知。本钉保证:
  ① 漂移回调经 PushHub 推 HIGH/SYSTEM (不是直连 notifier);
  ② 推送失败/异常不得打断 4h 巡检线程 (回调异常由 detector 兜接, 这里自身也不抛);
  ③ main.py 构造检测器时必须接上回调 (结构钉)。
"""
from __future__ import annotations

from pathlib import Path

import main as main_mod
from app.push import Category, Level


class _FakeHub:
    def __init__(self, result: bool = True, raises: bool = False):
        self._result = result
        self._raises = raises
        self.calls: list = []

    def push(self, msg, sync: bool = False):
        self.calls.append(msg)
        if self._raises:
            raise RuntimeError("hub down")
        return self._result


def _install_hub(monkeypatch, hub: _FakeHub) -> _FakeHub:
    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: hub)
    return hub


_DIFF = {
    "old_hash": "aaaa1111", "new_hash": "bbbb2222",
    "old_keys": 1200, "new_keys": 1203, "key_count_diff": 3,
}


def test_drift_alert_pushes_high_system_via_hub(monkeypatch):
    hub = _install_hub(monkeypatch, _FakeHub())

    main_mod._config_drift_alert(_DIFF)

    assert len(hub.calls) == 1
    msg = hub.calls[0]
    assert msg.level == Level.HIGH, "漂移是需人工确认的告警, 不能降成 NORMAL 静默"
    assert msg.category == Category.SYSTEM
    assert "aaaa1111" in msg.body and "bbbb2222" in msg.body
    assert msg.dedup_key == "config_drift:aaaa1111->bbbb2222", \
        "dedup 用 hash 对: 同一漂移不反复推, 新漂移仍能推"


def test_drift_alert_undelivered_is_non_fatal(monkeypatch):
    _install_hub(monkeypatch, _FakeHub(result=False))
    main_mod._config_drift_alert(_DIFF)  # 不抛 = 回调不会杀巡检线程


def test_drift_alert_hub_exception_is_non_fatal(monkeypatch):
    _install_hub(monkeypatch, _FakeHub(raises=True))
    main_mod._config_drift_alert(_DIFF)


def test_main_wires_drift_callback_into_detector():
    """结构钉: main.py 构造 ConfigDriftDetector 时必须传 on_drift_detected。"""
    src = Path(main_mod.__file__).read_text(encoding="utf-8")
    assert "on_drift_detected=_config_drift_alert" in src, \
        "回调又没接上 ⇒ 漂移只落本地日志, 值班端零感知"
