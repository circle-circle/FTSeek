"""
FTSeek-US 订单状态追踪器 (Order Tracker)

P0 根因修复 (2026-05-14):
  根因: 系统缺少订单状态追踪机制。平仓订单提交后如果未确认成交，
        系统仍标记为 pending_close 并发送通知，导致：
        1. 用户收到错误的平仓通知
        2. 持仓状态与实际不一致
        3. 盘后/盘前提交的订单可能永远无法确认

  修复: 引入 OrderTracker，对每个提交的订单进行全生命周期追踪：
        - SUBMITTED → PENDING_CONFIRM → FILLED / CANCELLED / FAILED / STALE
        - 盘后提交的订单标记为 AFTER_HOURS_PENDING，开盘时重新评估
        - 未确认的订单不会被标记为已平仓，持仓继续被监控
        - 通知只在确认成交后发送

状态机:
  SUBMITTED ──► PENDING_CONFIRM ──► FILLED (通知)
                                      │
                   CANCELLED ◄───────┤
                   FAILED ◄──────────┤
                   STALE ◄───────────┤  (超时未确认)
"""
import logging
import sqlite3
import time
import warnings
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path
from enum import Enum

logger = logging.getLogger("ftseek.order_tracker")

# ═══════════════════════════════════════════════════════════════════
#  Task #37 (2026-06-16): 时段感知超时管理
#  根因: 盘后/闭市订单挂单 8 小时无人撤; 实盘 US.MU 案例 21:30 提交
#        挂到次日凌晨, 持仓无法清算
#  修复: 按 ET 时段动态选择超时 + 超时即撤 + Dashboard 推送 + 信号重排队
# ═══════════════════════════════════════════════════════════════════

# 盘前: 04:00-09:30 ET
PREMARKET_CONFIRM_TIMEOUT = 300       # 5 分钟
# 盘中: 09:30-16:00 ET
DAY_TRADING_CONFIRM_TIMEOUT = 120     # 2 分钟
# 盘后: 16:00-20:00 ET
AFTER_HOURS_CONFIRM_TIMEOUT = 600     # 10 分钟 (Task #37 用户要求)
# 闭市: 20:00 ET 之后 / 周末 / 节假日
CLOSED_CONFIRM_TIMEOUT = 1800         # 30 分钟

# 时段 → 超时映射 (键值对齐 app.utils.timezone.get_market_session)
_TIMEOUT_BY_SESSION = {
    "pre_market":  PREMARKET_CONFIRM_TIMEOUT,
    "regular":     DAY_TRADING_CONFIRM_TIMEOUT,
    "after_hours": AFTER_HOURS_CONFIRM_TIMEOUT,
    "closed":      CLOSED_CONFIRM_TIMEOUT,
}


def get_dynamic_timeout(now_et_dt=None) -> int:
    """根据当前 ET 时间返回适当的订单确认超时秒数 (Task #37).

    时段边界 (与 app.utils.timezone.get_market_session 一致):
      - pre_market:  04:00 <= t < 09:30  → 300s
      - regular:     09:30 <= t <= 16:00 → 120s   (16:00 整点算盘中)
      - after_hours: 16:00 <  t <= 20:00 → 600s
      - closed:      其余 (含周末/节假日)→ 1800s

    注: 16:00 整点归属 regular (与上游 get_market_session 保持一致),
        16:00 之后第一秒进入 after_hours.

    Args:
        now_et_dt: 指定 ET 时间 (默认取当前 ET);
                   也可传入订单 submit_time 用以按提交时段决定超时,
                   便于单测构造历史时间.

    Returns:
        超时秒数; 异常情况 fallback 为 CLOSED_CONFIRM_TIMEOUT (保守值).
    """
    try:
        from app.utils.timezone import get_market_session
        session = get_market_session(now_et_dt)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning(f"[OrderTracker] 时段判定失败, 回退 CLOSED 超时: {exc}")
        return CLOSED_CONFIRM_TIMEOUT
    return _TIMEOUT_BY_SESSION.get(session, CLOSED_CONFIRM_TIMEOUT)


class _DeprecatedClassAttr:
    """Task #37: 旧字段访问时打 DeprecationWarning, 兼容外部调用方."""
    def __init__(self, value, old_name: str, alternative: str):
        self._value = value
        self._old_name = old_name
        self._alternative = alternative

    def __get__(self, obj, objtype=None):
        warnings.warn(
            f"OrderTracker.{self._old_name} 已弃用, 请改用 {self._alternative}",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._value


class OrderState(Enum):
    """订单状态"""
    SUBMITTED = "submitted"              # 已提交到 Futu
    PENDING_CONFIRM = "pending_confirm"  # 等待确认成交
    FILLED = "filled"                    # 已成交
    PARTIAL_FILLED = "partial_filled"    # F2 (2026-05-16): 部分成交 (dealt<expected) — 实盘对账关键
    CANCELLED = "cancelled"              # 已撤销
    FAILED = "failed"                    # 失败（被拒绝）
    STALE = "stale"                      # 超时未确认（需要重新评估）
    AFTER_HOURS_PENDING = "after_hours_pending"  # 盘后提交，等待开盘


# 订单状态存储
_ORDER_TRACKER_DB = None
_TRACKER_INSTANCE = None


class OrderTracker:
    """
    订单全生命周期追踪器 (单例)
    
    职责:
    1. 记录每个提交的订单状态
    2. 定期检查订单确认状态
    3. 处理盘后/盘前提交的订单
    4. 超时未确认的订单标记为 STALE
    5. 确认成交后才允许发送平仓通知
    """
    
    # Task #37 (2026-06-16): 原静态常量已弃用, 请使用模块级 get_dynamic_timeout()
    # 保留为 alias 仅为兼容那些可能在老代码路径上读取该字段的调用方.
    # 访问时会触发 DeprecationWarning. 内部超时判断一律走 get_dynamic_timeout().
    CONFIRM_TIMEOUT_SECONDS = _DeprecatedClassAttr(
        AFTER_HOURS_CONFIRM_TIMEOUT,
        old_name="CONFIRM_TIMEOUT_SECONDS",
        alternative="get_dynamic_timeout()",
    )
    AFTER_HOURS_MAX_WAIT = _DeprecatedClassAttr(
        AFTER_HOURS_CONFIRM_TIMEOUT,
        old_name="AFTER_HOURS_MAX_WAIT",
        alternative="get_dynamic_timeout()",
    )
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
        self.db_path = db_path
        self._pending_orders: Dict[str, Dict] = {}  # futu_order_id → order_info
        self._load_pending_orders()
        logger.info(f"[OrderTracker] 已初始化 | 待追踪订单: {len(self._pending_orders)}")
    
    def _load_pending_orders(self):
        """从数据库加载未完成的订单"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='order_tracking'")
            if not cursor.fetchone():
                self._create_table(cursor)
                conn.commit()
                conn.close()
                return
            
            # 加载未完成的订单
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            cursor.execute("""
                SELECT futu_order_id, symbol, direction, quantity, order_type, 
                       state, submit_time, last_check, metadata_json
                FROM order_tracking 
                WHERE state IN ('submitted', 'pending_confirm', 'after_hours_pending')
                  AND submit_time > ?
            """, (cutoff,))
            
            for row in cursor.fetchall():
                oid = row[0]
                self._pending_orders[oid] = {
                    "futu_order_id": oid,
                    "symbol": row[1],
                    "direction": row[2],
                    "quantity": row[3],
                    "order_type": row[4],
                    "state": row[5],
                    "submit_time": row[6],
                    "last_check": row[7],
                    "metadata": self._parse_json(row[8]),
                }
            conn.close()
        except Exception as e:
            logger.warning(f"[OrderTracker] 加载待追踪订单失败: {e}")
    
    def _create_table(self, cursor):
        """创建订单追踪表"""
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS order_tracking (
                futu_order_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                quantity INTEGER,
                order_type TEXT,
                state TEXT NOT NULL,
                submit_time TEXT NOT NULL,
                last_check TEXT,
                confirmed_time TEXT,
                exit_reason TEXT,
                metadata_json TEXT
            )
        """)
    
    @staticmethod
    def _parse_json(text):
        import json
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                return {}
        return {}
    
    def _save_order(self, order_info: Dict):
        """保存订单状态到数据库"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            self._create_table(cursor)
            cursor.execute("""
                INSERT OR REPLACE INTO order_tracking 
                (futu_order_id, symbol, direction, quantity, order_type, 
                 state, submit_time, last_check, confirmed_time, exit_reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_info["futu_order_id"],
                order_info["symbol"],
                order_info["direction"],
                order_info["quantity"],
                order_info.get("order_type", "MARKET"),
                order_info["state"],
                order_info["submit_time"],
                order_info.get("last_check"),
                order_info.get("confirmed_time"),
                order_info.get("exit_reason"),
                self._to_json(order_info.get("metadata", {})),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[OrderTracker] 保存订单状态失败: {e}")
    
    @staticmethod
    def _to_json(obj):
        import json
        try:
            return json.dumps(obj)
        except (TypeError, ValueError, OverflowError):
            return "{}"
    
    def register_order(self, futu_order_id: str, symbol: str, direction: str, 
                       quantity: int, exit_reason: str = None, 
                       is_after_hours: bool = False) -> Dict:
        """
        注册新提交的订单
        
        Returns:
            订单信息字典
        """
        now = datetime.now().isoformat()
        order_info = {
            "futu_order_id": futu_order_id,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "order_type": "MARKET",
            "state": OrderState.AFTER_HOURS_PENDING.value if is_after_hours else OrderState.PENDING_CONFIRM.value,
            "submit_time": now,
            "last_check": now,
            "confirmed_time": None,
            "exit_reason": exit_reason,
            "metadata": {},
        }
        
        self._pending_orders[futu_order_id] = order_info
        self._save_order(order_info)
        
        state_desc = "盘后待处理" if is_after_hours else "等待确认"
        logger.info(f"[OrderTracker] 注册订单 | {symbol} {direction} qty={quantity} "
                    f"futu_id={futu_order_id} 状态={state_desc}")
        return order_info
    
    def update_order_state(self, futu_order_id: str, new_state: OrderState, 
                           metadata: Dict = None) -> Optional[Dict]:
        """更新订单状态"""
        order = self._pending_orders.get(futu_order_id)
        if not order:
            logger.warning(f"[OrderTracker] 订单 {futu_order_id} 未找到")
            return None
        
        old_state = order["state"]
        order["state"] = new_state.value
        order["last_check"] = datetime.now().isoformat()
        
        if new_state == OrderState.FILLED:
            order["confirmed_time"] = datetime.now().isoformat()
        if metadata:
            order["metadata"].update(metadata)
        
        self._save_order(order)
        logger.info(f"[OrderTracker] 订单状态更新 | {order['symbol']} {futu_order_id} "
                    f"{old_state} → {new_state.value}")
        return order
    
    def check_order_status(self, futu_order_id: str, broker_adapter, 
                           trd_env) -> Optional[Dict]:
        """
        检查单个订单的实际状态
        
        Returns:
            更新后的订单信息，或 None（订单不存在）
        """
        order = self._pending_orders.get(futu_order_id)
        if not order:
            return None
        
        try:
            from futu import RET_OK as RET_OK_FUTU
            ret, data = broker_adapter.trade_ctx.order_list_query(
                order_id=futu_order_id,
                trd_env=trd_env
            )
            
            if ret == RET_OK_FUTU and not data.empty:
                _row = data.iloc[0]
                status = str(_row.get('order_status', ''))
                order["last_check"] = datetime.now().isoformat()
                order["metadata"]["last_status"] = status

                # F2 (2026-05-16): PARTIAL_FILLED 处理 — 此前 0 处响应, 实盘必坑
                # 同时把 dealt_qty 写入 metadata, 供 position_store 矫正用
                _dealt_qty = float(_row.get('dealt_qty', 0) or 0)
                _expected_qty = float(_row.get('qty', order.get('quantity', 0)) or 0)
                if _dealt_qty > 0:
                    order["metadata"]["dealt_qty"] = _dealt_qty
                    order["metadata"]["expected_qty"] = _expected_qty
                    order["metadata"]["fill_ratio"] = (
                        _dealt_qty / _expected_qty if _expected_qty > 0 else 0.0
                    )

                # 完全成交
                if 'FILLED' in status or 'DEALT' in status:
                    if _dealt_qty > 0 and _expected_qty > 0 and _dealt_qty < _expected_qty:
                        # 状态说 FILLED 但 dealt < expected → 实际 partial
                        logger.warning(
                            f"[OrderTracker·F2] {futu_order_id} 状态 FILLED 但 dealt={_dealt_qty}/{_expected_qty} "
                            f"(fill_ratio={_dealt_qty/_expected_qty:.2%}) — 按 partial 处理"
                        )
                        return self.update_order_state(
                            futu_order_id, OrderState.PARTIAL_FILLED,
                            {"futu_status": status, "dealt_qty": _dealt_qty, "expected_qty": _expected_qty}
                        )
                    return self.update_order_state(futu_order_id, OrderState.FILLED,
                                                  {"futu_status": status, "dealt_qty": _dealt_qty})
                # F2: 显式 PARTIAL 状态分支
                elif 'PARTIAL' in status or 'PART' in status:
                    logger.warning(
                        f"[OrderTracker·F2] {futu_order_id} PARTIAL_FILLED: dealt={_dealt_qty}/{_expected_qty}"
                    )
                    return self.update_order_state(
                        futu_order_id, OrderState.PARTIAL_FILLED,
                        {"futu_status": status, "dealt_qty": _dealt_qty, "expected_qty": _expected_qty}
                    )
                elif 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
                    # F2: 取消时也可能已有部分成交, 不能丢失这部分
                    if _dealt_qty > 0:
                        logger.warning(
                            f"[OrderTracker·F2] {futu_order_id} CANCELLED 但已 dealt={_dealt_qty}/{_expected_qty} "
                            f"— position_store 需保留这部分仓位"
                        )
                        # 仍标 FAILED, 但 metadata 中 dealt_qty>0, 让后续 position 矫正使用
                    return self.update_order_state(futu_order_id, OrderState.FAILED,
                                                  {"futu_status": status, "dealt_qty": _dealt_qty})
                elif 'SUBMITTING' in status or 'SUBMITTED' in status or 'WAITING' in status:
                    # 仍在排队
                    return self.update_order_state(futu_order_id, OrderState.PENDING_CONFIRM,
                                                  {"futu_status": status})
            else:
                logger.warning(f"[OrderTracker] 查询订单 {futu_order_id} 失败: ret={ret}")
                
        except Exception as e:
            logger.warning(f"[OrderTracker] 检查订单 {futu_order_id} 状态失败: {e}")
        
        return order
    
    def gc_stale_orders(self, max_age_hours: int = 24) -> int:
        """
        P0-5 (2026-05-14): 清理 STALE / FAILED / CANCELLED 状态超过 max_age 的订单.

        Returns: 清理数
        """
        now = time.time()
        cutoff = now - max_age_hours * 3600
        removed = 0
        for oid in list(self._pending_orders.keys()):
            o = self._pending_orders.get(oid)
            if not o:
                continue
            terminal = o["state"] in (
                OrderState.STALE.value,
                OrderState.FAILED.value,
                OrderState.CANCELLED.value,
            )
            try:
                submit_ts = datetime.fromisoformat(o["submit_time"]).timestamp()
            except Exception:
                submit_ts = 0
            if terminal and submit_ts < cutoff:
                self.remove_order(oid)
                removed += 1
        if removed > 0:
            logger.info(f"[OrderTracker·GC] 清理 {removed} 笔陈旧 STALE/FAILED/CANCELLED 订单")
        return removed

    def restore_pending_on_startup(self, broker_adapter, trd_env) -> Dict[str, int]:
        """
        P0-E3 (2026-05-14): 启动时恢复 PENDING_CONFIRM / SUBMITTED 订单的真实状态.

        防止重启后孤儿订单 — 实际已成交但本地仍 PENDING, 持仓不在 PositionStore,
        平仓逻辑无人触发.

        Returns:
            {"restored": N, "still_pending": M, "marked_stale": K}
        """
        if broker_adapter is None or not hasattr(broker_adapter, "trade_ctx"):
            logger.warning("[OrderTracker·restore] broker_adapter 不可用, 跳过启动恢复")
            return {"restored": 0, "still_pending": 0, "marked_stale": 0}

        pending_states = (
            OrderState.SUBMITTED.value,
            OrderState.PENDING_CONFIRM.value,
            OrderState.AFTER_HOURS_PENDING.value,
        )
        candidates = [o for o in self._pending_orders.values() if o["state"] in pending_states]
        logger.info(f"[OrderTracker·restore] 启动恢复, 发现 {len(candidates)} 笔 pending 订单")

        restored = 0
        still_pending = 0
        marked_stale = 0
        for o in candidates:
            try:
                updated = self.check_order_status(o["futu_order_id"], broker_adapter, trd_env)
                if updated is None:
                    # broker 已无此订单 → 标 STALE
                    self.update_order_state(o["futu_order_id"], OrderState.STALE,
                                           {"reason": "not_found_in_broker_on_restart"})
                    marked_stale += 1
                    logger.warning(f"[OrderTracker·restore] {o['symbol']} {o['futu_order_id']} broker 无记录 → STALE")
                    continue
                new_state = updated["state"]
                if new_state == OrderState.FILLED.value:
                    restored += 1
                    logger.info(f"[OrderTracker·restore] {o['symbol']} {o['futu_order_id']} 重启后确认 FILLED")
                elif new_state in (OrderState.FAILED.value, OrderState.CANCELLED.value, OrderState.STALE.value):
                    marked_stale += 1
                    logger.info(f"[OrderTracker·restore] {o['symbol']} {o['futu_order_id']} → {new_state}")
                else:
                    still_pending += 1
                    logger.info(f"[OrderTracker·restore] {o['symbol']} {o['futu_order_id']} 仍 pending, 等待确认")
            except Exception as e:
                logger.warning(f"[OrderTracker·restore] {o.get('futu_order_id')} 恢复失败: {e}")

        logger.info(
            f"[OrderTracker·restore] 完成 — restored={restored} still_pending={still_pending} "
            f"marked_stale={marked_stale}"
        )
        return {"restored": restored, "still_pending": still_pending, "marked_stale": marked_stale}

    def check_stale_orders(self, broker_adapter=None) -> List[Dict]:
        """检查超时未确认的订单 (Task #37 重构).

        超时阈值不再是定值, 而是按订单 submit_time 所属的 ET 时段动态取:
          pre_market=300s / regular=120s / after_hours=600s / closed=1800s

        对于 AFTER_HOURS_PENDING 订单: 仍使用提交时段超时 (仍会取到 600/1800)
        避免原本 28800s 的 8 小时黑洞.

        超时后动作 (逐个调用 _handle_timeout_order):
          1) 调 broker_adapter.cancel_order 撤单 (失败只记 ERROR 不抛异常)
          2) 推送 WebSocket 事件 order_timeout_canceled
          3) 写入 signal_requeue_queue 供下次开盘重新评估
          4) 订单状态置为 CANCELLED + metadata 记录撤单时间戳

        Args:
            broker_adapter: 可选; 为 None 时仍会打状态为 CANCELLED + 推 WS 提醒,
                            但跳过 broker 撤单调用 (调用方责任保证下游人工介入).

        Returns:
            被识别为超时的订单列表 (含处理后状态).
        """
        now = datetime.now()
        stale_orders: List[Dict] = []

        for oid, order in list(self._pending_orders.items()):
            try:
                submit_time = datetime.fromisoformat(order["submit_time"])
            except Exception:
                continue
            if order["state"] not in (
                OrderState.AFTER_HOURS_PENDING.value,
                OrderState.PENDING_CONFIRM.value,
                OrderState.SUBMITTED.value,
            ):
                continue

            elapsed = (now - submit_time).total_seconds()
            # 按提交时所在 ET 时段取超时 (避免跨时段误判)
            try:
                from app.utils.timezone import to_et
                submit_et = to_et(submit_time)
            except Exception:
                submit_et = None
            timeout_sec = get_dynamic_timeout(submit_et)

            if elapsed > timeout_sec:
                order["_elapsed_at_timeout"] = elapsed
                order["_timeout_threshold"] = timeout_sec
                stale_orders.append(order)

        for order in stale_orders:
            self._handle_timeout_order(order, broker_adapter)

        return stale_orders

    # ─── Task #37 超时处理辅助函数 ───
    def _handle_timeout_order(self, order: Dict, broker_adapter) -> None:
        """超时单笔订单处理: 撤单 + 推送告警 + 入队重评 (Task #37).

        任何环节异常都不会抛到上层 monitor loop, 仅记 ERROR/WARNING 日志.
        """
        oid = order["futu_order_id"]
        symbol = order["symbol"]
        elapsed = order.get("_elapsed_at_timeout", 0.0)
        threshold = order.get("_timeout_threshold", 0)
        canceled_at = datetime.now().isoformat()

        logger.warning(
            f"[OrderTracker·Timeout] 订单超时 | {symbol} {oid} 已等 {elapsed:.0f}s "
            f"(阈值 {threshold}s, state={order['state']}) → 准备撤单"
        )

        # 1) 调 broker 撤单 (失败不阻塞后续推送/重排队)
        cancel_ok = False
        cancel_err: Optional[str] = None
        if broker_adapter is not None and hasattr(broker_adapter, "cancel_order"):
            try:
                cancel_ok = bool(broker_adapter.cancel_order(oid))
                if not cancel_ok:
                    logger.error(
                        f"[OrderTracker·Timeout] {symbol} {oid} broker.cancel_order 返回 False"
                    )
            except Exception as e:
                cancel_err = str(e)
                logger.error(
                    f"[OrderTracker·Timeout] {symbol} {oid} 撤单异常 (忍受不抛): {e}"
                )
        else:
            cancel_err = "broker_adapter_unavailable"
            logger.warning(
                f"[OrderTracker·Timeout] {symbol} {oid} 无 broker_adapter, 跳过撤单调用"
            )

        # 2) 保存 metadata + 状态 → CANCELLED (源于超时自动撤单路径)
        meta_patch = {
            "timeout_canceled": True,
            "timeout_threshold_sec": threshold,
            "timeout_elapsed_sec": round(elapsed, 1),
            "auto_canceled_at": canceled_at,
            "cancel_reason": "order_confirm_timeout",
            "broker_cancel_ok": cancel_ok,
        }
        if cancel_err:
            meta_patch["broker_cancel_error"] = cancel_err
        self.update_order_state(oid, OrderState.CANCELLED, meta_patch)

        # 3) 写入 signal_requeue_queue 供下次开盘重评估
        try:
            self._enqueue_for_next_open(order, canceled_at)
        except Exception as e:
            logger.error(f"[OrderTracker·Timeout] {symbol} {oid} 重排队写入失败: {e}")

        # 4) 推送 Dashboard WebSocket 事件
        try:
            from app.web_dashboard.routes.ws import publish_event_sync
            publish_event_sync("order_timeout_canceled", {
                "futu_order_id": oid,
                "symbol": symbol,
                "direction": order.get("direction"),
                "quantity": order.get("quantity"),
                "exit_reason": order.get("exit_reason"),
                "elapsed_sec": round(elapsed, 1),
                "timeout_threshold_sec": threshold,
                "auto_canceled_at": canceled_at,
                "broker_cancel_ok": cancel_ok,
                "cancel_error": cancel_err,
            })
        except Exception as e:
            logger.error(f"[OrderTracker·Timeout] {symbol} {oid} WS 推送失败 (忽略): {e}")

    def _enqueue_for_next_open(self, order: Dict, canceled_at: str) -> None:
        """将被撤订单对应信号入队下次开盘重评估 (Task #37).

        使用轻量表 signal_requeue_queue: 代码下游 (开盘前 plan 环节)
        写一次 SELECT * FROM signal_requeue_queue WHERE consumed=0 AND symbol=...
        即可拿到需要重新下单的信号. close 类信号会被下次 plan 直接重新挂入
        平仓队列; open 类信号需重走信号管道. 表设计为追加式, GC 交给上游.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_requeue_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    futu_order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT,
                    quantity INTEGER,
                    exit_reason TEXT,
                    signal_kind TEXT,        -- 'close' | 'open' | 'unknown'
                    canceled_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    consumed_at TEXT,
                    metadata_json TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_requeue_consumed ON signal_requeue_queue(consumed, symbol)"
            )
            # close 类信号: exit_reason 非空意味着这是一笔平仓订单
            signal_kind = "close" if order.get("exit_reason") else "open"
            cur.execute(
                """
                INSERT INTO signal_requeue_queue
                (futu_order_id, symbol, direction, quantity, exit_reason,
                 signal_kind, canceled_at, created_at, consumed, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    order["futu_order_id"],
                    order["symbol"],
                    order.get("direction"),
                    order.get("quantity"),
                    order.get("exit_reason"),
                    signal_kind,
                    canceled_at,
                    datetime.now().isoformat(),
                    self._to_json(order.get("metadata", {})),
                ),
            )
            conn.commit()
            logger.info(
                f"[OrderTracker·Requeue] {order['symbol']} {order['futu_order_id']} "
                f"已入 next-open 重排队 (kind={signal_kind})"
            )
        finally:
            conn.close()

    def get_pending_orders(self, symbol: str = None) -> List[Dict]:
        """获取所有待处理的订单"""
        if symbol:
            return [o for o in self._pending_orders.values() 
                    if o["symbol"] == symbol and o["state"] not in 
                    (OrderState.FILLED.value, OrderState.FAILED.value, OrderState.CANCELLED.value)]
        return [o for o in self._pending_orders.values() 
                if o["state"] not in 
                (OrderState.FILLED.value, OrderState.FAILED.value, OrderState.CANCELLED.value)]
    
    def is_order_confirmed(self, futu_order_id: str) -> bool:
        """检查订单是否已确认成交 (含 partial — 视为已确认有仓位)"""
        order = self._pending_orders.get(futu_order_id)
        if not order:
            return False
        state = order["state"]
        return state in (OrderState.FILLED.value, OrderState.PARTIAL_FILLED.value)

    def get_dealt_qty(self, futu_order_id: str) -> float:
        """F2 (2026-05-16): 返回实际成交数量 (用于 position_store.size 矫正).

        Returns:
            0.0  — 订单未找到或未成交
            >0   — 实际成交量 (PARTIAL 时 < 请求量, FILLED 时 = 请求量)
        """
        order = self._pending_orders.get(futu_order_id)
        if not order:
            return 0.0
        return float(order.get("metadata", {}).get("dealt_qty", 0) or 0)

    def remove_order(self, futu_order_id: str):
        """从内存中移除已完成的订单"""
        self._pending_orders.pop(futu_order_id, None)


def get_order_tracker(db_path: str = None) -> OrderTracker:
    """获取全局 OrderTracker 实例"""
    global _TRACKER_INSTANCE
    if _TRACKER_INSTANCE is None:
        _TRACKER_INSTANCE = OrderTracker(db_path)
    return _TRACKER_INSTANCE
