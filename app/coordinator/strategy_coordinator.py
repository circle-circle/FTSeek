"""
StrategyCoordinator — 多策略并行调度的唯一入口 (Phase A 2026-05-14)

Phase A 范围:
- pre_trade_arbitrate(): 接收候选信号, 走 Arbitrator + Capital + SymbolLock 三道闸
- record_position_owner(): 下单成功后落 owner_strategy
- on_position_closed(): 平仓时释放资金 + 锁

Phase A 默认观察模式 (各组件 observe_only=True), 不阻断现有流程.

Phase B/C/D 待加: run_open_cycle 主循环, OrderGateway 注入, ExecutionMode.

⚠️ observe_only 已知局限 (2026-06-19 更新):
- observe_only=True 时 SymbolLock 完全不强制 → 多策略可能在同标的上竞争
- Phase B 切 enforce 时需评估各策略是否应接入完整 _check_gates_and_conflicts
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .capital_allocator import CapitalAllocator
from .symbol_lock import SymbolLockManager
from .signal_arbitrator import SignalArbitrator, ArbitrationResult
from .exit_dispatcher import ExitDispatcher

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """coordinator 单次决策结果"""
    symbol: str
    action: str  # ALLOWED | REJECTED_NO_SIGNAL | REJECTED_CAPITAL | REJECTED_SYMBOL_LOCKED
    winner_strategy: Optional[str] = None
    reason: str = ""
    arbitration: Optional[ArbitrationResult] = None
    capital_snapshot: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


class StrategyCoordinator:
    """
    多策略协调入口. Phase A 暂只做"决策仲裁 + 账本记录", 不接管下单循环.

    用法 (Phase A 集成进 scheduler):
        coord = get_coordinator()
        result = coord.pre_trade_arbitrate(symbol, [(strat, sig)])
        if result.action == "ALLOWED":
            # 走原下单逻辑, 但下单后调 coord.record_position_owner
            order_id = scheduler.submit_order(...)
            coord.record_position_owner(symbol, result.winner_strategy, sig.position_pct)
        else:
            # 仍然走原逻辑 (observe 模式), 但事件已记录
            ...
    """

    def __init__(
        self,
        capital_allocator: CapitalAllocator,
        symbol_lock: SymbolLockManager,
        signal_arbitrator: SignalArbitrator,
        exit_dispatcher: ExitDispatcher,
        enabled: bool = False,
    ):
        self.capital = capital_allocator
        self.lock = symbol_lock
        self.arb = signal_arbitrator
        self.exit = exit_dispatcher
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool):
        self._enabled = on
        logger.info(f"[StrategyCoordinator] enabled={on}")

    def is_observe_only(self) -> bool:
        """B.6 (2026-05-14): 是否处于观察模式 (capital + lock 两者状态一致)"""
        return self.capital.is_observe_only()

    def is_enforce(self) -> bool:
        """启用且非观察"""
        return self._enabled and not self.is_observe_only()

    def switch_to_enforce(self, position_registry):
        """
        P0-4 (2026-05-14): observe → enforce 切换的安全入口.

        步骤:
        1. 用真实 PositionStore 重建账本 (清零 + reconcile)
        2. 用真实持仓重建 SymbolLock
        3. 切 capital + lock observe_only=false

        切换后立即 enforce, 任何超 cap 信号会被拒.
        """
        if not self._enabled:
            logger.warning("[Coordinator·switch_enforce] coordinator 未启用, 拒绝切换")
            return False

        try:
            positions = position_registry.all() if hasattr(position_registry, "all") else []
            # 从持仓重建 by_strategy_pct 与 by_strategy_symbols
            by_strat_pct: Dict[str, float] = {}
            by_strat_sym: Dict[str, List[str]] = {}
            equity = 0.0
            try:
                from app.risk.capital_safety import get_capital_safety_system
                equity = float(get_capital_safety_system()._state.current_equity or 0)
            except Exception:
                pass
            for p in positions:
                owner = getattr(p, "owner_strategy", None) or "main"
                pct = float(getattr(p, "position_pct", 0) or 0)
                if pct == 0 and equity > 0:
                    cost = float(getattr(p, "size", 0) or 0) * float(getattr(p, "avg_entry_price", 0) or 0)
                    pct = cost / equity if cost > 0 else 0
                by_strat_pct[owner] = by_strat_pct.get(owner, 0) + pct
                by_strat_sym.setdefault(owner, []).append(p.symbol)

            # 1. capital reset + reconcile
            self.capital.reset_and_reconcile(by_strat_pct)
            # 2. lock 重建
            self.lock.reconcile_with_positions(by_strat_sym)
            # 3. 切 enforce
            self.capital.set_observe_only(False)
            self.lock.set_observe_only(False)
            logger.warning(
                "[Coordinator·switch_enforce] ✅ 已切换到 ENFORCE 模式. "
                f"positions={sum(len(s) for s in by_strat_sym.values())} "
                f"capital={self.capital.snapshot().by_strategy}"
            )
            return True
        except Exception as e:
            logger.error(f"[Coordinator·switch_enforce] 切换失败 (保持 observe): {e}")
            return False

    def switch_to_observe(self):
        """切回 observe 模式 (回滚)"""
        self.capital.set_observe_only(True)
        self.lock.set_observe_only(True)
        logger.warning("[Coordinator·switch_observe] 已切回 OBSERVE 模式")

    # ────────────────────────────────────────────────────────────
    # 决策入口 (开仓前)
    # ────────────────────────────────────────────────────────────
    def pre_trade_arbitrate(
        self,
        symbol: str,
        candidates: List[Tuple[Any, Any]],  # [(strategy_obj, signal_obj)]
    ) -> ExecutionResult:
        """
        在 scheduler 走 RiskGateway 之前调用.
        Phase A: 只仲裁 + 记录, 不阻断 (observe 模式).
        """
        if not self._enabled:
            # coordinator 关闭 → 直接 ALLOWED, 不记录
            return ExecutionResult(symbol=symbol, action="ALLOWED",
                                   winner_strategy=None, reason="COORDINATOR_DISABLED")

        if not candidates:
            return ExecutionResult(symbol=symbol, action="REJECTED_NO_SIGNAL")

        # Step 1: 仲裁
        arb = self.arb.arbitrate(symbol, candidates)
        if arb is None:
            return ExecutionResult(symbol=symbol, action="REJECTED_NO_SIGNAL")

        winner_name = arb.winner_strategy
        winner_sig = arb.winner_signal
        position_pct = float(getattr(winner_sig, "position_pct", 0.0) or 0.0)
        # M14 (2026-06-03): 如果 signal.position_pct 为 0, 从 metadata 或估算
        if position_pct <= 0:
            _meta = getattr(winner_sig, 'metadata', {}) or {}
            position_pct = float(_meta.get("position_pct", 0.0) or 0.0)
        if position_pct <= 0:
            # 兜底估算: 假设 10% 仓位 (避免 try_reserve 成为 no-op)
            position_pct = 0.10
            logger.debug(f"[Coordinator·M14] {symbol} position_pct 估算为 {position_pct}")

        # Step 2: 资金账本 (失败立即返回, 不再试 lock)
        cap_ok = self.capital.try_reserve(winner_name, position_pct)
        if not cap_ok:
            # 强制模式 reject; 锁未被占
            return ExecutionResult(
                symbol=symbol, action="REJECTED_CAPITAL",
                winner_strategy=winner_name,
                reason="capital_cap",
                arbitration=arb,
            )

        # Step 3: 符号锁 (capital 已 reserve, lock 失败需回滚 capital)
        lock_ok = self.lock.acquire(symbol, winner_name)
        if not lock_ok:
            # 强制模式 reject + 释放刚拿到的资金
            self.capital.release(winner_name, position_pct)
            return ExecutionResult(
                symbol=symbol, action="REJECTED_SYMBOL_LOCKED",
                winner_strategy=winner_name,
                reason=f"symbol_held_by_{self.lock.owner(symbol)}",
                arbitration=arb,
            )

        snap = self.capital.snapshot()
        return ExecutionResult(
            symbol=symbol,
            action="ALLOWED",
            winner_strategy=winner_name,
            reason="passed_arbitrate_capital_lock",
            arbitration=arb,
            capital_snapshot=snap.by_strategy.get(winner_name, 0.0),
        )

    # ────────────────────────────────────────────────────────────
    # 下单成功后
    # ────────────────────────────────────────────────────────────
    def record_position_owner(self, symbol: str, strategy: str, position_pct: float):
        """
        Phase A: 入场后登记 owner + reserve capital (幂等).

        使用 symbol 锁作为"是否已 reserve 过"的真实代理 — 避免按 by_strategy 差额检查
        漏掉同 strategy 多次 entry 的情况

        - 主线 (走 pre_trade_arbitrate): 已 acquire+reserve → lock.owner==strategy → 直接 return
        - 其他策略 (不走 arbitrate): 第 1 次 lock.owner==None → acquire+reserve
        - 平仓后再开同 symbol: on_position_closed 已 release → lock.owner==None → 走完整路径
        """
        if not self._enabled:
            return
        current_owner = self.lock.owner(symbol)
        if current_owner == strategy:
            # 已 acquire + reserve 过, idempotent (防主线 pre_trade_arbitrate 后重复记账)
            logger.debug(
                f"[Coordinator·idempotent] {symbol} 已被 {strategy} 占用 (lock+capital 已就位)"
            )
            return
        if current_owner is not None:
            # 锁被其他 strategy 占 — 记录但不抢锁 (Phase A observe_only, 防误杀)
            logger.warning(
                f"[Coordinator·conflict] {symbol} 锁被 {current_owner} 占, "
                f"{strategy} 跳过 record (可能是双策略并发开仓, 需排查)"
            )
            return
        # 未 acquire — 走完整 reserve + acquire 闭环
        acquired = self.lock.acquire(symbol, strategy)
        if acquired:
            self.capital.try_reserve(strategy, position_pct)
            logger.debug(
                f"[Coordinator·record] {symbol} → {strategy} pct={position_pct:.3f} (reserve+acquire 完成)"
            )

    # ────────────────────────────────────────────────────────────
    # 平仓后
    # ────────────────────────────────────────────────────────────
    def on_position_closed(self, symbol: str, strategy: Optional[str], position_pct: float):
        """平仓后释放资金 + 锁"""
        if not self._enabled:
            return
        if strategy:
            self.capital.release(strategy, position_pct)
        self.lock.release(symbol, strategy)
        logger.debug(f"[Coordinator] closed: {symbol} ({strategy}) pct={position_pct:.3f}")

    # ────────────────────────────────────────────────────────────
    # Reconcile (定期巡检, 防止账本漂移)
    # ────────────────────────────────────────────────────────────
    def reconcile(self, position_registry):
        """
        从 PositionRegistry 拉真实持仓, 修正 capital 账本 + 符号锁.
        建议每 5 分钟跑一次.

        Phase 1 (2026-06-14): 手动/受保护仓位单独计入 manual_reserved,
        不占 auto bucket cap, 但占 total cap. 确保 auto 可用额度 = total - manual.

        position_pct=0 的兜底: 用 size * avg_entry_price / equity 估算
        """
        if not self._enabled:
            return
        try:
            positions = position_registry.all() if hasattr(position_registry, "all") else []
            # 拉一次 equity 用于兜底估算
            _equity = 0.0
            try:
                from app.risk.capital_safety import get_capital_safety_system
                _equity = float(get_capital_safety_system()._state.current_equity or 0)
            except Exception:
                pass

            # Task #35 (2026-06-16): 三类资本隔离 — auto / legacy_protected / manual
            # 替代旧 "is_manual" 二分逻辑, 字段缺失 fallback 为 legacy_protected (保护用户)
            try:
                from app.positions.source import (
                    classify_owner,
                    OWNER_AUTO, OWNER_LEGACY_PROTECTED,
                    OWNER_MANUAL, OWNER_MANUAL_ORPHAN,
                )
            except Exception as _ce:
                logger.warning(f"[Coordinator·reconcile] classify_owner 不可用: {_ce}")
                classify_owner = lambda _p: "legacy_protected"
                OWNER_AUTO = "auto"
                OWNER_LEGACY_PROTECTED = "legacy_protected"
                OWNER_MANUAL = "manual"
                OWNER_MANUAL_ORPHAN = "manual_orphan"

            by_strat_pct: Dict[str, float] = {}
            by_strat_sym: Dict[str, List[str]] = {}
            legacy_total_pct = 0.0
            manual_total_pct = 0.0
            auto_total_pct = 0.0
            legacy_count = 0
            manual_count = 0
            orphan_count = 0
            auto_count = 0

            for p in positions:
                try:
                    cls = classify_owner(p)
                except Exception:
                    cls = OWNER_LEGACY_PROTECTED  # 兜底保护

                pct = float(getattr(p, "position_pct", 0.0) or 0.0)
                if pct == 0.0 and _equity > 0:
                    _cost = abs(float(getattr(p, "size", 0) or 0)) * float(getattr(p, "avg_entry_price", 0) or 0)
                    if _cost > 0:
                        pct = _cost / _equity
                        try:
                            setattr(p, "position_pct", round(pct, 4))
                        except Exception:
                            pass

                if cls == OWNER_AUTO:
                    owner = getattr(p, "owner_strategy", None) or "main"
                    by_strat_pct[owner] = by_strat_pct.get(owner, 0.0) + pct
                    by_strat_sym.setdefault(owner, []).append(p.symbol)
                    auto_total_pct += pct
                    auto_count += 1
                elif cls == OWNER_LEGACY_PROTECTED:
                    legacy_total_pct += pct
                    legacy_count += 1
                elif cls == OWNER_MANUAL:
                    manual_total_pct += pct
                    manual_count += 1
                elif cls == OWNER_MANUAL_ORPHAN:
                    # manual_orphan 视同 manual 隔离 (但记录孤儿计数, 待后续补登记)
                    manual_total_pct += pct
                    manual_count += 1
                    orphan_count += 1

            # 同步 auto 仓位到 bucket 账本
            self.capital.reconcile_with_positions(by_strat_pct)
            self.lock.reconcile_with_positions(by_strat_sym)

            # Task #35: 一次性更新 legacy + manual 占比 (原子, 避免 auto_pool 短暂为负)
            # 2026-06-26: legacy_total_pct 封顶在 max_total, 避免 SIM 仓位 notional > equity
            # 导致 auto_pool 永久为 0 (SIM 有杠杆, notional 可超过 equity)
            _max_total = getattr(self.capital, '_max_total', 0.9)
            legacy_total_pct = min(legacy_total_pct, _max_total * 0.8)  # 留至少20%给auto
            if hasattr(self.capital, "set_owner_pools"):
                self.capital.set_owner_pools(legacy_total_pct, manual_total_pct)
            elif hasattr(self.capital, "set_manual_reserved"):
                # 向后兼容: 老版本只有 manual 入口 → 把 legacy 也累入 manual 池, 至少
                # 保证不被算到 auto_pool, 不会再出现负 headroom
                self.capital.set_manual_reserved(manual_total_pct + legacy_total_pct)

            # Task #35: 生成并缓存 USD 维度 auto 容量报告 (供 dashboard 读 / 验收用)
            try:
                if hasattr(self.capital, "auto_capacity_report") and _equity > 0:
                    rep = self.capital.auto_capacity_report(_equity, positions)
                    logger.info(
                        f"[Coordinator·reconcile·Task#35] equity=${rep.total_equity:,.0f} "
                        f"legacy={legacy_count}笔/${rep.legacy_protected_value:,.0f} "
                        f"manual={manual_count}笔/${rep.manual_value:,.0f} "
                        f"auto={auto_count}笔/${rep.current_auto_exposure:,.0f} "
                        f"→ auto_pool=${rep.auto_pool:,.0f} "
                        f"auto_headroom=${rep.auto_headroom:,.0f} ({rep.auto_headroom_pct:.1%})"
                    )
                    if orphan_count > 0:
                        logger.warning(
                            f"[Coordinator·reconcile·Task#35] manual_orphan={orphan_count} 笔 "
                            f"(MANUAL 来源但未在 ManualHoldRegistry 注册, 已隔离, 建议补登记)"
                        )
            except Exception as _re:
                logger.debug(f"[Coordinator·reconcile·Task#35] report 生成失败: {_re}")

            if (legacy_count + manual_count) > 0:
                logger.debug(
                    f"[Coordinator·reconcile] legacy={legacy_count} ({legacy_total_pct:.1%}) | "
                    f"manual={manual_count} ({manual_total_pct:.1%}) | "
                    f"auto={auto_count} ({auto_total_pct:.1%})"
                )
        except Exception as e:
            logger.warning(f"[Coordinator·reconcile] 失败: {e}")


_singleton: Optional[StrategyCoordinator] = None


def get_coordinator(
    capital: Optional[CapitalAllocator] = None,
    lock: Optional[SymbolLockManager] = None,
    arb: Optional[SignalArbitrator] = None,
    exit_dispatcher: Optional[ExitDispatcher] = None,
    enabled: bool = False,
) -> StrategyCoordinator:
    """单例. 第一次调用必须传齐 4 组件."""
    global _singleton
    if _singleton is None:
        if not all([capital, lock, arb, exit_dispatcher]):
            raise ValueError("StrategyCoordinator 第一次构造必须传齐 4 个组件")
        _singleton = StrategyCoordinator(
            capital_allocator=capital,
            symbol_lock=lock,
            signal_arbitrator=arb,
            exit_dispatcher=exit_dispatcher,
            enabled=enabled,
        )
    return _singleton


def reset_coordinator():
    """单测用: 清空单例"""
    global _singleton
    _singleton = None
