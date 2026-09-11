"""send_critical 单路化钉 (DEB-20260910-logging §11, 2026-09-11)

钉住两件事, 缺一不可:
  ① PushHub 受理/送达成功 ⇒ 旧 `send_critical_sync` **不得并跑** (否则同渠道重复推送);
  ② PushHub 未受理 (抛异常 / 返回 False) ⇒ 旧路径**必须兜底** ("双重保险"不许被修没了)。

实测动机: 9/10 夜两条 ERROR 各被钉钉推送 **2 次** —
  21:31:25 `pre_market 轮次跨开盘仍未结束 → 入场执行被阻塞` (相隔 10ms)
  21:32:28 `Watchdog: playbook_exit_loop 已 146s 无心跳`            (相隔 1ms)
一条是 PushHub 渲染的 🚨 截断版, 一条是旧路径全文版。
病根: 2026-05-16 "双路过渡" 把 hub.push() 的返回值丢弃, docstring 写的
"PushHub 失败时旧路径仍跑" 这个条件从未被判定 ⇒ 恒双跑。
"""
from __future__ import annotations

import threading
from builtins import open as builtins_open
from pathlib import Path

import pytest

from app.utils import critical_alerts as ca


class _FakeHub:
    """替身 PushHub — 只记调用, 绝不真发钉钉/飞书。"""

    def __init__(self, result: bool = True, raises: bool = False):
        self._result = result
        self._raises = raises
        self.calls: list = []

    def push(self, msg, sync: bool = False):
        self.calls.append({"title": msg.title, "body": msg.body,
                           "level": msg.level, "sync": sync})
        if self._raises:
            raise RuntimeError("hub down")
        return self._result


@pytest.fixture
def legacy(monkeypatch):
    """把旧多通道路径换成记录器, 返回 (records, done_event)。"""
    records: list = []
    done = threading.Event()

    def _fake_sync(content, *a, **kw):
        records.append(content)
        done.set()
        return True

    monkeypatch.setattr(ca, "send_critical_sync", _fake_sync)
    return records, done


def _install_hub(monkeypatch, hub: _FakeHub) -> _FakeHub:
    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: hub)
    return hub


# ── ① 单路: hub 受理 ⇒ 旧路径不跑 ─────────────────────────────────
def test_async_hub_accepted_does_not_run_legacy(monkeypatch, legacy):
    records, done = legacy
    hub = _install_hub(monkeypatch, _FakeHub(result=True))

    assert ca.send_critical("⚠️ Watchdog: 主循环 X 已 146s 无心跳", async_mode=True) is None

    assert len(hub.calls) == 1
    assert hub.calls[0]["sync"] is False, "async 模式不得阻塞主线 (hub 走异步路径)"
    done.wait(0.5)          # 给旧路径线程留足机会 — 若它错跑了必被记到
    assert records == [], "hub 已受理, 旧路径并跑 = 同渠道重复推送 (9/10 夜实测病形)"


def test_sync_hub_delivered_returns_true_and_skips_legacy(monkeypatch, legacy):
    records, _ = legacy
    hub = _install_hub(monkeypatch, _FakeHub(result=True))

    assert ca.send_critical("⚠️ sync 告警", async_mode=False) is True

    assert hub.calls[0]["sync"] is True, "sync 模式要真实送达结果 ⇒ 必须走 hub sync=True"
    assert records == []


def test_critical_level_and_title_body_split_preserved(monkeypatch, legacy):
    """收敛不得顺手改掉两件事: CRITICAL 级 (hub 靠它决定全通道广播 + flag 兜底)
    与 title/body 切分 (钉钉单条长度限制)。"""
    from app.push import Level
    _records, done = legacy
    hub = _install_hub(monkeypatch, _FakeHub(result=True))

    ca.send_critical("🚨 KILL SWITCH 已激活\n\nReason: drawdown>3%\n\nTime: 2026-09-11T09:00",
                     async_mode=True)

    call = hub.calls[0]
    assert call["level"] == Level.CRITICAL
    assert call["title"] == "🚨 KILL SWITCH 已激活"
    assert "Reason: drawdown>3%" in call["body"]
    done.wait(0.5)
    assert _records == []


# ── ② 兜底: hub 未受理 ⇒ 旧路径必须接管 ───────────────────────────
def test_hub_exception_falls_back_to_legacy(monkeypatch, legacy):
    records, _ = legacy
    _install_hub(monkeypatch, _FakeHub(raises=True))

    ca.send_critical("⚠️ hub 炸了也要送达", async_mode=False)

    assert records == ["⚠️ hub 炸了也要送达"], "hub 异常时旧路径不接管 = 告警链断掉"


def test_hub_rejected_falls_back_to_legacy(monkeypatch, legacy):
    records, _ = legacy
    _install_hub(monkeypatch, _FakeHub(result=False))

    ca.send_critical("⚠️ hub 全通道失败", async_mode=False)

    assert records == ["⚠️ hub 全通道失败"], "hub 返回 False 时旧路径不接管 = 双重保险被修没了"


def test_async_hub_exception_spawns_legacy_thread(monkeypatch, legacy):
    records, done = legacy
    _install_hub(monkeypatch, _FakeHub(raises=True))

    assert ca.send_critical("⚠️ 异步兜底", async_mode=True) is None

    assert done.wait(2.0), "async + hub 异常 ⇒ 应起 CriticalNotifier 线程跑旧路径"
    assert records == ["⚠️ 异步兜底"]


# ── ③ 结构钉: 防回归到"返回值丢弃 ⇒ 恒双跑" ──────────────────────
def test_source_consumes_hub_result_before_legacy_path():
    """病根是"hub.push() 返回值被丢弃"。钉住: 返回值必须被消费, 且旧路径必须在
    `if hub_ok` 短路**之后**才可能出现。撤掉任一条 (改回裸调用 / 把旧路径提前) ⇒ 本钉碎。
    """
    src = Path(ca.__file__).read_text(encoding="utf-8")
    fn = src[src.index("def send_critical("):]
    fn = fn[:fn.index("\ndef ", 1)]
    flat = " ".join(fn.split())

    assert "hub_ok = bool(get_push_hub().push(" in flat, "hub.push() 返回值又被丢弃了"
    assert "if hub_ok:" in flat, "缺少 hub 受理短路 ⇒ 旧路径会无条件并跑"
    assert flat.index("if hub_ok:") < flat.index("target=send_critical_sync"), \
        "旧路径必须排在 hub_ok 短路之后, 否则收敛失效"
    assert "sync=not async_mode" in flat, "sync 语义必须透传给 hub (调用方据此判重试)"


# ── ④ flag 哨兵: 空白 flag 不得算"有未送达告警" (§11.9) ──────────
def test_contentless_flag_is_not_pending(tmp_path, monkeypatch):
    """病形实测: data/critical_alerts.flag 曾是 5/22 遗留的 1 字节 `\\n`
    ⇒ has_pending_critical() 恒 True 达 141 天, 哨兵失灵。"""
    flag = tmp_path / "critical_alerts.flag"
    flag.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(ca, "_flag_path", lambda: str(flag))

    assert ca.has_pending_critical() is False, "空白 flag 不是未送达告警, 恒真=哨兵无信息"


def test_real_flag_content_is_pending(tmp_path, monkeypatch):
    """两条写入方的真内容都必须判为 pending — 加固不得把真告警一起判没。"""
    flag = tmp_path / "critical_alerts.flag"
    monkeypatch.setattr(ca, "_flag_path", lambda: str(flag))

    # 写入方 A: send_critical_sync 的 atomic_write_text 全文
    flag.write_text("[2026-09-11 10:00:00] CRITICAL ALERT NOT DELIVERED\n"
                    "Content: ⚠️ Watchdog stale\nLast error: timeout\n", encoding="utf-8")
    assert ca.has_pending_critical() is True

    # 写入方 B: FlagFileChannel.send 的追加体
    flag.write_text("\n--- 2026-09-11 10:00:00 [critical] risk_alert ---\n🚨 KILL SWITCH\n",
                    encoding="utf-8")
    assert ca.has_pending_critical() is True


def test_missing_flag_is_not_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "_flag_path", lambda: str(tmp_path / "nope.flag"))
    assert ca.has_pending_critical() is False


def test_unreadable_flag_fails_conservative(tmp_path, monkeypatch):
    """读不动 (权限/瞬时锁) → 保守报"有", 不可因加固而漏掉未送达告警。"""
    flag = tmp_path / "critical_alerts.flag"
    flag.write_text("⚠️ 真告警", encoding="utf-8")
    monkeypatch.setattr(ca, "_flag_path", lambda: str(flag))

    real_open = builtins_open

    def _boom(*a, **kw):
        raise OSError("locked")

    monkeypatch.setattr("builtins.open", _boom)
    assert ca.has_pending_critical() is True
    monkeypatch.setattr("builtins.open", real_open)
