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
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # Tuple 用于 _infer_source 返回值

from .source import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state, tag_position,
)

logger = logging.getLogger(__name__)


# Task #36 (2026-06-16) 幂等保护:
# 启动期 reconciler 会反复把已知的 broker_only 持仓 (如 US.SNDK) 标为
# "新发现 / broker_only 补录" 并在 WARNING 日志/飞书刷屏. 真正的状态变化
# 只发生在 (symbol, direction, qty, source, owner_strategy) 五元组
# 变化时. 此处用一份持久化签名缓存做去重: 签名一致 → 静默 (DEBUG); 变化
# 才走原有的 INFO/WARNING 路径.
_SIGNATURE_FILE_NAME = "reconcile_signatures.json"


def _signature_for(
    symbol: str,
    direction: str,
    qty: float,
    source: str = "",
    owner_strategy: str = "",
) -> str:
    """对 (symbol, direction, qty, source, owner_strategy) 生成稳定签名 (sha256[:16]).

    qty 取整到 4 位小数避免浮点抖动 (2026-06-16 Task #36).
    """
    raw = (
        f"{symbol}|{(direction or '').upper()}|{round(float(qty or 0), 4)}|"
        f"{(source or '').lower()}|{(owner_strategy or '').lower()}"
    )
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


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
                db_path = str(Path(__file__).resolve().parent.parent.parent /
                             "data" / "ftseek_us.db")
        self._db_path = db_path
        # Task #36: 幂等签名缓存 (持久化到 data/reconcile_signatures.json)
        self._signature_path: Path = (
            Path(db_path).resolve().parent / _SIGNATURE_FILE_NAME
        )
        self._last_seen_signature: Dict[str, str] = self._load_signatures()
        # 本轮活跃签名集合, _save_signatures() 用 (剔除消失的 symbol)
        self._current_signature: Dict[str, str] = {}

    # ───────────────────── Task #36: 签名持久化 ─────────────────────
    def _load_signatures(self) -> Dict[str, str]:
        try:
            if self._signature_path.exists():
                with open(self._signature_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f) or {}
                if isinstance(raw, dict):
                    return {str(k): str(v) for k, v in raw.items()}
        except Exception as e:
            logger.debug(f"[Reconciler·sig] 加载签名缓存失败 (忽略): {e}")
        return {}

    def _save_signatures(self) -> None:
        try:
            self._signature_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._signature_path.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._current_signature, f, ensure_ascii=False, indent=2)
            tmp.replace(self._signature_path)
        except Exception as e:
            logger.debug(f"[Reconciler·sig] 保存签名缓存失败 (忽略): {e}")

    def _signature_changed(self, symbol: str, signature: str) -> bool:
        """True 表示状态变化 (新发现/数量/方向/来源 变化), False 表示与上次完全一致."""
        prev = self._last_seen_signature.get(symbol)
        self._current_signature[symbol] = signature
        return prev != signature

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
        finally:
            # Task #36: 持久化本轮签名 — 即便部分失败也保留已计算的部分
            self._save_signatures()
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
        # Task #36 (2026-06-16): 已知签名 (上一轮已记录) 跳过 "新发现" 告警, 仅
        #   静默补录到内存即可; 真正的状态变化 (qty/方向/来源) 才打 WARNING.
        for sym in (broker_syms - local_syms):
            try:
                info = broker_positions[sym]
                self._add_legacy_position(sym, info)
                result.broker_only_added += 1
            except Exception as e:
                logger.warning(f"[Reconciler·broker_only] {sym}: {e}")
                result.errors += 1

        # Task #36: 已 matched 的持仓也写入签名 (供下次对账判断 "未变化")
        for sym in (broker_syms & local_syms):
            try:
                info = broker_positions[sym]
                pos = local_positions.get(sym)
                src = (
                    (getattr(pos, 'metadata', {}) or {}).get('source', '')
                    or getattr(pos, 'source', '')
                )
                owner = getattr(pos, 'owner_strategy', '') or ''
                sig = _signature_for(
                    sym, info.get('direction', 'LONG'), info.get('qty', 0),
                    str(src), str(owner),
                )
                self._current_signature[sym] = sig
            except Exception:
                pass

        # 3. local_only — 本地有 + broker 无 → 幽灵, 标记保留 (用户决定清理)
        # 2026-07-01: 期权检测已删除 (自动交易不做期权)
        for sym in (local_syms - broker_syms):
            try:
                pos = local_positions[sym]
                self._mark_ghost(pos)
                result.local_only_ghosted += 1
            except Exception as e:
                logger.warning(f"[Reconciler·local_only] {sym}: {e}")
                result.errors += 1

        return result

    def _infer_source(self, symbol: str, position: Any) -> Tuple[PositionSource, ProtectionState]:
        """M1.1 (2026-05-16): 多线索推断仓位 source.

        策略孤儿仓 (策略产生但 metadata 丢失) 必须识别出来 → MANAGED 维持监控.
        例: QQQ 5/3 入场, 13 天后 trade_records 丢失但 action_log_json 显示 ExitMonitor 在更新 trail.
        若一刀切标 LEGACY+PROTECTED, ExitMonitor 跳过监控, 用户保利的 SL 失效.

        优先级:
        0. 手动持仓检查 (ManualHoldRegistry + DB owner=manual) → MANUAL + PROTECTED
        1. trade_records 历史 (有入场记录 → 是策略仓)
        2. action_log_json 监控痕迹 (ExitMonitor 已在管)
        3. strategy_id 启发式 (futu_sync 之外的策略名)
        4. 兜底: LEGACY + PROTECTED
        """
        # P0 线索 0 (2026-06-15): 手动持仓优先 — 防止被误判为策略孤儿
        # MU 事件: 手动 SHORT 被 _infer_source 误判为 AUTO_MAIN, ExitMonitor 自动平仓
        try:
            if self._is_manual_position(symbol, position):
                logger.info(f"[Reconciler·_infer_source] {symbol}: 识别为手动持仓 → MANUAL+PROTECTED")
                return PositionSource.MANUAL, ProtectionState.PROTECTED
        except Exception as e:
            logger.debug(f"[Reconciler·_infer_source·manual_check] {symbol}: {e}")

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
            # 2026-07-03 fix: PLAYBOOK_SIM/LIVE 仓位有独立的 EOD 退出逻辑 (cohort B 持夜等),
            # 不应被 ExitMonitor 组合止损干涉. 返回 UNKNOWN → PROTECTED, 旧系统不碰.
            # 根因: from_str("playbook_sim") → AUTO_MAIN → MANAGED → ExitMonitor 收养 → 分批平仓
            for _val in (src_type, owner, strat_id):
                if _val and _val.lower() in ("playbook_sim", "playbook_live"):
                    return PositionSource.UNKNOWN
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

    def _is_manual_position(self, symbol: str, position: Any) -> bool:
        """P0 线索 0 (2026-06-15): 判断仓位是否为手动开仓.

        三层检查 (任一命中即返回 True):
        1. ManualHoldRegistry 内存注册表
        2. DB open_positions.owner = 'manual'
        3. DB open_positions.trader_type = 'MANUAL' 且 strategy_id 不含 'auto'
        """
        # 检查 1: ManualHoldRegistry
        try:
            from app.coordinator.manual_hold_registry import manual_hold_registry
            if manual_hold_registry.is_manual_held(symbol):
                return True
        except Exception:
            pass

        # 检查 2+3: DB open_positions
        try:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT owner, trader_type, strategy_id, source FROM open_positions WHERE symbol = ? LIMIT 1",
                (symbol,)
            ).fetchone()
            conn.close()
            if row:
                owner = (row[0] or "").lower()
                trader_type = (row[1] or "").upper()
                strategy_id = (row[2] or "").lower()
                source = (row[3] or "").lower()
                # owner='manual' 是手动持仓的明确标记
                if owner == "manual":
                    return True
                # trader_type=MANUAL 且 strategy_id 非自动策略
                if trader_type == "MANUAL" and not any(
                    s in strategy_id for s in ("auto_", "main_pipeline", "voting_llm", "orb")
                ):
                    return True
                # source 字段标记为 manual
                if source in ("manual", "futu_manual", "manual_sync"):
                    return True
        except Exception:
            pass

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

        # Task #36: 先用 broker 信息算 "轻签名" 判断是否真正 "新发现";
        # 完整 source/owner 在 _infer_source 后才确定 → 用于本轮签名持久化.
        sig_basic = _signature_for(symbol, direction, size, "broker_only", "")
        is_new_or_changed = self._signature_changed(symbol, sig_basic)

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
        # 额外信息 — 时间戳走 utils.timezone.now_et (Task #36 约束)
        meta = pos.metadata
        try:
            from app.utils.timezone import now_et
            meta["reconciled_at"] = now_et().isoformat()
        except Exception:
            meta["reconciled_at"] = datetime.now().isoformat()
        meta["reconcile_reason"] = "broker_only_at_startup"

        self.position_store.add(symbol, pos)

        # Task #36: 完整签名 (含 source/owner) 覆盖 sig_basic, 留作下一轮对比依据
        full_sig = _signature_for(
            symbol, direction, size, inferred_src.value,
            getattr(pos, "owner_strategy", "") or inferred_src.value,
        )
        self._current_signature[symbol] = full_sig

        # Task #36: 仅在 "真正状态变化" (新发现 / qty 变 / 方向变 / 来源变) 时
        #          走原有 WARNING 路径; 已知持仓静默 (DEBUG) 防止 5 分钟刷屏.
        if not is_new_or_changed:
            logger.debug(
                f"[Reconciler·idempotent] {symbol} {direction} qty={size} "
                f"签名未变 (sig={sig_basic}), 跳过 broker_only 告警"
            )
            return

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
        """local_only — broker 无, 内存有 → 标记 ghost (幂等: 签名未变则静默)"""
        symbol = getattr(position, "symbol", "?")
        # Task #36 幂等: 签名未变 → DEBUG 静默 (防启动期刷屏)
        sig = _signature_for(
            symbol,
            getattr(position, "direction", ""),
            getattr(position, "size", 0),
            "ghost", "",
        )
        is_new_or_changed = self._signature_changed(symbol, sig)

        meta = getattr(position, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["is_ghost"] = True
        meta["ghost_detected_at"] = datetime.now().isoformat()
        try:
            position.metadata = meta
        except Exception:
            pass

        if not is_new_or_changed:
            logger.debug(
                f"[Reconciler·idempotent] {symbol} 幽灵签名未变 (sig={sig}), 跳过告警"
            )
            return

        logger.warning(
            f"[Reconciler] 👻 local_only 幽灵: {symbol} qty={getattr(position,'size',0)} "
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
