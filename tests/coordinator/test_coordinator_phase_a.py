"""
Coordinator Phase A 核心冲突场景单测 (2026-05-14).

覆盖:
1. CapitalAllocator: cap 边界 + observe vs enforce 模式
2. CapitalAllocator: release 流程
3. SymbolLockManager: 同 symbol 互斥 + 重入
4. SignalArbitrator: priority × confidence 仲裁
5. Coordinator pre_trade_arbitrate: 三道闸联动
"""
import os
import tempfile
import pytest

from app.coordinator import (
    CapitalAllocator,
    SymbolLockManager,
    SignalArbitrator,
    ExitDispatcher,
)
from app.coordinator.strategy_coordinator import StrategyCoordinator


# ─── 测试用 mock ────────────────────────────────────────────────
class _Strat:
    def __init__(self, name, priority):
        self.name = name
        self.priority = priority


class _Sig:
    def __init__(self, confidence, position_pct=0.10, direction="LONG"):
        self.confidence = confidence
        self.position_pct = position_pct
        self.direction = direction


# ─── 1. CapitalAllocator: cap 边界 ──────────────────────────────
def test_capital_allocator_observe_mode_records_but_allows():
    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=True)
    # observe 模式下即使超 cap 也放行
    assert cap.try_reserve("main", 0.50) is True
    assert cap.try_reserve("main", 0.30) is True  # 总 0.80 > cap 0.65, observe 放行
    snap = cap.snapshot()
    assert snap.by_strategy["main"] == pytest.approx(0.80)
    # 但事件标 BLOCKED
    events = cap.get_recent_events(10)
    assert any(e["decision"] == "BLOCKED_STRATEGY_CAP" for e in events)


def test_capital_allocator_enforce_mode_rejects_over_cap():
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=False)
    assert cap.try_reserve("main", 0.50) is True
    # 0.50 + 0.20 = 0.70 > 0.65 cap → 拒
    assert cap.try_reserve("main", 0.20) is False
    # 账本仍是 0.50, 没记进去
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.50)


def test_capital_allocator_enforce_total_cap():
    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25},
                           max_total_pct=0.85, observe_only=False)
    assert cap.try_reserve("main", 0.60) is True
    # 0.60 + 0.30 = 0.90 > 0.85 total → 拒 (即使 scalping 单 cap 还够)
    assert cap.try_reserve("scalping", 0.30) is False


# ─── 2. release 流程 ────────────────────────────────────────────
def test_capital_allocator_release():
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=False)
    assert cap.try_reserve("main", 0.40) is True
    cap.release("main", 0.20)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.20)
    # 释放过头不会变负
    cap.release("main", 1.0)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.0)


def test_capital_allocator_reconcile():
    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=False)
    cap.try_reserve("main", 0.30)
    cap.try_reserve("scalping", 0.10)
    # 真实持仓: main 0.40, scalping 0.05 → 修正
    cap.reconcile_with_positions({"main": 0.40, "scalping": 0.05})
    snap = cap.snapshot()
    assert snap.by_strategy["main"] == pytest.approx(0.40)
    assert snap.by_strategy["scalping"] == pytest.approx(0.05)


# ─── B.3 (2026-05-14): bucket_map (fast_lane 共享 main bucket) ──────
def test_capital_allocator_bucket_map_routes_to_main():
    """fast_lane 占用计入 main bucket"""
    cap = CapitalAllocator(
        caps={"main": 0.65, "scalping": 0.25},
        observe_only=False,
        bucket_map={"fast_lane": "main"},
    )
    assert cap.try_reserve("fast_lane", 0.10) is True
    snap = cap.snapshot()
    # bucket 视图: main = 0.10 (fast_lane 计入)
    assert snap.by_strategy["main"] == pytest.approx(0.10)
    # strategy 视图: fast_lane = 0.10
    assert cap.by_strategy_snapshot()["fast_lane"] == pytest.approx(0.10)
    # main 再来 0.50 → 0.10 + 0.50 = 0.60, 不超 cap 0.65
    assert cap.try_reserve("main", 0.50) is True
    # 再来 0.10 (main+fastlane=0.70) > 0.65 → 拒
    assert cap.try_reserve("fast_lane", 0.10) is False


def test_capital_allocator_bucket_release_routes_correctly():
    """fast_lane release 减少 main bucket"""
    cap = CapitalAllocator(
        caps={"main": 0.65},
        observe_only=False,
        bucket_map={"fast_lane": "main"},
    )
    cap.try_reserve("fast_lane", 0.10)
    cap.try_reserve("main", 0.20)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.30)
    cap.release("fast_lane", 0.05)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.25)
    assert cap.by_strategy_snapshot()["fast_lane"] == pytest.approx(0.05)


def test_capital_allocator_is_observe_only_public_api():
    """B.6: is_observe_only 公开 API"""
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=True)
    assert cap.is_observe_only() is True
    cap.set_observe_only(False)
    assert cap.is_observe_only() is False


# ─── 3. SymbolLockManager ───────────────────────────────────────
def test_symbol_lock_acquire_and_release():
    lock = SymbolLockManager(observe_only=False)
    assert lock.acquire("NVDA", "main") is True
    assert lock.owner("NVDA") == "main"
    # 同 strategy 重入 OK
    assert lock.acquire("NVDA", "main") is True
    # 不同 strategy 拒
    assert lock.acquire("NVDA", "scalping") is False
    # 释放后可被另一个 strategy 拿
    lock.release("NVDA")
    assert lock.acquire("NVDA", "scalping") is True


def test_symbol_lock_observe_mode_does_not_block():
    lock = SymbolLockManager(observe_only=True)
    assert lock.acquire("AAPL", "main") is True
    # observe 模式: 第二个 strategy 也"成功", 但事件记录冲突
    assert lock.acquire("AAPL", "scalping") is True  # observe 放行
    events = lock.get_recent_events(10)
    assert any(e["decision"] == "OBSERVE_CONFLICT" for e in events)


def test_symbol_lock_release_with_wrong_strategy_no_op():
    lock = SymbolLockManager(observe_only=False)
    lock.acquire("TSLA", "main")
    # 错误 strategy 释放不应该误删
    lock.release("TSLA", "scalping")
    assert lock.owner("TSLA") == "main"


# ─── 4. SignalArbitrator: priority × confidence ─────────────────
def test_arbitrator_priority_times_confidence():
    db = tempfile.mktemp(suffix=".db")
    try:
        arb = SignalArbitrator(audit_db_path=db, audit_enabled=True)
        candidates = [
            (_Strat("main", 5),     _Sig(0.7)),  # 3.5
            (_Strat("scalping", 10),_Sig(0.6)),  # 6.0  ← winner
            (_Strat("fast_lane", 8),_Sig(0.5)),  # 4.0
        ]
        result = arb.arbitrate("NVDA", candidates)
        assert result is not None
        assert result.winner_strategy == "scalping"
        assert result.score == pytest.approx(6.0)
        assert len(result.candidates) == 3
    finally:
        if os.path.exists(db):
            os.unlink(db)


def test_arbitrator_priority_tiebreak():
    arb = SignalArbitrator(audit_enabled=False)
    # 同 score: 同 priority + 同 confidence
    candidates = [
        (_Strat("main", 5),     _Sig(0.6)),  # 3.0
        (_Strat("fast_lane", 5),_Sig(0.6)),  # 3.0
    ]
    result = arb.arbitrate("AAPL", candidates)
    # 任一胜均 OK, 但必须确定性 (sort 排序保证)
    assert result.winner_strategy in ("main", "fast_lane")


def test_arbitrator_empty_candidates_returns_none():
    arb = SignalArbitrator(audit_enabled=False)
    assert arb.arbitrate("FOO", []) is None


# ─── 5. Coordinator 三道闸联动 ──────────────────────────────────
def test_coordinator_arbitrate_full_flow_observe():
    """observe 模式: 高 priority 胜出, 资金/锁均落到胜者"""
    db = tempfile.mktemp(suffix=".db")
    try:
        cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=True)
        lock = SymbolLockManager(observe_only=True)
        arb = SignalArbitrator(audit_db_path=db, audit_enabled=True)
        disp = ExitDispatcher()
        coord = StrategyCoordinator(cap, lock, arb, disp, enabled=True)

        result = coord.pre_trade_arbitrate("NVDA", [
            (_Strat("main", 5),     _Sig(0.7, 0.10)),   # 3.5
            (_Strat("scalping", 10),_Sig(0.6, 0.08)),   # 6.0
        ])
        assert result.action == "ALLOWED"
        assert result.winner_strategy == "scalping"
        assert cap.snapshot().by_strategy["scalping"] == pytest.approx(0.08)
        assert cap.snapshot().by_strategy["main"] == pytest.approx(0.0)
        assert lock.owner("NVDA") == "scalping"
    finally:
        if os.path.exists(db):
            os.unlink(db)


def test_coordinator_disabled_short_circuits():
    """coordinator.enabled=false 不做任何处理"""
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=False)
    lock = SymbolLockManager(observe_only=False)
    arb = SignalArbitrator(audit_enabled=False)
    disp = ExitDispatcher()
    coord = StrategyCoordinator(cap, lock, arb, disp, enabled=False)

    result = coord.pre_trade_arbitrate("NVDA", [(_Strat("main", 5), _Sig(0.7))])
    assert result.action == "ALLOWED"
    assert result.reason == "COORDINATOR_DISABLED"
    # 资金 / 锁 均无变化
    assert cap.snapshot().by_strategy["main"] == 0.0
    assert lock.owner("NVDA") is None


def test_coordinator_enforce_capital_reject():
    """enforce 模式: 资金超 → 拒, 锁不被占"""
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=False)
    lock = SymbolLockManager(observe_only=False)
    arb = SignalArbitrator(audit_enabled=False)
    disp = ExitDispatcher()
    coord = StrategyCoordinator(cap, lock, arb, disp, enabled=True)

    # 已占 0.60, 再来 0.10 超 cap
    cap.try_reserve("main", 0.60)
    result = coord.pre_trade_arbitrate("NVDA", [(_Strat("main", 5), _Sig(0.7, 0.10))])
    assert result.action == "REJECTED_CAPITAL"
    # 锁不应该被占
    assert lock.owner("NVDA") is None


def test_coordinator_enforce_lock_reject_releases_capital():
    """enforce: 拿了资金后锁失败 → 必须回滚资金"""
    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=False)
    lock = SymbolLockManager(observe_only=False)
    arb = SignalArbitrator(audit_enabled=False)
    disp = ExitDispatcher()
    coord = StrategyCoordinator(cap, lock, arb, disp, enabled=True)

    # 提前让 scalping 占住 NVDA
    lock.acquire("NVDA", "scalping")

    # main 来抢 NVDA: arbitrate 后 main 是唯一候选 → reserve 资金 → acquire 锁失败 → 释放资金
    result = coord.pre_trade_arbitrate("NVDA", [(_Strat("main", 5), _Sig(0.7, 0.10))])
    assert result.action == "REJECTED_SYMBOL_LOCKED"
    # main 的资金账本应该是 0 (有 reserve 后 release)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.0)


def test_coordinator_on_position_closed_releases_both():
    """平仓 → 资金释放 + 锁释放"""
    cap = CapitalAllocator(caps={"main": 0.65}, observe_only=False)
    lock = SymbolLockManager(observe_only=False)
    arb = SignalArbitrator(audit_enabled=False)
    disp = ExitDispatcher()
    coord = StrategyCoordinator(cap, lock, arb, disp, enabled=True)

    coord.pre_trade_arbitrate("NVDA", [(_Strat("main", 5), _Sig(0.7, 0.10))])
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.10)
    assert lock.owner("NVDA") == "main"

    coord.on_position_closed("NVDA", "main", 0.10)
    assert cap.snapshot().by_strategy["main"] == pytest.approx(0.0)
    assert lock.owner("NVDA") is None
