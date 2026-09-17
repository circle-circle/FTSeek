#!/usr/bin/env python3
"""
US Position Sync Module (FTSeek-US)
- Runtime position reconciliation between Futu SSOT and local DB
- Runs as background thread, 30s interval
- Handles both LONG and SHORT positions
"""

import json
import logging
import threading
import time
import sqlite3
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional

from app.core.db_utils import safe_sqlite_connect

logger = logging.getLogger("ftseek_us.position_sync")

# 2026-08-05: 自管持仓跳过日志的进程内去重 (首条 INFO, 后续 debug)
_SELF_MANAGED_SKIP_LOGGED: set = set()

# external_close 查重判据 (2026-08-21 建立, 2026-08-28 收紧): 只有"已了结的记账行"才算重复。
# 旧判据 realized_pnl IS NOT NULL 会把同一仓位的**开仓行**也当作已记账平仓 (未平仓行历史上带
# realized_pnl=0.0 占位) → 真实券商平仓被跳过 INSERT 与通知, 而 open_positions 的 DELETE 照常
# 执行 → 该笔平仓在系统里彻底消失。改按全仓统一的已平仓口径 exit_price > 0 判定。
# 抽为模块常量: 守卫本体深在 ~500 行同步方法内不可单测, 由 tests 直接对该 SQL 钉不变量。
EXTERNAL_CLOSE_DUP_SQL = (
    "SELECT id, entry_time FROM trade_records WHERE symbol=? AND direction=? "
    "AND exit_price > 0"
)

# ── [RFC-080 2026-09-09] 系统委托 remark 白名单 — AUTO 认领的唯一凭证 ──────────
# 病灶: _query_today_futu_orders 返回当日 env 内**所有**已成交委托, AUTO/MANUAL 分类
# 此前只看 "symbol in _today_orders" ⇒ 切实盘后 (AUTO_TRADE_ACCOUNT=real), 用户在富途
# APP 直接下的手动单 (无 remark) 会被认领为 AUTO → 纳入 PositionStore/DB trader_type=
# AUTO → 被自动退出监控平掉真钱手动仓。2026-07-02 "安全默认 MANUAL" 只修了 else 分支,
# 没修 _today_orders 命中分支。
# 活体探针实证 (2026-09-09, history_order_list_query 9/1-9/9):
#   SIMULATE env 159 笔 remark 全部 'pb_*' (playbook client_order_id=pb_{iid}_{env});
#   REAL env 45 笔 remark 全部 '' (APP/看板手动单 place_order 不带 remark)。
#   remark 经 broker 往返不丢失 ⇒ 可作归属凭证。
# 白名单 = 全部 auto 下单生产者的 remark 前缀 (grep 'remark=' 全仓枚举):
#   pb_*              playbook 入场 (pb_{iid}_{env}) + 平仓 (pb_close_/pb_be_close_/pb_stop_)
#                     — dual_broker.py:228 remark=order.client_order_id
#   orb_*             ORB 引擎 (engine.py:2327, 现禁用, 预留)
#   OVER_LIMIT_TRIM   position_sync 自trim (:2427)
# fail-safe 方向: 未知/空 remark → 非系统单 → MANUAL (宁可漏认领也不误平手动仓)。
# 严格大小写匹配 (生产者全小写 pb_/orb_, OVER_LIMIT_TRIM 全大写; 不做 case-fold,
# 意外大小写变体归 MANUAL 更安全)。
# 注意: 外部平仓检测 (~:1548 _today_orders.get) **不得**用本白名单过滤 —
# 手动/APP 平仓正是它要检测的对象。本白名单只用于 AUTO 认领两处分类点。
_SYSTEM_ORDER_REMARK_PREFIXES = ("pb_", "orb_", "ext_entry", "ext_exit_", "OVER_LIMIT_TRIM")


def is_system_order_remark(remark) -> bool:
    """委托 remark 是否出自系统下单路径 (RFC-080 AUTO 认领白名单).

    fail-safe: None/空/未知前缀 → False (归 MANUAL, 与 2026-07-02 安全默认同向)。
    """
    if not remark:
        return False
    r = str(remark)
    return any(r.startswith(p) for p in _SYSTEM_ORDER_REMARK_PREFIXES)


def upsert_external_close_trade_record(
    conn, *, symbol, direction, entry_time, entry_price, exit_price, quantity,
    realized_pnl, pnl_pct, trade_mode, created_at, trace_id=None, signal_id=None,
    futu_order_id=None, close_order_id=None,
):
    closed = conn.execute(
        "SELECT id FROM trade_records WHERE symbol=? AND direction=? AND exit_price > 0 "
        "ORDER BY id DESC LIMIT 1",
        (symbol, direction),
    ).fetchone()
    if closed:
        return {"action": "duplicate_skip", "trade_id": closed[0]}

    open_trade = conn.execute(
        "SELECT id FROM trade_records WHERE symbol=? AND direction=? "
        "AND (COALESCE(exit_price, 0)=0 OR COALESCE(exit_time, '')='') "
        "ORDER BY id DESC LIMIT 1",
        (symbol, direction),
    ).fetchone()
    if open_trade:
        conn.execute(
            "UPDATE trade_records SET exit_price=?, exit_time=?, quantity=?, "
            "realized_pnl=?, pnl_pct=?, exit_reason='external_close', "
            "close_futu_order_id=?, trace_id=COALESCE(trace_id, ?), "
            "signal_id=COALESCE(signal_id, ?), futu_order_id=COALESCE(futu_order_id, ?) "
            "WHERE id=? AND (COALESCE(exit_price, 0)=0 OR COALESCE(exit_time, '')='')",
            (exit_price, created_at, quantity, realized_pnl, pnl_pct,
             close_order_id, trace_id, signal_id, futu_order_id, open_trade[0]),
        )
        return {"action": "updated_open_trade", "trade_id": open_trade[0]}

    cursor = conn.execute(
        "INSERT INTO trade_records "
        "(trade_date, symbol, direction, entry_time, exit_time, entry_price, exit_price, "
        "quantity, realized_pnl, pnl_pct, exit_reason, source_type, mode, trade_mode, created_at, "
        "trace_id, signal_id, futu_order_id, close_futu_order_id, config_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'v2')",
        (created_at[:10], symbol, direction, entry_time, created_at, entry_price,
         exit_price, quantity, realized_pnl, pnl_pct, "external_close",
         "POSITION_SYNC", "manual", trade_mode, created_at, trace_id, signal_id,
         futu_order_id, close_order_id),
    )
    return {"action": "inserted", "trade_id": cursor.lastrowid}

def _get_db_path():
    from app.core.db_utils import get_main_db
    return str(get_main_db())

# Legacy module-level constant - replaced by _get_db_path() for mode isolation
DB_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'ftseek_us.db'))


class USPositionSync:
    """美股持仓同步器"""

    def _backfill_playbook_trade_record(self, symbol: str, direction: str, qty: int,
                                        entry: float, sl: float, tp: float,
                                        trace_id: str, futu_order_id: str = "",
                                        broker_env: str = "sim") -> bool:
        """统一管线 #4 (2026-07-20): 恢复仓位标 playbook_sim 时补录未平 PLAYBOOK_SIM trade_record.

        场景: playbook 下单后 GHOST-RC1 拒绝记账但订单实际成交 → trade_records 无记录 →
        is_playbook_open_position=False. 若只改 owner_strategy 标记, 主线护栏 (查 trade_records)
        跳过 + playbook 监控 (查 trade_records) 也不管 → 两不管孤儿仓. 补录后 playbook 接管.
        幂等: 已有未平 PLAYBOOK 记录则跳过. 失败仅告警不中断同步 (可用
        scripts/adopt_position_to_playbook.py 人工收编).
        """
        try:
            db_path = _get_db_path()
            _source = "PLAYBOOK_LIVE" if broker_env == "real" else "PLAYBOOK_SIM"
            conn = safe_sqlite_connect(db_path, timeout=5)
            try:
                existing = conn.execute(
                    "SELECT id FROM trade_records WHERE symbol=? AND source_type=? "
                    "AND (exit_price=0 OR exit_price IS NULL OR exit_time='' OR exit_time IS NULL) "
                    "LIMIT 1", (symbol, _source)
                ).fetchone()
            finally:
                conn.close()
            if existing:
                return True
            now_iso = datetime.now().astimezone().isoformat()
            # ③ 防重 (2026-09-16): 同 symbol+direction+price 且入场时间差<60s 的 open 记录已存在则跳过
            # (防 playbook 簿记失败后 P3 补录与 playbook 重试写入产生重复行, 如 1511/1512)
            try:
                dup_conn = safe_sqlite_connect(db_path, timeout=5)
                try:
                    dup = dup_conn.execute(
                        "SELECT id FROM trade_records WHERE symbol=? AND direction=? "
                        "AND ABS(entry_price - ?) < 0.01 "
                        "AND (exit_price=0 OR exit_price IS NULL OR exit_time='' OR exit_time IS NULL) "
                        "AND ABS(julianday(entry_time) - julianday(?)) * 86400 < 60 "
                        "LIMIT 1",
                        (symbol, direction, entry, now_iso)
                    ).fetchone()
                finally:
                    dup_conn.close()
            except Exception:
                dup = None
            if dup:
                logger.info(
                    f"[US Sync·P3] {symbol} 同方向同价格 open 记录已存在 (id={dup[0]}), 跳过补录"
                )
                return True
            # atr 从 SL 距离反推 (playbook 退出用 %-drawdown, atr 仅诊断用途)
            atr_guess = abs(entry - sl) / 2.0 if (entry and sl) else 0.0
            record = {
                "trace_id": trace_id,
                "trade_date": now_iso[:10],
                "symbol": symbol,
                "direction": direction,
                "entry_time": now_iso,
                "exit_time": "",
                "entry_price": entry,
                "exit_price": 0,
                "quantity": qty,
                "realized_pnl": 0,
                "pnl_pct": 0,
                "exit_reason": "",
                "holding_hours": 0,
                "signal_strength": 50,
                "signal_confidence": 0.5,
                "futu_order_id": futu_order_id,
                "source_type": "PLAYBOOK_LIVE" if broker_env == "real" else "PLAYBOOK_SIM",
                "entry_reason": "sync_recovery_backfill",
                "owner_strategy": "playbook_live" if broker_env == "real" else "playbook_sim",
                "strategy_id": "playbook_live" if broker_env == "real" else "playbook_sim",
                "market_regime": "RANGE",
                "mode": "normal",
                "trade_mode": "live" if broker_env == "real" else "sim",
                "signal_id": trace_id,
                "metadata_json": json.dumps({
                    "stop_loss": sl,
                    "take_profit": tp,
                    "atr": atr_guess,
                    "peak_price": entry,
                    "trough_price": entry,
                    "entry_price_source": "sync_recovery_backfill",
                    "adopted": True,
                    "adopt_origin": "position_sync",
                }),
            }
            from app.storage.trade_store import TradeStore
            rid = TradeStore(db_path).save_trade(record)
            logger.info(
                f"[US Sync·P3] {symbol} 已补录未平 {_source} trade_record (id={rid}, "
                f"sl=${sl:.2f}) → playbook 监控接管"
            )
            return True
        except Exception as e:
            logger.error(
                f"[US Sync·P3] {symbol} playbook trade_record 补录失败: {e} — "
                f"该仓可能两不管, 请人工执行 scripts/adopt_position_to_playbook.py {symbol}"
            )
            return False

    def _reconcile_playbook_execution_states(self, ctx, trd_env, broker_env: str,
                                             futu_positions: Dict, limit: int = 25) -> None:
        """B1-lite 未决订单对账；只有 broker 明确零成交终态才解锁。"""
        db_path = _get_db_path()
        if not os.path.exists(db_path):
            return
        conn = safe_sqlite_connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, symbol, direction, stop_loss, take_profit, execution_price, "
                "execution_time, execution_state, execution_attempt, broker_order_id, broker_env "
                "FROM playbook_instructions WHERE execution_state IN "
                "('UNKNOWN','SUBMITTED','FILLED_UNBOOKED') ORDER BY updated_at, id LIMIT ?",
                (int(limit),),
            ).fetchall()
            from app.playbook.db import PlaybookDB
            pdb = PlaybookDB(conn=conn)
            for row in rows:
                env = str(row["broker_env"] or broker_env).lower()
                if env != broker_env:
                    continue
                instruction_id = int(row["id"])
                state = str(row["execution_state"] or "")
                order_id = str(row["broker_order_id"] or "")
                attempt = int(row["execution_attempt"] or 0)
                dealt_qty = 0.0
                dealt_avg_price = 0.0

                if state in ("UNKNOWN", "SUBMITTED"):
                    try:
                        if order_id:
                            ret, data = ctx.order_list_query(order_id=order_id, trd_env=trd_env)
                        else:
                            ret, data = ctx.order_list_query(trd_env=trd_env)
                            if ret == 0 and data is not None and not data.empty:
                                if "remark" not in data:
                                    data = data.iloc[0:0]
                                else:
                                    remark = f"pb_{instruction_id}_{env}_{attempt}"
                                    data = data[data["remark"].astype(str) == remark]
                        if ret != 0 or data is None or data.empty:
                            logger.warning(
                                f"[US Sync·B1] 未找到未决订单 instruction={instruction_id} "
                                f"attempt={attempt}, 保持锁定"
                            )
                            continue
                        broker_row = data.iloc[-1]
                        discovered_id = str(broker_row.get("order_id", "") or order_id)
                        status = str(broker_row.get("order_status", "") or "UNKNOWN")
                        dealt_qty = float(broker_row.get("dealt_qty", 0) or 0)
                        dealt_avg_price = float(broker_row.get("dealt_avg_price", 0) or 0)
                        if not discovered_id or not pdb.mark_execution_submitted(
                                instruction_id, attempt, discovered_id, env, status):
                            logger.critical(
                                f"[US Sync·B1] broker order 身份持久化失败 instruction={instruction_id} "
                                f"attempt={attempt} order_id={discovered_id}, 保持锁定"
                            )
                            continue
                        order_id = discovered_id
                        if dealt_qty > 0 or "FILLED_ALL" in status or "FILLED_PART" in status:
                            if not pdb.mark_execution_filled_unbooked(
                                    instruction_id, attempt, status):
                                continue
                            state = "FILLED_UNBOOKED"
                        else:
                            pdb.unlock_execution_if_zero_fill_terminal(
                                instruction_id, attempt, status, dealt_qty=dealt_qty)
                            continue
                    except Exception as exc:
                        logger.exception(
                            f"[US Sync·B1] order 查询失败 instruction={instruction_id} "
                            f"order_id={order_id}: {exc}"
                        )
                        continue

                if state != "FILLED_UNBOOKED":
                    continue
                pos = futu_positions.get(str(row["symbol"]))
                if not pos:
                    continue
                entry = float(
                    dealt_avg_price or row["execution_price"] or pos.get("cost", 0) or 0
                )
                if entry <= 0:
                    continue
                qty = int(dealt_qty or pos.get("qty") or 0)
                if qty <= 0:
                    continue
                ok = self._backfill_playbook_trade_record(
                    symbol=str(row["symbol"]), direction=str(pos.get("direction") or row["direction"]),
                    qty=qty, entry=entry,
                    sl=float(row["stop_loss"] or 0), tp=float(row["take_profit"] or 0),
                    trace_id=f"playbook-reconcile-{instruction_id}-{attempt}",
                    futu_order_id=order_id, broker_env=env,
                )
                if ok:
                    pdb.mark_execution_booked(
                        instruction_id, attempt, execution_price=entry,
                        execution_time=row["execution_time"] or datetime.now(timezone.utc).isoformat(),
                        outcome="holding",
                    )
        except sqlite3.OperationalError as exc:
            logger.warning(f"[US Sync·B1] execution state 对账跳过: {exc}", exc_info=True)
        except Exception as exc:
            logger.exception(f"[US Sync·B1] execution state 对账失败: {exc}")
        finally:
            conn.close()

    def __init__(self, sync_interval: int = 30, position_store=None, broker_adapter=None):
        self.sync_interval = sync_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_sync_time: Optional[datetime] = None
        self._broker_adapter = broker_adapter  # 复用执行引擎的 broker 连接
        self._sync_count = 0
        self._error_count = 0
        self.position_store = position_store  # P0 修复 2026-04-25: PositionStore 内存引用
        self._futu_ctx = None  # P2 修复: 复用 Futu 连接，避免每 30 秒新建/断开
        # P0 修复 (2026-06-24): _reset_futu_ctx 初始化的属性 (_consecutive_empty_queries /
        # _last_known_positions / _closed_tombstones 等) 此前只在 reset 时才赋值, 连接
        # 正常、从未 reset 的实例首次跑到空查询计数 (line ~482) 会 AttributeError →
        # "Failed to get Futu US positions" 告警。__init__ 末尾调一次保证属性就位。
        # _futu_ctx 为 None 时 close 块是 no-op, 仅走属性初始化, 无副作用。
        self._reset_futu_ctx("init")

    def _reset_futu_ctx(self, reason: str = ""):
        """安全重置 Futu 连接: 先关闭旧连接再置 None，防止连接泄漏到 OpenD"""
        if self._futu_ctx is not None:
            try:
                self._futu_ctx.close()
            except Exception:
                pass
            logger.info(f"[US Sync] Futu 连接已关闭并重置 ({reason})")
            self._futu_ctx = None
        # P0 修复 (2026-05-11): 添加已知持仓缓存，防止连接瞬断时误删幽灵仓
        self._last_known_positions: Dict[str, Dict] = {}
        self._consecutive_empty_queries = 0
        self._max_consecutive_empty = 3  # 连续3次空查询才确认真实空仓
        # 2026-08-03 (任务#6 降噪): 空仓期每 3 轮一轮告警 ≈14次/h 纯噪音 (07-27 修复后
        #   计数器不再重置, 空仓期循环"跳过本轮→确认真空"永不停止). 改为每 streak 只告警
        #   首轮 + 确认轮, 之后静默至出现非空查询.
        self._empty_streak_warned = False
        # P0-2 修复 (2026-05-19): tombstone 防止刚平仓被 sync 反向恢复
        # 5/18 ARM 紊乱: 22:02:54 ghost 清理 → 22:03:25 又"从 DB 恢复" → 死循环
        # 根因: broker 异步确认延迟期间, sync 看到 broker 暂无该仓位 → 走 ghost 清理,
        # 下一轮 broker 又报有 → 走"从 DB 恢复" → 内存又有了 ARM → 引发新 close 循环
        # 解决: 主流程平仓后在 mark_closed_tombstone(), sync 看到 tombstone 就跳过 restore
        # P0-3 修复 (2026-06-14): 存储格式从 float 改为 tuple (ts, ttl),
        #   支持 confirm_tombstone 延长 TTL (60s → 300s)
        self._closed_tombstones: Dict[str, tuple] = {}  # symbol → (close_at_ts, ttl_sec)
        self._tombstone_ttl_sec = 60.0  # 默认 TTL: 60s
        # ── P1 (2026-06-16): 即时 sync 触发 (5s debounce) ──
        self._immediate_sync_requested = False
        self._last_immediate_sync_time: float = 0.0
        self._immediate_sync_debounce_sec = 5.0
        
        # P0 修复 (2026-06-18): 幽灵仓告警缓存，避免相同幽灵仓每30秒重复告警
        self._last_reported_ghosts = frozenset()

    def start(self):
        if self._running:
            logger.warning("US position sync already running")
            return
        
        # P0 修复 2026-04-25: 启动时立即执行一次完整对账
        # 确保 PositionStore 与 Broker 100% 一致，避免恢复遗漏
        logger.info("🔍 [US Sync] 启动时立即执行 Broker 对账...")
        try:
            self._sync_once()
            logger.info("✅ [US Sync] 启动对账完成")
        except Exception as e:
            logger.warning(f"⚠️ [US Sync] 启动对账失败 (将继续后台同步): {e}")
        
        self._running = True
        self._thread = threading.Thread(target=self._sync_loop, daemon=True, name="PositionSync")
        self._thread.start()
        logger.info("US position sync started (interval=%ds)", self.sync_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("US position sync stopped")

    # ─── P0-2 (2026-05-19): tombstone API — 防止 sync 反向恢复刚平仓 ───
    def mark_closed_tombstone(self, symbol: str) -> None:
        """主流程平仓成功后调用. sync 在 ttl 内不允许从 DB 恢复此 symbol."""
        if not symbol:
            return
        self._closed_tombstones[symbol] = (time.time(), self._tombstone_ttl_sec)
        logger.debug(f"[US Sync] tombstone 标记 {symbol} (ttl={self._tombstone_ttl_sec:.0f}s)")

    def _is_tombstoned(self, symbol: str) -> bool:
        """检查 symbol 是否在 tombstone ttl 内 (期间不允许 sync 恢复). 顺带清理过期项."""
        entry = self._closed_tombstones.get(symbol)
        if entry is None:
            return False
        # 兼容旧格式 (float) 和新格式 (tuple)
        if isinstance(entry, (list, tuple)):
            ts, ttl = entry[0], entry[1]
        else:
            ts, ttl = entry, self._tombstone_ttl_sec
        if time.time() - ts > ttl:
            self._closed_tombstones.pop(symbol, None)
            return False
        return True

    # P0-3 修复 (2026-06-14): confirm_tombstone — 延长已确认平仓的 tombstone TTL
    # 场景: order_executor 通过 broker 确认成交后调用, 将 tombstone TTL 从 60s 延长至 300s
    #   broker 异步结算延迟可能超过默认 60s, 延长至 5min 防止 sync 在结算窗口内误恢复
    # 此前该方法不存在, order_executor.py 调用被 except Exception: pass 静默吞掉
    _CONFIRMED_TOMBSTONE_TTL_SEC = 300.0  # 已确认平仓: 5 分钟

    def confirm_tombstone(self, symbol: str) -> None:
        """确认平仓已完成 — 将 tombstone TTL 从默认 60s 延长至 300s.

        由 order_executor 在 broker 确认成交后调用.
        防止 broker 结算延迟期间 position_sync 误将刚平仓的 symbol 恢复.
        """
        if not symbol:
            return
        # 使用延长 TTL, 刷新时间戳
        self._closed_tombstones[symbol] = (time.time(), self._CONFIRMED_TOMBSTONE_TTL_SEC)
        logger.info(
            f"[US Sync] tombstone 确认 {symbol} "
            f"(TTL={self._CONFIRMED_TOMBSTONE_TTL_SEC:.0f}s)"
        )

    def request_immediate_sync(self) -> None:
        """请求立即执行一次 sync (5s debounce, 合并连续成交).

        由 order_executor 在成交确认后调用. 多次快速调用只触发一次 sync.
        """
        now = time.time()
        if now - self._last_immediate_sync_time < self._immediate_sync_debounce_sec:
            logger.debug(
                f"[US Sync] 即时 sync debounce: 距上次 {now - self._last_immediate_sync_time:.1f}s < "
                f"{self._immediate_sync_debounce_sec}s, 跳过"
            )
            return
        self._immediate_sync_requested = True
        self._last_immediate_sync_time = now
        logger.info("[US Sync] 即时 sync 已请求 (5s debounce)")

    def _sync_loop(self):
        """同步循环 (后台线程运行) — 根据市场阶段动态调整频率"""
        consecutive_skips = 0
        max_skips_before_log = 120  # 每 30 分钟 (15s * 120) 输出一次跳过日志

        while self._running:
            try:
                # 2026-07-31 议题④ E1 (DEB-cleanup-remaining, 用户裁"整体执行"):
                #   RiskStateMachine.auto_recovery 驱动从 emergency_interceptor 假哨兵
                #   线程 (position_store 恒空, 6/12 起零触发, 已退役) 迁至本现役 30s 心跳。
                #   auto_recovery 非 RISK_OFF 态首行早退, 零成本; 失败不阻断同步。
                try:
                    from app.us_pipeline.risk_state_machine import RiskStateMachine
                    RiskStateMachine().auto_recovery()  # 进程级单例 (__new__ 双检锁)
                except Exception:
                    pass
                phase, interval = self._get_market_phase()
                if phase == "CLOSED":
                    consecutive_skips += 1
                    if consecutive_skips == 1:
                        logger.info("[US Sync] Market closed, sync paused (will resume next session)")
                    elif consecutive_skips >= max_skips_before_log:
                        logger.debug("[US Sync] Skipped %d cycles (market closed)", consecutive_skips)
                        consecutive_skips = 0
                    # 休眠 interval 秒
                    for _ in range(interval * 10):
                        if not self._running:
                            break
                        time.sleep(0.1)
                    continue

                if consecutive_skips > 0:
                    logger.info(f"[US Sync] Market {phase}, sync resumed (interval={interval}s)")
                consecutive_skips = 0
                self._sync_once()
                self._sync_count += 1
                self._immediate_sync_requested = False  # 清除即时 sync 标记
                # 休眠 interval 秒 (每 0.1s 检查即时 sync 请求)
                for _ in range(interval * 10):
                    if not self._running:
                        break
                    if self._immediate_sync_requested:
                        logger.info("[US Sync] 即时 sync 触发, 中断休眠")
                        break
                    time.sleep(0.1)
            except Exception as e:
                self._error_count += 1
                logger.error("US position sync failed: %s", e, exc_info=True)
                # 异常后等 60 秒再重试
                for _ in range(600):
                    if not self._running:
                        break
                    time.sleep(0.1)

    def _get_market_phase(self) -> tuple:
        """
        判断当前市场阶段并返回 (phase, sync_interval)

        美股时间 (ET, UTC-4 夏令时 / UTC-5 冬令时):
        - PRE_MARKET:  04:00 - 09:30 ET → 同步间隔 300s (5 分钟)
        - TRADING:     09:30 - 16:00 ET → 同步间隔 30s (默认)
        - AFTER_MARKET: 16:00 - 20:00 ET → 同步间隔 300s (5 分钟)
        - CLOSED:      20:00 - 04:00 ET → 暂停同步

        北京时间 (CST, UTC+8):
        - 夏令时 (3月-11月): ET+12h
          PRE:  16:00-21:30 CST
          TRADE: 21:30-04:00 CST (次日)
          AFTER: 04:00-08:00 CST
          CLOSED: 08:00-16:00 CST
        - 冬令时 (11月-3月): ET+13h
          PRE:  17:00-22:30 CST
          TRADE: 22:30-05:00 CST (次日)
          AFTER: 05:00-09:00 CST
          CLOSED: 09:00-17:00 CST
        """
        from datetime import datetime
        import pytz

        now = datetime.now()

        # 2026-07-27 修复 (时区批同款漏网): 周末判定原用 CST weekday, 时段判定用 ET 时刻 —
        #   CST 周一凌晨 = ET 周日下午, 12h 错位窗内 ET 周日被误判 TRADING/AFTER
        #   (实锤: 07-27 周一 03:42-08:00 CST 白跑 84 轮同步+解锁)。统一改 ET weekday。
        try:
            _et_now = now.astimezone(pytz.timezone("US/Eastern"))
        except Exception:
            _et_now = now
        weekday = _et_now.weekday()

        # 周末判定 (ET 口径: 周六/周日全天 CLOSED; 周五收盘后由下方 ET 时刻分支管)
        if weekday >= 5:  # ET 周六/周日
            return ("CLOSED", 1800)  # 30 分钟检查一次

        # 节假日检查 (2026-07-27: 统一 ET 口径 — 用 ET 交易日查日历, 删除旧 CST 周一凌晨
        #   特判 (那是 CST-weekday 时代补丁, ET 口径下周五夜盘属 ET 周五, 无需特判))
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            if not cal.is_trading_day("US", _et_now.date()):
                return ("CLOSED", 1800)
        except Exception:
            pass

        # 转换为美东时间进行精确判断
        try:
            et_tz = pytz.timezone("US/Eastern")
            et_now = now.astimezone(et_tz)
            et_hour = et_now.hour
            et_minute = et_now.minute
            et_time = et_hour * 60 + et_minute  # 分钟数
        except Exception:
            # fallback: 使用 CST 估算 (假设夏令时)
            et_hour = (now.hour - 12) % 24
            et_time = et_hour * 60 + now.minute

        # 市场阶段判断 (ET)
        pre_market_start = 4 * 60       # 04:00 ET
        market_open = 9 * 60 + 30       # 09:30 ET
        market_close = 16 * 60          # 16:00 ET
        after_market_end = 20 * 60      # 20:00 ET

        if pre_market_start <= et_time < market_open:
            return ("PRE_MARKET", 300)     # 5 分钟
        elif market_open <= et_time < market_close:
            return ("TRADING", self.sync_interval)  # 默认 30 秒
        elif market_close <= et_time < after_market_end:
            return ("AFTER_MARKET", 300)  # 5 分钟
        else:
            return ("CLOSED", 1800)        # 30 分钟

    def _get_futu_context(self, opend_host: str, opend_port: int, trade_env, trd_market, security_firm):
        """获取或复用 Futu 交易上下文 (避免每 30 秒新建/断开连接)

        三态融合 (2026-06-25): 始终解锁 REAL 权限，同一 ctx 可查 SIMULATE + REAL
        """
        from futu import OpenSecTradeContext, TrdEnv, RET_OK
        
        if self._futu_ctx is None:
            try:
                self._futu_ctx = OpenSecTradeContext(
                    filter_trdmarket=trd_market,
                    host=opend_host,
                    port=opend_port,
                    is_encrypt=False,
                )
                # 始终解锁实盘交易 (即使 trade_env=SIMULATE，也需要查 REAL 持仓)
                import os
                pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
                if pwd_md5:
                    ret, data = self._futu_ctx.unlock_trade(
                        password_md5=pwd_md5, is_unlock=True,
                    )
                    if ret != RET_OK:
                        logger.error(f"[US Sync] 实盘解锁失败: {data}")
                        self._reset_futu_ctx("实盘解锁失败")
                        return None
                    logger.info("[US Sync] ✅ REAL 持仓查询权限已解锁 (只读对账用, SIMULATE+REAL 双环境; 下单环境仍由 AUTO_TRADE_ACCOUNT 决定)")
                else:
                    logger.error("[US Sync] FUTU_UNLOCK_PWD_MD5 未设置，无法查询 REAL 持仓")
                    self._reset_futu_ctx("缺少解锁密码")
                    return None
            except Exception as e:
                logger.warning(f"[US Sync] 创建 Futu 连接失败: {e}")
                return None
        return self._futu_ctx

    def query_broker_positions(self) -> Dict[str, Dict]:
        """
        公共方法: 查询 Broker 持仓 (复用连接，不创建/关闭新连接)
        
        供 _reconcile_position_store 等外部模块调用，避免各自创建 OpenSecTradeContext。
        优先复用 broker_adapter 的 trade_ctx (已解锁)，失败时回退到自建连接。
        
        Returns:
            {symbol: {'qty': float, 'cost': float, 'direction': 'LONG'|'SHORT'}}
        """
        from futu import RET_OK, TrdEnv, TrdMarket, SecurityFirm
        import os
        from app.core.config_container import get_config
        
        # 2026-07-03: trade_env 跟随 AUTO_TRADE_ACCOUNT (与 _envs_to_query 对齐)
        _auto_acc = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        trade_env = TrdEnv.SIMULATE if _auto_acc == "sim" else TrdEnv.REAL
        
        # 优先复用 broker_adapter 的已解锁连接
        ctx = None
        if self._broker_adapter and getattr(self._broker_adapter, 'connected', False):
            ctx = getattr(self._broker_adapter, 'trade_ctx', None)
            if ctx:
                logger.debug("[US Sync] 复用 broker_adapter trade_ctx")
        
        # 回退: 自建连接
        if ctx is None:
            opend_host = os.getenv("FUTU_OPEND_HOST", os.getenv("FUTU_OPEND_HOST", "127.0.0.1"))
            opend_port = int(os.getenv("FUTU_OPEND_PORT", os.getenv("FUTU_OPEND_PORT", "11111")))
            ctx = self._get_futu_context(opend_host, opend_port, trade_env, TrdMarket.US, SecurityFirm.FUTUINC)
            if ctx is None:
                return {}
        
        try:
            ret, data = ctx.position_list_query(trd_env=trade_env, refresh_cache=True)  # 2026-07-16: 强制刷新, 防陈旧缓存幻影 (见 _sync_once 同款修复)
            if ret != RET_OK:
                logger.warning(f"[US Sync] query_broker_positions 查询失败 (ret={ret})，重置连接")
                self._reset_futu_ctx("query失败")
                return {}
            if data is None or data.empty:
                # 空持仓可能是真实的，不重置连接
                return {}
            
            positions = {}
            for _, row in data.iterrows():
                qty = float(row.get('qty', 0))
                if qty != 0:
                    code = str(row['code'])
                    # 2026-06-11: 跳过非美股标的 (如 HK.00700)
                    # 2026-06-30: 跳过 #SIM 后缀 (Futu SIM账户持仓, 不进入pipeline)
                    if not code.startswith('US.') or code.count('.') > 1 or '#' in code:
                        continue
                    side = str(row.get('position_side', ''))
                    is_short = (qty < 0) or (side == 'SHORT')
                    positions[code] = {
                        'qty': abs(qty),
                        'cost': float(row.get('cost_price', 0)),
                        'direction': 'SHORT' if is_short else 'LONG'
                    }
            return positions
        except Exception as e:
            logger.warning(f"[US Sync] query_broker_positions 异常: {e}")
            self._reset_futu_ctx("query异常")
            return {}

    def _query_today_futu_orders(self) -> Dict[str, Dict]:
        """
        查询 Futu 当日已成交委托，用于区分策略开仓 vs 手动开仓。
        
        Returns:
            {symbol: {'order_id': str, 'price': float, 'qty': float, 'direction': str, 'time': str}}
        """
        from futu import RET_OK, TrdEnv
        import os

        # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT (与 _sync_once 的 _envs_to_query 对齐)
        # 根因: 原读 TRADE_ENV (仅控制 DB 路由), 切 real 后查 SIMULATE 订单 →
        # 找不到系统委托 → 仓位被误标 MANUAL → 被退出监控跳过
        _auto_acc = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        trade_env = TrdEnv.SIMULATE if _auto_acc == "sim" else TrdEnv.REAL

        # 复用已有的 _futu_ctx（不新建连接）
        if self._futu_ctx is None:
            return {}
        
        try:
            from app.utils.timezone import now_et as _now_et_sync
            today_str = _now_et_sync().strftime('%Y-%m-%d')
        except Exception:
            today_str = datetime.now().strftime('%Y-%m-%d')
        result = {}
        
        try:
            ret, data = self._futu_ctx.history_order_list_query(
                start=today_str,
                end=today_str,
                trd_env=trade_env,
            )
            if ret != RET_OK or data is None or data.empty:
                return {}
            
            for _, order in data.iterrows():
                code = str(order.get('code', ''))
                # 2026-06-11: 跳过非美股标的
                if not code.startswith('US.') or code.count('.') > 1:
                    continue
                if not code.startswith('US.'):
                    code = 'US.' + code
                dealt_qty = float(order.get('dealt_qty', 0))
                if dealt_qty <= 0:
                    continue  # 未成交的跳过
                
                order_type = str(order.get('order_type', ''))
                trd_side = str(order.get('trd_side', ''))
                # BUY = LONG开仓 or SHORT平仓; SELL = SHORT开仓 or LONG平仓
                # 简化处理：记录所有已成交委托，后续用 direction 匹配
                price = float(order.get('price', 0))
                order_id = str(order.get('order_id', ''))
                create_time = str(order.get('create_time', ''))
                
                # 保留最新的一笔（可能被多次 fill 覆盖）
                if code not in result or create_time > result[code].get('time', ''):
                    # RFC-080: 记录 remark + 系统单判定。外部平仓检测 (~1548) 需要
                    # **全部**委托 (手动/APP 平仓正是其检测对象), 故此处不过滤;
                    # AUTO 认领分类点用 is_system 白名单过滤。
                    _remark = str(order.get('remark', '') or '')
                    result[code] = {
                        'order_id': order_id,
                        'price': price,
                        'qty': dealt_qty,
                        'trd_side': trd_side,
                        'time': create_time,
                        'remark': _remark,
                        'is_system': is_system_order_remark(_remark),
                    }
            
            if result:
                logger.debug(f"[US Sync·P3] Futu 当日委托: {len(result)} 只标的有成交记录")  # 2026-07-28 降噪 INFO→DEBUG (每轮心跳重复)
        except Exception as e:
            logger.debug(f"[US Sync·P3] 查询 Futu 当日委托失败: {e}")
        
        return result

    def _sync_once(self):
        from futu import RET_OK, TrdEnv, TrdMarket, SecurityFirm
        import os

        # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT (与 _envs_to_query 对齐)
        # trade_env 仅用于 _get_futu_context 创建/复用连接, 实际查询用 _envs_to_query
        _auto_acc_sync = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        trade_env = TrdEnv.SIMULATE if _auto_acc_sync == "sim" else TrdEnv.REAL
        opend_host = os.getenv("FUTU_OPEND_HOST", os.getenv("FUTU_OPEND_HOST", "127.0.0.1"))
        opend_port = int(os.getenv("FUTU_OPEND_PORT", os.getenv("FUTU_OPEND_PORT", "11111")))

        # 1. Get Futu positions (复用连接)
        # 三态融合 (2026-06-25): 同时查询 SIMULATE + REAL，合并持仓
        # 根因: 原代码只查一个 trade_env → REAL 账户持仓永远不被同步 → 实盘仓无 SL/无监控
        futu_positions = {}
        ctx = self._get_futu_context(opend_host, opend_port, trade_env, TrdMarket.US, SecurityFirm.FUTUINC)
        if ctx is None:
            return
        try:
            # 2026-07-03: _envs_to_query 跟随 AUTO_TRADE_ACCOUNT
            # sim → 只查 SIMULATE (自动交易在模拟环境)
            # real → 只查 REAL (未来切实盘后, 自动交易在实盘环境)
            _auto_acc = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            _envs_to_query = [TrdEnv.SIMULATE if _auto_acc == "sim" else TrdEnv.REAL]
            _combined_data = None
            _query_failed = False
            for _env in _envs_to_query:
                # 2026-07-16 根因修复: refresh_cache=True 强制服务端刷新。
                #   缺失时读 OpenD 陈旧缓存 → 已平仓位(qty=0)检测不到 →
                #   open_positions 残留幻影 (如 RIVN 平后仍 SHORT 182, 被标 synced) →
                #   退出引擎可能对已平仓发错误回补单(反向开仓)。
                ret, data = ctx.position_list_query(trd_env=_env, refresh_cache=True)
                if ret != 0:
                    logger.warning(f"[US Sync] Futu 持仓查询失败 (env={_env}, ret={ret})")
                    _query_failed = True
                    break
                if data is not None and not data.empty:
                    if _combined_data is None:
                        _combined_data = data
                    else:
                        import pandas as _pd
                        _combined_data = _pd.concat([_combined_data, data], ignore_index=True)

            if _query_failed:
                logger.warning(f"[US Sync] 持仓查询失败，重置连接，跳过本轮")
                self._reset_futu_ctx("position查询失败")
                return

            data = _combined_data
            # P0 修复 (2026-05-11): 空持仓处理 — 使用连续空查询计数器 + 已知持仓缓存
            # 之前问题: 连接瞬断时返回空持仓列表，导致真实持仓被误删为幽灵仓
            if data is None or data.empty:
                self._consecutive_empty_queries += 1
                if self._consecutive_empty_queries < self._max_consecutive_empty:
                    # 使用上次已知的持仓数据继续同步，不跳过
                    if self._last_known_positions:
                        (logger.warning if self._consecutive_empty_queries == 1 else logger.debug)(
                            f"[US Sync] Futu 返回空持仓列表 (连续{self._consecutive_empty_queries}次), "
                            f"使用上次已知持仓({len(self._last_known_positions)}只)继续同步"
                        )
                        futu_positions = dict(self._last_known_positions)
                    else:
                        (logger.warning if self._consecutive_empty_queries == 1 else logger.debug)(
                            f"[US Sync] Futu 返回空持仓列表 (连续{self._consecutive_empty_queries}次), "
                            f"无已知持仓缓存，跳过本轮"
                        )
                        # 2026-07-27 修复 (自咬尾巴死循环): 原此处 _reset_futu_ctx 断连接
                        #   并把计数器清零 → "连续3次确认真空"分支永不可达, 每轮重建连接
                        #   +重新 unlock REAL (07-26 实锤 86 次/日)。查询成功返回空 df
                        #   说明连接健康 — 保留连接与计数器, 3 轮后正常走"确认真实空仓"。
                        #   (2026-05-11 P0 防的是"瞬断误删持仓", 那由 query 失败分支负责。)
                        return
                else:
                    # 连续多次空查询，认为真实空仓
                    if not self._empty_streak_warned:
                        logger.warning(
                            f"[US Sync] Futu 连续{self._max_consecutive_empty}次返回空持仓列表, "
                            f"确认真实空仓，清理已知持仓缓存 (本streak后续静默)"
                        )
                        self._empty_streak_warned = True
                    else:
                        logger.debug(f"[US Sync] 持续空仓 (streak已告警, 静默)")
                    self._last_known_positions = {}
                    self._consecutive_empty_queries = 0
                    # 继续执行: futu_positions 保持空，将清理所有幽灵仓
            else:
                # 非空查询，重置计数器
                self._consecutive_empty_queries = 0
                self._empty_streak_warned = False
                for _, row in data.iterrows():
                    qty = float(row.get('qty', 0))
                    if qty != 0:
                        code = str(row['code'])
                        # 2026-06-11: 跳过非美股标的 (如 HK.00700)
                        # 2026-06-30: 跳过 #SIM 后缀 (Futu SIM账户持仓, 不进入pipeline)
                        if not code.startswith('US.') or code.count('.') > 1 or '#' in code:
                            continue
                        if not code.startswith('US.'):
                            code = 'US.' + code
                        futu_positions[code] = {
                            'qty': abs(qty),
                            'cost': float(row.get('cost_price', 0)),
                            'direction': 'SHORT' if ((qty < 0) or (str(row.get('position_side', '')) == 'SHORT')) else 'LONG'
                        }
                # 更新已知持仓缓存
                self._last_known_positions = dict(futu_positions)
        except Exception as e:
            logger.warning("Failed to get Futu US positions: %s", e)
            # 连接异常时重置，下次同步会重新创建
            self._reset_futu_ctx("position查询异常")
            return

        # B1-lite: 每轮有限扫描未决 Playbook 执行并按 broker 明确终态恢复。
        self._reconcile_playbook_execution_states(
            ctx, _envs_to_query[0], _auto_acc, futu_positions, limit=25)

        # 2. Get DB positions
        if not os.path.exists(_get_db_path()):
            logger.warning("US DB not found: %s", _get_db_path())
            return

        # P0-4 修复 (2026-06-12): timeout=10 + busy_timeout 防止并发写时 "database is locked"
        from app.core.db_utils import safe_sqlite_connect
        conn = safe_sqlite_connect(_get_db_path(), timeout=10)
        cursor = conn.cursor()

        # Ensure trader_type column exists
        try:
            cursor.execute("ALTER TABLE open_positions ADD COLUMN trader_type TEXT DEFAULT 'MANUAL'")
            conn.commit()
            logger.info("[US Sync] Added trader_type column")
        except sqlite3.OperationalError:
            pass  # 列已存在，正常跳过

        # Get current DB positions
        # P0 (2026-06-05): 过滤已关闭记录，避免 ghost 仓每 30s 重复检测刷屏
        cursor.execute(
            "SELECT symbol, size, direction, COALESCE(owner,'auto'), COALESCE(sync_status,'') FROM open_positions "
            "WHERE sync_status IS NULL "
            "OR sync_status NOT IN ("
            "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
            "'closed_emergency','stale_not_in_broker')"
        )
        db_positions = {}
        for row in cursor.fetchall():
            db_positions[row[0]] = {'size': row[1], 'direction': row[2], 'owner': row[3], 'sync_status': row[4]}
        # P3 修复 (2026-06-12): DB 中已有 owner='manual' 的符号，PositionStore 同步时保持 manual
        self._db_manual_symbols = {sym for sym, info in db_positions.items() if info['owner'] == 'manual'}

        futu_symbols = set(futu_positions.keys())
        db_symbols = set(db_positions.keys())

        # 3. Close ghost positions (DB has, Futu doesn't)
        # P0 修复 (2026-05-04): 新创建不足60秒的持仓跳过清理，等待 Futu 确认成交
        # 2026-07-21 时区修复: _now 用 ET-aware, 避免 naive北京 − ET-aware entry_dt 抛 TypeError
        #   被静默吞掉 → 180s 保护窗失效 → 误删刚开、待成交的仓.
        from app.utils.timezone import now_et as _now_et_ghost, to_et as _to_et_ghost
        _now = _now_et_ghost()
        # 2026-08-22 audit: 循环外预载 playbook 未平仓集合 (原逐 symbol 一连接 = N+1)
        from app.playbook.pipeline import load_playbook_open_symbols, playbook_open_check
        _pb_open_syms = load_playbook_open_symbols()
        for symbol in db_symbols - futu_symbols:
            # P0 修复 2026-07-06: Playbook SIM 仓位由 playbook exit monitor 独占管理,
            # position_sync 不应将其标记为 ghost. 2026-07-16: 统一用 is_playbook_open_position.
            if playbook_open_check(symbol, _pb_open_syms):
                logger.info(f"[US Sync] 跳过 Playbook SIM 仓位 ghost 清理: {symbol} (由 playbook exit monitor 管理)")
                continue

            # P0 修复 2026-04-25: 检查是否为 pending_close 标记 (通过 action_log)
            cursor.execute("SELECT action_log_json, entry_time FROM open_positions WHERE symbol=?", (symbol,))
            row = cursor.fetchone()
            is_pending_close = False
            if row and row[0]:
                try:
                    actions = json.loads(row[0])
                    if actions and isinstance(actions, list):
                        last_action = actions[-1].get("action", "")
                        is_pending_close = (last_action == "pending_close")
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

            # P0 修复 (2026-05-04 / 2026-05-12): 新持仓保护窗口 — 给 Futu 180s 确认成交
            # 2026-05-12 延长 60s→180s: 模拟盘 Futu SIMULATE 延迟明显，60s 不足
            if row and row[1]:
                try:
                    entry_dt = _to_et_ghost(datetime.fromisoformat(str(row[1])))
                    age_seconds = (_now - entry_dt).total_seconds()
                    if age_seconds < 180 and not is_pending_close:
                        logger.info(
                            "[US Sync] 跳过新持仓 %s (创建 %.0fs 前, 等待 Futu 确认成交)",
                            symbol, age_seconds,
                        )
                        continue
                except (ValueError, TypeError):
                    pass

            if is_pending_close:
                logger.info(f"[US Sync] ✅ 确认平仓 {symbol} (Futu 已无，pending_close 完成)")
            else:
                logger.warning("[US Sync] Ghost position: %s (DB only, closing)", symbol)
            # 2026-07-14: 直接DELETE, 不再软删除 (trade_records已有完整审计记录)
            cursor.execute(
                "DELETE FROM open_positions WHERE symbol=?",
                (symbol,)
            )
            # 2026-06-25: 清理 ghost 对应的幻影 trade_records (订单 submit 时写入但未成交)
            cursor.execute(
                "DELETE FROM trade_records WHERE symbol=? AND (exit_price=0 OR exit_price IS NULL) AND source_type='MANUAL'",
                (symbol,)
            )

        # 3b. 孤儿 trade_records 清理 (2026-07-16 源头规避)
        # 根因: MANUAL 单写 trade_records(exit_time=NULL) 但从未进 open_positions
        #       (如 PDD id=1039 100股) → ghost 清理循环只遍历 open_positions, 够不到它
        #       → 账本永久残留幻影未平仓. 此扫描补齐这一缺口.
        # 保护条件: 非 broker 持仓 + 非 open_positions + 非 playbook 管理 + 建仓>1天
        #   (1天保护窗避免抢跑成交, 且本次 sync 只查单一 trade env, 防止误伤另一环境真实仓).
        try:
            from app.playbook.pipeline import is_playbook_open_position as _is_pb_open
            cursor.execute(
                "SELECT id, symbol, entry_price, entry_time, quantity, source_type "
                "FROM trade_records WHERE exit_time IS NULL OR exit_time=''"
            )
            _orphan_rows = cursor.fetchall()
            _orphan_cleaned = 0
            _orphan_pending = 0
            for _orow in _orphan_rows:
                _oid, _osym = _orow[0], _orow[1]
                _oentry, _oetime = _orow[2], _orow[3]
                # (a)(b) broker 或 open_positions 持有 → 跳过
                if _osym in futu_symbols or _osym in db_symbols:
                    continue
                # (c) playbook 管理仓 → 跳过 (由 exit monitor 负责)
                if _is_pb_open(_osym):
                    continue
                # (d) 建仓时间保护窗: 1 天内不清 (防抢跑成交/单环境查询误判)
                if _oetime:
                    try:
                        # 2026-08-07 修复 (主动审计 P1-1): 原实现 strip tzinfo 成 naive 后与
                        #   _now(ET-aware) 相减必抛 TypeError → 被下层 except 吞 → 保护窗
                        #   永远走不到清理分支, 孤儿 trade_records 永久残留。
                        #   修法: 照抄 ghost 循环 L733 的 to_et 统一转 aware (naive 视为北京
                        #   时间, 与 _to_et_ghost 同口径), aware-aware 相减正常。
                        _oentry_dt = _to_et_ghost(
                            datetime.fromisoformat(str(_oetime).replace('Z', '+00:00'))
                        )
                        if (_now - _oentry_dt).total_seconds() < 86400:
                            continue
                    except (ValueError, TypeError):
                        # 时间解析失败 → 保守跳过, 不清
                        continue
                else:
                    # 无 entry_time → 无法判定保护窗 → 保守跳过
                    continue
                _oexit = datetime.now().isoformat()
                # ── R-2 (2026-08-23 DEB-20260823): 默认不再自动伪造平仓 ──
                # 原实现直接 UPDATE exit_price=entry_price, realized_pnl=0。三个前置判据
                # (broker 无 / open_positions 无 / 非 playbook 仓) 全是单点判断, 且 broker
                # 侧只查当前单一 env (:577 _envs_to_query 单元素)。任一误判后果不可逆:
                # 券商真实持仓被写上 exit_time → monitor_playbook_sim_exits 的
                # WHERE exit_time IS NULL 永久查不到 → 裸仓无止损、不自愈; 同时向毕业统计
                # 注入伪造的 pnl=0 样本, 污染终裁口径。
                # 现默认只告警 + 落盘待人工 adopt。回滚: reconcile.orphan_autoclose_enabled=true
                try:
                    from app.core.config_container import get_config as _get_cfg_orph
                    _orph_autoclose = bool(
                        _get_cfg_orph().get_dotted("reconcile.orphan_autoclose_enabled", False)
                    )
                except Exception:
                    _orph_autoclose = False

                if not _orph_autoclose:
                    _orphan_pending += 1
                    logger.error(
                        f"🚨 [US Sync] 疑似孤儿 trade_records (未自动平仓, 待人工确认): "
                        f"id={_oid} {_osym} entry={_oentry} entry_time={_oetime} "
                        f"— broker/open_positions 均无且建仓>1天。若券商实有此仓则为误判, "
                        f"自动平仓会使该仓永久失去止损监控, 故仅告警。"
                    )
                    try:
                        import json as _oj
                        _oa_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'audit')
                        os.makedirs(_oa_dir, exist_ok=True)
                        with open(os.path.join(_oa_dir, 'orphan_trade_records.jsonl'),
                                  'a', encoding='utf-8') as _of:
                            _of.write(_oj.dumps({
                                "detected_at": _oexit, "trade_record_id": _oid,
                                "symbol": _osym, "entry_price": _oentry,
                                "entry_time": _oetime,
                                "action": "ALERT_ONLY_PENDING_MANUAL_ADOPT",
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    continue

                cursor.execute(
                    "UPDATE trade_records SET exit_time=?, exit_price=entry_price, "
                    "realized_pnl=0, pnl_pct=0, holding_hours=0, exit_reason=? "
                    "WHERE id=? AND (exit_time IS NULL OR exit_time='')",
                    (_oexit, 'RECONCILE_ORPHAN_CLOSE', _oid)
                )
                if cursor.rowcount:
                    _orphan_cleaned += 1
                    logger.warning(
                        f"[US Sync] 孤儿 trade_records 清理: id={_oid} {_osym} "
                        f"(broker/open_positions 均无, 建仓>1天) → 标记已平 pnl=0"
                    )
            if _orphan_cleaned:
                logger.warning(f"[US Sync] 孤儿 trade_records 清理完成: {_orphan_cleaned} 笔")
            if _orphan_pending:
                logger.error(
                    f"🚨 [US Sync] 疑似孤儿 trade_records 待人工处理: {_orphan_pending} 笔 "
                    f"(详见 data/audit/orphan_trade_records.jsonl)"
                )
        except Exception as _orphan_err:
            logger.warning(f"[US Sync] 孤儿 trade_records 扫描异常 (已隔离, 不影响 sync): {_orphan_err}")

        # 4. Update/Insert Futu positions
        # P0 fix (2026-05-21): Load PRICE_FAIL-closed set to skip re-insert/update
        _pf_closed = set()
        try:
            import json as _pfj
            _pf_p = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
            if os.path.exists(_pf_p):
                _pf_closed = set(_pfj.load(open(_pf_p)))
        except Exception:
            pass

        # P3 修复 (2026-06-09): 查询 Futu 当日委托 (DB INSERT 和 position_store 共用)
        _today_orders = self._query_today_futu_orders()
        self._today_orders = _today_orders  # 供 _sync_to_position_store_locked 使用

        # P3 修复 (2026-06-12): 查询 manual_order_audit 今日手动订单，防止被误标为 auto_main
        _manual_today = set()
        try:
            from app.utils.timezone import get_trade_date
            today_et = get_trade_date()
            cursor.execute(
                "SELECT DISTINCT symbol FROM manual_order_audit "
                "WHERE status='submitted' AND date(timestamp) = ?",
                (today_et,)
            )
            _manual_today = {row[0] for row in cursor.fetchall()}
            if _manual_today:
                _mt_fp = str(sorted(_manual_today))
                if _mt_fp != getattr(self, "_last_manual_fp", None):  # 2026-07-28 降噪: 变化才 INFO
                    self._last_manual_fp = _mt_fp
                    logger.info(f"[US Sync·P3] 今日手动订单标的: {_manual_today}")
                else:
                    logger.debug(f"[US Sync·P3] 今日手动订单标的: {_manual_today}")
            self._manual_today = _manual_today
        except Exception as e:
            logger.debug(f"[US Sync·P3] manual_order_audit 查询失败: {e}")
            self._manual_today = set()

        # 实盘隔离: 自管持仓不纳入系统管理 (yaml + DB owner='manual' 合并)
        try:
            from app.core.config_container import get_all_manual_symbols
            _self_managed = get_all_manual_symbols()
        except Exception:
            _self_managed = set()

        for symbol, info in futu_positions.items():
            # 2026-06-11: 跳过非美股标的 (防御性检查)
            if not symbol.startswith('US.') or symbol.count('.') > 1:
                continue
            # P0 fix: Skip PRICE_FAIL-closed options entirely (no DB update or insert)
            if symbol in _pf_closed:
                # Also clean from open_positions if present (prevents _restore_position_from_db)
                if symbol in db_positions:
                    cursor.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
                    logger.warning(f"[US Sync·P0] 清理 PRICE_FAIL 已关闭期权(DB): {symbol}")
                continue

            # 实盘隔离: 跳过自管持仓，不纳入系统管理 (保留 DB 记录，owner='manual')
            if _self_managed and symbol in _self_managed:
                if symbol in db_positions:
                    db_info = db_positions[symbol]
                    db_owner = db_info.get('owner', 'auto')
                    # 更新 owner + 修正 stub 行的 direction/size/sync_status
                    needs_owner_fix = db_owner != 'manual'
                    needs_data_fix = (db_info.get('sync_status') == 'pending_sync' or
                                      abs(db_info['size'] - info['qty']) > 0.1 or
                                      db_info['direction'] != info['direction'])
                    if needs_owner_fix or needs_data_fix:
                        cursor.execute(
                            "UPDATE open_positions SET owner='manual', direction=?, size=?, "
                            "avg_entry_price=?, sync_status='synced', updated_at=? WHERE symbol=?",
                            (info['direction'], info['qty'], info['cost'],
                             datetime.now().isoformat(), symbol))
                        if needs_owner_fix:
                            logger.info(f"[US Sync·隔离] {symbol} owner→manual + 数据修正")
                else:
                    # 首次发现自管持仓，写入 DB 标 manual
                    # P0-4 修复 (2026-06-12): UPSERT 防止与 manual_order_service stub 行 UNIQUE 冲突
                    cursor.execute(
                        "INSERT INTO open_positions (symbol, direction, size, avg_entry_price, owner, sync_status, updated_at, account_type) "
                        "VALUES (?, ?, ?, ?, 'manual', 'synced', ?, 'real') "
                        "ON CONFLICT(symbol) DO UPDATE SET "
                        "direction=excluded.direction, size=excluded.size, "
                        "avg_entry_price=excluded.avg_entry_price, owner='manual', "
                        "sync_status='synced', updated_at=excluded.updated_at, account_type='real'",
                        (symbol, info['direction'], info['qty'], info['cost'], datetime.now().isoformat()))
                    logger.info(f"[US Sync·隔离] 录入自管持仓: {symbol} ({info['direction']} {info['qty']}股)")
                continue

            if symbol in db_positions:
                # Update existing
                db_info = db_positions[symbol]
                size_changed = abs(db_info['size'] - info['qty']) > 0.1
                # P0 修复 (2026-06-03): 方向翻转检测 — 原代码仅检查 size 变化
                # 根因: XLF 从 SHORT -11202 翻转为 LONG +11202 时 abs size 不变，
                # UPDATE 被跳过 → DB 方向过期 → 平仓单变成加仓单
                direction_changed = db_info.get('direction', '') != info['direction']
                if size_changed or direction_changed:
                    if direction_changed:
                        logger.warning(
                            f"[US Sync·方向翻转] {symbol} {db_info.get('direction','?')}→{info['direction']} "
                            f"(size={info['qty']}, cost=${info['cost']:.2f})"
                        )
                    else:
                        logger.info("[US Sync] Updating %s: size %s -> %s", symbol, db_info['size'], info['qty'])
                    cursor.execute(
                        "UPDATE open_positions SET size=?, direction=?, avg_entry_price=?, updated_at=?, sync_status='synced' WHERE symbol=?",
                        (info['qty'], info['direction'], info['cost'], datetime.now().isoformat(), symbol)
                    )
            else:
                # P2 修复 (2026-04-30): 插入新持仓时验证仓位占比 + 使用 ATR 动态止损
                # ponytail (2026-06-25): Playbook SIM 仓位跳过 25% cap — 由 playbook 自管 sizing
                _is_pb_sim = False
                try:
                    from app.core.scheduler_triggers import ET as _et
                    _et_yesterday = (datetime.now(_et).date() - timedelta(days=1)).isoformat()
                    _pb_row = cursor.execute(
                        "SELECT 1 FROM trade_records WHERE symbol=? AND source_type='PLAYBOOK_SIM' "
                        "AND entry_time >= ? AND exit_time IS NULL LIMIT 1",
                        (symbol, _et_yesterday)).fetchone()
                    _is_pb_sim = _pb_row is not None
                except Exception:
                    pass
                if _is_pb_sim:
                    logger.debug(f"[US Sync·P0] 跳过 Playbook SIM 仓位 {symbol} 25% cap (由 playbook sizing 管理)")
                    cursor.execute(
                        """INSERT INTO open_positions (
                            symbol, direction, size, avg_entry_price,
                            initial_sl, current_sl, initial_tp,
                            entry_time, updated_at, strategy_id, trader_type, owner_strategy,
                            owner, sync_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced')
                        ON CONFLICT(symbol) DO UPDATE SET
                            direction=excluded.direction, size=excluded.size,
                            avg_entry_price=excluded.avg_entry_price, updated_at=excluded.updated_at,
                            sync_status='synced'""",
                        (symbol, info['direction'], info['qty'], info['cost'],
                         0, 0, 0, datetime.now().isoformat(), datetime.now().isoformat(),
                         'playbook_sim', 'AUTO', 'playbook_sim', 'auto'))
                    continue
                # 1. 仓位占比验证
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    capital = get_capital_safety_system()
                    total_equity = capital._state.current_equity if capital._state.current_equity > 0 else 100000
                    position_value = info['qty'] * info['cost']
                    position_pct = position_value / total_equity
                    
                    # 获取配置限制
                    try:
                        from app.core.config_container import get_config
                        cfg = get_config()
                        # [DEB-fullchain #19] 回退值与 yaml SSOT 0.08 同步 (原 0.25)
                        max_per_symbol = float(cfg.get_dotted("risk.position.max_per_symbol", 0.08))
                    except Exception:
                        max_per_symbol = 0.08
                    
                    if position_pct > max_per_symbol:
                        # 根因修复 (2026-06-05): 超限仓位触发自动减仓
                        # 计算需要平掉的股数
                        max_value = total_equity * max_per_symbol
                        excess_value = position_value - max_value
                        excess_shares = int(excess_value / info['cost'])
                        
                        if excess_shares > 0:
                            logger.warning(
                                f"🚫 [US Sync] {symbol} 占比 {position_pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"触发自动减仓: {excess_shares}股 (${excess_value:,.0f}) | "
                                f"当前 {info['qty']}股(${position_value:,.0f}) → 目标 {info['qty']-excess_shares}股(${max_value:,.0f})"
                            )
                            # 标记需要减仓，在 sync 循环结束后统一处理
                            if not hasattr(self, '_pending_trims'):
                                self._pending_trims = {}
                            self._pending_trims[symbol] = {
                                'shares': excess_shares,
                                'direction': info['direction'],
                                'reason': f'sync_over_limit_{max_per_symbol:.0%}',
                            }
                        else:
                            logger.warning(
                                f"⚠️ [US Sync] {symbol} 占比 {position_pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"但计算减仓股数为 0 (市值 ${position_value:,.0f} / 净值 ${total_equity:,.0f})"
                            )
                except Exception as e:
                    logger.debug(f"[US Sync] 仓位验证失败 (不影响同步): {e}")
                
                # 2. ATR 动态止损 (根因修复 2026-06-05)
                # SL 与 exit_policy.yaml atr_stop.multiplier=4.0 对齐
                # 1分钟 ATR 值小, 4× 给出合理日内止损距离 (ETF≈4%, 个股≈3-5%)
                # TP 用百分比而非 ATR×N (ATR×4 对 ETF 是幻影目标)
                try:
                    atr = self._get_symbol_atr(symbol, info['cost'])
                    atr_mult = self._get_atr_stop_mult()  # SSOT: exit_policy.yaml
                    # ETF vs 个股: 不同 TP 目标
                    from app.risk.position_limits import ETF_EXPOSURE_DISCOUNT
                    _is_etf = symbol in ETF_EXPOSURE_DISCOUNT
                    _tp_pct = 0.03 if _is_etf else 0.05  # ETF 3%, 个股 5%
                    if info['direction'] == 'LONG':
                        initial_sl = info['cost'] - atr * atr_mult
                        initial_tp = info['cost'] * (1 + _tp_pct)
                    else:
                        initial_sl = info['cost'] + atr * atr_mult
                        initial_tp = info['cost'] * (1 - _tp_pct)
                    
                    # 硬底限保护: 与 exit_policy.yaml max_loss_pct=15% 对齐
                    _max_loss_pct = 0.15  # exit_policy global max_loss_pct
                    try:
                        from app.risk.exit_policy_guardian import ExitPolicyGuardian as _EPG
                        _pol = _EPG()._load_exit_policy_dict()
                        _ml = float(_pol.get("exit_rules", {}).get(
                            "atr_stop", {}).get("max_loss_pct", 15.0))
                        _max_loss_pct = _ml / 100.0
                    except Exception:
                        pass
                    hard_stop = info['cost'] * (1 - _max_loss_pct) if info['direction'] == 'LONG' else info['cost'] * (1 + _max_loss_pct)
                    if info['direction'] == 'LONG':
                        initial_sl = max(initial_sl, hard_stop)
                    else:
                        initial_sl = min(initial_sl, hard_stop)
                    
                    _sl_pct = abs(initial_sl - info['cost']) / info['cost'] * 100
                    _tp_pct_actual = abs(initial_tp - info['cost']) / info['cost'] * 100
                    logger.info(
                        f"[US Sync] {symbol} SL=${initial_sl:.2f} ({_sl_pct:.1f}%) "
                        f"TP=${initial_tp:.2f} ({_tp_pct_actual:.1f}%) "
                        f"ATR=${atr:.2f}×{atr_mult:.0f} hard_stop=${hard_stop:.2f}"
                    )
                except Exception as e:
                    # Fallback: 固定百分比 (与 atr_stop 4×ATR 等效的距离)
                    logger.debug(f"[US Sync] {symbol} ATR 获取失败，使用固定止损: {e}")
                    atr = 0
                    _fb_sl = 0.06  # 6% 止损 (约等于 4× 日均 ATR)
                    _fb_tp = 0.05  # 5% 止盈 (ETF/个股通用)
                    if info['direction'] == 'LONG':
                        initial_sl = info['cost'] * (1 - _fb_sl)
                        initial_tp = info['cost'] * (1 + _fb_tp)
                    else:
                        initial_sl = info['cost'] * (1 + _fb_sl)
                        initial_tp = info['cost'] * (1 - _fb_tp)

                logger.info("[US Sync] New position: %s %s %s @ %s", symbol, info['direction'], info['qty'], info['cost'])
                # 2026-07-02: 安全默认MANUAL — 无法证明是系统开的就标manual
                # 根因: 原else=AUTO, 人工在App开的仓会被误标auto → 被自动平仓
                # 修复: 只有当日系统委托(_today_orders)有记录才标AUTO, 否则MANUAL
                # open_positions已是SSOT (playbook下单时也写open_positions), DB无记录=人工
                _db_already_manual = symbol in getattr(self, '_db_manual_symbols', set())
                _today_ord = _today_orders.get(symbol)
                # RFC-080: AUTO 认领必须凭系统委托 remark 白名单 — 否则切实盘后
                # APP 手动单 (无 remark) 被误认领 AUTO → 被自动退出监控平掉真钱仓。
                _is_system_order = bool(_today_ord and _today_ord.get('is_system'))
                if _db_already_manual:
                    _db_trader_type = "MANUAL"
                elif symbol in _manual_today:
                    _db_trader_type = "MANUAL"
                elif _is_system_order:
                    _db_trader_type = "AUTO"
                else:
                    _db_trader_type = "MANUAL"  # was AUTO (2026-07-02: 安全默认)
                _db_strategy_id = "auto_main" if _db_trader_type == "AUTO" else "futu_sync"
                # P0-4 修复 (2026-06-12): UPSERT — 防止与 stub 行/隔离自管行 UNIQUE 冲突
                # 根因: 之前裸 INSERT 触发 "UNIQUE constraint failed: open_positions.symbol" 持续刷屏
                cursor.execute("""
                    INSERT INTO open_positions (
                        symbol, direction, size, avg_entry_price,
                        initial_sl, current_sl, initial_tp,
                        entry_time, updated_at, strategy_id, trader_type, owner_strategy,
                        owner
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        direction=excluded.direction,
                        size=excluded.size,
                        avg_entry_price=excluded.avg_entry_price,
                        initial_sl=COALESCE(open_positions.initial_sl, excluded.initial_sl),
                        current_sl=COALESCE(open_positions.current_sl, excluded.current_sl),
                        initial_tp=COALESCE(open_positions.initial_tp, excluded.initial_tp),
                        updated_at=excluded.updated_at,
                        strategy_id=COALESCE(open_positions.strategy_id, excluded.strategy_id),
                        trader_type=COALESCE(open_positions.trader_type, excluded.trader_type),
                        owner_strategy=COALESCE(open_positions.owner_strategy, excluded.owner_strategy),
                        sync_status='synced',
                        owner=CASE
                            WHEN open_positions.owner='manual' THEN 'manual'
                            ELSE excluded.owner
                        END
                """, (
                    symbol, info['direction'], info['qty'], info['cost'],
                    initial_sl, initial_sl, initial_tp,
                    datetime.now().isoformat(), datetime.now().isoformat(),
                    _db_strategy_id, _db_trader_type, _db_strategy_id,
                    'manual' if _db_trader_type == 'MANUAL' else 'auto'
                ))

        conn.commit()
        conn.close()

        # ═══ 根因修复 (2026-06-05): 执行超限减仓 + 扫描所有持仓 ═══
        self._execute_pending_trims_and_check_all_oversized()
        # P0 修复 2026-04-25: 同步到 PositionStore 内存
        if self.position_store:
            self._sync_to_position_store(futu_positions)

        self._last_sync_time = datetime.now()
        _db_count = self.position_store.count() if self.position_store else len(futu_positions)
        # playbook 自管仓按设计不纳入 PositionStore (line ~1099 防旧管线劫持),
        # 故 position_store.count() 只含主线内存仓。单独统计 playbook 开仓数,
        # 避免 "Futu:N | DB:M" 因 playbook 仓存在而恒显不一致 (误报噪音)。
        _pb_count = 0
        try:
            _pb_conn = safe_sqlite_connect(_get_db_path(), timeout=5)
            _pb_row = _pb_conn.execute(
                "SELECT COUNT(*) FROM trade_records "
                "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)"
            ).fetchone()
            _pb_count = int(_pb_row[0]) if _pb_row else 0
            _pb_conn.close()
        except Exception:
            _pb_count = -1  # 取数失败标记, 不阻断
        _futu_n = len(futu_positions)
        # 2026-07-28 降噪: 每轮心跳 (盘中 30s) 无变化仍打 INFO → 日志 12% 是同一句。
        #   状态指纹变化或不一致才 INFO, 否则 DEBUG (可观测性不降: 异常必现)。
        if _pb_count >= 0:
            _consistent = "✓" if _futu_n == _db_count + _pb_count else "⚠差异"
            _sync_fp = (_futu_n, _db_count, _pb_count, _consistent)
            _log_fn = logger.info if (_sync_fp != getattr(self, "_last_sync_fp", None)
                                      or _consistent != "✓") else logger.debug
            self._last_sync_fp = _sync_fp
            _log_fn(
                "[US Sync] Done | Futu: %d | 主线DB: %d | playbook自管: %d | 一致性: %s",
                _futu_n, _db_count, _pb_count, _consistent,
            )
        else:
            logger.info("[US Sync] Done | Futu: %d | 主线DB: %d | playbook自管: n/a", _futu_n, _db_count)

        # P2 新增 (2026-04-29): 一致性校验 + 资本安全同步
        self._verify_and_sync_capital(futu_positions)

    def _sync_to_position_store(self, futu_positions: Dict):
        """P0 修复 2026-04-25: 将 Futu Broker 持仓同步到 PositionStore 内存
        P0-4a 修复 2026-05-03: 整个 all() → diff → add/remove 序列在 position_store._lock 内原子执行
        防止交易循环在 snapshot 和 mutation 之间插入新持仓

        这是消除「手动操作无止损保护」的关键修复。
        场景:
        - 用户在 Futu App 手动开仓 → 30s 后此方法发现新持仓 → 写入 PositionStore → ExitMonitor 开始监控
        - 用户在 Futu App 手动平仓 → 30s 后此方法发现持仓消失 → 从 PositionStore 移除 → 停止监控幽灵仓
        """
        from app.core.models import Position, Direction, MarketRegime

        # P0-4a 修复: 整个序列原子执行
        with self.position_store._lock:
            self._sync_to_position_store_locked(futu_positions)

    def _sync_to_position_store_locked(self, futu_positions: Dict):
        """在 position_store._lock 内执行的同步逻辑 (P0-4a)"""
        from app.core.models import Position, Direction, MarketRegime

        # 实盘隔离: 自管持仓列表 (yaml + DB owner='manual' 合并)
        try:
            from app.core.config_container import get_all_manual_symbols
            _self_managed = get_all_manual_symbols()
        except Exception:
            _self_managed = set()

        mem_positions = list(self.position_store._positions.values())  # 已持有锁，直接读取
        mem_symbols = set(p.symbol for p in mem_positions)
        futu_symbols = set(futu_positions.keys())

        # P1 修复 (2026-06-07): 查 DB 已有仓位，区分"重启恢复"和"真正新发现"
        # 重启恢复: DB 有 synced 记录 → 只加载到内存，不发通知
        # 真正新发现: DB 没有 → 发通知
        _db_known_symbols = set()
        try:
            conn = safe_sqlite_connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute(
                "SELECT symbol FROM open_positions "
                "WHERE sync_status IS NULL OR sync_status NOT IN ("
                "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                "'closed_emergency','stale_not_in_broker')"
            )
            _db_known_symbols = set(row[0] for row in cursor.fetchall())
            conn.close()
        except Exception:
            pass

        # Broker-first (2026-06-03): 不再预读 DB，所有仓位以 Futu 为准

        # 2026-08-22 audit: 循环外预载 playbook 未平仓集合 (原逐 symbol 一连接 = N+1)
        from app.playbook.pipeline import load_playbook_open_symbols, playbook_open_check
        _pb_open_syms_new = load_playbook_open_symbols()

        # 1. 新增持仓 (Futu 有，内存没有) → 手动开仓检测
        # P0 fix (2026-05-21): Skip re-importing options that were force-closed via PRICE_FAIL.
        # These positions still exist in Futu (user opened manually, SELL may have failed),
        # but re-importing them triggers the infinite PRICE_FAIL re-import loop.
        _price_fail_closed = set()
        try:
            import json as _pf_json
            _pf_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
            if os.path.exists(_pf_path):
                _price_fail_closed = set(_pf_json.load(open(_pf_path)))
        except Exception:
            pass
        new_symbols = futu_symbols - mem_symbols
        
        # _today_orders 已在上方查询 (DB INSERT 共用)
        _today_orders = getattr(self, '_today_orders', {})
        
        for symbol in new_symbols:
            # P0 fix: Skip PRICE_FAIL-closed options
            if symbol in _price_fail_closed:
                logger.warning(f"[US Sync·P0] 跳过 PRICE_FAIL 已关闭期权 {symbol} (防止重导入循环)")
                continue
            # 实盘隔离: 跳过自管持仓 (startup reconcile 也需检查)
            if _self_managed and symbol in _self_managed:
                # 2026-08-05: 自管标的永远停留在 "Futu有/内存无" → 每 30s 轮必刷 INFO.
                #   首次 INFO 知会, 后续降 debug (噪音淹没真信号, 今日 BABA 实例).
                if symbol not in _SELF_MANAGED_SKIP_LOGGED:
                    _SELF_MANAGED_SKIP_LOGGED.add(symbol)
                    logger.info(f"[US Sync·隔离] 跳过自管持仓(startup): {symbol}")
                else:
                    logger.debug(f"[US Sync·隔离] 跳过自管持仓(startup): {symbol}")
                continue
            # H6 (2026-06-03): tombstone 检查 — 防止刚平仓的 position 被重新创建
            if self._is_tombstoned(symbol):
                logger.warning(f"[US Sync·H6] 跳过 tombstoned {symbol} (刚平仓, 防止重入)")
                continue
            # 2026-07-09: Playbook 仓位统一跳过 — 防止跨日重启后 position_sync 劫持
            # 2026-07-16: 统一用 is_playbook_open_position 替代时间窗口检查 (旧逻辑用
            #   2-day/1-day 窗口导致跨日/跨窗口漏判, position_sync 标记 auto_main →
            #   旧管线 exit_monitor 接管 → 双重平仓, 7/15 RIVN/NVDA/XOM 各亏 $37-52).
            if playbook_open_check(symbol, _pb_open_syms_new):
                logger.debug(f"[US Sync·playbook] 跳过 playbook 仓位 {symbol} (由 monitor_playbook_sim_exits 自管)")
                continue
            # Broker-first (2026-06-03): 不再从 DB 恢复，新发现的仓位统一用全新计算的 SL/TP

            info = futu_positions[symbol]
            direction = Direction.SHORT if info['direction'] == 'SHORT' else Direction.LONG
            cost = info['cost']
            size = info['qty']
            
            # P2 修复 (2026-04-30): 使用 ATR 动态止损替代固定百分比
            try:
                atr = self._get_symbol_atr(symbol, cost)
                atr_mult = self._get_atr_stop_mult()  # P1: SSOT 替代硬编码 2.5
                if info['direction'] == 'LONG':
                    sl_price = cost - atr * atr_mult
                    tp_price = cost + atr * atr_mult * 2.0
                else:
                    sl_price = cost + atr * atr_mult
                    tp_price = cost - atr * atr_mult * 2.0
                
                # 硬底限保护: 读 exit_policy.yaml SSOT (与 line 841-851 对齐)
                try:
                    from app.risk.exit_policy_guardian import ExitPolicyGuardian
                    _ep = ExitPolicyGuardian()
                    _policy = _ep._load_exit_policy_dict()
                    _ml = float(_policy.get("exit_rules", {}).get(
                        "atr_stop", {}).get("max_loss_pct", 2.0))
                    _max_loss_pct = _ml / 100.0
                except Exception:
                    _max_loss_pct = 0.02  # fallback 2%
                hard_stop = cost * (1 - _max_loss_pct) if info['direction'] == 'LONG' else cost * (1 + _max_loss_pct)
                if info['direction'] == 'LONG':
                    sl_price = max(sl_price, hard_stop)
                else:
                    sl_price = min(sl_price, hard_stop)
                    
            except Exception as e:
                logger.debug(f"[US Sync] {symbol} ATR 获取失败，使用固定止损: {e}")
                sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                tp_mult = 0.85 if info['direction'] == 'SHORT' else 1.15
                sl_price = cost * sl_mult
                tp_price = cost * tp_mult

            # Broker-first (2026-06-03): SL 方向校验 — 方向错误直接重算
            if info['direction'] == 'SHORT' and sl_price <= cost:
                logger.error(f"[SL校验·修复] {symbol} SHORT SL=${sl_price:.2f} < entry=${cost:.2f}, 重算")
                sl_price = cost * 1.08  # fallback 8%
            elif info['direction'] == 'LONG' and sl_price >= cost:
                logger.error(f"[SL校验·修复] {symbol} LONG SL=${sl_price:.2f} > entry=${cost:.2f}, 重算")
                sl_price = cost * 0.92  # fallback 8%

            # P3 修复 (2026-06-12): 三层手动判断优先级: DB已有manual > 今日手动审计 > 富途委托
            _is_db_manual = symbol in getattr(self, '_db_manual_symbols', set())
            _is_manual_today = symbol in getattr(self, '_manual_today', set())
            # RFC-080: 认领 AUTO 必须凭系统委托 remark 白名单 (is_system) — 无 remark 的
            # APP/看板手动单落入 else → manual_sync, 不被自动退出监控接管。
            _today_ord_ps = _today_orders.get(symbol)
            _has_today_order = bool(_today_ord_ps and _today_ord_ps.get('is_system')) \
                and not _is_manual_today and not _is_db_manual
            if _is_db_manual:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"
                logger.info(
                    f"[US Sync·P3] {symbol} DB已有owner=manual，保持手动管理"
                )
            elif _is_manual_today:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"
                logger.info(
                    f"[US Sync·P3] {symbol} 今日有手动订单，标记为 manual_sync"
                )
            elif _has_today_order:
                # 2026-07-16: 统一用 is_playbook_open_position 跳过 Playbook 仓位.
                # 旧逻辑用 1-day 时间窗口查 PLAYBOOK_SIM 记录, 跨日/跨窗口漏判后
                # position_sync 标记 auto_main → 旧管线 exit_monitor 接管 → 双重平仓.
                from app.playbook.pipeline import is_playbook_open_position
                if is_playbook_open_position(symbol):
                    logger.debug(f"[US Sync·P0] 跳过 Playbook SIM 仓位 {symbol} (由 playbook exit monitor 独占管理)")
                    continue
                # 2026-07-20 统一管线: 主线入场已永久冻结 (order_executor.py:3120 仅放行
                #   PLAYBOOK/ORB), ORB 已禁用 → 当日非手动 broker 单只可能来自 playbook.
                #   playbook trade_record 未落库时 (SUBMITTED/ghost) is_playbook_open_position
                #   漏判, 旧逻辑标 auto_main → 主 ExitMonitor 接管 → 双重管理+ATR=0. 改归 playbook_sim.
                strategy_id = "playbook_sim"
                trader_type = "AUTO"
                _order_info = _today_orders[symbol]
                logger.info(
                    f"[US Sync·P3] {symbol} 匹配到当日委托 (order_id={_order_info['order_id']}), "
                    f"标记为 playbook_sim (统一管线, 非手动)"
                )
            else:
                strategy_id = "manual_sync"
                trader_type = "MANUAL"

            position = Position(
                strategy_id=strategy_id,
                symbol=symbol,
                direction=direction,
                size=size,
                avg_entry_price=cost,
                initial_sl=sl_price,
                current_sl=sl_price,
                initial_tp=tp_price,
                take_profit_levels=[],
                time_stop_hours=72,
                market_regime=MarketRegime.RANGE,
            )
            position.owner_strategy = strategy_id
            position.trail_state = {
                "highest_price": cost,
                "lowest_price": cost,
                "prev_scan_price": cost,
            }
            # Broker-first (2026-06-03): 如实标记来源，不再伪装 broker_sync
            # P1 归因修复 (2026-06-06): 补充 trace_id + entry_reason，防止 metadata 全空
            import uuid as _sync_uuid
            _sync_trace = f"sync-{symbol}-{int(time.time())}-{_sync_uuid.uuid4().hex[:4]}"
            if _has_today_order:
                # P3: 策略开仓恢复 — 2026-07-20 统一管线, 归 playbook_sim (非 auto_main)
                _order_info = _today_orders[symbol]
                position.metadata = {
                    "source": "playbook_sim",
                    "origin": "sync_recovery",
                    "sync_origin": "position_sync",
                    "trace_id": _sync_trace,
                    "entry_reason": "sync_recovery_matched_order",
                    "strategy_id": strategy_id,
                    "futu_order_id": _order_info.get('order_id', ''),
                    "futu_order_time": _order_info.get('time', ''),
                }
            elif _is_db_manual or _is_manual_today:
                # Task #10 (2026-06-15): DB/审计已确认是手动开仓 →
                # metadata.source 必须是 'manual', 让 PositionSource.MANUAL 生效,
                # ManualHoldRegistry.sync_from_positions 启动恢复才会注册.
                position.metadata = {
                    "source": "manual",
                    "origin": "manual_recovery" if _is_db_manual else "manual_today",
                    "sync_origin": "position_sync",
                    "trace_id": _sync_trace,
                    "entry_reason": "manual_open",
                    "strategy_id": strategy_id,
                    "protection": "protected",
                }
            else:
                position.metadata = {
                    "source": "futu_discovery",
                    "origin": "external",
                    "sync_origin": "position_sync",
                    "trace_id": _sync_trace,
                    "entry_reason": "futu_discovery",
                    "strategy_id": strategy_id,
                }
            self.position_store.add(symbol, position)
            # P3: 日志区分策略恢复 vs 手动开仓
            if _has_today_order:
                logger.info(
                    f"🔄 [US Sync·P3] 策略仓位恢复 {symbol} ({info['direction']} {size}股 @${cost:.2f})，"
                    f"匹配当日委托 → playbook_sim (统一管线) | SL=${sl_price:.2f}"
                )
                # 2026-07-20 统一管线 #4: 标 playbook_sim 的同时必须补录未平 PLAYBOOK_SIM
                # trade_record — 主线护栏与 monitor_playbook_sim_exits 查的都是 trade_records
                # 而非 owner_strategy, 只改标记会造成 "主线跳过 + playbook 不管" 的两不管孤儿仓.
                # (走到这里说明 L1195 is_playbook_open_position=False, 即 playbook 漏账/ghost 场景)
                # 本仓惯例: 局部读 AUTO_TRADE_ACCOUNT (与 L587/L650/L1753 一致);
                # broker_env=="real" → PLAYBOOK_LIVE, 否则 PLAYBOOK_SIM (见 L95)
                _auto_acc = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
                self._backfill_playbook_trade_record(
                    symbol=symbol, direction=info['direction'], qty=int(size),
                    entry=cost, sl=sl_price, tp=tp_price,
                    trace_id=_sync_trace,
                    futu_order_id=str(_order_info.get('order_id', '')),
                    broker_env=_auto_acc,
                )
            else:
                logger.warning(
                    f"⚠️ [US Sync] 发现手动开仓 {symbol} ({info['direction']} {size}股 @${cost:.2f})，"
                    f"已加入 ExitMonitor 监控 | SL=${sl_price:.2f}"
                )
            
            # P1 修复 (2026-06-07): 只在 DB 中不存在时发通知（真正新发现）
            # 重启恢复的仓位已在 DB 中，不发通知避免刷屏
            if symbol not in _db_known_symbols:
                try:
                    from app.notifier.feishu import notify_order_filled
                    dir_str = "BUY" if info['direction'] == 'LONG' else "SELL"
                    notify_order_filled(
                        symbol=symbol,
                        direction=dir_str,
                        filled_price=cost,
                        quantity=size,
                        stop_loss=sl_price,
                        take_profit=[tp_price] if tp_price else [],
                        signal_strength=0,
                        signal_confidence=0,
                        strategy="manual_sync",
                        source_type="POSITION_SYNC",  # 明确标记为历史仓位同步
                    )
                    logger.info(f"[US Sync] 飞书成交通知已发送: {symbol} (DB 中不存在，新发现)")
                except Exception as e:
                    logger.warning(f"[US Sync] 飞书成交通知发送失败: {e}")
            else:
                logger.debug(f"[US Sync] {symbol} 已在 DB 中，跳过通知 (重启恢复)")

        # 2. 幽灵持仓 (内存有，Futu 没有) → 手动平仓检测
        # P0 修复 2026-04-28: 期权持仓不在 broker 中，不应被误判为幽灵仓
        # P0-4c 修复 2026-05-03: 查询失败返回 None，跳过清理
        # P0 修复 2026-05-04: 新创建不足60秒的持仓跳过，等待 Futu 确认成交
        ghost_symbols = mem_symbols - futu_symbols
        option_symbols = self._get_option_position_symbols()
        _now_sync = datetime.now()
        if option_symbols is None:
            # 查询失败，无法区分期权和股票，保守跳过所有清理
            logger.warning("[US Sync] 期权查询失败，跳过本轮幽灵仓清理")
        else:
            for symbol in ghost_symbols:
                if symbol in option_symbols:
                    logger.debug(f"[US Sync] 跳过期权仓 {symbol} (不在 broker 中，正常)")
                    continue
                # P0 修复 (2026-05-04 / 2026-05-12 / 2026-05-13): 新持仓保护窗口
                # 根因: 模拟盘 Futu 延迟可达 10 分钟, 固定 180s 保护窗口, 期间不清理幽灵仓
                existing_pos = self.position_store.get(symbol)
                if existing_pos and hasattr(existing_pos, 'entry_time'):
                    try:
                        age_seconds = (_now_sync - existing_pos.entry_time).total_seconds()
                        # 保护窗口 180s (主管线), 期间等待 Futu 确认成交不清理
                        grace_period = 180  # 默认 (主管线)
                        # Task #7 (2026-05-31): owner_strategy 优先, strategy_id 仅作向后兼容 fallback
                        _owner = (
                            getattr(existing_pos, 'owner_strategy', '')
                            or getattr(existing_pos, 'strategy_id', '')
                            or ''
                        )
                        # 检查是否为活跃策略的持仓
                        if age_seconds < grace_period:
                            logger.info(
                                "[US Sync] 跳过新持仓 %s (创建 %.0fs 前 < %.0fs 保护窗口, 等待 Futu 确认成交)",
                                symbol, age_seconds, grace_period,
                            )
                            continue
                    except (TypeError, ValueError):
                        pass
                # ── 根因修复 (2026-06-16): 区分「外部平仓」vs「真幽灵仓」──
                # 根因: 用户通过 Futu App 手动平仓 → broker 无持仓 → 系统误判为幽灵仓
                #       → exit_price=0 → 假通知 + 假 trade_record
                # 修法: 先查 broker 今日成交记录，有平仓成交 → 外部平仓，用实际价格
                #       无成交记录 → 真幽灵仓，静默移除，不发通知
                self.position_store.remove(symbol)
                _today_orders = getattr(self, '_today_orders', {})
                _deal_info = _today_orders.get(symbol)
                
                # 判断是否为平仓方向的成交 (LONG 平仓=SELL, SHORT 平仓=BUY)
                _is_external_close = False
                _exit_price = 0.0
                _exit_qty = 0.0
                if _deal_info and existing_pos:
                    _trd_side = str(_deal_info.get('trd_side', '')).upper()
                    _mem_dir = existing_pos.direction.name if hasattr(existing_pos.direction, 'name') else str(existing_pos.direction)
                    # LONG 持仓 + SELL 成交 = 平仓; SHORT 持仓 + BUY 成交 = 平仓
                    if (_mem_dir == 'LONG' and 'SELL' in _trd_side) or \
                       (_mem_dir == 'SHORT' and 'BUY' in _trd_side):
                        _is_external_close = True
                        _exit_price = float(_deal_info.get('price', 0))
                        _exit_qty = float(_deal_info.get('qty', 0))
                
                if _is_external_close and _exit_price > 0 and existing_pos:
                    # ── 外部平仓: 用实际成交价创建正确的 trade record + 通知 ──
                    _entry = existing_pos.avg_entry_price
                    _size = existing_pos.size
                    _dir_name = existing_pos.direction.name if hasattr(existing_pos.direction, 'name') else str(existing_pos.direction)
                    if _dir_name == 'LONG':
                        _pnl = (_exit_price - _entry) * _size
                        _pnl_pct = (_exit_price - _entry) / _entry if _entry > 0 else 0
                    else:
                        _pnl = (_entry - _exit_price) * _size
                        _pnl_pct = (_entry - _exit_price) / _entry if _entry > 0 else 0
                    
                    logger.info(
                        f"📤 [US Sync] 外部平仓检测: {symbol} "
                        f"(入${_entry:.2f} → 出${_exit_price:.2f}, {_size}股, "
                        f"PnL=${_pnl:.2f}/{_pnl_pct*100:.1f}%)"
                    )
                    
                    # 写入 trade_records (使用真实数据)
                    # [RFC-080 demix2 2026-09-09] trade_mode 标签跟随 AUTO_TRADE_ACCOUNT —
                    # 与被扫描 broker 仓位的 env 同源 (上方 :539 同款逻辑)。原硬编码 'live'
                    # 导致 sim 仓外部平仓被打真钱标签 → RFC-078 迁移按标签搬进 live 库 →
                    # 混同实锤 (MSFT#900/AVGO#901/TTE#902/BULL#904, broker REAL 成交史零证据,
                    # demix2_broker_verified.py 已清)。库路由 _get_db_path() 不动: 单库翻转
                    # 模型下 TRADE_ENV 与 AUTO_TRADE_ACCOUNT 同翻, 路由自然正确。
                    _auto_acc_ec = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
                    _trade_mode_label = 'live' if _auto_acc_ec == "real" else 'simulate'
                    _dup_skip = False  # 查重守卫命中标记 (2026-08-21): try 前初始化防 NameError
                    try:
                        import sqlite3 as _sqlite3
                        _db_path = _get_db_path()
                        _conn = _sqlite3.connect(_db_path)
                        from app.utils.timezone import now_et as _now_et
                        _et_now = _now_et()
                        _entry_time = getattr(existing_pos, 'entry_time', None) or _et_now
                        _entry_time_str = _entry_time.isoformat() if hasattr(_entry_time, 'isoformat') else str(_entry_time)

                        _book_result = upsert_external_close_trade_record(
                            _conn,
                            symbol=symbol,
                            direction=_dir_name,
                            entry_time=_entry_time_str,
                            entry_price=_entry,
                            exit_price=_exit_price,
                            quantity=int(_size),
                            realized_pnl=round(_pnl, 2),
                            pnl_pct=round(_pnl_pct, 4),
                            trade_mode=_trade_mode_label,
                            created_at=_et_now.isoformat(),
                            trace_id=getattr(existing_pos, 'trace_id', None),
                            signal_id=getattr(existing_pos, 'signal_id', None),
                            futu_order_id=getattr(existing_pos, 'futu_order_id', None),
                            close_order_id=str(_deal_info.get('order_id') or '') or None,
                        )
                        _conn.commit()
                        _conn.close()
                        _trade_id = _book_result["trade_id"]
                        _dup_skip = _book_result["action"] == "duplicate_skip"

                        # ── 归因分析: 为外部平仓生成5维度归因 (查重命中时跳过) ──
                        if not _dup_skip:
                            try:
                                from app.analysis.trade_attribution import attribute_from_db
                                import json as _attribution_json
                                _trade_row = {
                                    "symbol": symbol,
                                    "direction": _dir_name,
                                    "entry_price": _entry,
                                    "exit_price": _exit_price,
                                    "quantity": int(_size),
                                    "entry_time": _entry_time_str,
                                    "exit_time": _et_now.isoformat(),
                                }
                                _attr_result = attribute_from_db(_trade_row)
                                if _attr_result is not None:
                                    _meta = {"attribution": _attr_result.to_dict()}
                                    _meta_json = _attribution_json.dumps(_meta, ensure_ascii=False)
                                    _conn2 = _sqlite3.connect(_db_path)
                                    _conn2.execute(
                                        "UPDATE trade_records SET metadata_json=? WHERE id=?",
                                        (_meta_json, _trade_id),
                                    )
                                    _conn2.commit()
                                    _conn2.close()
                                    logger.info(f"📊 [US Sync] 归因已生成: {symbol} {_attr_result.summary}")
                            except Exception as _attr_e:
                                logger.warning(f"[US Sync] 归因生成失败 {symbol}: {_attr_e}")
                    except Exception as _e:
                        logger.warning(f"[US Sync] trade_record 写入失败 {symbol}: {_e}")
                    
                    # 从 open_positions DB 移除
                    try:
                        _db_path = _get_db_path()
                        import sqlite3 as _sqlite3
                        _conn = _sqlite3.connect(_db_path)
                        _conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
                        _conn.commit()
                        _conn.close()
                    except Exception:
                        pass
                    
                    # 发送正确的飞书通知 (有真实 PnL) — 查重命中时跳过(executor 已通知过)
                    if not _dup_skip:
                        try:
                            from app.notifier.feishu import notify_exit
                            notify_exit(
                                symbol=symbol,
                                direction="SELL" if _dir_name == "LONG" else "BUY",
                                entry_price=_entry,
                                exit_price=_exit_price,
                                quantity=int(_size),
                                pnl=round(_pnl, 2),
                                pnl_pct=round(_pnl_pct * 100, 2),
                                exit_reason="external_close",
                                holding_hours=0,
                                source_type="POSITION_SYNC",
                            )
                        except Exception as _e:
                            logger.warning(f"[US Sync] 外部平仓通知发送失败 {symbol}: {_e}")
                
                else:
                    # ── 真幽灵仓: 静默移除，不发通知，不写 trade_record ──
                    logger.info(f"🧹 [US Sync] 清理真幽灵仓 {symbol} (broker 无持仓且无今日成交记录)")
                    # 从 open_positions DB 移除
                    try:
                        _db_path = _get_db_path()
                        import sqlite3 as _sqlite3
                        _conn = _sqlite3.connect(_db_path)
                        _conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
                        _conn.commit()
                        _conn.close()
                    except Exception:
                        pass
                    # ⛔ 不发通知 — 真幽灵仓没有真实成交，通知只会误导

        # 3. 数量变化 (部分成交/加仓) → 更新内存
        common_symbols = mem_symbols & futu_symbols
        for symbol in common_symbols:
            info = futu_positions[symbol]
            existing = self.position_store.get(symbol)
            if existing is None:
                continue

            # P0 修复 (2026-06-04): 方向翻转检测 — Broker-first 覆盖内存方向
            # 根因: 6/3 XLE SHORT→DB残留LONG→平仓变加仓→仓位翻倍→亏$7255
            #        6/3 XLF LONG→DB残留SHORT→退出方向错误→亏$4458
            # 场景: 重启后 position_monitor 从 DB 恢复方向, 但 DB 方向已过期.
            #       position_sync 读到 Broker 正确方向, 更新了 DB 但未修正内存.
            # 修复: 内存方向与 Broker 不一致时, 强制以 Broker 为准覆盖.
            broker_dir = Direction.SHORT if info['direction'] == 'SHORT' else Direction.LONG
            mem_dir = existing.direction
            if broker_dir != mem_dir:
                old_dir_str = mem_dir.value if hasattr(mem_dir, 'value') else str(mem_dir)
                new_dir_str = info['direction']
                logger.critical(
                    f"🔴🔴🔴 [US Sync·方向翻转·P0] {symbol} "
                    f"内存={old_dir_str} vs Broker={new_dir_str} "
                    f"(size={info['qty']}, cost=${info['cost']:.2f}) "
                    f"→ 强制以 Broker 为准覆盖内存! "
                    f"如果正在平仓, 平仓方向将自动修正."
                )
                existing.direction = broker_dir
                existing.avg_entry_price = info['cost']
                existing.size = info['qty']
                # 2026-06-30: 只在initial_sl=0/NULL时才重算SL, 不覆盖已有的vol_buckets SL
                _need_sl_recalc = not existing.initial_sl or existing.initial_sl == 0
                if _need_sl_recalc:
                    try:
                        atr = self._get_symbol_atr(symbol, info['cost'])
                        atr_mult = self._get_atr_stop_mult()  # P1: SSOT 替代硬编码 2.5
                        if info['direction'] == 'LONG':
                            existing.initial_sl = info['cost'] - atr * atr_mult
                            existing.current_sl = existing.initial_sl
                        else:
                            existing.initial_sl = info['cost'] + atr * atr_mult
                            existing.current_sl = existing.initial_sl
                    except Exception:
                        sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                        existing.initial_sl = info['cost'] * sl_mult
                        existing.current_sl = existing.initial_sl
                # P0 修复 (2026-06-04): 清理旧方向 trail_state
                # 旧 peak/highest/lowest 基于反方向计算, 方向翻转后全部失效
                # 不清理会导 trailing stop 方向错误 (SHORT 按 LONG peak 回撤)
                existing.trail_state = {
                    "highest_price": info['cost'],
                    "lowest_price": info['cost'],
                    "prev_scan_price": info['cost'],
                }
                # 同步 DB
                try:
                    import sqlite3 as _sqlite3
                    db_path = _get_db_path()
                    conn = safe_sqlite_connect(Path(db_path), timeout=10)
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute(
                        "UPDATE open_positions SET direction=?, avg_entry_price=?, size=?, "
                        "initial_sl=?, current_sl=?, updated_at=? WHERE symbol=?",
                        (info['direction'], info['cost'], info['qty'],
                         existing.initial_sl, existing.current_sl,
                         datetime.now().isoformat(), symbol)
                    )
                    conn.commit()
                    conn.close()
                except Exception as _dbe:
                    logger.warning(f"[US Sync·方向翻转] DB 同步失败 (不影响内存修正): {_dbe}")
                # 同步 daily_active_pool
                try:
                    from app.utils.daily_active_pool import get_daily_active_pool
                    pool = get_daily_active_pool(market_prefix="US.")
                    pool.add_position(symbol, metadata={
                        "direction": new_dir_str,
                        "quantity": info['qty'],
                    })
                except Exception:
                    pass
                # 飞书告警 (2026-08-09 风暴消除: 改走 PushHub + dedup_key,
                #   同 key 60s 去重 (PushHub 默认 TTL), broker 抖动时防每轮重推.
                #   Qoder 复核修正: level 必须 CRITICAL — 仅 CRITICAL 广播全通道,
                #   HIGH 是"任一成功即停" (DingTalk 优先) → P0 告警会在飞书静默丢失)
                try:
                    from app.push import get_push_hub, PushMessage, Level, Category
                    get_push_hub().push(PushMessage(
                        title=f"🔴 [方向翻转·P0] {symbol}",
                        body=(
                            f"内存={old_dir_str} → Broker={new_dir_str}\n"
                            f"size={info['qty']}, cost=${info['cost']:.2f}\n"
                            f"SL 已重算, DB 已同步"
                        ),
                        level=Level.CRITICAL,
                        category=Category.SYSTEM,
                        dedup_key=f"pos_flip:{symbol}:{new_dir_str}",
                    ))
                except Exception:
                    pass
                continue  # 方向已修正, 跳过后续 qty 逻辑

            qty_changed = abs(existing.size - info['qty']) > 0.1
            # P0-1 修复 (2026-05-19, Karpathy 二审 v2): 区分加仓 vs 减仓, 不一刀切.
            #
            # 5/18 ARM 紊乱根因: paper broker 在 partial_close 后短暂报错 cost=$216.11,
            # 替换真实 $203.53 → PortfolioDrawdown 误判 -2.5% 触发 7×FLAT_ALL.
            #
            # 修复策略 (3 分支, 显式不重叠):
            #   1. size 增加 (加仓 / 漏仓补回): broker cost 是新加权均价, 必须更新
            #   2. size 减少 (部分平仓): cost 是入场加权均价, 不应改, 保留 SSOT
            #   3. cost == 0 (本地 avg 缺失): 用 broker cost 兜底初始化
            if qty_changed:
                size_increased = info['qty'] > existing.size

                # ── P0 (2026-06-12): Scale-out 保护 ──
                # 根因: SNDK 灾难 — 分批止盈刚减仓, position_sync 读 broker 尚未结算,
                #        误判为"加仓"覆盖回大值, 导致下一 tick 又触发分批止盈
                # 修法: 如果 position 有 _scale_out_until 且 broker size > 本地 size,
                #        跳过覆盖 (这是结算延迟, 不是真加仓)
                _exist_meta = getattr(existing, "metadata", {}) or {}
                _so_until = _exist_meta.get("_scale_out_until", 0)
                if size_increased and _so_until and time.time() < _so_until:
                    logger.warning(
                        f"⚠️ [US Sync·scale-out保护] {symbol} 跳过加仓覆盖: "
                        f"本地size={existing.size} broker={info['qty']} "
                        f"(分批止盈冷却中, broker结算延迟)"
                    )
                    continue

                if size_increased:
                    # 加仓: 用 broker 端的新加权均价 (broker 是加仓事件的 SSOT)
                    if existing.avg_entry_price > 0 and abs(existing.avg_entry_price - info['cost']) > 0.5:
                        logger.warning(
                            f"⚠️ [US Sync·加仓] {symbol} size {existing.size}→{info['qty']}, "
                            f"avg_cost ${existing.avg_entry_price:.2f}→${info['cost']:.2f} (broker 加权均价覆盖)"
                        )
                    existing.avg_entry_price = info['cost']
                elif not existing.avg_entry_price or existing.avg_entry_price <= 0:
                    # cost 缺失兜底
                    logger.warning(
                        f"⚠️ [US Sync] {symbol} avg_entry_price=0 → 用 broker cost=${info['cost']:.2f} 兜底"
                    )
                    existing.avg_entry_price = info['cost']
                else:
                    # 减仓: 保留 SSOT cost. 若漂移显著 → WARNING 让 daily_review grep 出来
                    if abs(existing.avg_entry_price - info['cost']) > 0.5:
                        logger.warning(
                            f"⚠️ [US Sync·减仓] {symbol} size {existing.size}→{info['qty']}, "
                            f"local_cost=${existing.avg_entry_price:.2f} broker_cost=${info['cost']:.2f} "
                            f"(broker 漂移, 保留 SSOT, 不覆盖. 若是真实分红/拆股事件请人工处理)"
                        )

                existing.size = info['qty']
                # initial_sl/current_sl 仅在缺失时初始化, 不覆盖
                if not existing.initial_sl or existing.initial_sl == 0:
                    sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                    existing.initial_sl = existing.avg_entry_price * sl_mult
                if not existing.current_sl or existing.current_sl == 0:
                    sl_mult = 1.08 if info['direction'] == 'SHORT' else 0.92
                    existing.current_sl = existing.avg_entry_price * sl_mult
                existing.trail_state = existing.trail_state or {}
                existing.trail_state["prev_scan_price"] = info['cost']
                logger.info(
                    f"📊 [US Sync] 更新 {symbol}: size={info['qty']}, "
                    f"avg_cost=${existing.avg_entry_price:.2f} ({'加仓' if size_increased else '减仓'})"
                )

    # ═══════════════════════════════════════════════════════════════════
    #  P2 新增: 一致性校验 + 资本安全同步 (2026-04-29)
    # ═══════════════════════════════════════════════════════════════════

    def _verify_and_sync_capital(self, futu_positions: Dict):
        """
        持仓同步后的一致性校验与资本安全更新。

        职责:
        1. 校验 Broker/DB/Memory 三层持仓一致性
        2. 从 Broker 实时获取账户资金数据 (equity/cash/market_value)
        3. 更新 Capital Safety 状态，确保风控基于真实数据
        4. 检测持仓变动 (手动开/平仓) 并触发风控刷新
        """
        try:
            # --- 1. 三层一致性校验 ---
            db_positions = self._get_db_positions()
            mem_positions = {}
            if self.position_store:
                for p in self.position_store.all():
                    mem_positions[p.symbol] = {
                        'size': p.size,
                        'direction': p.direction.value if hasattr(p.direction, 'value') else str(p.direction),
                    }

            futu_set = set(futu_positions.keys())
            db_set = set(db_positions.keys())
            mem_set = set(mem_positions.keys())

            # 获取自管持仓列表 — 用于 Memory 比较时排除
            # 根因 (2026-06-17): _sync_to_position_store_locked 正确排除自管持仓,
            #   但此处 futu_set 包含自管持仓 → 永久误报 "Memory 缺少 Broker 持仓"
            try:
                from app.core.config_container import get_all_manual_symbols
                _self_managed = get_all_manual_symbols()
            except Exception:
                _self_managed = set()

            # 2026-07-14: 同步排除 playbook_sim 仓位 — sync 循环正确跳过了它们 (line 1069),
            #   但一致性校验未排除 → 永久误报 "Memory 缺少 Broker 持仓"
            _playbook_symbols = set()
            try:
                _pc = safe_sqlite_connect(_get_db_path(), timeout=5)
                _pb_rows = _pc.execute(
                    "SELECT DISTINCT symbol FROM open_positions "
                    "WHERE owner_strategy = 'playbook_sim' "
                    "AND (sync_status IS NULL OR sync_status NOT IN ("
                    "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                    "'closed_emergency','stale_not_in_broker'))"
                ).fetchall()
                _playbook_symbols = {r[0] for r in _pb_rows}
                _pc.close()
            except Exception:
                pass

            # 检查不一致
            issues = []
            # DB 比较: DB 和 Broker 都应有 playbook_sim 仓位 (sync_status='synced'), 不排除
            if futu_set != db_set:
                missing_db = futu_set - db_set
                missing_futu = db_set - futu_set
                if missing_db:
                    issues.append(f"DB 缺少 Broker 持仓: {missing_db}")
                if missing_futu:
                    issues.append(f"DB 有多余持仓 (Broker 已无): {missing_futu}")

            # Memory 比较时排除自管持仓 + playbook_sim (两者均故意不写入 position_store)
            futu_set_for_mem = futu_set - (_self_managed or set()) - _playbook_symbols
            if futu_set_for_mem != mem_set and self.position_store:
                missing_mem = futu_set_for_mem - mem_set
                missing_mem_ghost = mem_set - futu_set_for_mem
                opt_symbols = self._get_option_position_symbols()
                missing_mem_ghost = {s for s in missing_mem_ghost if s not in opt_symbols}
                if missing_mem:
                    issues.append(f"Memory 缺少 Broker 持仓: {missing_mem}")
                if missing_mem_ghost:
                    # P0 修复 (2026-06-18): 只有新出现的幽灵仓才告警，已知的静默
                    ghost_set = frozenset(missing_mem_ghost)
                    new_ghosts = ghost_set - self._last_reported_ghosts
                    if new_ghosts:
                        issues.append(f"Memory 有幽灵仓: {set(new_ghosts)}")
                        self._last_reported_ghosts = ghost_set  # 更新缓存
                    # 如果幽灵仓消失了（被清理了），重置缓存
                    if not ghost_set:
                        self._last_reported_ghosts = frozenset()

            # B3 (2026-07-19): 入场量一致性校验 (DB.size vs Broker.qty).
            #   现有对账仅覆盖 symbol 存在性, 不覆盖 quantity 级.
            #   部分成交时 trade_records.quantity 可能记错 → 幽灵仓.
            for sym in (futu_set & db_set):
                try:
                    broker_qty = float(futu_positions[sym].get('qty', 0) or 0)
                    db_qty = float(db_positions[sym].get('size', 0) or 0)
                    if broker_qty > 0 and db_qty > 0 and abs(broker_qty - db_qty) >= 1:
                        issues.append(
                            f"⚠️ {sym} 量偏差: DB={db_qty:.0f} vs Broker={broker_qty:.0f} "
                            f"(差{abs(broker_qty-db_qty):.0f}股, 可能部分成交记账偏差)"
                        )
                except Exception:
                    pass  # 单个 symbol 校验失败不阻断整体

            if issues:
                logger.warning(
                    "⚠️ [US Sync Consistency] 检测到 %d 个不一致:\n  %s",
                    len(issues), "\n  ".join(issues)
                )
            else:
                logger.debug(
                    "[US Sync Consistency] ✅ 三层一致 | 持仓: %s",
                    ", ".join(f"{s}({futu_positions[s]['qty']})" for s in futu_set) if futu_set else "空仓"
                )

            # --- 2. 从 Broker 获取实时资金数据 ---
            # 优先复用 broker_adapter 的 get_account_info (已含 HKD→USD 等 FX 转换),
            # 避免直接用原始 power 字段覆盖 capital_safety._state.buying_power.
            broker_data = {}
            if self._broker_adapter is not None and hasattr(self._broker_adapter, 'get_account_info'):
                try:
                    _acc_info = self._broker_adapter.get_account_info()
                    if _acc_info:
                        broker_data = {
                            'cash': _acc_info.get('cash', 0),
                            'market_value': _acc_info.get('market_val', 0),
                            'total_assets': _acc_info.get('total_assets', 0),
                            'buying_power': _acc_info.get('buying_power', 0),
                        }
                except Exception as e:
                    logger.debug("[US Sync Capital] broker_adapter.get_account_info 失败: %s", e)

            # Fallback: 若 broker_adapter 不可用, 用 position_sync 自己的 futu_ctx 查询
            if not broker_data.get('total_assets', 0) and self._futu_ctx is not None:
                try:
                    from futu import RET_OK, TrdEnv
                    import os as _os_acc
                    # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT
                    _auto_acc_ai = _os_acc.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
                    trd_env = TrdEnv.SIMULATE if _auto_acc_ai == "sim" else TrdEnv.REAL
                    _acc_id = int(_os_acc.getenv("SIMULATE_ACCOUNT_ID", "0")) if _auto_acc_ai == "sim" \
                        else int(_os_acc.getenv("REAL_ACCOUNT_ID", "0"))
                    ret, data = self._futu_ctx.accinfo_query(trd_env=trd_env, acc_id=_acc_id)
                    if ret == RET_OK and len(data) > 0:
                        row = data.iloc[0]
                        sf = lambda v: float(v) if v and str(v) not in ("N/A", "nan", "None") else 0.0
                        broker_data = {
                            'cash': sf(row.get("cash")),
                            'market_value': sf(row.get("market_val")),
                            'total_assets': sf(row.get("total_assets")),
                            'buying_power': sf(row.get("power")),
                        }
                except Exception as e:
                    logger.debug("[US Sync Capital] accinfo_query 失败: %s", e)

            # Fallback: 无资金数据时跳过 (由 position_monitor 统一处理)
            if not broker_data.get('total_assets', 0):
                logger.debug("[US Sync Capital] 无 Broker 资金数据，跳过 (由 position_monitor 处理)")
                return

            # --- 3. 更新 Capital Safety ---
            if broker_data.get('total_assets', 0) > 0:
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    capital = get_capital_safety_system()
                    old_equity = capital._state.current_equity
                    old_level = capital._state.water_level.value

                    capital.update_account(
                        current_equity=broker_data['total_assets'],
                        cash=broker_data['cash'],
                        market_value=broker_data['market_value'],
                        buying_power=broker_data['buying_power'],
                    )

                    new_equity = capital._state.current_equity
                    new_level = capital._state.water_level.value

                    # 只在变动时输出 INFO 级别日志
                    if abs(new_equity - old_equity) > 100 or new_level != old_level:
                        logger.info(
                            "💰 [US Sync Capital] 更新: 净值=$%.0f | "
                            "现金=$%.0f | "
                            "市值=$%.0f | "
                            "水位=%s (旧=%s)",
                            new_equity, broker_data['cash'],
                            broker_data['market_value'],
                            new_level, old_level
                        )
                    else:
                        logger.debug(
                            "[US Sync Capital] 未显著变化: 净值=$%.0f, 现金=$%.0f",
                            new_equity, broker_data['cash']
                        )
                except Exception as e:
                    logger.debug(f"[US Sync Capital] Capital Safety 更新失败 (不影响主流程): {e}")

        except Exception as e:
            logger.debug(f"[US Sync Consistency] 校验异常 (已忽略): {e}")

    def _get_trade_env(self) -> int:
        """获取当前交易环境 (0=实盘, 1=模拟) — 跟随 AUTO_TRADE_ACCOUNT"""
        import os as _os_te
        _auto_acc = _os_te.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        return 1 if _auto_acc == "sim" else 0

    def _get_option_position_symbols(self) -> Optional[set]:
        """2026-07-01: 期权已删除, 返回空集"""
        return set()

    def _get_db_positions(self) -> Dict[str, dict]:
        """从 DB 读取当前持仓 (用于判断是否为已存在仓位)"""
        import sqlite3
        if not os.path.exists(_get_db_path()):
            return {}
        try:
            conn = safe_sqlite_connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            # 根因修复 (2026-06-05): 与 _sync_once 保持相同过滤条件
            # 排除已关闭/stale 行, 避免一致性检查重复报告已处理的 ghost 仓
            cursor.execute(
                "SELECT symbol, direction, size, avg_entry_price, initial_sl, current_sl "
                "FROM open_positions "
                "WHERE sync_status IS NULL "
                "OR sync_status NOT IN ("
                "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                "'closed_emergency','stale_not_in_broker')"
            )
            positions = {}
            for row in cursor.fetchall():
                positions[row[0]] = {
                    'direction': row[1],
                    'size': row[2],
                    'avg_entry_price': row[3],
                    'initial_sl': row[4],
                    'current_sl': row[5],
                }
            conn.close()
            return positions
        except Exception as e:
            logger.debug(f"[US Sync] 读取 DB 持仓失败: {e}")
            return {}

    def _get_symbol_atr(self, symbol: str, fallback_price: float) -> float:
        """P2 修复 (2026-04-30): 获取股票 ATR 用于动态止损计算

        优先级 (2026-06-14 ATR 统一修复):
        0. ExitMonitor 日线 ATR 缓存 (含 VIX 缩放, 与止损同源)
        1. TwelveData 历史数据 × VIX 缩放 (盘前/盘后可用)
        2. Futu LV3 K 线 × VIX 缩放 (交易时段可用)
        3. Fallback: 2% 价格 (保守估计)
        """
        # P0 修复 (2026-05-21): 期权代码不走 TwelveData/MarketDataRouter（不支持期权格式）
        from app.data.futu_lv3_provider import FutuLV3Provider
        if FutuLV3Provider._is_option_code(symbol):
            # 期权无传统 ATR，使用 3% 价格作为保守估计（期权波动率通常更高）
            default_atr = fallback_price * 0.03
            logger.debug(f"[US Sync ATR] {symbol} 期权代码，使用默认 ATR=${default_atr:.2f} (3%)")
            return default_atr

        # ── P0 (2026-06-14): 优先读 ExitMonitor 日线 ATR (含 VIX 缩放) ──
        # 与 ExitMonitor 止损决策同源, 避免 initial_sl 与 trailing SL 不一致
        try:
            from app.risk.position_exit_monitor import get_cached_atr
            _exit_atr = get_cached_atr(symbol)
            if _exit_atr > 0:
                logger.debug(f"[US Sync ATR] {symbol} ATR=${_exit_atr:.3f} (ExitMonitor cache)")
                return _exit_atr
        except Exception:
            pass

        # ── VIX 缩放因子 (与 ExitMonitor._get_atr 对齐) ──
        vix_factor = self._get_vix_factor()

        # 尝试从 TwelveData 获取
        try:
            from app.data.twelvedata_provider import TwelveDataProvider
            provider = TwelveDataProvider()
            ts_data = provider.get_historical_data(symbol, interval="1day", outputsize=20)
            if ts_data and 'atr' in ts_data:
                atr = float(ts_data['atr'])
                if atr > 0:
                    atr_scaled = atr * vix_factor
                    logger.debug(
                        f"[US Sync ATR] {symbol} ATR=${atr:.2f} × vix={vix_factor:.2f} "
                        f"= ${atr_scaled:.2f} (TwelveData)"
                    )
                    return atr_scaled
        except Exception as e:
            logger.debug(f"[US Sync ATR] TwelveData 获取失败: {e}")

        # 尝试从市场数据路由器获取 (1-min K线, 量级可能偏差, 需谨慎)
        try:
            from app.data.market_data_router import get_market_data_router
            router = get_market_data_router()
            context = router.get_full_market_context(symbol)
            if context and context.get('atr', 0) > 0:
                atr_raw = float(context['atr'])
                # 1-min ATR 量级远小于日线 ATR, 用价格百分比限幅
                atr_pct = atr_raw / fallback_price if fallback_price > 0 else 0
                if atr_pct < 0.005:  # < 0.5% → 可能是 1-min ATR, 使用 fallback
                    logger.debug(
                        f"[US Sync ATR] {symbol} MarketDataRouter ATR%={atr_pct:.4f} < 0.5%, "
                        f"疑似1-min ATR, 跳过"
                    )
                else:
                    atr_scaled = atr_raw * vix_factor
                    logger.debug(
                        f"[US Sync ATR] {symbol} ATR=${atr_raw:.2f} × vix={vix_factor:.2f} "
                        f"= ${atr_scaled:.2f} (MarketDataRouter)"
                    )
                    return atr_scaled
        except Exception as e:
            logger.debug(f"[US Sync ATR] MarketDataRouter 获取失败: {e}")

        # Fallback: 使用 2% 价格作为保守 ATR 估计
        default_atr = fallback_price * 0.02
        logger.debug(f"[US Sync ATR] {symbol} 使用默认 ATR=${default_atr:.2f} (2% 价格)")
        return default_atr

    def _get_vix_factor(self) -> float:
        """获取 VIX 缩放因子 (与 ExitMonitor._get_atr 逻辑对齐)

        VIX/20 作为波动放大因子, 上限 2.5×, 下限 1.0×.
        获取失败时返回 1.0 (不缩放).
        """
        try:
            from app.data.alphavantage_provider import AlphaVantageProvider
            vix = AlphaVantageProvider().get_vix()
            if vix and vix > 0:
                factor = max(1.0, min(float(vix) / 20.0, 2.5))
                return factor
        except Exception:
            pass
        return 1.0

    def _get_atr_stop_mult(self) -> float:
        """P1 修复 (2026-06-07): SSOT 读取 ATR 止损倍数

        从 exit_policy.yaml 读取 atr_stop.multiplier_by_profit.initial,
        消除 position_sync 中 3 处硬编码 (2.5/4.0) 的不一致。
        默认 4.0 (与 exit_policy.yaml SSOT 对齐)。
        """
        try:
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            _ep = ExitPolicyGuardian()
            _policy = _ep._load_exit_policy_dict()
            return float(_policy.get("exit_rules", {}).get(
                "atr_stop", {}).get("multiplier_by_profit", {}).get("initial", 4.0))
        except Exception:
            return 4.0

    def _execute_pending_trims_and_check_all_oversized(self):
        """根因修复 (2026-06-05): 执行超限减仓 + 扫描所有持仓
        
        职责:
        1. 执行 _pending_trims 中标记的减仓 (新发现的超限仓位)
        2. 扫描所有 open_positions，检测已有超限仓位并触发减仓
        3. 通过 ExecutionEngine 提交部分平仓单
        """
        import sqlite3
        
        # 1. 收集所有需要减仓的 symbol + shares
        trims_to_execute = {}
        
        # 1a. 从 _pending_trims 获取 (新发现的)
        if hasattr(self, '_pending_trims') and self._pending_trims:
            trims_to_execute.update(self._pending_trims)
            self._pending_trims.clear()
        
        # 1b. 扫描所有 open_positions 检测已有超限仓位
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            total_equity = capital._state.current_equity if capital._state.current_equity > 0 else 100000
            
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                # [DEB-fullchain #19] 回退值与 yaml SSOT 0.08 同步 (原 0.25)
                max_per_symbol = float(cfg.get_dotted("risk.position.max_per_symbol", 0.08))
            except Exception:
                max_per_symbol = 0.08
            
            # 🔴 P0 (2026-06-13): 周期扫描必须过滤 self_managed, 防止对手动仓位触发减仓
            try:
                from app.utils.watchlist_manager import get_all_manual_symbols
                _scan_self_managed = get_all_manual_symbols()
            except Exception:
                _scan_self_managed = set()

            conn = safe_sqlite_connect(_get_db_path(), timeout=10)
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol, size, avg_entry_price, direction, trader_type
                FROM open_positions
                WHERE sync_status IS NULL OR sync_status NOT IN (
                    'closed_by_daemon','closed_by_scheduler','closed_by_sync',
                    'closed_emergency','stale_not_in_broker')
            """)
            # 告警冷却: 同一 symbol 5 分钟内只打一次 warning (不影响减仓执行)
            if not hasattr(self, '_last_scan_alert_time'):
                self._last_scan_alert_time: Dict[str, float] = {}
            _alert_now = time.time()
            _alert_cooldown = 300  # 5 minutes
            for row in cursor.fetchall():
                sym, size, price, direction, trader_type = row[0], row[1], row[2], row[3], (row[4] if len(row) > 4 else None)
                # 2026-06-11: 跳过非美股标的
                if not sym.startswith('US.') or sym.count('.') > 1:
                    continue
                # 🔴 P0 (2026-06-13): 跳过 self_managed / manual 仓位 — 系统不碰手动仓
                if _scan_self_managed and sym in _scan_self_managed:
                    continue
                if trader_type and str(trader_type).upper() == 'MANUAL':
                    continue
                # ponytail (2026-06-25): 跳过 Playbook SIM 仓位 — 由 playbook exit monitor 独占管理
                # 2026-07-16: 统一用 is_playbook_open_position 替代 owner_strategy 字符串匹配
                from app.playbook.pipeline import is_playbook_open_position
                if is_playbook_open_position(sym):
                    continue
                if sym in trims_to_execute:
                    continue  # 已在 pending_trims 中
                notional = size * price
                pct = notional / total_equity
                if pct > max_per_symbol:
                    max_value = total_equity * max_per_symbol
                    excess_value = notional - max_value
                    excess_shares = int(excess_value / price) if price > 0 else 0
                    if excess_shares > 0:
                        # 告警冷却: 同一 symbol 5 分钟内只打一次 log
                        if _alert_now - self._last_scan_alert_time.get(sym, 0) >= _alert_cooldown:
                            self._last_scan_alert_time[sym] = _alert_now
                            logger.warning(
                                f"🚫 [仓位扫描] {sym} 占比 {pct:.1%} 超限 {max_per_symbol:.0%}，"
                                f"触发减仓: {excess_shares}/{size}股 (${excess_value:,.0f})"
                            )
                        trims_to_execute[sym] = {
                            'shares': excess_shares,
                            'direction': direction,
                            'reason': f'periodic_scan_over_limit_{max_per_symbol:.0%}',
                        }
            conn.close()
        except Exception as e:
            logger.debug(f"[仓位扫描] 扫描失败: {e}")
        
        # 2. 执行减仓
        if not trims_to_execute:
            return
        
        # 冷却机制: 同一 symbol 10 分钟内不重复减仓
        if not hasattr(self, '_last_trim_time'):
            self._last_trim_time: Dict[str, float] = {}
        _now = time.time()
        _trim_cooldown = 600  # 10 minutes
        
        # 准备 Futu 交易上下文
        try:
            from futu import TrdEnv, TrdMarket, SecurityFirm, OrderType as FutuOrderType, TrdSide
            import os as _os_trim
            # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT
            _auto_acc_trim = _os_trim.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            trade_env = TrdEnv.SIMULATE if _auto_acc_trim == "sim" else TrdEnv.REAL
        except Exception as e:
            logger.error(f"[减仓] Futu 初始化失败: {e}")
            return
        
        # P1-1 修复 (2026-06-12): 永久跳过名单 (业务级错误后避免反复重试至重启)
        if not hasattr(self, '_trim_permanent_skip'):
            self._trim_permanent_skip: set = set()

        # P1-1 修复 (2026-06-12): symbol 正则白名单, 拦截 US.HK.00700 这种非法代码
        _us_symbol_re = re.compile(r'^US\.[A-Z][A-Z0-9.]*$')

        for symbol, trim_info in trims_to_execute.items():
            # P1-1: 非法 symbol 永久跳过 (US.HK.00700, US.NA, etc.)
            if not _us_symbol_re.match(symbol) or symbol.count('.') > 1:
                if symbol not in self._trim_permanent_skip:
                    self._trim_permanent_skip.add(symbol)
                    logger.error(f"[减仓] {symbol} 非法美股代码, 加入永久跳过名单")
                continue
            # P1-1: 已知业务级错误的 symbol 直到下次重启都不再尝试
            if symbol in self._trim_permanent_skip:
                logger.debug(f"[减仓] {symbol} 已在永久跳过名单, 不再尝试")
                continue
            # 冷却检查
            if _now - self._last_trim_time.get(symbol, 0) < _trim_cooldown:
                logger.debug(f"[减仓] {symbol} 冷却中，跳过")
                continue
            
            shares = trim_info['shares']
            direction = trim_info['direction']
            reason = trim_info.get('reason', 'sync_over_limit')
            try:
                # 通过 Futu trade_ctx 直接提交市价平仓单
                ctx = self._get_futu_context(
                    os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                    int(os.getenv("FUTU_OPEND_PORT", "11111")),
                    trade_env, TrdMarket.US, SecurityFirm.FUTUINC,
                )
                if ctx is None:
                    logger.error(f"[减仓] {symbol} Futu 连接不可用")
                    continue
                
                # 平仓方向: LONG→SELL, SHORT→BUY (COVER)
                close_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY
                
                # 获取当前价格 (市价单需要参考价格)
                try:
                    from app.data.market_data_router import get_market_data_router
                    _md = get_market_data_router().get_latest(symbol)
                    _price = float(_md.get('price', 0) or 0) if _md else 0
                except Exception:
                    _price = 0
                if _price <= 0:
                    _price = trim_info.get('price', 0)
                
                ret, data = ctx.place_order(
                    price=_price,
                    qty=shares,
                    code=symbol,
                    trd_side=close_side,
                    order_type=FutuOrderType.MARKET,
                    trd_env=trade_env,
                    remark=f"OVER_LIMIT_TRIM:{reason}",
                )
                if ret == 0:
                    _oid = data.iloc[0].get("order_id", "?") if hasattr(data, 'iloc') else "?"
                    logger.info(
                        f"✅ [减仓] {symbol} {close_side} {shares}股 已提交 "
                        f"(order_id={_oid}, reason={reason})"
                    )
                    self._last_trim_time[symbol] = _now  # 更新冷却时间
                else:
                    # P1-1 修复 (2026-06-12): 失败也写冷却 + 业务错误识别
                    self._last_trim_time[symbol] = _now
                    _err_text = str(data).lower()
                    _business_keywords = (
                        "找不到", "not found", "invalid", "交叉交易", "相反方向",
                        "不支持", "no such", "permission", "禁止", "不存在",
                    )
                    if any(kw in _err_text for kw in _business_keywords):
                        self._trim_permanent_skip.add(symbol)
                        logger.error(
                            f"[减仓] {symbol} 业务级错误, 加入永久跳过名单: ret={ret}, data={data}"
                        )
                    else:
                        logger.error(f"[减仓] {symbol} 提交失败 (10min 冷却): ret={ret}, data={data}")
            except Exception as e:
                # 异常路径同样写冷却, 防止异常驱动的快速重试
                self._last_trim_time[symbol] = _now
                logger.error(f"[减仓] {symbol} 执行异常 (10min 冷却): {e}")

    # Broker-first (2026-06-03): _restore_position_from_db 已删除
    # 不再从 DB 恢复持仓到内存 — broker 是唯一真相源
    # 新发现的仓位统一走 _sync_to_position_store_locked 的全新计算路径


# Global instance
_us_sync: Optional[USPositionSync] = None
_us_sync_lock = threading.Lock()  # P0-3b 修复 (2026-05-03): 防止多实例竞争


def get_us_position_sync() -> USPositionSync:
    global _us_sync
    if _us_sync is None:
        with _us_sync_lock:
            # 双重检查: 可能在等待锁期间其他线程已初始化
            if _us_sync is None:
                _us_sync = USPositionSync()
    return _us_sync


def start_us_position_sync(sync_interval: int = 30, position_store=None, broker_adapter=None):
    sync = get_us_position_sync()
    sync.sync_interval = sync_interval
    sync.position_store = position_store  # P0 修复 2026-04-25
    sync._broker_adapter = broker_adapter  # 复用执行引擎 broker 连接
    sync.start()
    return sync
