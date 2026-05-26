"""启动强制对账 (2026-05-16)

启动时一次性 reconcile:
1. 拉 broker 实际持仓
2. 对比 PositionStore (内存) + open_positions (DB)
3. 差异分类:
    - broker_only (broker 有 + 本地无) → 自动补录, 标 LEGACY (启动期都标历史) + PROTECTED
    - local_only (本地有 + broker 无) → 标记 ghost, 写 ghost_outcomes 表
    - matched → 不动, 但补 source/protection 标签 (若缺)

修复 2026-05-20: 期权持仓不在 broker 中 (模拟期权), 不应被误判为 ghost.
                 与 position_sync._get_option_position_symbols() 保持一致.

不重写 position_sync.py — reconciler 是"启动期一次性", position_sync 是"运行时 30s".

非阻塞: 单次启动调用, 失败降级 (不阻断主流程).
"""
from __future__ import annotations
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple  # Tuple 用于 _infer_source 返回值

from .source import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state, tag_position,
)

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    matched: int = 0
    broker_only_added: int = 0   # 新发现 → 智能推断 source
    local_only_ghosted: int = 0  # 幽灵 → 标记
    tagged_legacy: int = 0       # matched 但缺 source 标 → 补
    adopted_strategy: int = 0    # M1.1: 识别为策略孤儿 → MANAGED 续命
    errors: int = 0


class StartupReconciler:
    """启动对账 — 单次调用, 与 position_sync 独立"""

    def __init__(self, position_store, broker_adapter=None, db_path: Optional[str] = None):
        self.position_store = position_store
        self.broker_adapter = broker_adapter
        if db_path is None:
            try:
                from app.core.paths import get_trade_records_db
                db_path = str(get_trade_records_db())
            except Exception:
                from pathlib import Path
                db_path = str(Path(__file__).resolve().parent.parent.parent /
                             "data" / "ftseek_us.db")
        self._db_path = db_path

    def run(self) -> ReconcileResult:
        """执行启动对账 — 安全降级 (任何步骤失败不阻断主流程)"""
        result = ReconcileResult()
        try:
            broker_positions = self._fetch_broker_positions()
            local_positions = self._fetch_local_positions()
            result = self._reconcile(broker_positions, local_positions)
            self._log_summary(result, broker_positions, local_positions)
        except Exception as e:
            logger.warning(f"[Reconciler] 启动对账异常 (降级运行): {e}", exc_info=True)
            result.errors += 1
        return result

    # ─────────────────────────────────────────────────────────────
    def _fetch_broker_positions(self) -> Dict[str, Dict]:
        """从 broker 拉真实持仓 — {symbol: {direction, size, cost}}"""
        if self.broker_adapter is None:
            logger.info("[Reconciler] broker_adapter 未提供, 跳过 broker 端拉取")
            return {}
        try:
            # 优先用 USPositionSync 已实现的 fetch
            from app.core.position_sync import get_us_position_sync
            sync = get_us_position_sync()
            if sync is not None and hasattr(sync, "query_broker_positions"):
                return sync.query_broker_positions() or {}
        except Exception as e:
            logger.debug(f"[Reconciler] USPositionSync fetch 失败: {e}")
        return {}

    def _fetch_local_positions(self) -> Dict[str, Any]:
        """{symbol: Position} 来自内存 PositionStore"""
        if self.position_store is None:
            return {}
        return {p.symbol: p for p in self.position_store.all()}

    def _reconcile(self, broker_positions: Dict[str, Dict],
                   local_positions: Dict[str, Any]) -> ReconcileResult:
        result = ReconcileResult()
        broker_syms = set(broker_positions.keys())
        local_syms = set(local_positions.keys())

        # 1. matched — 在两侧都存在, 智能推断 source
        # M1.1 (2026-05-16): 不一刀切标 LEGACY. 策略孤儿仓 (元数据丢但策略实际在管) 应识别为 AUTO_*
        for sym in (broker_syms & local_syms):
            try:
                pos = local_positions[sym]
                src = get_position_source(pos)
                if src == PositionSource.UNKNOWN:
                    # M1.1: 多线索推断
                    inferred_src, inferred_protection = self._infer_source(sym, pos)
                    tag_position(pos, inferred_src, inferred_protection)
                    if inferred_src.is_auto():
                        result.adopted_strategy += 1
                        logger.warning(
                            f"[Reconciler·M1.1·adopt] {sym} 识别为策略孤儿仓 "
                            f"→ {inferred_src.value} + {inferred_protection.value} (维持监控)"
                        )
                    else:
                        result.tagged_legacy += 1
                        logger.info(
                            f"[Reconciler] {sym} 补 {inferred_src.value} 标签 + {inferred_protection.value}"
                        )
                result.matched += 1
            except Exception as e:
                logger.debug(f"[Reconciler·matched] {sym}: {e}")
                result.errors += 1

        # 2. broker_only — broker 有 + 本地无 → 启动期统一标 LEGACY + PROTECTED
        for sym in (broker_syms - local_syms):
            try:
                info = broker_positions[sym]
                self._add_legacy_position(sym, info)
                result.broker_only_added += 1
            except Exception as e:
                logger.warning(f"[Reconciler·broker_only] {sym}: {e}")
                result.errors += 1

        # 3. local_only — 本地有 + broker 无 → 幽灵, 标记保留 (用户决定清理)
        # 2026-05-20 修复: 期权持仓不在 broker 中 (模拟期权), 跳过 ghost 检测
        option_symbols = self._get_option_symbols()
        for sym in (local_syms - broker_syms):
            # 双重过滤: DB 记录 + 启发式模式匹配 (捕获遗漏的期权)
            if sym in option_symbols or self._is_option_symbol(sym):
                logger.info(f"[Reconciler] 跳过期权仓 {sym} (模拟期权, 不在 broker 中)")
                continue
            try:
                pos = local_positions[sym]
                self._mark_ghost(pos)
                result.local_only_ghosted += 1
            except Exception as e:
                logger.warning(f"[Reconciler·local_only] {sym}: {e}")
                result.errors += 1

        return result

    def _get_option_symbols(self) -> set:
        """从 option_positions DB 表读取期权持仓标的 (2026-05-20 修复)

        期权持仓不在 broker 中 (模拟期权), 不应被误判为 ghost.
        与 position_sync._get_option_position_symbols() 保持一致.

        注意: option_positions 表有 symbol (标的) 和 option_code (合约代码) 两列.
        ghost 检测用的是 option_code (如 US.QQQ260602P710000), 不是 symbol.

        Returns:
            期权合约代码集合; 查询失败返回空集 (保守策略: 跳过过滤)
        """
        try:
            conn = sqlite3.connect(self._db_path)
            symbols = {row[0] for row in conn.execute(
                "SELECT DISTINCT option_code FROM option_positions"
            )}
            conn.close()
            if symbols:
                logger.debug(f"[Reconciler] 期权持仓: {symbols} (跳过 ghost 检测)")
            return symbols
        except Exception as e:
            logger.debug(f"[Reconciler·_get_option_symbols] {e}")
            return set()

    def _is_option_symbol(self, symbol: str) -> bool:
        """启发式检测是否为期权合约代码 (2026-05-20 补充)

        格式: US.QQQ260602P710000 (标的 + 日期YYMMDD + P/C + 行权价)
        用于捕获 option_positions 表中遗漏的期权 (如 SPY260602P730000).
        """
        import re
        # 匹配: US.<标的><6位日期><P或C><数字>
        return bool(re.match(r'^US\.[A-Z]+\d{6}[PC]\d+$', symbol))

    def _infer_source(self, symbol: str, position: Any) -> Tuple[PositionSource, ProtectionState]:
        """M1.1 (2026-05-16): 多线索推断仓位 source.

        策略孤儿仓 (策略产生但 metadata 丢失) 必须识别出来 → MANAGED 维持监控.
        例: QQQ 5/3 入场, 13 天后 trade_records 丢失但 action_log_json 显示 ExitMonitor 在更新 trail.
        若一刀切标 LEGACY+PROTECTED, ExitMonitor 跳过监控, 用户保利的 SL 失效.

        优先级:
        1. trade_records 历史 (有入场记录 → 是策略仓)
        2. action_log_json 监控痕迹 (ExitMonitor 已在管)
        3. strategy_id 启发式 (futu_sync 之外的策略名)
        4. 兜底: LEGACY + PROTECTED
        """
        # 线索 1: trade_records 历史
        try:
            hist = self._lookup_trade_records_source(symbol)
            if hist != PositionSource.UNKNOWN:
                return hist, ProtectionState.MANAGED
        except Exception as e:
            logger.debug(f"[Reconciler·_infer_source·trade_records] {symbol}: {e}")

        # 线索 2: action_log_json 含 exit_monitor 痕迹 → 策略已在管
        try:
            if self._has_exit_monitor_history(position):
                # 默认 AUTO_MAIN (主线), 因为只有主线 ExitMonitor 写 action_log
                return PositionSource.AUTO_MAIN, ProtectionState.MANAGED
        except Exception as e:
            logger.debug(f"[Reconciler·_infer_source·action_log] {symbol}: {e}")

        # 线索 3: strategy_id 启发式
        try:
            strat_id = (getattr(position, "strategy_id", "") or "").lower()
            # futu_sync / broker_recovery 是同步标记, 不是策略名 — 跳过
            if strat_id and strat_id not in ("futu_sync", "broker_recovery", ""):
                inferred = PositionSource.from_str(strat_id)
                if inferred != PositionSource.UNKNOWN and inferred.is_auto():
                    return inferred, ProtectionState.MANAGED
            # 同时看 owner_strategy
            owner = (getattr(position, "owner_strategy", "") or "").lower()
            if owner and owner not in ("",):
                inferred = PositionSource.from_str(owner)
                if inferred != PositionSource.UNKNOWN and inferred.is_auto():
                    return inferred, ProtectionState.MANAGED
        except Exception as e:
            logger.debug(f"[Reconciler·_infer_source·strategy_id] {symbol}: {e}")

        # 兜底: LEGACY + PROTECTED
        return PositionSource.LEGACY, ProtectionState.PROTECTED

    def _lookup_trade_records_source(self, symbol: str) -> PositionSource:
        """线索 1: 查 trade_records 最近的入场记录"""
        try:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT owner_strategy, source_type, strategy_id "
                "FROM trade_records WHERE symbol = ? AND exit_time IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            if row is None:
                # 也看含已平仓的最近 30 天 (防 trade_records 入场记录丢但有出场)
                row = conn.execute(
                    "SELECT owner_strategy, source_type, strategy_id "
                    "FROM trade_records WHERE symbol = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
            conn.close()
            if row is None:
                return PositionSource.UNKNOWN
            owner, src_type, strat_id = row
            for cand in (owner, src_type, strat_id):
                if not cand:
                    continue
                inferred = PositionSource.from_str(cand)
                if inferred != PositionSource.UNKNOWN:
                    return inferred
        except Exception as e:
            logger.debug(f"[Reconciler·_lookup_trade_records] {symbol}: {e}")
        return PositionSource.UNKNOWN

    def _has_exit_monitor_history(self, position: Any) -> bool:
        """线索 2: open_positions.action_log_json 中是否有 ExitMonitor 监控痕迹.

        Position 对象上不一定带 action_log_json (内存对象), 查 DB.
        """
        try:
            symbol = getattr(position, "symbol", "")
            if not symbol:
                return False
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT action_log_json FROM open_positions WHERE symbol = ? LIMIT 1",
                (symbol,)
            ).fetchone()
            conn.close()
            if row is None or not row[0]:
                return False
            log_json = str(row[0])
            # 含 update_trail / exit_monitor 字符串 = 主线 ExitMonitor 已在管
            return ("exit_monitor" in log_json or "update_trail" in log_json)
        except Exception:
            return False

    def _add_legacy_position(self, symbol: str, info: Dict):
        """broker_only — 启动期补录. M1.1: 多线索推断, 不一刀切."""
        from app.core.models import Position, Direction, MarketRegime
        size = float(info.get("qty", 0) or 0)
        cost = float(info.get("cost", 0) or 0)
        direction = info.get("direction", "LONG")
        if size <= 0 or cost <= 0:
            logger.debug(f"[Reconciler] {symbol} 跳过 (size/cost 非法 size={size} cost={cost})")
            return

        pos = Position(
            symbol=symbol,
            direction=Direction.LONG if direction == "LONG" else Direction.SHORT,
            size=size,
            avg_entry_price=cost,
            initial_sl=0.0,
            current_sl=0.0,
            initial_tp=0.0,
            entry_time=datetime.now(),  # 真实 entry 不可知, 用启动时间
            market_regime=MarketRegime.RANGE,
        )

        # M1.1: 智能推断 — 即使 broker_only, 仍可能是策略孤儿仓 (例: QQQ 5/3 入场, trade_records 丢)
        inferred_src, inferred_protection = self._infer_source(symbol, pos)
        tag_position(pos, inferred_src, inferred_protection)
        # 额外信息
        meta = pos.metadata
        meta["reconciled_at"] = datetime.now().isoformat()
        meta["reconcile_reason"] = "broker_only_at_startup"

        self.position_store.add(symbol, pos)

        if inferred_src.is_auto():
            logger.warning(
                f"[Reconciler·M1.1·adopt] 🤖 broker_only 识别为策略孤儿: "
                f"{symbol} {direction} qty={size} cost=${cost:.2f} "
                f"→ {inferred_src.value} + {inferred_protection.value} "
                f"(ExitMonitor 续命, 不打断已有 trail/SL)"
            )
        else:
            logger.warning(
                f"[Reconciler] 🔍 broker_only 补录: {symbol} {direction} qty={size} cost=${cost:.2f} "
                f"→ {inferred_src.value} + {inferred_protection.value} "
                f"(策略不自动操作; 如需接管: scripts/position_takeover.py takeover {symbol})"
            )

    def _mark_ghost(self, position: Any):
        """local_only — broker 无, 内存有 → 标记 ghost"""
        meta = getattr(position, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["is_ghost"] = True
        meta["ghost_detected_at"] = datetime.now().isoformat()
        try:
            position.metadata = meta
        except Exception:
            pass
        logger.warning(
            f"[Reconciler] 👻 local_only 幽灵: {position.symbol} qty={getattr(position,'size',0)} "
            f"(broker 无对应持仓, 已标 is_ghost; 30s position_sync 会清理)"
        )

    def _log_summary(self, result: ReconcileResult, broker_positions, local_positions):
        logger.warning("=" * 70)
        logger.warning("[Reconciler] 启动对账完成")
        logger.warning(f"  Broker 持仓: {len(broker_positions)} | 本地内存: {len(local_positions)}")
        logger.warning(f"  matched         : {result.matched}")
        logger.warning(f"  broker_only     : {result.broker_only_added} (智能推断)")
        logger.warning(f"  ↳ adopted       : {result.adopted_strategy} (策略孤儿 → MANAGED, ExitMonitor 续命)")
        logger.warning(f"  ↳ legacy        : {result.broker_only_added + result.tagged_legacy - result.adopted_strategy} (LEGACY/MANUAL → PROTECTED)")
        logger.warning(f"  local_only      : {result.local_only_ghosted} (标 ghost)")
        logger.warning(f"  errors          : {result.errors}")
        if result.adopted_strategy > 0:
            logger.warning(
                "  🤖 adopted 仓位 (识别为策略孤儿): ExitMonitor 续命监控, 已有 trail/SL 不变."
            )
        legacy_count = result.broker_only_added + result.tagged_legacy - result.adopted_strategy
        if legacy_count > 0:
            logger.warning(
                f"  ⚠ {legacy_count} 笔 LEGACY 受保护 — 策略不操作; 如需接管: "
                "python scripts/position_takeover.py takeover <symbol>"
            )
        logger.warning("=" * 70)


def run_startup_reconcile(position_store, broker_adapter=None) -> ReconcileResult:
    """启动期一次性对账入口"""
    return StartupReconciler(position_store, broker_adapter).run()
