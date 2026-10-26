"""订单执行 — Futu API 调用层 (下单/平仓/撤单/止损/查询).

公开函数 (供 ManualOrderService facade 委派):
    - submit_order(service, order, idempotency_key)
    - close_position(service, symbol, reason, close_qty, price)
    - get_today_orders(service)
    - cancel_order(service, order_id)
    - takeover_position(service, symbol, owner)
    - modify_stop_loss(service, symbol, new_sl)
    - check_idempotency(service, key)
    - cancel_broker_order(service, order_id)
    - fetch_dealt_avg_price(service, order_id, trd_env, acc_id, retries, delay)
    - calc_exit_price(row, raw_qty)              # static-style
    - place_protective_stop(service, order)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.coordinator.manual_hold_registry import manual_hold_registry
# SIM 隔离 (2026-09-05, DEB-20260905 §13.15): 看板恒走 REAL 券商, 但 DB 由 TRADE_ENV
# 路由到 sim 库, 且 open_positions 的 PK 是 symbol ⇒ 以 symbol 为键的实盘侧写操作一旦
# 撞上 sim 数据收集仓就会改写它。scoped_update 自动加隔离谓词并在撞仓时 CRITICAL 出声。
from app.web_dashboard.env_scope import is_simulate_row, scoped_update

if TYPE_CHECKING:  # pragma: no cover
    from app.web_dashboard.manual_order_service import ManualOrder, ManualOrderService

logger = logging.getLogger("ftseek_us.manual_order_service.executor")

# ── 延迟挂单注册表: 开仓单未成交时, SL/TP 排队等待 ──
# {order_id: {"order": ManualOrder, "service": ManualOrderService, "registered_at": datetime}}
_pending_protections: Dict[str, dict] = {}
# DEB-20260903 #115: 注册表读写加锁 — 下单线程(写)与保护监控线程(读/pop)
# 并发访问裸 dict, 可能竞态丢单/重复挂单
_protections_lock = threading.Lock()
_protection_monitor_started = False

SQL_TAKEOVER = (
    "INSERT INTO open_positions "
    "(symbol, direction, size, avg_entry_price, owner, sync_status, updated_at) "
    "VALUES (?, ?, ?, ?, ?, 'synced', ?) "
    "ON CONFLICT(symbol) DO UPDATE SET "
    "direction=excluded.direction, size=excluded.size, "
    "avg_entry_price=excluded.avg_entry_price, owner=excluded.owner, "
    "sync_status='synced', updated_at=excluded.updated_at"
)


# ─────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────

def check_idempotency(service: "ManualOrderService", key: Optional[str]) -> bool:
    """检查幂等 key 是否已使用. 返回 True = 重复请求应拒绝.

    P0-3 修复 (2026-08-16, DEB-fullchain):
      旧实现把 key 在下单**前**写入 (预烧): place_order 超时 (broker 可能已收单) 后
      60s 内同 key 重试被拒"重复提交"而实际成交无 order_id/无保护单; 60s 后重试 →
      第二笔真实下单; 且纯内存, 重启清空。
      新语义: check 只查已**成功提交**时记录的 key (record_idempotency), 失败不烧 key;
      持久层接 app/execution/idempotency.py (SQLite, 1h 窗口, 重启重载),
      内存 60s 表仅作持久层不可用时的 fallback。
    """
    if not key:
        return False
    namespaced = f"manual:{key}"
    mgr = _get_persistent_idempotency()
    if mgr is not None:
        try:
            return mgr.is_duplicate(namespaced)
        except Exception as e:
            logger.warning(f"[幂等] 持久层查询失败, 回退内存表: {e}")
    # Fallback: 内存 60s (旧路径, 仅持久层不可用时)
    # 同样不预烧: 只查已记录 (record_idempotency 写入) 的 key
    now = time.time()
    expired = [k for k, ts in service._recent_keys.items()
               if now - ts > service._idempotency_ttl]
    for k in expired:
        del service._recent_keys[k]
    return key in service._recent_keys


# ── 持久幂等层 (P0-3 修复 2026-08-16): SQLite 持久化 + 1h 窗口 + 重启重载 ──
_persistent_idempotency = None


def _get_persistent_idempotency():
    """懒加载 IdempotencyManager (与主执行链同源组件); 不可用时返回 None (调用方回退内存)."""
    global _persistent_idempotency
    if _persistent_idempotency is not None:
        return _persistent_idempotency
    try:
        from app.execution.idempotency import IdempotencyManager
        _persistent_idempotency = IdempotencyManager(window_seconds=3600)
    except Exception as e:
        logger.warning(f"[幂等] 持久层初始化失败, 回退内存表: {e}")
        return None
    return _persistent_idempotency


def record_idempotency(service: "ManualOrderService", key: str, order_id: str) -> None:
    """下单**成功后**记录幂等 key (P0-3): key→order_id 持久化, 1h 窗口内去重 + 重启重载。

    只在 place_order RET_OK 后调用 — 失败/超时不记录, 用户可用同 key 安全重试。
    """
    if not key:
        return
    service._recent_keys[key] = time.time()  # 进程内快路径
    mgr = _get_persistent_idempotency()
    if mgr is not None:
        try:
            mgr.record_order(f"manual:{key}", order_id or "")
        except Exception as e:
            logger.warning(f"[幂等] 持久层记录失败 (不影响下单): {e}")


def synthesize_idem_key(order: "ManualOrder") -> str:
    """P0-3: 前端未传 idempotency_key 时生成指纹 key —
    30s 窗口内相同 symbol+方向+数量视为同一请求 (防双击/快速重试重复下单);
    30s 后重新下单落不同桶, 不误拦合法重复操作。
    """
    bucket = int(time.time() // 30)
    return f"auto:{order.symbol}:{order.action}:{order.quantity}:{bucket}"


# DEB-20260904 #198: 滑动窗口防重 — 固定 30s 桶在边界处失效 (双击 t=29.9/t=30.1 跨桶逃逸),
# 补同 symbol+action+quantity 最近 N 秒判重; 仅作用于前端未传幂等键的兜底路径
_RAPID_REPEAT_WINDOW_SEC = 30
_last_request_ts: Dict[str, float] = {}


def _request_fingerprint(order: "ManualOrder") -> str:
    return f"{order.symbol}:{order.action}:{order.quantity}"


def _is_rapid_repeat(order: "ManualOrder") -> bool:
    """滑动窗口判重: 上次成功提交同指纹请求距今 < 窗口 即视为重复"""
    now = time.time()
    expired = [k for k, ts in _last_request_ts.items()
               if now - ts > _RAPID_REPEAT_WINDOW_SEC]
    for k in expired:
        del _last_request_ts[k]
    ts = _last_request_ts.get(_request_fingerprint(order))
    return ts is not None and (now - ts) < _RAPID_REPEAT_WINDOW_SEC


# ─────────────────────────────────────────────────────────────
# 下单
# ─────────────────────────────────────────────────────────────

def submit_order(service: "ManualOrderService", order: "ManualOrder",
                 idempotency_key: str = None) -> Dict[str, Any]:
    """提交订单到 Futu broker (带锁 + 幂等保护)."""

    # 0. 幂等去重 (防止双击/网络重试)
    # P0-3 修复 (2026-08-16): 前端不传 key 时用指纹 key 兜底; key 在提交成功后
    #   才记录 (record_idempotency), 不再预烧 → 下单失败可用同 key 安全重试。
    _idem_key = idempotency_key or synthesize_idem_key(order)
    with service._order_lock:
        if check_idempotency(service, _idem_key):
            logger.warning(f"[手动下单] 重复请求被拒绝: key={_idem_key}")
            return {"success": False, "error": "重复提交，请勿连续点击"}
        # DEB-20260904 #198: 无前端幂等键时补滑动窗口判重 — 固定桶在 30s 边界处防重失效
        if not idempotency_key and _is_rapid_repeat(order):
            logger.warning(f"[手动下单] 重复请求被拒绝(滑动窗口): {_request_fingerprint(order)}")
            return {"success": False, "error": "重复提交，请勿连续点击"}

        # 1. 风控校验
        errors = service.validate_order(order)
        if errors:
            order.status = "rejected"
            order.error_msg = "; ".join(errors)
            service._write_audit(order)
            return {"success": False, "errors": errors, "order": asdict(order)}

        # 1b. [RFC-080 S3b] 反向守卫: auto-live 持仓标的禁止手动下单 (缺口4).
        #   正向 (auto 避手动) 已有 ManualHoldRegistry; 反向此前无守卫。
        #   冲突机制: open_positions PK=symbol 单列 ⇒ 同标的 auto-live 行与 manual 行
        #   物理无法共存, 手动单成交后 broker 净额合并 → 一行两主 → 仓位孤儿化。
        #   现状 (TRADE_ENV=1) auto 仓在 sim 库/模拟账户, live 库无 owner=auto 行
        #   ⇒ 本守卫恒 no-op; cutover (单库翻转) 后 auto-live 行落 live 库 ⇒ 守卫生效。
        #   fail-open (协调门, 与 ManualHoldRegistry 同款哲学): DB 异常不阻断手动交易。
        #   局限: 富途 APP 直接下单不经本路径, 无法代码守卫 (靠 RFC-078 批E 成交推送
        #   秒级可见 + position_sync 安全默认 MANUAL 兜底)。
        try:
            import sqlite3 as _rg_sql
            from app.core.db_utils import get_dashboard_db as _rg_db
            _rg_conn = _rg_sql.connect(f"file:{_rg_db()}?mode=ro", uri=True, timeout=3)
            try:
                _auto_row = _rg_conn.execute(
                    "SELECT owner_strategy FROM open_positions "
                    "WHERE symbol=? AND owner='auto' AND COALESCE(size,0) > 0 LIMIT 1",
                    (order.symbol,),
                ).fetchone()
            finally:
                _rg_conn.close()
            if _auto_row is not None:
                order.status = "rejected"
                order.error_msg = (
                    f"{order.symbol} 当前由自动交易持有 ({_auto_row[0] or 'auto'}), "
                    f"同标的无法双轨管理 — 请先在看板平掉自动仓再手动下单"
                )
                service._write_audit(order)
                logger.warning(f"[手动下单] 反向守卫拦截: {order.error_msg}")
                return {"success": False, "error": "auto_position_conflict",
                        "errors": [order.error_msg], "order": asdict(order)}
        except Exception as _rg_err:
            logger.warning(f"[手动下单] 反向守卫查询失败 (fail-open 放行): {_rg_err}")

        # 2. 连接 broker
        if not service.connect():
            order.status = "error"
            order.error_msg = "Broker 连接失败"
            service._write_audit(order)
            return {"success": False, "error": "broker_connection_failed"}

        # 3. 执行下单
        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, TimeInForce, RET_OK

            trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE

            # 方向映射
            if order.action == "BUY":
                trd_side = TrdSide.BUY
            elif order.action == "SELL":
                trd_side = TrdSide.SELL
            else:
                # 从 direction 推断
                trd_side = TrdSide.BUY if order.direction == "LONG" else TrdSide.SELL

            # 订单类型
            if order.order_type == "LIMIT" and order.price:
                futu_order_type = FutuOrderType.NORMAL
                price = order.price
            else:
                futu_order_type = FutuOrderType.MARKET
                price = 0  # MARKET 单价格为 0

            # 获取账户 ID
            if service._is_live():
                acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))

            order.timestamp = datetime.now().isoformat()
            order.mode = service._get_mode_label()

            # 有效期映射
            tif = TimeInForce.GTC if order.time_in_force == "GTC" else TimeInForce.DAY

            logger.info(
                f"[手动下单] 提交: {order.symbol} {order.action} {order.quantity}股 "
                f"{'限价$'+str(price) if price else '市价'} "
                f"TIF={order.time_in_force} RTH外={'是' if order.fill_outside_rth else '否'} "
                f"环境={'实盘' if service._is_live() else '模拟'}"
            )

            ret, data = service._broker_ctx.place_order(
                price=price,
                qty=order.quantity,
                code=order.symbol,
                trd_side=trd_side,
                order_type=futu_order_type,
                trd_env=trd_env,
                acc_id=acc_id,
                time_in_force=tif,
                fill_outside_rth=order.fill_outside_rth,
            )

            if ret == RET_OK:
                order.order_id = str(data.iloc[0]["order_id"]) if not data.empty else ""
                order.status = "submitted"
                logger.info(f"[手动下单] ✅ 订单提交成功: order_id={order.order_id}")
                # P0-3 修复 (2026-08-16): 提交成功后才记录幂等 key (不再预烧) —
                # 失败/超时不记录, 用户可用同 key 安全重试; 成功则 1h 窗口内去重 + 重启重载
                record_idempotency(service, _idem_key, order.order_id)
                # DEB-20260904 #198: 记录成功时间, 供滑动窗口判重 (仅成功才标记, 失败可安全重试)
                _last_request_ts[_request_fingerprint(order)] = time.time()
                # 标记为手动持仓, 避免被系统退出监控误平
                # DEB-20260903 #116: 包 try/except — 原裸调用异常会传播到外层
                # except, 把已提交成功的订单覆写为 error, 且幂等已记录致重试被拒
                try:
                    service._mark_position_manual(order.symbol, order.direction)
                except Exception as _mark_err:
                    logger.warning(f"[手动下单] 标记 manual 失败 (不影响下单): {_mark_err}")
                # 注册到 ManualHoldRegistry: 自动管线扫描时跳过此标的 (实时, 无延迟)
                try:
                    manual_hold_registry.register(
                        order.symbol, order.direction, int(order.quantity),
                        reason=f"manual_order:{order.reason}",
                        account="real",
                    )
                except Exception as _reg_err:
                    logger.warning(f"[手动下单] ManualHoldRegistry 注册失败 (不影响下单): {_reg_err}")
                # PDT 记录移至平仓时 — 开仓不计入 day trade (P0 2026-06-11)

                # P1-6: 注册延迟保护挂单 — 等开仓单成交后再挂 SL/TP
                if order.stop_loss > 0 or order.take_profit > 0:
                    _register_pending_protection(service, order)

                # Auto-thesis: 自动组装入场决策上下文
                try:
                    thesis_ctx = service._auto_generate_thesis(order)
                    service._save_thesis_to_db(order, thesis_ctx)
                except Exception as _thesis_err:
                    logger.debug(f"[auto-thesis] 生成失败 (不影响下单): {_thesis_err}")

                # 手动开仓写入 trade_records (exit_price=0, 平仓时 UPDATE 闭环)
                try:
                    service._record_manual_open(order)
                except Exception as _open_err:
                    logger.debug(f"[手动交易闭环] 开仓写入失败 (不影响下单): {_open_err}")

                # WebSocket 实时推送
                try:
                    from app.web_dashboard.routes.ws import publish_event_sync
                    publish_event_sync("order_update", {
                        "order_id": order.order_id, "status": "submitted",
                        "symbol": order.symbol, "side": order.action,
                        "qty": order.quantity, "price": price,
                    })
                except ImportError:
                    pass
            else:
                order.status = "error"
                order.error_msg = str(data)
                logger.error(f"[手动下单] ❌ 订单提交失败: {data}")
                try:
                    from app.web_dashboard.routes.ws import publish_event_sync
                    publish_event_sync("order_update", {
                        "order_id": "", "status": "error",
                        "symbol": order.symbol, "error": "订单提交失败",
                    })
                except ImportError:
                    pass

        except Exception as e:
            order.status = "error"
            order.error_msg = "订单提交异常"
            logger.error(f"[手动下单] 下单异常: {e}")

        # 4. 写审计日志
        service._write_audit(order)

        return {
            "success": order.status == "submitted",
            "order": asdict(order),
        }


# ─────────────────────────────────────────────────────────────
# 平仓
# ─────────────────────────────────────────────────────────────

def _release_close_locks(symbol: str) -> None:
    """平仓尝试结束即释放竞态锁.

    P1-7 修复 (2026-08-16, DEB-fullchain): 旧实现仅在平仓失败分支释放锁;
    部分平仓成功后 _pending_close 永久残留 → position_exit_monitor 的
    `if _meta.get('_pending_close'): continue` 使组合级止损 (FLAT_ALL/WARN/REDUCE)
    永久跳过该标的, 直至重启。全平路径 position_store.remove 失败时同样需要兜底释放。
    """
    try:
        import importlib as _il
        _sched = getattr(_il.import_module("app.core.scheduler_core"), "_scheduler_instance", None)
        if _sched and hasattr(_sched, 'position_store') and _sched.position_store.has(symbol):
            _pos = _sched.position_store.get(symbol)
            _meta = getattr(_pos, 'metadata', {}) or {}
            _changed = False
            for _k in ("_close_in_progress", "_close_in_progress_ts", "_pending_close"):
                if _k in _meta:
                    _meta.pop(_k, None)
                    _changed = True
            if _changed:
                _pos.metadata = _meta
                logger.info(f"[手动平仓] {symbol} 竞态锁已释放 (平仓尝试结束)")
    except Exception as _e:
        logger.debug(f"[手动平仓] {symbol} 竞态锁释放失败: {_e}")


def close_position(service: "ManualOrderService", symbol: str, reason: str,
                   close_qty: int = 0, price: float = None,
                   fill_outside_rth: bool = False) -> Dict[str, Any]:
    """平仓指定持仓.

    Args:
        symbol: 标的代码
        reason: 平仓理由
        close_qty: 平仓数量 (0=全平, >0=部分平仓)
        price: 限价 (None=市价平仓, >0=限价平仓用于止损/止盈)
        fill_outside_rth: 是否允许盘前/盘后成交 (夜盘平仓必需)
    """
    from app.web_dashboard.manual_order_service import ManualOrder

    with service._close_lock:  # P0 2026-06-15: 防止手动/自动平仓竞态
        if not service.connect():
            return {"success": False, "error": "broker_connection_failed"}

        # 2026-10-15 用户裁口径: 手动平仓可走 web dashboard — 拆除此处 ALLOW_REAL_TRADING 闸,
        # 对齐 validator.py:86 的既定设计 (2026-06-24: 该旗标只锁 AUTO pipeline, 手动恒 REAL 不适用)。
        # 原 6/12 P0-2 闸的防护场景 (切回模拟盘后误下实盘) 在「手动恒 REAL」设计下不成立;
        # 旧闸造成「能开不能平」单向门 (买入侧无闸), 平真实持仓被拒属事故态。

        try:
            from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, RET_OK

            trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
            if service._is_live():
                acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))

            # 查询当前持仓
            ret, positions = service._broker_ctx.position_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret != RET_OK:
                return {"success": False, "error": f"持仓查询失败: {positions}"}

            pos = positions[positions["code"] == symbol]
            if pos.empty:
                return {"success": False, "error": f"未找到 {symbol} 的持仓"}

            row = pos.iloc[0]
            raw_qty = float(row.get("qty", 0))
            if raw_qty == 0:
                return {"success": False, "error": f"{symbol} 持仓数量为 0"}

            # 根据持仓方向决定平仓方向
            side = str(row.get("position_side", "")).upper()
            is_long = not ((raw_qty < 0) or (side == "SHORT"))
            position_dir = "LONG" if is_long else "SHORT"
            total_qty = int(abs(raw_qty))
            close_side = TrdSide.SELL if is_long else TrdSide.BUY  # 平多卖, 平空买

            # 部分平仓 vs 全平
            if close_qty <= 0 or close_qty >= total_qty:
                qty = total_qty
                close_type = "全平"
            else:
                qty = close_qty
                close_type = f"部分({qty}/{total_qty})"

            _protection_snapshot = None
            if service._is_live():
                _protection_snapshot = _load_protective_snapshot(service, symbol)
                if (
                    0 < close_qty < total_qty
                    and (
                        _protection_snapshot is None
                        or _protection_snapshot["current_sl"] <= 0
                        or _protection_snapshot["initial_tp"] <= 0
                    )
                ):
                    logger.critical(
                        f"[手动平仓] {symbol} 实盘部分平仓前无法读取 open_positions 的 "
                        f"SL/TP 价位，拒绝下单以避免剩余仓位失去 broker 端保护"
                    )
                    return {
                        "success": False,
                        "error": "实盘部分平仓前读取 SL/TP 价位失败，已拒绝下单",
                    }

            logger.info(
                f"[手动平仓] {symbol}: {close_type} {position_dir}, "
                f"平仓动作={'SELL' if is_long else 'BUY'}, 数量={qty}"
            )

            # 限价 or 市价
            is_limit = price is not None and price > 0
            futu_order_type = FutuOrderType.NORMAL if is_limit else FutuOrderType.MARKET
            order_price = price if is_limit else 0
            order_type_label = f"LIMIT@${price:.2f}" if is_limit else "MARKET"

            # P0-2 修复 (2026-06-12): 平仓金额上限校验 (限价单按指定价, 市价单按持仓成本估算)
            try:
                _ref_price = price if is_limit else float(row.get("cost_price", 0) or 0)
                if _ref_price > 0:
                    _est_value = qty * _ref_price
                    if _est_value > service.MAX_CLOSE_VALUE_USD:
                        logger.error(
                            f"[手动平仓] {symbol} 拦截: 估算金额 ${_est_value:,.0f} "
                            f"超过平仓上限 ${service.MAX_CLOSE_VALUE_USD:,.0f}"
                        )
                        return {
                            "success": False,
                            "error": f"平仓金额 ${_est_value:,.0f} 超过单笔上限 ${service.MAX_CLOSE_VALUE_USD:,.0f}",
                        }
            except Exception as _check_err:
                logger.warning(f"[手动平仓] 金额校验失败 (放行): {_check_err}")

            # 构造审计订单
            order = ManualOrder(
                symbol=symbol,
                direction=position_dir,
                action="SELL" if is_long else "BUY",
                quantity=qty,
                order_type="LIMIT" if is_limit else "MARKET",
                price=price if is_limit else None,
                reason=f"[{close_type}] {reason}",
                mode=service._get_mode_label(),
            )

            # P0: 平仓竞态锁 — 在提交 broker 订单前标记，阻止 ExitMonitor 同时平仓
            _lock_acquired = False
            try:
                import importlib as _il
                _sc_mod = _il.import_module("app.core.scheduler_core")
                _sched = getattr(_sc_mod, "_scheduler_instance", None)
                if _sched and hasattr(_sched, 'position_store') and _sched.position_store.has(symbol):
                    _pos = _sched.position_store.get(symbol)
                    _meta = getattr(_pos, 'metadata', {}) or {}
                    _meta['_close_in_progress'] = True
                    _meta['_close_in_progress_ts'] = time.time()
                    _meta['_pending_close'] = True
                    _pos.metadata = _meta
                    _lock_acquired = True
                    logger.info(f"[手动平仓] {symbol} 已设置 _close_in_progress 竞态锁")
            except Exception as _lock_err:
                logger.warning(f"[手动平仓] 设置竞态锁失败 (非致命): {_lock_err}")

            ret, data = service._broker_ctx.place_order(
                price=order_price,
                qty=qty,
                code=symbol,
                trd_side=close_side,
                order_type=futu_order_type,
                trd_env=trd_env,
                acc_id=acc_id,
                fill_outside_rth=fill_outside_rth,
            )

            if ret == RET_OK:
                order.order_id = str(data.iloc[0]["order_id"]) if not data.empty else ""
                order.status = "submitted"
                logger.info(f"[手动平仓] ✅ {symbol} 平仓成功: {int(qty)}股 RTH外={'是' if fill_outside_rth else '否'}")

                # P1-8 修复 (2026-08-16, DEB-fullchain): 成交状态前置判定 —
                # 未成交的平仓单不得执行清理/记账 (旧行为: 用 cur_price 写假
                # exit_price/realized_pnl → 污染 pre_trade_gate 日回撤熔断)。
                import time as _t
                # 等待 1s 让 broker 成交回报写入 (市价单一般立即成交, 限价单可能延迟)
                _t.sleep(1.0)
                _precise_exit = service._fetch_dealt_avg_price(
                    order.order_id, trd_env, acc_id, retries=3, delay=1.0
                )
                _order_status = ""
                _filled_qty = 0.0
                try:
                    _ret2, _data2 = service._broker_ctx.order_list_query(
                        order_id=order.order_id, trd_env=trd_env, acc_id=acc_id
                    )
                    if _ret2 == RET_OK and not _data2.empty:
                        _order_status = str(_data2.iloc[0].get("order_status", "") or "")
                        _filled_qty = float(_data2.iloc[0].get("dealt_qty", 0) or 0)
                except Exception:
                    pass
                if _filled_qty <= 0:
                    if "CANCEL" in _order_status.upper():
                        # 已取消: 不写 trade_records
                        logger.warning(
                            f"[手动平仓] {symbol} 订单已取消 ({_order_status}), 不写入 trade_records"
                        )
                        order.status = "cancelled"
                        order.error_msg = f"订单已取消: {_order_status}"
                    else:
                        # 未成交 (限价挂单未到价/市价无回报): 撤单 + 不记账。
                        # 残留挂单与 ExitMonitor 双通道平仓 = 双倍平仓风险, 必须撤单。
                        # DEB-20260904 #200: 撤单返回值此前被忽略且消息恒称"已撤单" —
                        # 撤单失败时挂单残留, 与监控形成双通道平仓. 失败需显性告警.
                        _cancel_ok = False
                        try:
                            _cancel_ok = bool(service._cancel_broker_order(order.order_id))
                        except Exception as _cx_err:
                            logger.warning(
                                f"[手动平仓] {symbol} 未成交单撤单失败 (请人工处理): {_cx_err}"
                            )
                        if _cancel_ok:
                            order.error_msg = "平仓单未成交, 已自动撤单"
                            logger.warning(
                                f"[手动平仓] {symbol} 平仓单未成交 (status={_order_status or 'unknown'}), "
                                f"已撤单; 不写 trade_records, 仓位继续受退出监控保护"
                            )
                        else:
                            order.error_msg = "平仓单未成交, 撤单未确认 — 挂单可能残留"
                            logger.critical(
                                f"[手动平仓] {symbol} 平仓单未成交且撤单未确认 "
                                f"(status={_order_status or 'unknown'}), "
                                f"挂单可能残留 → 与退出监控形成双通道平仓风险, 请人工核查!"
                            )
                    _release_close_locks(symbol)
                    service._write_audit(order)
                    return {
                        "success": False,
                        "error": f"{symbol} 平仓未成交 (status={_order_status or 'unknown'}), "
                                 f"已撤单/取消; 仓位仍受监控保护, 可改市价重试",
                    }
                # 成交: 按实际成交数量处理 (部分成交只记已成交部分)
                _record_qty = int(_filled_qty)
                if _record_qty < int(qty):
                    logger.warning(
                        f"[手动平仓] {symbol} 部分成交 {_record_qty}/{int(qty)}, "
                        f"撤销剩余挂单, 仅按已成交部分处理"
                    )
                    # DEB-20260904 #200: 撤余单返回值检查 — 失败意味着剩余挂单仍在,
                    # 可能继续成交与已记账仓位冲突
                    try:
                        _pc_ok = bool(service._cancel_broker_order(order.order_id))
                        if not _pc_ok:
                            logger.critical(
                                f"[手动平仓] {symbol} 部分成交后撤余单未确认 — "
                                f"剩余挂单可能继续成交, 请人工核查!"
                            )
                    except Exception:
                        logger.critical(
                            f"[手动平仓] {symbol} 部分成交后撤余单异常 — 剩余挂单可能残留"
                        )
                # P1-7 修复 (2026-08-16, DEB-fullchain): 仅"实际全部成交"时改判 manual
                # 与执行全平清理。部分止盈后剩余仓位应继续受策略退出保护;
                # 请求全平但部分成交 (剩余挂单已撤) 同样按部分处理。
                _effective_full = (close_type == "全平") and (_record_qty >= total_qty)
                if _effective_full:
                    service._mark_position_manual(symbol, position_dir)

                # Task #11: WebSocket 实时推送 — 平仓 order_update + position_update
                try:
                    from app.web_dashboard.routes.ws import publish_event_sync
                    publish_event_sync("order_update", {
                        "order_id": order.order_id, "status": "submitted",
                        "symbol": symbol, "side": order.action,
                        "qty": qty, "price": order_price,
                        "close": True, "close_type": close_type,
                    })
                    publish_event_sync("position_update", {
                        "action": "exit" if _effective_full else "partial_exit",
                        "symbol": symbol,
                        "direction": position_dir,
                        "qty": _record_qty, "total_qty": total_qty,
                        "reason": reason,
                        "source": "manual",
                    })
                except ImportError:
                    pass
                except Exception as _ws_err:
                    logger.debug(f"[手动平仓] WS 推送失败 (不影响主流程): {_ws_err}")

                # P1-5 (2026-06-14): 通知 ExitMonitor 清理 trail_state
                # 防止: 手动平仓后 trail_state 残留 → 重启 ghost 恢复时带旧状态触发错误退出
                # P1-7 (2026-08-16): 仅实际全平时清理 — 部分平仓剩余仓位仍需 trail 状态
                if _effective_full:
                    try:
                        from app.risk.trail_state_store import get_trail_state_store
                        _ts_store = get_trail_state_store()
                        _ts_store.delete(symbol)
                        logger.info(f"[手动平仓] {symbol} trail_state 已清理")
                    except Exception as _ts_err:
                        logger.debug(f"[手动平仓] trail_state 清理跳过: {_ts_err}")

                # P1-5 (2026-06-14): 全平时从 position_store 移除
                # 部分平仓时不移除 — position_sync 30s 内会更新 size
                if _effective_full:
                    try:
                        import importlib
                        _sc_mod = importlib.import_module("app.core.scheduler_core")
                        _sched = getattr(_sc_mod, "_scheduler_instance", None)
                        if _sched and hasattr(_sched, 'position_store') and _sched.position_store.has(symbol):
                            _sched.position_store.remove(symbol)
                            logger.info(f"[手动平仓] {symbol} 已从 position_store 移除")
                    except Exception as _ps_err:
                        logger.debug(f"[手动平仓] position_store 清理跳过 (position_sync 会兜底): {_ps_err}")
                    # 全平时从 ManualHoldRegistry 注销, 释放标的给自动管线
                    try:
                        manual_hold_registry.unregister(symbol, account="real")
                    except Exception as _unreg_err:
                        logger.warning(f"[手动平仓] ManualHoldRegistry 注销失败 (不影响平仓): {_unreg_err}")

                # P1-6: 取消保护性订单组 (SL + TP)
                # DEB-20260904 #201 (S5): 原 unconditional 调用 — 部分平仓也撤掉 SL/TP
                # 且不重建 → 剩余仓位在 broker 端裸奔. 改: 仅全平撤单; 部分平仓按剩余量重建.
                if _effective_full:
                    try:
                        cancel_order_group(service, symbol)
                    except Exception:
                        pass
                else:
                    _remain_qty = int(total_qty) - _record_qty
                    if _remain_qty > 0:
                        try:
                            cancel_order_group(service, symbol)
                            _rebuild_protective_after_partial(
                                service, symbol, position_dir, _remain_qty,
                                _protection_snapshot,
                            )
                        except Exception as _rp_err:
                            logger.warning(
                                f"[手动平仓] {symbol} 部分平仓后保护单重建失败 "
                                f"(剩余 {_remain_qty} 股无 broker 端 SL/TP): {_rp_err}"
                            )

                # PDT 日内交易记录: 平仓时判断是否 round trip (P0 2026-06-11)
                try:
                    from app.risk.pdt_guard import get_pdt_guard
                    get_pdt_guard().record_close(symbol, row.get("open_date", row.get("entry_time")))
                except Exception as _pdt_err:
                    logger.debug(f"[手动平仓] PDT 记录失败 (不影响平仓): {_pdt_err}")

                # 2026-06-10: 写入 trade_records 形成手动交易闭环
                # P1-6 (2026-06-12): 优先用 broker dealt_avg_price 精确回填 exit_price
                # P1-8 (2026-08-16): 成交状态已在 RET_OK 后前置判定 (_filled_qty>0 必到此处),
                # 此处仅解析 exit_price; 记账数量 = 实际成交数量 _record_qty
                try:
                    _exit_px = float(_precise_exit) if (_precise_exit and _precise_exit > 0) else 0.0
                    if _exit_px <= 0:
                        _exit_px = service._calc_exit_price(row, raw_qty)
                        if _exit_px <= 0:
                            # 最后兜底: 用 Futu 实时报价
                            try:
                                ret, snap = service._broker_ctx._quote_ctx.get_market_snapshot([symbol])
                                if ret == RET_OK and not snap.empty:
                                    _exit_px = float(snap.iloc[0].get("cur_price", 0) or 0)
                            except Exception:
                                pass
                        # ── P0 (2026-06-16): 终极兜底 — 用订单限价 ──
                        # 根因: NVDA 手动平仓 exit_price=0 → 假亏损 $6155 → 触发日熔断
                        # 当 broker 成交回报、position 行、Futu snapshot 全部失败时,
                        # 用户设定的限价是最接近实际成交价的合理代理。
                        if _exit_px <= 0 and order_price and order_price > 0:
                            _exit_px = float(order_price)
                            logger.warning(
                                f"[手动平仓] {symbol} 所有成交价查询失败, "
                                f"使用订单限价 ${order_price:.2f} 作为 exit_price"
                            )
                    # ADR-007: 读取入场理由 (thesis/rationale) 供平仓时写入 entry_reason
                    _entry_reason_text = ""
                    try:
                        _conn_er = service._db()
                        _row_er = _conn_er.execute(
                            "SELECT thesis, entry_context_json FROM open_positions WHERE symbol=?",
                            (symbol,),
                        ).fetchone()
                        if _row_er:
                            _entry_reason_text = _row_er[0] or ""  # thesis text
                            if _row_er[1]:
                                import json as _j2
                                try:
                                    _ec = _j2.loads(_row_er[1])
                                    _plan_rationale = (_ec.get("plan", {}).get("rationale", "") or "")
                                    if _plan_rationale and not _entry_reason_text:
                                        _entry_reason_text = _plan_rationale[:300]
                                except Exception:
                                    pass
                        _conn_er.close()
                    except Exception:
                        pass

                    service._record_manual_close(
                        symbol=symbol,
                        direction=position_dir,
                        entry_price=float(row.get("cost_price", 0)),
                        exit_price=_exit_px,
                        quantity=_record_qty,
                        reason=f"[{close_type}] {reason}",
                        order_id=order.order_id,
                        entry_reason=_entry_reason_text,
                    )
                    # 自动生成交易归因
                    try:
                        from app.analysis.trade_attribution import compute_attribution
                        attr = compute_attribution(
                            symbol=symbol, direction=position_dir,
                            entry_price=float(row.get("cost_price", 0)),
                            exit_price=_exit_px, quantity=_record_qty,
                            entry_time=row.get("entry_time", row.get("updated_at", "")),
                            exit_time=datetime.now().isoformat(),
                        )
                        logger.info(f"[归因] {symbol} {position_dir}: {attr.summary} | {attr.improvement}")
                        # 写入归因 + thesis 到 trade_records.metadata_json
                        conn = service._db()
                        try:
                            import json as _json
                            # 读取入场 thesis + entry_time (open_positions 行尚在, sync 还未清理)
                            _thesis_data = None
                            _entry_time_str = ""
                            _entry_ctx = None
                            try:
                                _trow = conn.execute(
                                    "SELECT thesis, entry_context_json, entry_time FROM open_positions WHERE symbol=?",
                                    (symbol,),
                                ).fetchone()
                                if _trow:
                                    _entry_ctx = _json.loads(_trow[1]) if _trow[1] else None
                                    _thesis_data = {
                                        "thesis_text": _trow[0] or "",
                                        "entry_context": _entry_ctx,
                                    }
                                    _entry_time_str = _trow[2] or ""
                            except Exception:
                                pass

                            # 构建 exit context
                            _exit_ctx = service._build_exit_context(symbol, _entry_time_str, _entry_ctx)

                            meta = {"attribution": attr.to_dict()}
                            if _thesis_data:
                                meta["thesis"] = _thesis_data
                            meta["exit_context"] = _exit_ctx
                            meta_json = _json.dumps(meta, ensure_ascii=False)
                            # P2-15 修复 (2026-08-16, DEB-fullchain): SQLite 不支持
                            # UPDATE...ORDER BY...LIMIT — 旧语句必抛 syntax error 且被
                            # debug 级静默吞 → 归因 metadata 从不落库。改为普通 UPDATE,
                            # 仅更新尚无 metadata 的行 (幂等, 不覆盖已写行)。
                            conn.execute(
                                "UPDATE trade_records SET metadata_json=? "
                                "WHERE close_futu_order_id=? "
                                "AND (metadata_json IS NULL OR metadata_json='')",
                                (meta_json, order.order_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                        # 推送归因报告到飞书
                        try:
                            service._push_attribution_to_feishu(attr.to_dict())
                        except Exception as _push_err:
                            logger.debug(f"[归因] 飞书推送失败 (不影响主流程): {_push_err}")
                    except Exception as _attr_err:
                        logger.debug(f"[归因] 生成失败 (不影响平仓): {_attr_err}")
                except Exception as _rec_err:
                    logger.warning(f"[手动平仓] trade_records 写入失败 (不影响平仓): {_rec_err}")
                # P1-7 修复: 平仓尝试结束即释放竞态锁 (部分平仓成功后也必须释放,
                # 否则 _pending_close 残留 → 组合级止损永久跳过该标的)
                # DEB-20260904 #202: 全平路径同样无条件释放 — 旧逻辑全平的锁释放依赖
                # position_store.remove 成功 (其异常被 debug 吞), remove 失败 → 锁残留.
                # _release_close_locks 对已被 remove 的 symbol 是 no-op, 调用安全.
                _release_close_locks(symbol)
            else:
                order.status = "error"
                order.error_msg = str(data)
                # 平仓失败 — 释放竞态锁，允许 ExitMonitor 重试 (P1-7: 统一走 helper)
                _release_close_locks(symbol)

            service._write_audit(order)
            return {
                "success": order.status == "submitted",
                "order": asdict(order),
            }

        except Exception as e:
            logger.error(f"[手动平仓] 异常: {e}")
            # DEB-20260903 #117: 外层兜底释放竞态锁 — 原未释放致
            # _close_in_progress/_pending_close 永久残留,
            # 退出监控 `if _meta.get('_pending_close'): continue` 永久跳过该标的
            _release_close_locks(symbol)
            return {"success": False, "error": "操作失败，请稍后重试"}


# ─────────────────────────────────────────────────────────────
# 订单查询 / 撤单
# ─────────────────────────────────────────────────────────────

def get_today_orders_with_status(service: "ManualOrderService") -> Dict[str, Any]:
    """查询今日订单并显式返回来源, 避免 DB fallback 冒充券商实时状态.

    Returns:
        {"orders": list, "source": "broker"|"audit_db"|"none",
         "broker_ok": bool, "error": str|None, "as_of": ISO-8601 ET}
    """
    orders: List[Dict[str, Any]] = []
    broker_ok = False
    source = "none"
    query_error: Optional[str] = None

    # ── 1. 尝试 Futu broker 查询 ──
    try:
        if not service.connect():
            query_error = "broker_not_connected"
        else:
            from futu import TrdEnv, RET_OK
            trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
            if service._is_live():
                acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
            else:
                acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))

            ret, data = service._broker_ctx.order_list_query(
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret == RET_OK:
                # RET_OK + 空表也是券商权威应答: 来源仍是 broker, 不是 unknown。
                broker_ok = True
                source = "broker"
            else:
                query_error = f"query_failed: {data}"
            if ret == RET_OK and not data.empty:
                # Use ET timezone for today filter (Futu returns ET timestamps)
                from app.utils.timezone import get_trade_date
                today_et = get_trade_date()
                for _, row in data.iterrows():
                    # 综合账户返回全市场订单, 美股页只展示 US. (港股见 /trade/hk)
                    if not str(row.get("code", "")).startswith("US."):
                        continue
                    create_time = str(row.get("create_time", ""))
                    # Extract date part from "2026-06-17 23:36:13.188"
                    order_date = create_time.split(" ")[0] if create_time else ""
                    if order_date != today_et:
                        continue
                    _st = str(row.get("order_status", "")).upper()
                    _is_filled = "FILLED" in _st
                    orders.append({
                        "order_id": str(row.get("order_id", "")),
                        "code": str(row.get("code", "")),
                        "qty": int(row.get("qty", 0)),
                        "price": float(row.get("price", 0)),
                        "order_type": str(row.get("order_type", "")),
                        "order_status": str(row.get("order_status", "")),
                        "trd_side": str(row.get("trd_side", "")),
                        "create_time": create_time,
                        "update_time": str(row.get("update_time", "")),
                        # DEB-20260903 #118: 实际成交量取 dealt_qty —
                        # qty 是委托量, 部分成交时高估 filled_qty 误导对账
                        "filled_qty": int(row.get("dealt_qty", 0) or 0) if _is_filled else 0,
                    })
    except Exception as e:
        query_error = f"exception: {e}"
        logger.warning(f"[订单查询] Broker 查询异常, 降级到审计 DB: {e}")

    # ── 2. Broker 为空时 fallback 到本地审计 DB ──
    if not orders:
        try:
            import sqlite3
            # RFC-078 (2026-09-08): 看板专属库路由 — 今日**实盘手动**订单的 DB fallback,
            # 原 get_main_db 在 TRADE_ENV=1 下读 sim 库的 manual_order_audit。
            from app.core.db_utils import get_dashboard_db
            db_path = get_dashboard_db()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            # DEB-20260721 E-1: "今日"按 ET 交易日过滤 (原北京 LIKE 盘中差一天).
            # timestamp 写入为 naive 北京; ET 日 D 对应北京区间 [ET午夜→北京, ET次日午夜→北京).
            # DEB-20260904 #203: 硬编码 ±12h 在冬令时 (EST, 差 13h) 边界偏移 1 小时 —
            # 改用真实时区换算: ET 当日 00:00 与次日 00:00 localize 后转北京, 自动处理 DST.
            from app.utils.timezone import now_et as _now_et, ET as _ET, CST as _CST, _localize
            from datetime import datetime as _dt, timedelta as _td, time as _time
            _et_today = _now_et().date()

            def _et_midnight_to_bj(d) -> str:
                return _localize(_dt.combine(d, _time.min), _ET).astimezone(_CST).strftime("%Y-%m-%dT%H:%M:%S")

            _lo = _et_midnight_to_bj(_et_today)
            _hi = _et_midnight_to_bj(_et_today + _td(days=1))
            rows = conn.execute(
                "SELECT * FROM manual_order_audit "
                "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC LIMIT 50",
                (_lo, _hi),
            ).fetchall()
            conn.close()
            for r in rows:
                r = dict(r)
                _status = (r.get("status") or "submitted").upper()
                _is_filled = "FILLED" in _status
                orders.append({
                    "order_id": str(r.get("order_id", "")),
                    "code": str(r.get("symbol", "")),
                    "qty": int(r.get("quantity", 0)),
                    "price": float(r.get("price") or 0),
                    "order_type": str(r.get("order_type", "LIMIT")),
                    "order_status": _status,
                    "trd_side": str(r.get("action", "BUY")),
                    "create_time": (r.get("timestamp") or "").replace("T", " ")[:19],
                    "update_time": "",
                    "filled_qty": 0,
                })
            if orders:
                source = "audit_db"
            logger.info(f"[订单查询] Broker 无数据, 从审计 DB 加载 {len(orders)} 条")
        except Exception as e:
            if not orders:
                query_error = query_error or f"audit_db_failed: {e}"
            logger.warning(f"[订单查询] 审计 DB fallback 失败: {e}")

    # 按创建时间倒序
    orders.sort(key=lambda x: x["create_time"], reverse=True)
    try:
        from app.utils.timezone import now_et
        as_of = now_et().isoformat(timespec="seconds")
    except Exception:
        as_of = datetime.now().isoformat(timespec="seconds")
    return {
        "orders": orders,
        "source": source,
        "broker_ok": broker_ok,
        "error": query_error,
        "as_of": as_of,
    }


def get_today_orders(service: "ManualOrderService") -> List[Dict[str, Any]]:
    """兼容旧签名: 只返回订单列表。"""
    return get_today_orders_with_status(service)["orders"]


def cancel_order(service: "ManualOrderService", order_id: str) -> Dict[str, Any]:
    """撤销指定订单."""
    if not service.connect():
        return {"success": False, "error": "broker_connection_failed"}

    try:
        from futu import TrdEnv, RET_OK, ModifyOrderOp
        trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
        if service._is_live():
            acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
        else:
            acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))

        ret, data = service._broker_ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=trd_env,
            acc_id=acc_id,
        )

        if ret == RET_OK:
            logger.info(f"[撤单] ✅ {order_id} 撤销成功")
            # 更新审计 DB 状态, 防止 DB fallback 仍显示 submitted
            try:
                import sqlite3
                # RFC-078 (2026-09-08): 看板专属库路由 — 撤单状态回写必须与下单审计同载体,
                # 否则写进 sim 库而实盘审计在 live 库 ⇒ DB fallback 仍显示 submitted。
                from app.core.db_utils import get_dashboard_db
                conn = sqlite3.connect(str(get_dashboard_db()))
                conn.execute(
                    "UPDATE manual_order_audit SET status='cancelled' WHERE order_id=?",
                    (order_id,),
                )
                conn.commit()
                conn.close()
            except Exception as _ae:
                logger.warning(f"[撤单] 审计 DB 更新失败 (非致命): {_ae}")
            # Task #11: WebSocket 推送撤单事件
            try:
                from app.web_dashboard.routes.ws import publish_event_sync
                publish_event_sync("order_update", {
                    "order_id": order_id, "status": "cancelled",
                })
            except ImportError:
                pass
            except Exception:
                pass
            return {"success": True, "message": f"订单 {order_id} 已撤销"}
        else:
            logger.error(f"[撤单] ❌ {order_id} 撤销失败: {data}")
            return {"success": False, "error": "订单撤销失败"}

    except Exception as e:
        logger.error(f"[撤单] 异常: {e}")
        return {"success": False, "error": "操作失败，请稍后重试"}


def cancel_broker_order(service: "ManualOrderService", order_id: str) -> bool:
    """内部辅助: 撤销 broker 订单, 返回是否成功 (用于 SL 单替换)."""
    try:
        from futu import TrdEnv, RET_OK, ModifyOrderOp
        trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
        if service._is_live():
            acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
        else:
            acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))
        ret, _ = service._broker_ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id, qty=0, price=0,
            trd_env=trd_env, acc_id=acc_id,
        )
        return ret == RET_OK
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# LEGACY 持仓托管
# ─────────────────────────────────────────────────────────────

def takeover_position(service: "ManualOrderService", symbol: str,
                      owner: str = "manual") -> Dict[str, Any]:
    """P1-2 (2026-06-12): import broker LEGACY position into open_positions."""
    if not symbol or not symbol.startswith("US."):
        return {"success": False, "error": "invalid symbol"}
    if owner not in ("manual", "auto"):
        owner = "manual"
    try:
        service.connect()
        if service._broker_ctx is None:
            return {"success": False, "error": "broker not connected"}
        trd_env, acc_id = service._trd_env(), service._acc_id()
        ret, positions = service._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
        if ret != 0:
            return {"success": False, "error": f"position query failed: {positions}"}
        pos = positions[positions["code"] == symbol]
        if pos.empty:
            return {"success": False, "error": f"no broker position for {symbol}"}
        row = pos.iloc[0]
        qty = float(row.get("qty", 0))
        if qty == 0:
            return {"success": False, "error": f"{symbol} qty=0"}
        cost = float(row.get("cost_price", 0)) or float(row.get("nominal_price", 0))
        side = str(row.get("position_side", "")).upper()
        direction = "SHORT" if (qty < 0 or side == "SHORT") else "LONG"
        qty_abs = abs(qty)
    except Exception as e:
        return {"success": False, "error": f"read broker pos failed: {e}"}
    try:
        with service._db() as conn:
            exist = conn.execute(
                "SELECT symbol, owner FROM open_positions WHERE symbol=?",
                (symbol,),
            ).fetchone()
            if exist:
                # 撞 SIM 收集仓时把话说清楚: 不是含糊的"已存在", 而是**两 env 抢同一 PK**
                if is_simulate_row(conn, symbol):
                    logger.critical(
                        f"[EnvIsolation] [Takeover] 拒绝接管 {symbol}: 该标的由 SIM 数据收集仓持有 "
                        f"(account_type='simulate'; PK=symbol ⇒ 实盘仓无法与其共存)。"
                        f"未改写任何 sim 行 — 库级分离见 DEB-20260905 §13.15。"
                    )
                    return {
                        "success": False,
                        "error": f"{symbol} 是 SIM 数据收集仓, 看板(实盘台)不接管 — DEB-20260905 §13.15",
                    }
                return {"success": False, "error": f"{symbol} already in DB (owner={exist[1]})"}
            now = datetime.now().isoformat()
            conn.execute(SQL_TAKEOVER, (symbol, direction, qty_abs, cost, owner, now))
            conn.commit()
    except Exception as e:
        return {"success": False, "error": f"DB write failed: {e}"}
    logger.warning(
        f"[Takeover] {symbol} imported dir={direction} qty={qty_abs} cost={cost} owner={owner}"
    )
    return {
        "success": True, "symbol": symbol, "direction": direction,
        "quantity": qty_abs, "cost_price": cost, "owner": owner,
        "message": f"{symbol} imported ({owner})",
    }


# ─────────────────────────────────────────────────────────────
# 止损单管理
# ─────────────────────────────────────────────────────────────

def modify_stop_loss(service: "ManualOrderService", symbol: str,
                     new_sl: float) -> Dict[str, Any]:
    """修改持仓止损 — 撤销旧 STOP 单 + 下新 STOP 单 + 更新 DB.

    关键: 之前 routes/trade.py 仅 UPDATE DB current_sl, 不下 broker 单 →
    程序退出后止损不存在, 实盘风险极高。
    模拟盘: Futu SIMULATE 不支持 STOP, 仅更新 DB (graceful degrade)。
    """
    if new_sl <= 0:
        return {"success": False, "error": "止损价必须 > 0"}

    # 1. 确保 DB 列存在
    service._ensure_sl_column()

    # 2. 查询当前持仓 (从 broker 拿 size + direction)
    if not service.connect():
        return {"success": False, "error": "broker_connection_failed"}

    try:
        from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, TimeInForce, RET_OK

        trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
        if service._is_live():
            acc_id = int(service._read_env("REAL_ACCOUNT_ID", "0"))
        else:
            acc_id = int(service._read_env("SIMULATE_ACCOUNT_ID", "0"))

        ret, positions = service._broker_ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
        if ret != RET_OK:
            return {"success": False, "error": f"持仓查询失败: {positions}"}

        pos = positions[positions["code"] == symbol]
        if pos.empty:
            return {"success": False, "error": f"未找到 {symbol} 的持仓"}

        row = pos.iloc[0]
        raw_qty = float(row.get("qty", 0))
        if raw_qty == 0:
            return {"success": False, "error": f"{symbol} 持仓为 0"}

        side = str(row.get("position_side", "")).upper()
        is_long = not ((raw_qty < 0) or (side == "SHORT"))
        direction = "LONG" if is_long else "SHORT"
        qty = int(abs(raw_qty))

        # 3. 查 DB 取旧 sl_broker_order_id
        old_sl_oid = None
        try:
            conn = service._db()
            cur = conn.execute(
                "SELECT sl_broker_order_id FROM open_positions WHERE symbol=?",
                (symbol,),
            ).fetchone()
            if cur and cur[0]:
                old_sl_oid = str(cur[0])
            conn.close()
        except Exception:
            pass

        # 4. 撤旧 STOP 单 (如果存在)
        if old_sl_oid:
            cancelled = service._cancel_broker_order(old_sl_oid)
            logger.info(
                f"[止损·替换] {symbol} 旧 STOP 单 {old_sl_oid} "
                f"撤销{'成功' if cancelled else '失败 (可能已成交/不存在)'}"
            )
            if not cancelled:
                # P1-9 修复 (2026-08-16, DEB-fullchain): 撤单失败不得盲下新 STOP —
                # 旧单若已成交 → 新 STOP 以旧 qty 挂出 = 触发时开出裸空头;
                # 若仅网络失败 → 同仓位两张 STOP = 双倍平仓+反手。
                # 复查旧单状态, 仅确认已撤才继续; 未知状态一律中止 (fail-safe)。
                _old_status = ""
                try:
                    _r3, _d3 = service._broker_ctx.order_list_query(
                        order_id=old_sl_oid, trd_env=trd_env, acc_id=acc_id
                    )
                    if _r3 == RET_OK and not _d3.empty:
                        _old_status = str(_d3.iloc[0].get("order_status", "") or "")
                except Exception:
                    pass
                if "CANCEL" not in _old_status.upper():
                    logger.error(
                        f"[止损·替换] {symbol} 旧 STOP {old_sl_oid} 撤单失败且状态="
                        f"{_old_status or 'unknown'} — 中止新 STOP 下单 (防裸空头/双 STOP); "
                        f"若旧单已成交请先核对持仓"
                    )
                    return {
                        "success": False,
                        "error": f"旧止损单撤单失败 (status={_old_status or 'unknown'}), "
                                 f"已中止新止损单; 请人工确认旧单状态后重试",
                    }

        # 5. 下新 STOP 单
        new_oid = None
        sl_status = "db_only"  # 默认状态 (若 broker 失败)
        if trd_env == TrdEnv.SIMULATE:
            logger.info(f"[止损] {symbol} 模拟盘不支持 STOP, 仅更新 DB")
            sl_status = "simulate_db_only"
        else:
            # LONG → SELL STOP, SHORT → BUY STOP
            trd_side = TrdSide.SELL if is_long else TrdSide.BUY
            try:
                ret2, data2 = service._broker_ctx.place_order(
                    price=0,  # STOP 单触发后市价成交
                    qty=qty, code=symbol,
                    trd_side=trd_side,
                    order_type=FutuOrderType.STOP,
                    trd_env=trd_env, acc_id=acc_id,
                    time_in_force=TimeInForce.GTC,
                    aux_price=new_sl,  # 触发价 (Futu STOP 单必须用 aux_price)
                    trail_spread=0,  # 非追踪止损
                )
                if ret2 == RET_OK and not data2.empty:
                    new_oid = str(data2.iloc[0].get("order_id", ""))
                    sl_status = "broker_attached"
                    logger.info(
                        f"[止损] ✅ {symbol} {direction} STOP@${new_sl} qty={qty} → {new_oid}"
                    )
                else:
                    logger.error(f"[止损] {symbol} STOP 单失败 (DB 仍更新): {data2}")
            except Exception as e:
                logger.error(f"[止损] {symbol} 下 STOP 单异常 (DB 仍更新): {e}")

        # DEB-20260903 #119: 实盘 STOP 未挂出即返回失败 — 原仅记日志仍走
        # "更新 DB"+success=True, 用户以为已改止损实际 broker 端无保护, 静默裸奔
        if trd_env != TrdEnv.SIMULATE and not new_oid:
            try:
                from app.push import get_push_hub, PushMessage, Level, Category
                get_push_hub().push(PushMessage(
                    title=f"⚠️ 止损挂单失败 ({symbol})",
                    body=(
                        f"📌 标的: {symbol} {direction}\n"
                        f"📉 新止损: ${new_sl}\n"
                        f"❌ 实盘 STOP 单提交失败, 仓位无 broker 端止损保护\n"
                        f"⚠️ 请人工检查并重挂"
                    ),
                    level=Level.CRITICAL,
                    category=Category.RISK_ALERT,
                    dedup_key=f"sl_place_fail:{symbol}",
                ))
            except Exception as _pe:
                logger.warning(f"[止损] {symbol} 挂单失败的 CRITICAL 推送发送失败: {_pe}")
            return {
                "success": False,
                "symbol": symbol,
                "new_sl": new_sl,
                "broker_order_id": None,
                "status": "broker_failed",
                "error": "实盘 STOP 单提交失败, DB 未更新, 仓位无止损保护",
            }

        # 6. 更新 DB
        try:
            conn = service._db()
            scoped_update(
                conn,
                "UPDATE open_positions SET current_sl=?, sl_broker_order_id=?, updated_at=? WHERE symbol=?",
                (new_sl, new_oid, datetime.now().isoformat(), symbol),
                symbol, "手动改止损 SL 落库",
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"[止损] DB 更新失败: {e}")

        return {
            "success": True,
            "symbol": symbol,
            "new_sl": new_sl,
            "broker_order_id": new_oid,
            "status": sl_status,
        }

    except Exception as e:
        logger.error(f"[止损] modify_stop_loss 异常: {e}")
        return {"success": False, "error": "操作失败，请稍后重试"}


# ─────────────────────────────────────────────────────────────
# 价格/订单成交辅助
# ─────────────────────────────────────────────────────────────

def calc_exit_price(row: dict, raw_qty: float) -> float:
    """从持仓行估算平仓价格 (近似值, 精确值需从 Futu 成交回报获取)."""
    if raw_qty == 0:
        return 0.0
    cur = float(row.get("cur_price", 0) or 0)
    if cur > 0:
        return cur
    val = float(row.get("val_price", 0) or 0)
    if val > 0:
        return val / abs(raw_qty)
    return 0.0


def fetch_dealt_avg_price(service: "ManualOrderService", order_id: str,
                          trd_env, acc_id: int,
                          retries: int = 2, delay: float = 0.4):
    """P1-6: 用 broker 成交回报获取精确平仓价 (dealt_avg_price).

    市价单一般立即成交,限价单可能挂单中.
    - 仅当 dealt_qty>0 且 dealt_avg_price>0 时返回精确值
    - 否则返回 None,调用方回退 calc_exit_price 估算
    """
    if not order_id:
        return None
    try:
        from futu import RET_OK
    except Exception:
        return None
    for i in range(max(1, retries)):
        try:
            ret, data = service._broker_ctx.order_list_query(
                order_id=str(order_id),
                trd_env=trd_env, acc_id=acc_id,
            )
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                dealt_qty = float(row.get("dealt_qty", 0) or 0)
                dealt_avg = float(row.get("dealt_avg_price", 0) or 0)
                if dealt_qty > 0 and dealt_avg > 0:
                    logger.info(
                        f"[exit_price] order={order_id} dealt_qty={dealt_qty} "
                        f"dealt_avg_price={dealt_avg}"
                    )
                    return dealt_avg
        except Exception as _e:
            logger.debug(f"[exit_price] query attempt {i+1} failed: {_e}")
        if i + 1 < retries:
            try:
                import time as _t
                _t.sleep(delay)
            except Exception:
                pass
    return None


# ─────────────────────────────────────────────────────────────
# 延迟保护挂单 (等开仓成交后挂 SL/TP)
# ─────────────────────────────────────────────────────────────

def _register_pending_protection(service: "ManualOrderService", order: "ManualOrder") -> None:
    """注册延迟保护挂单: 开仓单可能未成交 (GTC 排队), 等成交后再挂 SL/TP."""
    global _protection_monitor_started

    order_id = order.order_id
    if not order_id:
        logger.warning(f"[延迟挂单] {order.symbol} 无 order_id, 跳过")
        return

    # 只有限价单才需要延迟 (市价单通常秒级成交)
    if order.order_type != "LIMIT":
        # 市价单: 延迟 3 秒后回填成交价再挂 (P0-2 修复, 见 _try_place_protection)
        import threading
        threading.Timer(3.0, _try_place_protection, args=[service, order]).start()
        return

    with _protections_lock:
        _pending_protections[order_id] = {
            "order": order,
            "service": service,
            "registered_at": datetime.now(),
        }
    logger.info(
        f"[延迟挂单] ✅ 注册 {order.symbol} {order.direction} "
        f"order_id={order_id} SL=${order.stop_loss} TP=${order.take_profit}"
    )
    _ensure_monitor_started()


def _ensure_monitor_started() -> None:
    """启动保护单监控线程 (只启动一次)."""
    global _protection_monitor_started
    if not _protection_monitor_started:
        import threading
        _protection_monitor_started = True
        t = threading.Thread(target=_protection_monitor_loop, daemon=True, name="protection-monitor")
        t.start()
        logger.info("[延迟挂单] 监控线程已启动 (30s 轮询)")


def _backfill_fill_price(service: "ManualOrderService", order: "ManualOrder") -> bool:
    """查询 broker 订单成交均价/数量并回填 order 对象. 返回 True = 已成交且拿到价格.

    P0-2 修复 (2026-08-16, DEB-fullchain): 与限价单 monitor loop 同源回填
    (dealt_avg_price/dealt_qty)。市价单提交时 price=0, 不回填则 place_protective_stop
    静默丢弃 SL/TP → 仓位裸奔。
    """
    try:
        if not order.order_id or not service._broker_ctx or not service._connected:
            return False
        from futu import TrdEnv, RET_OK
        trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
        ret, data = service._broker_ctx.order_list_query(
            order_id=order.order_id,
            trd_env=trd_env,
        )
        if ret != RET_OK or data.empty:
            return False
        row = data.iloc[0]
        dealt_qty = int(row.get("dealt_qty", 0) or 0)
        dealt_avg = float(row.get("dealt_avg_price", 0) or 0)
        if dealt_qty > 0:
            order.quantity = dealt_qty
        if dealt_avg > 0:
            order.price = dealt_avg
            return True
        return False
    except Exception as e:
        logger.warning(f"[延迟挂单] {order.symbol} 查询成交价失败: {e}")
        return False


def _try_place_protection(service: "ManualOrderService", order: "ManualOrder") -> None:
    """尝试挂保护单 (市价单延迟调用).

    P0-2 修复 (2026-08-16, DEB-fullchain): 市价单 price=0, 旧实现 3s 后直接调
    place_protective_stop → _entry<=0 静默 return → SL/TP 永久丢失。
    现: 先查 broker 成交价回填; 未成交/无价 → 转入 monitor 队列重试 (30s 轮询,
    24h 超时保护已有) — 保护单绝不静默丢失。
    """
    try:
        if _backfill_fill_price(service, order):
            place_protective_stop(service, order)
            return
        # 未成交/无成交价 → 转入 monitor 队列重试
        if order.order_id:
            with _protections_lock:
                _pending_protections[order.order_id] = {
                    "order": order, "service": service, "registered_at": datetime.now(),
                }
            _ensure_monitor_started()
            logger.warning(
                f"[延迟挂单] {order.symbol} 市价单 3s 未成交, 转入监控队列等待成交后挂 SL/TP "
                f"(order_id={order.order_id})"
            )
        else:
            logger.error(
                f"[延迟挂单] ⚠️ {order.symbol} 市价单无 order_id, 无法挂保护单 — "
                f"仓位无 SL/TP 保护, 需人工检查!"
            )
    except Exception as e:
        logger.warning(f"[延迟挂单] {order.symbol} 市价单保护挂单失败: {e}")


def _protection_monitor_loop() -> None:
    """后台线程: 每 30s 检查待处理保护挂单, 开仓成交后立即挂 SL/TP."""
    while True:
        time.sleep(30)
        # DEB-20260903 #115: 加锁读取 — 防与下单线程写竞态
        with _protections_lock:
            if not _pending_protections:
                continue

            # 复制一份, 避免迭代时修改
            pending = list(_pending_protections.items())
        for order_id, entry in pending:
            order = entry["order"]
            service = entry["service"]
            registered_at = entry["registered_at"]

            # 超时: 超过 24 小时自动放弃
            if (datetime.now() - registered_at).total_seconds() > 86400:
                logger.warning(f"[延迟挂单] {order.symbol} 超时 24h, 移除")
                with _protections_lock:
                    _pending_protections.pop(order_id, None)
                continue

            try:
                # 检查开仓单状态
                if not service._broker_ctx or not service._connected:
                    if not service.connect():
                        continue

                from futu import TrdEnv, RET_OK
                trd_env = TrdEnv.REAL if service._is_live() else TrdEnv.SIMULATE
                ret, data = service._broker_ctx.order_list_query(
                    order_id=order_id,
                    trd_env=trd_env,
                )
                if ret != RET_OK or data.empty:
                    continue

                status = str(data.iloc[0].get("order_status", ""))
                dealt_qty = int(data.iloc[0].get("dealt_qty", 0))

                if "FILLED" in status.upper() or dealt_qty > 0:
                    # 开仓单已成交! 立即挂 SL/TP
                    total_qty = int(data.iloc[0].get("qty", 0)) or int(order.quantity)
                    _is_partial = (dealt_qty > 0 and dealt_qty < total_qty
                                   and "FILLED_ALL" not in status.upper())
                    logger.info(
                        f"[延迟挂单] 🎯 {order.symbol} 开仓单已成交 "
                        f"(status={status}, dealt={dealt_qty}/{total_qty}), 挂 SL/TP..."
                    )
                    # 用实际成交数量
                    if dealt_qty > 0:
                        order.quantity = dealt_qty
                    # 用实际成交均价 (如果有)
                    dealt_avg = float(data.iloc[0].get("dealt_avg_price", 0))
                    if dealt_avg > 0:
                        order.price = dealt_avg

                    place_protective_stop(service, order)

                    # DEB-20260903 #120: 部分成交时 SL/TP 已保护已成交部分,
                    # 剩余数量需继续跟踪, 不移出队列
                    if _is_partial:
                        order.quantity = total_qty - dealt_qty
                        logger.info(
                            f"[延迟挂单] ⏳ {order.symbol} 部分成交 "
                            f"({dealt_qty}/{total_qty}), SL/TP 已保护 {dealt_qty} 股, "
                            f"剩余 {order.quantity} 股继续跟踪"
                        )
                    else:
                        with _protections_lock:
                            # DEB-20260903 #121: place_protective_stop 内部可能因 TP
                            # 未确认重新入队 (registered_at 换新), 仅当 entry 未被替换
                            # 时才移除, 防误删重试注册
                            _cur = _pending_protections.get(order_id)
                            if _cur is not None and _cur.get("registered_at") is registered_at:
                                _pending_protections.pop(order_id, None)
                                logger.info(f"[延迟挂单] ✅ {order.symbol} SL/TP 已挂, 从队列移除")

                elif "CANCEL" in status.upper() or "FAIL" in status.upper() or "DEL" in status.upper():
                    # 开仓单被取消/失败, 移除
                    logger.info(f"[延迟挂单] {order.symbol} 开仓单 {status}, 移除")
                    with _protections_lock:
                        _pending_protections.pop(order_id, None)

                # 其他状态 (SUBMITTED, WAITING 等): 继续等待

            except Exception as e:
                logger.warning(f"[延迟挂单] {order.symbol} 检查异常: {e}")


# ─────────────────────────────────────────────────────────────
# 保护性止损 (开仓成交后挂 STOP + LIMIT)
# ─────────────────────────────────────────────────────────────

def place_protective_stop(service: "ManualOrderService", order: "ManualOrder") -> None:
    """P1-6: 手动开仓后自动挂保护性 STOP 单 + 止盈 LIMIT 单 (broker 端).
    
    实盘环境：
    - 止损：STOP 单（市价止损）
    - 止盈：LIMIT 单（限价止盈）
    - 订单组管理：SL/TP 成交时自动取消另一个（模拟 OCO）
    
    模拟盘环境：
    - Futu 不支持 STOP 单，改为在 DB 中记录 SL/TP
    - 由 ExitMonitor 轮询检查价格并触发平仓
    """
    try:
        if not service._broker_ctx or not service._connected:
            return
        if order.direction not in ("LONG", "SHORT"):
            return

        _entry = float(order.price or 0)
        _qty = int(order.quantity or 0)
        if _entry <= 0 or _qty <= 0:
            # P0-2 修复 (2026-08-16): 原静默 return 导致市价单 SL/TP 丢失无任何痕迹。
            # _backfill_fill_price 已保证正常路径有成交价; 走到这里=回填失败/数据异常,
            # 显性告警, 仓位保护缺失必须可见。
            logger.error(
                f"[保护挂单] ⚠️ {order.symbol} 无有效成交价/数量 (entry={_entry}, qty={_qty}) — "
                f"SL/TP 未挂出, 仓位无 broker 端保护, 需人工检查!"
            )
            return

        # ── 止损价: 优先用作战计划的 SL，fallback 3% ──
        _sl_price = float(order.stop_loss or 0)
        _sl_pct_limit_min = 0.01  # SL 至少距入场 1%
        _sl_pct_limit_max = 0.15  # SL 最远距入场 15%
        _sl_source = "fallback"
        
        if _sl_price > 0:
            # 校验: SL 方向和距离是否合理
            if order.direction == "LONG" and _sl_price >= _entry:
                logger.warning(f"[保护止损] {order.symbol} SL ${_sl_price} >= 入场 ${_entry}, 方向错误, fallback 3%")
                _sl_price = 0
            elif order.direction == "SHORT" and _sl_price <= _entry:
                logger.warning(f"[保护止损] {order.symbol} SL ${_sl_price} <= 入场 ${_entry}, 方向错误, fallback 3%")
                _sl_price = 0
            else:
                sl_dist = abs(_entry - _sl_price) / _entry
                if sl_dist < _sl_pct_limit_min:
                    logger.warning(f"[保护止损] {order.symbol} SL 距入场仅 {sl_dist:.1%} (<{_sl_pct_limit_min:.0%}), 自动放宽到 {_sl_pct_limit_min:.0%}")
                    _sl_price = round(_entry * (1 - _sl_pct_limit_min), 2) if order.direction == "LONG" else round(_entry * (1 + _sl_pct_limit_min), 2)
                elif sl_dist > _sl_pct_limit_max:
                    logger.warning(f"[保护止损] {order.symbol} SL 距入场 {sl_dist:.1%} (>{_sl_pct_limit_max:.0%}), 自动收紧到 {_sl_pct_limit_max:.0%}")
                    _sl_price = round(_entry * (1 - _sl_pct_limit_max), 2) if order.direction == "LONG" else round(_entry * (1 + _sl_pct_limit_max), 2)
                _sl_source = "作战计划"

        if _sl_price <= 0:
            _sl_pct = 0.03  # fallback 3%
            _sl_price = round(_entry * (1 - _sl_pct), 2) if order.direction == "LONG" else round(_entry * (1 + _sl_pct), 2)
            _sl_source = "默认3%"
        _sl_side_name = "SELL" if order.direction == "LONG" else "BUY"

        # ── 止盈价: 从作战计划获取 + 校验 ──
        _tp_price = float(order.take_profit or 0)
        _tp_pct_limit_min = 0.02  # TP 至少距入场 2%
        _tp_pct_limit_max = 0.50  # TP 最远距入场 50%
        
        if _tp_price > 0:
            if order.direction == "LONG" and _tp_price <= _entry:
                logger.warning(f"[保护止盈] {order.symbol} TP ${_tp_price} <= 入场 ${_entry}, 方向错误, 跳过")
                _tp_price = 0
            elif order.direction == "SHORT" and _tp_price >= _entry:
                logger.warning(f"[保护止盈] {order.symbol} TP ${_tp_price} >= 入场 ${_entry}, 方向错误, 跳过")
                _tp_price = 0
            else:
                tp_dist = abs(_tp_price - _entry) / _entry
                if tp_dist < _tp_pct_limit_min:
                    logger.warning(f"[保护止盈] {order.symbol} TP 距入场仅 {tp_dist:.1%} (<{_tp_pct_limit_min:.0%}), 跳过")
                    _tp_price = 0
                elif tp_dist > _tp_pct_limit_max:
                    logger.warning(f"[保护止盈] {order.symbol} TP 距入场 {tp_dist:.1%} (>{_tp_pct_limit_max:.0%}), 自动收紧")
                    _tp_price = round(_entry * (1 + _tp_pct_limit_max), 2) if order.direction == "LONG" else round(_entry * (1 - _tp_pct_limit_max), 2)
        _tp_side_name = "SELL" if order.direction == "LONG" else "BUY"

        from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, TimeInForce, RET_OK
        trd_env = service._trd_env()
        trd_side_sl = TrdSide.SELL if _sl_side_name == "SELL" else TrdSide.BUY

        # ── 模拟盘：仅记录到 DB ──
        if trd_env == TrdEnv.SIMULATE:
            logger.info(f"[保护止损] 模拟盘不支持 STOP 单，已记录 SL=${_sl_price} TP=${_tp_price} 到 DB")
            try:
                service._ensure_sl_column()
                conn = service._db()
                conn.execute(
                    "UPDATE open_positions SET current_sl=?, initial_tp=? WHERE symbol=?",
                    (_sl_price, _tp_price, order.symbol),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning(f"[保护止损] 模拟盘 DB 更新失败: {e}")
            return

        # ── 实盘：挂止损 STOP 单 ──
        # DEB-20260903 #121: TP 重试路径 SL 已挂出时跳过重挂, 防双止损单
        _sl_order_id = getattr(order, "_sl_order_id", None) if getattr(order, "_sl_attached", False) else None
        if _sl_order_id:
            logger.info(f"[保护止损] {order.symbol} SL 已挂出 (order_id={_sl_order_id}), 跳过重挂")
            ret, data = RET_OK, None
        else:
            ret, data = service._broker_ctx.place_order(
                price=0,  # STOP 单触发后市价成交
                qty=_qty,
                code=order.symbol,
                trd_side=trd_side_sl,
                order_type=FutuOrderType.STOP,
                trd_env=trd_env,
                acc_id=service._acc_id(),
                time_in_force=TimeInForce.GTC,
                aux_price=_sl_price,  # 触发价 (Futu STOP 单必须用 aux_price)
                trail_spread=0,  # 非追踪止损
            )

        if ret == RET_OK and _sl_order_id:
            pass  # SL 已在此前挂出 (TP 重试路径), 直接进 TP 挂单
        elif ret == RET_OK and not data.empty:
            _sl_order_id = str(data.iloc[0].get("order_id", ""))
            # DEB-20260903 #121: 标记已挂, TP 未确认重入队重试时跳过重挂
            order._sl_attached = True
            order._sl_order_id = _sl_order_id
            logger.info(
                f"[保护止损] ✅ {order.symbol} {order.direction} "
                f"STOP ${_sl_price} ({_sl_source}) qty={_qty} → order_id={_sl_order_id}"
            )
        else:
            error_msg = str(data) if not data.empty else "Empty response"
            logger.error(f"[保护止损] ❌ {order.symbol} STOP 单失败: {error_msg}")
            # DEB-20260903 #121: 止损未挂出即中止 — 原继续挂 TP 形成
            # "有止盈无止损"裸奔; 推送 CRITICAL 告警并 return, 不挂 TP
            try:
                from app.push import get_push_hub, PushMessage, Level, Category
                get_push_hub().push(PushMessage(
                    title=f"🚨 保护止损缺失 ({order.symbol})",
                    body=(
                        f"📌 标的: {order.symbol} {order.direction}\n"
                        f"📦 数量: {_qty} 股 @ ${_entry:.2f}\n"
                        f"❌ 实盘 STOP 止损单提交失败: {error_msg}\n"
                        f"⚠️ 仓位当前无任何止损保护, 请立即人工挂止损!"
                    ),
                    level=Level.CRITICAL,
                    category=Category.RISK_ALERT,
                    dedup_key=f"protective_sl_fail:{order.symbol}",
                ))
            except Exception as _pe:
                logger.warning(f"[保护止损] {order.symbol} 止损缺失的 CRITICAL 推送发送失败: {_pe}")
            return

        # ── 挂止盈 LIMIT 单 (GTC) ──
        _tp_order_id = None
        # DEB-20260904 扫描修复: 与 #121 _sl_attached 对称的 TP 防重 —
        # 部分成交跟踪/TP 重试路径每次 monitor 周期都会重入本函数,
        # 无此守卫则每轮再挂一张 GTC TP 卖单, 累计卖出量可超持仓 → 意外做空
        _tp_already = getattr(order, "_tp_attached", False) and getattr(order, "_tp_order_id", None)
        if _tp_already:
            _tp_order_id = order._tp_order_id
            logger.info(f"[保护止盈] {order.symbol} TP 已挂出 (order_id={_tp_order_id}), 跳过重挂")
        if _tp_price > 0 and not _tp_already:
            trd_side_tp = TrdSide.SELL if _tp_side_name == "SELL" else TrdSide.BUY

            # 等待持仓确认 (买入后 broker 可能尚未入账, 直接 SELL 会被拒绝为"做空")
            _pos_confirmed = False
            if trd_side_tp == TrdSide.SELL:
                for _wait_i in range(6):  # 最多等 18 秒 (6 × 3s)
                    try:
                        _ret_pos, _pos_data = service._broker_ctx.position_list_query(
                            trd_env=trd_env, acc_id=service._acc_id(),
                        )
                        if _ret_pos == RET_OK and not _pos_data.empty:
                            _sym_pos = _pos_data[_pos_data["code"] == order.symbol]
                            if not _sym_pos.empty and float(_sym_pos.iloc[0].get("qty", 0)) >= _qty:
                                _pos_confirmed = True
                                break
                    except Exception:
                        pass
                    if _wait_i < 5:
                        time.sleep(3)
                if not _pos_confirmed:
                    logger.warning(f"[保护止盈] {order.symbol} 持仓未确认 (等了18s), TP 单转入监控队列重试")
                    # DEB-20260903 #121: 原仅记日志声称重试但无任何注册, TP 保护
                    # 永久丢失; 重新入队由监控线程 30s 后重试 (SL 已挂则自动跳过)
                    if order.order_id:
                        with _protections_lock:
                            _pending_protections[order.order_id] = {
                                "order": order, "service": service,
                                "registered_at": datetime.now(),
                            }
                        _ensure_monitor_started()

            if _pos_confirmed or trd_side_tp == TrdSide.BUY:
                try:
                    ret_tp, data_tp = service._broker_ctx.place_order(
                        price=_tp_price,
                        qty=_qty,
                        code=order.symbol,
                        trd_side=trd_side_tp,
                        order_type=FutuOrderType.NORMAL,  # LIMIT
                        trd_env=trd_env,
                        acc_id=service._acc_id(),
                        time_in_force=TimeInForce.GTC,
                    )
                    if ret_tp == RET_OK and not data_tp.empty:
                        _tp_order_id = str(data_tp.iloc[0].get("order_id", ""))
                        # DEB-20260904 扫描修复: 标记已挂, 监控重入时跳过重挂 (防重复 TP 单)
                        order._tp_attached = True
                        order._tp_order_id = _tp_order_id
                        logger.info(
                            f"[保护止盈] ✅ {order.symbol} {order.direction} "
                            f"LIMIT ${_tp_price} qty={_qty} → order_id={_tp_order_id}"
                        )
                    else:
                        logger.warning(f"[保护止盈] {order.symbol} LIMIT 单失败: {data_tp}")
                except Exception as _tp_err:
                    logger.warning(f"[保护止盈] {order.symbol} 止盈单异常 (非致命): {_tp_err}")

        # ── 存储订单 ID 到 DB ──
        if _sl_order_id or _tp_order_id:
            try:
                service._ensure_sl_column()
                conn = service._db()
                scoped_update(
                    conn,
                    "UPDATE open_positions SET current_sl=?, sl_broker_order_id=?, initial_tp=?, tp_broker_order_id=? WHERE symbol=?",
                    (_sl_price, _sl_order_id, _tp_price, _tp_order_id, order.symbol),
                    order.symbol, "订单组 SL/TP 订单 ID 落库",
                )
                conn.commit()
                conn.close()
                logger.info(f"[订单组] {order.symbol} SL/TP 订单已记录到 DB")
            except Exception as e:
                logger.error(f"[订单组] DB 存储失败: {e}")



    except Exception as _sl_err:
        logger.warning(f"[保护止损] {order.symbol} 自动止损/止盈异常 (非致命): {_sl_err}")


def _load_protective_snapshot(service: "ManualOrderService", symbol: str) -> Optional[Dict[str, float]]:
    """Load live SL/TP levels from open_positions before any protective order is cancelled."""
    try:
        service._ensure_sl_column()
        conn = service._db()
        try:
            if symbol.startswith("US."):
                row = conn.execute(
                    "SELECT current_sl, initial_tp FROM open_positions WHERE symbol=?",
                    (symbol,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT current_sl, initial_tp FROM open_positions "
                    "WHERE symbol IN (?, ?)",
                    (symbol, f"US.{symbol}"),
                ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "current_sl": float(row[0] or 0),
            "initial_tp": float(row[1] or 0),
        }
    except Exception as error:
        logger.exception(f"[部分平仓·保护价位] {symbol} 读取 open_positions 失败: {error}")
        return None


def _rebuild_protective_after_partial(
    service: "ManualOrderService", symbol: str, direction: str,
    remain_qty: int, protection_snapshot: Optional[Dict[str, float]],
) -> None:
    """DEB-20260904 #201 (S5): 部分平仓后按剩余量重建 SL/TP 保护单.

    复用下单前从 open_positions 读取的 current_sl/initial_tp 价位 (不重新计算),
    仅按 remain_qty 重挂. best-effort: 任何失败只告警不抛 (仓位仍受 ExitMonitor 保护).
    """
    try:
        _sl_price = float((protection_snapshot or {}).get("current_sl") or 0)
        _tp_price = float((protection_snapshot or {}).get("initial_tp") or 0)
        if remain_qty <= 0:
            logger.debug(f"[部分平仓·重建] {symbol} 剩余量为 0, 跳过重建")
            return
        if _sl_price <= 0 and _tp_price <= 0:
            logger.critical(
                f"[部分平仓·重建] {symbol} open_positions 无可用 SL/TP 价位, "
                f"剩余 {remain_qty} 股无 broker 端保护单, 需人工立即处理"
            )
            return
        if not service._broker_ctx or not service._connected:
            logger.warning(f"[部分平仓·重建] {symbol} broker 未连接, 无法重建保护单")
            return

        from futu import TrdEnv, TrdSide, OrderType as FutuOrderType, TimeInForce, RET_OK
        trd_env = service._trd_env()
        # 模拟盘 Futu 不支持 STOP — 价位仍在 DB, 由 ExitMonitor 轮询保护, 无需重挂
        if trd_env == TrdEnv.SIMULATE:
            logger.info(f"[部分平仓·重建] {symbol} 模拟盘: SL/TP 价位保留 DB, ExitMonitor 兜底")
            return

        _close_side = TrdSide.SELL if direction == "LONG" else TrdSide.BUY
        _sl_order_id = None
        _tp_order_id = None

        if _sl_price > 0:
            try:
                ret, data = service._broker_ctx.place_order(
                    price=0, qty=int(remain_qty), code=symbol,
                    trd_side=_close_side, order_type=FutuOrderType.STOP,
                    trd_env=trd_env, acc_id=service._acc_id(),
                    time_in_force=TimeInForce.GTC, aux_price=_sl_price, trail_spread=0,
                )
                if ret == RET_OK and not data.empty:
                    _sl_order_id = str(data.iloc[0].get("order_id", ""))
                    logger.info(
                        f"[部分平仓·重建] ✅ {symbol} SL STOP ${_sl_price} "
                        f"qty={remain_qty} → {_sl_order_id}"
                    )
                else:
                    logger.warning(f"[部分平仓·重建] {symbol} SL 重挂失败: {data}")
            except Exception as _se:
                logger.warning(f"[部分平仓·重建] {symbol} SL 重挂异常: {_se}")

        if _tp_price > 0:
            try:
                ret, data = service._broker_ctx.place_order(
                    price=_tp_price, qty=int(remain_qty), code=symbol,
                    trd_side=_close_side, order_type=FutuOrderType.NORMAL,
                    trd_env=trd_env, acc_id=service._acc_id(),
                    time_in_force=TimeInForce.GTC,
                )
                if ret == RET_OK and not data.empty:
                    _tp_order_id = str(data.iloc[0].get("order_id", ""))
                    logger.info(
                        f"[部分平仓·重建] ✅ {symbol} TP LIMIT ${_tp_price} "
                        f"qty={remain_qty} → {_tp_order_id}"
                    )
                else:
                    logger.warning(f"[部分平仓·重建] {symbol} TP 重挂失败: {data}")
            except Exception as _te:
                logger.warning(f"[部分平仓·重建] {symbol} TP 重挂异常: {_te}")

        if _sl_order_id or _tp_order_id:
            try:
                service._ensure_sl_column()
                conn = service._db()
                scoped_update(
                    conn,
                    "UPDATE open_positions SET sl_broker_order_id=?, tp_broker_order_id=? WHERE symbol=?",
                    (_sl_order_id, _tp_order_id, symbol),
                    symbol, "部分平仓·重建保护单 ID 落库",
                )
                conn.commit()
                conn.close()
            except Exception as _dbe:
                logger.error(f"[部分平仓·重建] {symbol} 订单 ID 落库失败: {_dbe}")
    except Exception as _e:
        logger.warning(f"[部分平仓·重建] {symbol} 重建保护单异常 (非致命): {_e}")


# ── 订单组管理（OCO：One-Cancels-Other） ──
# 事件驱动实现：见 oco_handler.py + TradeOrderHandlerBase
# 手动平仓时取消订单组：见 cancel_order_group()


def cancel_order_group(service: "ManualOrderService", symbol: str) -> None:
    """手动平仓时取消订单组（SL + TP）."""
    try:
        service._ensure_sl_column()
        conn = service._db()
        row = conn.execute(
            "SELECT sl_broker_order_id, tp_broker_order_id FROM open_positions WHERE symbol=?",
            (symbol,),
        ).fetchone()
        conn.close()
        
        if not row:
            return
        
        sl_id, tp_id = row
        
        if sl_id:
            logger.info(f"[平仓] {symbol} 取消止损单 {sl_id}")
            _sl_ok = service._cancel_broker_order(sl_id)
            if not _sl_ok:
                # DEB-20260903 #122: 撤单失败时保留 DB 记录, 防止丢失对活跃 broker 挂单的追踪
                logger.error(
                    f"[平仓] {symbol} 止损单 {sl_id} 撤单失败 — "
                    f"保留 DB 记录以便后续对账, 请人工确认"
                )

        if tp_id:
            logger.info(f"[平仓] {symbol} 取消止盈单 {tp_id}")
            _tp_ok = service._cancel_broker_order(tp_id)
            if not _tp_ok:
                logger.error(
                    f"[平仓] {symbol} 止盈单 {tp_id} 撤单失败 — "
                    f"保留 DB 记录以便后续对账, 请人工确认"
                )

        # DEB-20260903 #122: 仅在撤单成功或无单可撤时清除 DB 记录
        _sl_cleared = not sl_id or (sl_id and _sl_ok)
        _tp_cleared = not tp_id or (tp_id and _tp_ok)
        if _sl_cleared and _tp_cleared:
            conn = service._db()
            scoped_update(
                conn,
                "UPDATE open_positions SET sl_broker_order_id=NULL, tp_broker_order_id=NULL WHERE symbol=?",
                (symbol,),
                symbol, "平仓后清除 SL/TP 订单 ID",
            )
            conn.commit()
            conn.close()
        else:
            # 部分撤单失败: 仅清除已成功撤单的一侧
            conn = service._db()
            if _sl_cleared and sl_id:
                scoped_update(
                    conn,
                    "UPDATE open_positions SET sl_broker_order_id=NULL WHERE symbol=?",
                    (symbol,),
                    symbol, "部分撤单后清除 SL 订单 ID",
                )
            if _tp_cleared and tp_id:
                scoped_update(
                    conn,
                    "UPDATE open_positions SET tp_broker_order_id=NULL WHERE symbol=?",
                    (symbol,),
                    symbol, "部分撤单后清除 TP 订单 ID",
                )
            conn.commit()
            conn.close()
        
    except Exception as e:
        logger.warning(f"[平仓] {symbol} 取消订单组失败 (非致命): {e}")
