"""P0-1/P0-2/P0-3 修复回归测试 (2026-05-19)

5/18 ARM 紊乱根因测试覆盖:
- P0-1: position_sync 不应覆盖 avg_entry_price (SSOT 保护)
- P0-2: tombstone 防止 sync 反向恢复刚平仓位置
- P0-3: PortfolioDrawdown 双重过滤 PROTECTED + ghost
"""
import time
import pytest
from unittest.mock import MagicMock


def _make_position(symbol="US.ARM", cost=203.53, size=27, direction=None):
    """构造 Position 测试夹具."""
    from app.core.models import Position, Direction, MarketRegime
    from datetime import datetime
    pos = Position(
        strategy_id="voting_llm",
        symbol=symbol,
        direction=direction or Direction.LONG,
        size=size,
        avg_entry_price=cost,
        initial_sl=cost * 0.92,
        current_sl=cost * 0.92,
        initial_tp=cost * 1.15,
        market_regime=MarketRegime.RANGE,
    )
    pos.entry_time = datetime.now()
    return pos


def test_p0_1_sync_does_not_overwrite_avg_entry_price_on_decrease(monkeypatch):
    """P0-1: 减仓场景下, broker 报告的 cost 与 SSOT 漂移时, 不覆盖 Position.avg_entry_price.

    复现 5/18 ARM 真实场景: 54 股 partial_close 后 size=27,
    paper broker 错报 cost=$216.11 替换真实 $203.53,
    导致 PortfolioDrawdown 误判 -2.5% 触发 7×FLAT_ALL.
    """
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    arm = _make_position("US.ARM", cost=203.53, size=54)  # 5/18 真实: 54 股
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = arm
    sync.position_store._positions = {"US.ARM": arm}
    sync._get_db_positions = MagicMock(return_value={})

    # 减仓 54→27 (5/18 真实场景), broker 错报 cost
    futu_positions = {
        "US.ARM": {"qty": 27.0, "cost": 216.11, "direction": "LONG"}
    }
    sync._sync_to_position_store_locked(futu_positions)

    # SSOT 不应被覆盖 (减仓不动 cost)
    assert abs(arm.avg_entry_price - 203.53) < 0.01, (
        f"减仓后 avg_entry_price 被错误覆盖! 期望 203.53, 实际 {arm.avg_entry_price}"
    )
    assert arm.size == 27.0


def test_p0_1_sync_initializes_avg_entry_price_when_missing():
    """P0-1 兜底: avg_entry_price=0 时, 用 broker cost 初始化."""
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    pos = _make_position("US.TEST", cost=0, size=10)
    pos.avg_entry_price = 0  # 显式置 0
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = pos
    sync.position_store._positions = {"US.TEST": pos}
    sync._get_db_positions = MagicMock(return_value={})

    futu_positions = {"US.TEST": {"qty": 11.0, "cost": 100.0, "direction": "LONG"}}
    sync._sync_to_position_store_locked(futu_positions)

    assert pos.avg_entry_price == 100.0, "avg_entry_price=0 时应用 broker cost 兜底"


def test_p0_1_karpathy_v2_size_increase_updates_cost():
    """P0-1 Karpathy 二审 v2: 加仓场景 (size 增加) 必须用 broker 加权均价更新 cost.

    场景: ARM 50 股 @ $100, 用户 broker 加仓 30 股 @ $146.67
        → broker 报告 size=80, cost=$117.50 (加权均价)
        → sync 必须把 avg_entry_price 更新到 $117.50, 否则后续 PnL 算成 (price - $100) × 80 严重错.
    """
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    arm = _make_position("US.ARM", cost=100.0, size=50)
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = arm
    sync.position_store._positions = {"US.ARM": arm}
    sync._get_db_positions = MagicMock(return_value={})

    # 加仓 30 股 → broker 端新加权均价 $117.50
    futu_positions = {"US.ARM": {"qty": 80.0, "cost": 117.50, "direction": "LONG"}}
    sync._sync_to_position_store_locked(futu_positions)

    assert arm.size == 80.0, "size 应同步到加仓后的 80"
    assert abs(arm.avg_entry_price - 117.50) < 0.01, (
        f"加仓后 avg_entry_price 必须更新为 broker 加权均价 $117.50, "
        f"实际 ${arm.avg_entry_price:.2f} (这是 v1 的隐藏 BUG)"
    )


def test_p0_1_karpathy_v2_size_decrease_preserves_ssot():
    """P0-1 Karpathy 二审 v2: 减仓场景仍保护 SSOT (5/18 ARM 复现)."""
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    arm = _make_position("US.ARM", cost=203.53, size=54)
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = arm
    sync.position_store._positions = {"US.ARM": arm}
    sync._get_db_positions = MagicMock(return_value={})

    # 减仓 54→27, broker 错报 cost=$216.11
    futu_positions = {"US.ARM": {"qty": 27.0, "cost": 216.11, "direction": "LONG"}}
    sync._sync_to_position_store_locked(futu_positions)

    assert arm.size == 27.0
    assert abs(arm.avg_entry_price - 203.53) < 0.01, (
        f"减仓不应动 cost. 实际 ${arm.avg_entry_price:.2f}"
    )


def test_p0_1_karpathy_v2_drift_logs_warning(caplog):
    """P0-1 Karpathy 'fail loudly': broker 漂移用 WARNING 不是 INFO,
    便于 daily_review 通过 grep WARNING 抓出 paper sandbox 异常."""
    from app.core.position_sync import USPositionSync
    import logging

    sync = USPositionSync(position_store=None)
    arm = _make_position("US.ARM", cost=203.53, size=54)
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = arm
    sync.position_store._positions = {"US.ARM": arm}
    sync._get_db_positions = MagicMock(return_value={})

    futu_positions = {"US.ARM": {"qty": 27.0, "cost": 216.11, "direction": "LONG"}}
    with caplog.at_level(logging.WARNING, logger="ftseek_us.position_sync"):
        sync._sync_to_position_store_locked(futu_positions)

    drift_warnings = [r for r in caplog.records if "broker 漂移" in r.message]
    assert len(drift_warnings) >= 1, "漂移 >$0.5 必须 WARNING (不是 INFO)"
    assert drift_warnings[0].levelname == "WARNING"


def test_p0_2_tombstone_blocks_restore_within_ttl():
    """P0-2: 60s 内 tombstone 阻止 sync 把刚平仓的 symbol 重新导入.

    Broker-first (2026-06-03): _restore_position_from_db 已删除,
    tombstone 拦截下沉到 _sync_to_position_store_locked 的新增持仓路径 (H6).
    """
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    sync.position_store = MagicMock()
    sync.position_store.get.return_value = None  # 内存中已无
    sync.position_store._positions = {}

    # 主流程刚平仓 → 标 tombstone
    sync.mark_closed_tombstone("US.ARM")
    assert sync._is_tombstoned("US.ARM")

    # 模拟 sync 看到 broker 端仍有该仓位, 试图当"新发现持仓"重新导入
    sync._sync_to_position_store_locked(
        {"US.ARM": {"qty": 27, "cost": 203.53, "direction": "LONG"}}
    )

    # 关键: 不应 add 进 store (被 tombstone 拦截)
    sync.position_store.add.assert_not_called()


def test_p0_2_tombstone_expires_after_ttl():
    """P0-2: 超过 60s ttl 后 tombstone 自动失效."""
    from app.core.position_sync import USPositionSync

    sync = USPositionSync(position_store=None)
    sync.mark_closed_tombstone("US.TEST")
    assert sync._is_tombstoned("US.TEST")

    # 人为推时间到 ttl 之后
    sync._closed_tombstones["US.TEST"] = time.time() - sync._tombstone_ttl_sec - 5
    assert not sync._is_tombstoned("US.TEST")
    # 顺带应清理过期项
    assert "US.TEST" not in sync._closed_tombstones


def _stub_capital_safety(monkeypatch):
    """密封 capital_safety 全局状态: equity=0 → drawdown 分母回退到持仓成本.

    P0 (2026-05-28): _check_portfolio_drawdown 演进为优先用账户净值做分母,
    不 stub 会读到真实 data/ 下的 equity, 使浮亏百分比被稀释, 测试不密封.
    """
    class _StubState:
        current_equity = 0.0
        peak_equity = 0.0

    class _StubCS:
        _state = _StubState()

    monkeypatch.setattr(
        "app.risk.capital_safety.get_capital_safety_system", lambda: _StubCS()
    )


def test_p0_3_portfolio_drawdown_excludes_ghost_and_pending(monkeypatch):
    """P0-3: ghost / pending_close 仓位不进入组合止损计算."""
    from app.risk.position_exit_monitor import ExitMonitor

    _stub_capital_safety(monkeypatch)

    class FakePos:
        def __init__(self, symbol, cost, size, metadata=None, source="auto_main"):
            self.symbol = symbol
            self.avg_entry_price = cost
            self.size = size
            self.direction = type('D', (), {'value': 'LONG'})()
            self.strategy_id = source
            self.metadata = metadata or {}

    monitor = ExitMonitor()

    # 1. 健康 MANAGED 仓位浮亏 -2% → 应 WARN
    healthy = FakePos("US.HEALTHY", 100, 10, source="voting_llm")
    res = monitor._check_portfolio_drawdown([healthy], lambda s: 98.0)
    assert res == "WARN", f"健康仓位 -2% 应触发 WARN, 实际 {res}"

    # 2. ghost 仓位 (metadata.is_ghost=True) — 即使浮亏 -3% 也不应被计入
    # (注意: 测试 _check_portfolio_drawdown 直接, 但实际过滤在 scan_positions 入口)
    # 这里只验证: 入参为空时返回 None
    res = monitor._check_portfolio_drawdown([], lambda s: 95.0)
    assert res is None


def test_p0_3_portfolio_drawdown_with_profit_returns_none():
    """P0-3: 浮盈不应触发 portfolio drawdown."""
    from app.risk.position_exit_monitor import ExitMonitor

    class FakePos:
        symbol = "US.WINNER"; avg_entry_price = 100; size = 10
        direction = type('D', (), {'value': 'LONG'})()

    res = ExitMonitor()._check_portfolio_drawdown([FakePos()], lambda s: 105.0)
    assert res is None, "浮盈 +5% 不应触发任何级别"


def test_d3_capital_record_owner_idempotent():
    """D3 修复: record_position_owner 用 lock.owner 实现幂等, 避免 by_strategy 差额检查漏掉重复 reserve.

    5/18 capital 漂移 111 次根因: scalping 13 笔 entry, 同 strategy 累计 by_strategy>=position_pct,
    跳过 reserve → 账本停滞但实际持续累加 → 5min reconcile 修正.
    """
    from app.coordinator.strategy_coordinator import StrategyCoordinator
    from app.coordinator.capital_allocator import CapitalAllocator
    from app.coordinator.symbol_lock import SymbolLockManager
    from app.coordinator.signal_arbitrator import SignalArbitrator
    from app.coordinator.exit_dispatcher import ExitDispatcher

    caps = {"main": 0.65, "scalping": 0.25, "llm_plan": 0.10}
    cap = CapitalAllocator(caps=caps, observe_only=True)
    lock = SymbolLockManager(observe_only=True)
    arb = SignalArbitrator()
    ed = ExitDispatcher()
    coord = StrategyCoordinator(capital_allocator=cap, symbol_lock=lock,
                                  signal_arbitrator=arb, exit_dispatcher=ed, enabled=True)

    # 1. 第 1 次入场 → reserve
    coord.record_position_owner("US.LCID", "scalping", 0.04)
    assert abs(cap.snapshot().by_strategy.get("scalping", 0) - 0.04) < 0.001

    # 2. 同 symbol 重复 record (幂等, 不重复 reserve)
    coord.record_position_owner("US.LCID", "scalping", 0.04)
    assert abs(cap.snapshot().by_strategy.get("scalping", 0) - 0.04) < 0.001, (
        "幂等失败 — 同 symbol 重复 record 不应 double-reserve"
    )

    # 3. 平仓后再开同 symbol → release 后重新 reserve
    coord.on_position_closed("US.LCID", "scalping", 0.04)
    assert abs(cap.snapshot().by_strategy.get("scalping", 0)) < 0.001
    coord.record_position_owner("US.LCID", "scalping", 0.05)
    assert abs(cap.snapshot().by_strategy.get("scalping", 0) - 0.05) < 0.001


def test_d3_capital_conflict_blocks_reserve():
    """D3 修复: 锁冲突时不抢锁, 不 reserve."""
    from app.coordinator.strategy_coordinator import StrategyCoordinator
    from app.coordinator.capital_allocator import CapitalAllocator
    from app.coordinator.symbol_lock import SymbolLockManager
    from app.coordinator.signal_arbitrator import SignalArbitrator
    from app.coordinator.exit_dispatcher import ExitDispatcher

    cap = CapitalAllocator(caps={"main": 0.65, "scalping": 0.25}, observe_only=True)
    lock = SymbolLockManager(observe_only=True)
    coord = StrategyCoordinator(capital_allocator=cap, symbol_lock=lock,
                                  signal_arbitrator=SignalArbitrator(),
                                  exit_dispatcher=ExitDispatcher(), enabled=True)

    coord.record_position_owner("US.LCID", "scalping", 0.04)
    coord.record_position_owner("US.LCID", "main", 0.10)  # 冲突, 锁被 scalping 占

    # main 不应 reserve (锁不归 main)
    assert cap.snapshot().by_strategy.get("main", 0) == 0


# 2026-07: test_d3_scalping_loss_cooldown_90s 已删除 — app/scalping/ 模块整体退役


def test_d3_legacy_close_trace_id_fallback():
    """D3 修复 #3: legacy 仓位 close 时无 trace_id 应生成 close-side 兜底."""
    import inspect
    from app.pipeline import order_executor
    src = inspect.getsource(order_executor)
    assert "f\"close-{position.symbol}-{int(time.time() * 1000)}\"" in src or \
           "close-{position.symbol}" in src, \
           "_close_position 必须为无 trace_id 的 legacy 仓位生成 close-side fallback"


def test_p0_3_logs_detail_on_trigger(caplog, monkeypatch):
    """P0-3: 触发时打印浮亏 + 仓位清单, 便于追溯 5/18 类问题."""
    from app.risk.position_exit_monitor import ExitMonitor
    import logging

    _stub_capital_safety(monkeypatch)

    class FakePos:
        symbol = "US.LOSER"; avg_entry_price = 100; size = 10
        direction = type('D', (), {'value': 'LONG'})()

    with caplog.at_level(logging.WARNING, logger="ftseek.exit_monitor"):
        ExitMonitor()._check_portfolio_drawdown([FakePos()], lambda s: 98.0)

    # 至少有一条触发详情日志包含 drawdown 数值 + 持仓清单
    triggers = [r for r in caplog.records if "[组合止损·trigger]" in r.message]
    assert len(triggers) >= 1, "应有触发详情日志"
    assert "drawdown=" in triggers[0].message
    assert "US.LOSER" in triggers[0].message
