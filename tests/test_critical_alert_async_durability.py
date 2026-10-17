"""异步兜底告警的写前留痕 (2026-10-17 审计修复)

修前病形: PushHub 未受理时, `send_critical(async_mode=True)` 起的是 **daemon** 线程,
而 `critical_alerts.flag` 只在 `send_critical_sync` **全部重试失败后**才写。进程在
~31s 重试窗口内退出/被杀 (watchdog 重启、os._exit、systemd 停机) ⇒ 线程直接消失,
这条 CRITICAL 既没送达也没有任何落盘痕迹 = 静默丢失。

修后契约:
  ① 兜底线程启动**之前**, 先把 PENDING 记录落盘 (记录本身即 Ops 告警留痕);
  ② 仅当通道确认送达且记录未被后来者覆盖时才清除;
  ③ 送达抛异常 / 线程根本没跑 ⇒ PENDING 必须保留。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.utils import critical_alerts as ca


class _RejectingHub:
    def push(self, msg, sync: bool = False):
        raise RuntimeError("hub down")


@pytest.fixture
def flag(tmp_path, monkeypatch):
    """隔离 flag 路径 + 让 hub 恒拒, 强制走异步兜底。"""
    path = tmp_path / "critical_alerts.flag"
    monkeypatch.setattr(ca, "_flag_path", lambda: str(path))
    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _RejectingHub())
    return path


class _NoStartThread:
    """替身线程: 记录但不启动 — 模拟进程在守护线程开跑前就退出。"""

    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass


def test_pending_is_durable_before_delivery_finishes(flag, monkeypatch):
    """线程还没发完, PENDING 就必须已在盘上; 送达成功后清除。"""
    release = threading.Event()
    done = threading.Event()

    def _slow_sync(content, *a, **kw):
        release.wait(2.0)
        done.set()
        return True

    monkeypatch.setattr(ca, "send_critical_sync", _slow_sync)

    assert ca.send_critical("🚨 KILL SWITCH", async_mode=True) is None

    text = flag.read_text(encoding="utf-8")
    assert ca._PENDING_HEADER in text
    assert "🚨 KILL SWITCH" in text
    assert ca.has_pending_critical() is True

    release.set()
    assert done.wait(2.0)
    deadline = time.time() + 2.0
    while flag.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert not flag.exists(), "确认送达后 PENDING 应清除, 否则哨兵留下假阳"


def test_pending_survives_when_process_exits_before_thread_runs(flag, monkeypatch):
    """daemon 线程没机会跑 (进程退出) ⇒ 留痕必须在, 这是不被静默吞掉的唯一证据。"""
    monkeypatch.setattr(ca.threading, "Thread", _NoStartThread)

    assert ca.send_critical("🚨 进程马上退出", async_mode=True) is None

    text = flag.read_text(encoding="utf-8")
    assert ca._PENDING_HEADER in text
    assert "🚨 进程马上退出" in text


def test_pending_kept_when_delivery_raises(flag, monkeypatch):
    """兜底线程异常 ⇒ 无法确认送达 ⇒ 不可清痕。"""
    done = threading.Event()

    def _boom(content, *a, **kw):
        done.set()
        raise RuntimeError("channel exploded")

    monkeypatch.setattr(ca, "send_critical_sync", _boom)

    ca.send_critical("🚨 通道爆炸", async_mode=True)

    assert done.wait(2.0)
    assert flag.exists()
    assert "🚨 通道爆炸" in flag.read_text(encoding="utf-8")


def test_hub_accepted_does_not_write_pending(tmp_path, monkeypatch):
    """留痕只属于兜底路径 — hub 受理时不得写 flag (否则每条告警都留假 PENDING)。"""
    path = tmp_path / "critical_alerts.flag"
    monkeypatch.setattr(ca, "_flag_path", lambda: str(path))

    class _OKHub:
        def push(self, msg, sync: bool = False):
            return True

    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: _OKHub())

    ca.send_critical("hub 正常", async_mode=True)
    assert not path.exists()


def test_clear_pending_does_not_wipe_newer_record(flag):
    """并发兜底: 先清旧记录不得抹掉后来者的 PENDING。"""
    first = ca._write_pending("first alert")
    second = ca._write_pending("second alert")
    assert first and second

    ca._clear_pending(first)
    assert flag.exists()
    assert "second alert" in flag.read_text(encoding="utf-8")

    ca._clear_pending(second)
    assert not flag.exists()


def test_delivery_failure_record_wins_over_pending(flag, monkeypatch):
    """全通道失败的 NOT DELIVERED 记录必须仍在 (send_critical_sync 负责写)。"""
    def _fail_sync(content, *a, **kw):
        ca.atomic_write_text(
            str(flag),
            f"[test] CRITICAL ALERT NOT DELIVERED\nContent: {content}\nLast error: timeout\n",
        )
        return False

    done = threading.Event()
    real_fail = _fail_sync

    def _fail_and_signal(content, *a, **kw):
        try:
            return real_fail(content, *a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(ca, "send_critical_sync", _fail_and_signal)

    ca.send_critical("🚨 全通道失败", async_mode=True)

    assert done.wait(2.0)
    text = flag.read_text(encoding="utf-8")
    assert "CRITICAL ALERT NOT DELIVERED" in text
    assert "🚨 全通道失败" in text
