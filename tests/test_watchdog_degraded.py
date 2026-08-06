"""watchdog degraded 阈值生效单测 (2026-08-07 启动竞态修复).

原缺陷: threshold 仅日志装饰, 第一次失败 (x1) 即标记 degraded 推 ERROR 告警,
60s 后自愈 — 纯噪音 (08-06/08-07 两次重启均复现)。
"""
import logging

from app.data.subscription_manager import SubscriptionManager


def _bare_manager():
    sm = object.__new__(SubscriptionManager)
    sm._degraded = False
    sm._degraded_reason = None
    sm._degraded_since = None
    return sm


def test_below_threshold_does_not_mark():
    sm = _bare_manager()
    for i in (1, 2):
        sm._maybe_mark_degraded(threshold=3, reason=f"reconnect_failed_x{i}",
                                level="reconnect", current=i)
        assert not sm._degraded, f"x{i} 不应标记"
        assert sm._degraded_reason is None


def test_at_threshold_marks():
    sm = _bare_manager()
    sm._maybe_mark_degraded(threshold=3, reason="reconnect_failed_x3",
                            level="reconnect", current=3)
    assert sm._degraded
    assert sm._degraded_reason == "reconnect_failed_x3"
    assert sm._degraded_since is not None


def test_without_current_marks_immediately_legacy_semantics():
    # 未传 current 的调用方保持旧行为 (手动/外部标记不受阈值影响)
    sm = _bare_manager()
    sm._maybe_mark_degraded(threshold=3, reason="manual_freeze", level="manual")
    assert sm._degraded


def test_clear_auto_degraded_on_recovery():
    sm = _bare_manager()
    sm._maybe_mark_degraded(threshold=3, reason="reconnect_failed_x3",
                            level="reconnect", current=3)
    assert sm._degraded
    sm._maybe_clear_degraded()
    assert not sm._degraded
    assert sm._degraded_reason is None
    assert sm._degraded_since is None
