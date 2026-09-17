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


@pytest.fixture(autouse=True)
def _isolate_failure_log(tmp_path, monkeypatch):
    """全通道失败落盘重定向到 tmp — 原实现在真实环境跑测试时把 t/b 写进生产 data/push_failures.jsonl。"""
    import app.core.paths as _paths
    monkeypatch.setattr(_paths, "get_data_dir", lambda: tmp_path)


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
# 2026-07-16 语义变更: 成交/平仓是关键事件, trade_entry/trade_exit 一律
# NORMAL + 立即推送, 不再走 LOW+聚合 (根因: 7/15 聚合窗口丢通知).
def test_trade_entry_helper():
    msg = trade_entry("NVDA", "LONG", 100, 145.50, strategy="main", trace_id="abc")
    assert msg.level == Level.NORMAL
    assert msg.category == Category.TRADE_ENTRY
    assert not msg.is_aggregable()  # 成交不聚合, 立即推
    assert msg.dedup_key == "entry:NVDA:abc"
    assert msg.aggregate_key is None
    assert msg.payload["symbol"] == "NVDA"


def test_trade_exit_small_loss_immediate():
    msg = trade_exit("NVDA", "LONG", -100.0, "STOP_LOSS")
    assert msg.level == Level.NORMAL  # 小额亏损也立即推 (2026-07-16)
    assert not msg.is_aggregable()


def test_trade_exit_large_gain_immediate():
    msg = trade_exit("ARM", "SHORT", 1200.0, "TRAILING_STOP")
    assert msg.level == Level.NORMAL
    assert not msg.is_aggregable()


@pytest.mark.parametrize("symbol", ["US.TEST", "TEST", "US.FAKE.TEST"])
def test_hub_blocks_test_symbols(symbol):
    """测试标的一律不进真实推送通道。"""
    ch = MockChannel()
    hub = _make_hub([ch])
    msg = trade_exit(symbol, "LONG", 1.0, "TEST")

    assert hub.push(msg) is True
    assert ch.sent == []
    hub.stop()


def test_hub_blocks_test_symbol_entry():
    """开仓通知同样不得透出测试标的。"""
    ch = MockChannel()
    hub = _make_hub([ch])
    msg = trade_entry("US.TEST", "LONG", 1, 10.0, strategy="main")

    assert hub.push(msg) is True
    assert ch.sent == []
    hub.stop()


def test_hub_allows_normal_symbols():
    """正常标的仍照常推送，防止测试过滤误伤。"""
    ch = MockChannel()
    hub = _make_hub([ch])
    msg = trade_exit("US.ARM", "LONG", 1.0, "TP_HIT")

    assert hub.push(msg) is True
    assert len(ch.sent) == 1
    hub.stop()


def test_critical_alert_helper():
    msg = critical_alert("RISK_OFF", "回撤 5%", dedup_key="risk_off")
    assert msg.level == Level.CRITICAL
    assert msg.is_critical()


# ─── DedupCache ───
def test_dedup_basic():
    cache = DedupCache(ttl_sec=60)
    assert cache.should_send("k1") is True
    # DEB-20260903 #110: should_send 改 peek (不记录), 发送成功后 mark_sent 才占窗
    cache.mark_sent("k1")
    assert cache.should_send("k1") is False  # 60s 内重复


def test_dedup_retry_not_blocked_without_mark():
    """DEB-20260903 #110: 发送失败(未 mark_sent)时重试不被误判为重复"""
    cache = DedupCache(ttl_sec=60)
    assert cache.should_send("k1") is True   # peek, 不占窗
    # 发送失败, 不调 mark_sent
    assert cache.should_send("k1") is True   # 重试仍放行


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
def _low_aggregable(symbol, kind="entry", pnl=None, dedup=None):
    """构造 LOW+aggregate_key 消息 (trade_entry/exit 已升 NORMAL, 聚合路径用原始消息测)"""
    payload = {"symbol": symbol, "kind": kind, "direction": "LONG"}
    if pnl is not None:
        payload["pnl"] = pnl
        payload["exit_reason"] = "SL"
    return PushMessage(
        title=f"evt {symbol}", body="b", level=Level.LOW,
        category=Category.TRADE_ENTRY if kind == "entry" else Category.TRADE_EXIT,
        dedup_key=dedup or f"{kind}:{symbol}", aggregate_key="trade_events",
        payload=payload,
    )


def test_hub_low_aggregates_not_immediate():
    ch = MockChannel()
    hub = _make_hub([ch], aggregate_window_sec=60)
    # 3 笔 LOW 入桶, 不立即推
    for i in range(3):
        hub.push(_low_aggregable(f"SYM{i}"))
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
    # send_retries=0: 关闭 2026-07-17 新增的单通道重试, 只验证 fallback 语义
    hub = _make_hub([ch1, ch2], send_retries=0)
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
    # trade_exit 已升 NORMAL 不入桶 (2026-07-16) — 用原始 LOW 消息测聚合汇总
    agg.add(_low_aggregable("A", kind="exit", pnl=100))
    agg.add(_low_aggregable("B", kind="exit", pnl=-50))
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
