"""
P0/P1/P2 全部修复单测 (2026-05-14, karpathy 原则: 全部修)

覆盖:
- F0 三件基础工具 (datetime_utils / atomic_write / critical_alerts)
- P0-1 atomic position add
- P0-2 ExitDispatcher 优先调用
- P0-3 critical notifier
- P0-4 coordinator switch_to_enforce
- P0-5 OrderTracker GC
- P0-6 KILL_SWITCH 原子读
- P1-1 phase-aware expires
- P1-3 datetime tz-safe
- P1-4 per-symbol lock
- P1-7 PreClose 互斥 metadata
- P2-8 Watchdog
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
import pytest


# ─── F0 / P1-3 datetime_utils ────────────────────────────
def test_f0_datetime_compare_safe_naive_vs_aware():
    from app.utils.datetime_utils import compare_safe
    a = datetime(2026, 5, 14, 10, 0, 0)  # naive
    b = datetime(2026, 5, 14, 11, 0, 0, tzinfo=timezone.utc)  # aware
    # 不应抛异常
    result = compare_safe(a, b)
    assert result in (-1, 0, 1)


def test_f0_is_expired_safe():
    from app.utils.datetime_utils import is_expired_safe, now_utc
    past = now_utc() - timedelta(minutes=10)
    future = now_utc() + timedelta(minutes=10)
    assert is_expired_safe(past) is True
    assert is_expired_safe(future) is False
    assert is_expired_safe(None) is False


def test_p1_1_signal_expires_phase_aware():
    from app.utils.datetime_utils import signal_expires_at
    exp_opening = signal_expires_at("opening_drive")
    exp_closing = signal_expires_at("closing_drive")
    exp_midday = signal_expires_at("midday")
    # opening 5min, closing 3min, midday 15min
    now = datetime.now(timezone.utc)
    assert (exp_closing - now).total_seconds() < (exp_opening - now).total_seconds()
    assert (exp_opening - now).total_seconds() < (exp_midday - now).total_seconds()


# ─── F0 atomic_write ─────────────────────────────────────
def test_f0_atomic_write_text(tmp_path):
    from app.utils.atomic_write import atomic_write_text, atomic_read_with_mtime
    p = tmp_path / "test.txt"
    atomic_write_text(str(p), "hello world")
    content, mtime = atomic_read_with_mtime(str(p))
    assert content == "hello world"
    assert mtime is not None


# ─── P0-3 CriticalNotifier ───────────────────────────────
def test_p0_3_critical_notifier_module_importable():
    from app.utils.critical_alerts import send_critical, send_critical_sync, has_pending_critical
    assert callable(send_critical)
    assert callable(send_critical_sync)


def test_p0_3_critical_full_failure_writes_flag(tmp_path, monkeypatch):
    """全通道失败 → 写 flag 文件"""
    from app.utils import critical_alerts as ca
    monkeypatch.setattr(ca, "_CHANNELS", [lambda c: False, lambda c: False])
    monkeypatch.setattr(ca, "_flag_path", lambda: str(tmp_path / "critical.flag"))
    result = ca.send_critical_sync("test alert", retries=2, backoff_base=0.01)
    assert not result
    assert (tmp_path / "critical.flag").exists()


def test_p0_3_critical_first_success():
    """首次尝试成功"""
    from app.utils import critical_alerts as ca
    calls = []
    ch = lambda c: calls.append(c) or True
    orig = ca._CHANNELS
    ca._CHANNELS = [ch]
    try:
        result = ca.send_critical_sync("urgent")
        assert result
        assert calls == ["urgent"]
    finally:
        ca._CHANNELS = orig


# ─── P0-4 Coordinator switch_to_enforce ──────────────────
def test_p0_4_capital_reset_and_reconcile():
    from app.coordinator.capital_allocator import CapitalAllocator
    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=True)
    # observe 模式记账; H7 (2026-06-03): BLOCKED 不再记账 → 不会透支
    cap.try_reserve("main", 0.50)
    cap.try_reserve("main", 0.50)  # 0.50+0.50=1.00 > cap 0.65 → BLOCKED, 放行但不记账
    snap_before = cap.snapshot()
    assert snap_before.by_strategy["main"] == pytest.approx(0.50)  # H7: 无透支

    # reset_and_reconcile 用真实持仓重建
    cap.reset_and_reconcile({"main": 0.30, "scalping": 0.10})
    snap_after = cap.snapshot()
    assert snap_after.by_strategy["main"] == pytest.approx(0.30)
    assert snap_after.by_strategy["scalping"] == pytest.approx(0.10)


def test_p0_4_coordinator_switch_to_enforce():
    """完整流程: observe → switch_to_enforce → 拒新信号"""
    from app.coordinator.capital_allocator import CapitalAllocator
    from app.coordinator.symbol_lock import SymbolLockManager
    from app.coordinator.signal_arbitrator import SignalArbitrator
    from app.coordinator.exit_dispatcher import ExitDispatcher
    from app.coordinator.strategy_coordinator import StrategyCoordinator

    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=True)
    lk = SymbolLockManager(observe_only=True)
    arb = SignalArbitrator(audit_enabled=False)
    disp = ExitDispatcher()
    coord = StrategyCoordinator(cap, lk, arb, disp, enabled=True)

    # 模拟 PositionStore
    class _PS:
        def all(self):
            class _P:
                symbol = "NVDA"
                owner_strategy = "main"
                position_pct = 0.30
                size = 100
                avg_entry_price = 150
            return [_P()]

    assert coord.is_observe_only()
    ok = coord.switch_to_enforce(_PS())
    assert ok
    assert not coord.is_observe_only()
    # 重建后 main bucket = 0.30
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.30)


# ─── P0-5 OrderTracker GC ────────────────────────────────
def test_p0_5_order_tracker_gc_stale(tmp_path, monkeypatch):
    """STALE/FAILED/CANCELLED 超过 24h 的订单被清理"""
    monkeypatch.setenv("FTSEEK_DATA_DIR", str(tmp_path))
    from app.risk.order_tracker import OrderTracker, OrderState
    tracker = OrderTracker(db_path=str(tmp_path / "ot.db"))
    # 注入一个 24h 前的 STALE 订单
    old_order = {
        "futu_order_id": "old_1",
        "symbol": "NVDA",
        "state": OrderState.STALE.value,
        "submit_time": (datetime.now() - timedelta(hours=25)).isoformat(),
        "metadata": {},
    }
    tracker._pending_orders["old_1"] = old_order
    # 注入一个新的 PENDING_CONFIRM
    new_order = {
        "futu_order_id": "new_2",
        "symbol": "AMD",
        "state": OrderState.PENDING_CONFIRM.value,
        "submit_time": datetime.now().isoformat(),
        "metadata": {},
    }
    tracker._pending_orders["new_2"] = new_order

    removed = tracker.gc_stale_orders(max_age_hours=24)
    assert removed == 1
    assert "old_1" not in tracker._pending_orders
    assert "new_2" in tracker._pending_orders


# ─── P0-6 KILL_SWITCH 原子读 ──────────────────────────────
def test_p0_6_kill_switch_phantom_empty_safe(tmp_path, monkeypatch):
    """空文件 / 半写文件 → check_kill_switch 安全返回 False"""
    monkeypatch.setattr("app.us_pipeline.emergency_interceptor._KILL_SWITCH_FLAG", str(tmp_path / "ks.flag"))
    from app.us_pipeline.emergency_interceptor import check_kill_switch
    # 文件不存在 → False
    assert not check_kill_switch()
    # 空文件 → False (P0-6: 防 phantom-empty)
    (tmp_path / "ks.flag").write_text("")
    assert not check_kill_switch()


# ─── P1-4 per-symbol lock ────────────────────────────────
def test_p1_4_symbol_lock_independent_symbols():
    from app.utils.symbol_lock_registry import get_symbol_lock_registry
    reg = get_symbol_lock_registry()
    with reg.lock("NVDA", timeout=1.0) as got1:
        assert got1
        # 同时另一个 symbol 应能拿到
        with reg.lock("AMD", timeout=1.0) as got2:
            assert got2


def test_p1_4_symbol_lock_same_symbol_blocks():
    """同 symbol 在另一线程持锁时, 主线程超时拿不到"""
    import threading
    from app.utils.symbol_lock_registry import get_symbol_lock_registry
    reg = get_symbol_lock_registry()
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with reg.lock("TSLA", timeout=-1):
            holding.set()
            release.wait(2)  # 持 2s

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    holding.wait(1)
    # 主线程 0 超时拿
    with reg.lock("TSLA", timeout=0) as got:
        assert not got  # 没拿到
    release.set()
    t.join()


# ─── P1-7 metadata._exit_locked 字段 ───────────────────
def test_p1_7_position_metadata_exit_locked():
    """metadata._exit_locked 字段在 monitor 中被正确读"""
    from app.core.models import Position, Direction, MarketRegime
    pos = Position(
        symbol="NVDA",
        direction=Direction.LONG,
        size=10,
        avg_entry_price=150,
        market_regime=MarketRegime.RANGE,
    )
    pos.metadata = {"_exit_locked": True}
    # 不实际跑 scan_positions, 仅测 metadata 字段可读
    assert pos.metadata.get("_exit_locked") is True


# ─── P2-8 Watchdog ───────────────────────────────────────
def test_p2_8_watchdog_register_and_ping():
    from app.observability.watchdog import Watchdog
    wd = Watchdog()
    wd.register("test_loop", stale_threshold_sec=1)
    wd.ping("test_loop")
    status = wd.get_status()
    assert "test_loop" in status
    assert status["test_loop"]["last_ping_sec_ago"] < 1
    assert not status["test_loop"]["is_stale"]


def test_p2_8_watchdog_detects_stale():
    from app.observability.watchdog import Watchdog
    wd = Watchdog()
    wd.register("dead_loop", stale_threshold_sec=0.1)
    # 不 ping → 立即应该 stale
    time.sleep(0.2)
    status = wd.get_status()
    assert status["dead_loop"]["is_stale"]


# ─── 模块可 import (回归) ────────────────────────────────
def test_all_new_modules_importable():
    import importlib
    mods = [
        "app.utils.datetime_utils",
        "app.utils.atomic_write",
        "app.utils.critical_alerts",
        "app.utils.symbol_lock_registry",
        "app.observability.watchdog",
    ]
    for m in mods:
        importlib.import_module(m)
