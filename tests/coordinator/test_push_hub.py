"""PushHub 核心行为验证 (2026-05-16)

覆盖: dedup, aggregation, fallback, level routing, helper functions
"""
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.push import (
    PushHub, PushMessage, Level, Category,
    trade_entry, trade_exit, critical_alert, daily_review,
    reset_push_hub,
)
from app.push.channels import Channel
from app.push.dedup import DedupCache
from app.push.aggregator import Aggregator


# ─── Mock Channel ───
class MockChannel(Channel):
    def __init__(self, name="mock", priority=1, will_succeed=True):
        self.name = name
        self.priority = priority
        self.will_succeed = will_succeed
        self.sent = []

    def is_available(self):
        return True

    def send(self, msg):
        self.sent.append(msg)
        return self.will_succeed


# ─── Helpers ───
def _make_hub(channels=None, **kw):
    # 测试用同步模式 — 异步要等线程, 验证 sent 计数不可靠
    kw.setdefault("async_mode", False)
    return PushHub(
        channels=channels or [MockChannel()],
        enable_health_check=False,
        **kw,
    )


# ─── Message helpers ───
def test_trade_entry_helper():
    msg = trade_entry("NVDA", "LONG", 100, 145.50, strategy="main", trace_id="abc")
    assert msg.level == Level.LOW
    assert msg.category == Category.TRADE_ENTRY
    assert msg.is_aggregable()
    assert msg.dedup_key == "entry:NVDA:abc"
    assert msg.aggregate_key == "trade_events"
    assert msg.payload["symbol"] == "NVDA"


def test_trade_exit_small_loss_aggregable():
    msg = trade_exit("NVDA", "LONG", -100.0, "STOP_LOSS")
    assert msg.level == Level.LOW
    assert msg.is_aggregable()


def test_trade_exit_large_gain_immediate():
    msg = trade_exit("ARM", "SHORT", 1200.0, "TRAILING_STOP")
    assert msg.level == Level.NORMAL  # > $500 升级
    assert not msg.is_aggregable()


def test_critical_alert_helper():
    msg = critical_alert("RISK_OFF", "回撤 5%", dedup_key="risk_off")
    assert msg.level == Level.CRITICAL
    assert msg.is_critical()


# ─── DedupCache ───
def test_dedup_basic():
    cache = DedupCache(ttl_sec=60)
    assert cache.should_send("k1") is True
    assert cache.should_send("k1") is False  # 60s 内重复


def test_dedup_different_keys():
    cache = DedupCache(ttl_sec=60)
    assert cache.should_send("k1") is True
    assert cache.should_send("k2") is True  # 不同 key 不冲突


def test_dedup_empty_key_passthrough():
    cache = DedupCache(ttl_sec=60)
    assert cache.should_send("") is True
    assert cache.should_send("") is True  # 空 key 不去重


# ─── Hub: 立即推送 ───
def test_hub_immediate_normal_message():
    ch = MockChannel()
    hub = _make_hub([ch])
    msg = critical_alert("test", "body")  # CRITICAL 立即
    assert hub.push(msg) is True
    assert len(ch.sent) == 1
    hub.stop()


def test_hub_dedup_skips_second():
    ch = MockChannel()
    hub = _make_hub([ch])
    msg = critical_alert("test", "body", dedup_key="same_key")
    hub.push(msg)
    hub.push(msg)  # 1min 内重复
    assert len(ch.sent) == 1
    hub.stop()


# ─── Hub: 聚合 ───
def test_hub_low_aggregates_not_immediate():
    ch = MockChannel()
    hub = _make_hub([ch], aggregate_window_sec=60)
    # 3 笔 LOW 入桶, 不立即推
    for i in range(3):
        hub.push(trade_entry(f"SYM{i}", "LONG", 100, 50.0, trace_id=f"t{i}"))
    assert len(ch.sent) == 0  # 入桶不立即推
    hub.flush_aggregated()
    # flush 后应 1 条聚合
    assert len(ch.sent) == 1
    assert "聚合" in ch.sent[0].title
    hub.stop()


def test_hub_critical_always_immediate():
    ch = MockChannel()
    hub = _make_hub([ch])
    hub.push(critical_alert("crit", "body1", dedup_key="k1"))
    hub.push(critical_alert("crit", "body2", dedup_key="k2"))
    assert len(ch.sent) == 2
    hub.stop()


# ─── Hub: fallback ───
def test_hub_fallback_to_secondary_on_failure():
    ch1 = MockChannel(name="ch1", priority=1, will_succeed=False)
    ch2 = MockChannel(name="ch2", priority=2, will_succeed=True)
    hub = _make_hub([ch1, ch2])
    msg = PushMessage(title="t", body="b", level=Level.NORMAL,
                      dedup_key="x")
    assert hub.push(msg) is True
    # 主失败, 备成功
    assert len(ch1.sent) == 1
    assert len(ch2.sent) == 1
    hub.stop()


def test_hub_all_fail_returns_false():
    ch1 = MockChannel(name="ch1", will_succeed=False)
    ch2 = MockChannel(name="ch2", priority=2, will_succeed=False)
    hub = _make_hub([ch1, ch2])
    msg = PushMessage(title="t", body="b", dedup_key="y")
    assert hub.push(msg) is False
    hub.stop()


def test_hub_critical_broadcasts_all():
    ch1 = MockChannel(name="ch1", priority=1, will_succeed=True)
    ch2 = MockChannel(name="ch2", priority=2, will_succeed=True)
    hub = _make_hub([ch1, ch2])
    hub.push(critical_alert("c", "b", dedup_key="bc"))
    assert len(ch1.sent) == 1
    assert len(ch2.sent) == 1  # CRITICAL broadcast 全通道
    hub.stop()


# ─── Aggregator 直接测 ───
def test_aggregator_summary_includes_pnl():
    flushed = []
    agg = Aggregator(window_sec=60, flush_callback=flushed.append)
    agg.add(trade_exit("A", "LONG", 100, "SL"))
    agg.add(trade_exit("B", "SHORT", -50, "TP"))
    agg.flush_all()
    assert len(flushed) == 1
    body = flushed[0].body
    assert "A" in body or "B" in body
    assert flushed[0].payload["total_pnl"] == 50  # 100 + (-50)
    agg.stop()


# ─── 向后兼容 push_text ───
def test_push_text_legacy_api():
    ch = MockChannel()
    hub = _make_hub([ch])
    hub.push_text("legacy text message")
    assert len(ch.sent) == 1
    hub.stop()


# ─── 单例 ───
def test_get_push_hub_singleton():
    reset_push_hub()
    from app.push import get_push_hub
    h1 = get_push_hub()
    h2 = get_push_hub()
    assert h1 is h2
    reset_push_hub()


# ─── R2 异步模式: 验证不阻塞 ───
def test_hub_async_mode_returns_immediately():
    """R2: async=True 下 push() 立即返回, 不等 channel.send 完成"""
    import time as _t

    class SlowChannel(Channel):
        name = "slow"
        priority = 1
        def __init__(self): self.sent = []
        def is_available(self): return True
        def send(self, msg):
            _t.sleep(0.5)  # 模拟慢网络
            self.sent.append(msg)
            return True

    slow = SlowChannel()
    hub = PushHub(channels=[slow], enable_health_check=False, async_mode=True)
    t0 = _t.time()
    hub.push(critical_alert("urgent", "now"))
    elapsed = _t.time() - t0
    assert elapsed < 0.1, f"async push 应立即返回, 实际 {elapsed:.3f}s"
    # 等异步完成
    _t.sleep(1.0)
    assert len(slow.sent) == 1
    hub.stop()
