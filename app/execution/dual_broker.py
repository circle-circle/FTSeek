"""
DualBrokerAdapter — 双账户 broker (模拟 + 实盘) per-call 路由.

三态共存架构 (Phase 1, 2026-06-24): 同一进程内同时持有富途模拟账户 + 实盘账户两个
OpenSecTradeContext, submit_order/close_position 按 target_env 路由到对应账户.

用途:
  - playbook futu_simulate 模式: 真实下单到模拟账户 (PLAYBOOK_SIM), OKR 量真实成交
  - playbook live 模式 (Phase 2, 需 graduation): 真实下单到实盘账户
  - 手动交易仍走 manual_order_service 独立 ctx (实盘), 不经此组件

设计:
  - 共享一个 OpenQuoteContext (行情无需区分账户)
  - sim_ctx: OpenSecTradeContext, 无需解锁, 自动探测 SIMULATE 账户
  - real_ctx: OpenSecTradeContext + unlock_trade, 自动探测 REAL 账户
  - real 解锁失败 → real_ctx=None (fail-close: 实盘下单被拒)
  - per-ctx threading.Lock 防并发下单竞态

不改动现有 FutuBrokerAdapter (服务主 pipeline ExecutionEngine) — 本组件是平行类,
仅给 playbook futu_simulate/live 路径使用.
"""
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 模块级单例
_dual_broker_instance = None
_dual_broker_lock = threading.Lock()


def get_dual_broker():
    """获取 DualBrokerAdapter 单例 (懒构造, 线程安全)."""
    global _dual_broker_instance
    if _dual_broker_instance is None:
        with _dual_broker_lock:
            # 双重检查
            if _dual_broker_instance is None:
                _dual_broker_instance = DualBrokerAdapter()
                _dual_broker_instance.connect()
    return _dual_broker_instance


class DualBrokerAdapter:
    """双账户 broker: 模拟 + 实盘 per-call 路由."""

    def __init__(self):
        self.host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        self.port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        self.sim_account_id = os.getenv("SIMULATE_ACCOUNT_ID", "").strip()
        self.real_account_id = os.getenv("REAL_ACCOUNT_ID", "").strip()
        self._unlock_pwd_md5 = os.getenv("FUTU_UNLOCK_PWD_MD5", "").strip()

        self._quote_ctx = None
        self._sim_ctx = None
        self._real_ctx = None
        self._sim_lock = threading.Lock()
        self._real_lock = threading.Lock()
        self._connected = False

    # ── 连接 ──────────────────────────────────────────────

    def connect(self) -> bool:
        """建立 sim + real 两个交易 context + 共享行情 context.

        real 解锁失败不阻断 sim (real_ctx=None, 实盘下单 fail-close).
        """
        if self._connected:
            return True
        try:
            from futu import OpenQuoteContext, OpenSecTradeContext, TrdMarket, RET_OK

            self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

            # ── sim ctx (2026-06-24: 也需解锁 — 美股模拟用实盘账户+TrdEnv.SIMULATE, 需解锁才能下单) ──
            try:
                self._sim_ctx = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.US, host=self.host, port=self.port, is_encrypt=False,
                )
                # 实盘账户做模拟交易也需要解锁
                if self._unlock_pwd_md5:
                    ret_sim, data_sim = self._sim_ctx.unlock_trade(password_md5=self._unlock_pwd_md5, is_unlock=True)
                    if ret_sim != RET_OK:
                        logger.warning(f"[DualBroker] sim ctx 解锁失败 (模拟下单可能受限): {data_sim}")
                self._discover_account(self._sim_ctx, env="sim")
                logger.info(f"[DualBroker] sim ctx 已连接 (acc_id={self.sim_account_id})")
            except Exception as e:
                logger.error(f"[DualBroker] sim ctx 连接失败: {e}")
                self._sim_ctx = None

            # ── real ctx (需解锁) ──
            try:
                self._real_ctx = OpenSecTradeContext(
                    filter_trdmarket=TrdMarket.US, host=self.host, port=self.port, is_encrypt=False,
                )
                if not self._unlock_pwd_md5:
                    logger.warning("[DualBroker] real ctx: FUTU_UNLOCK_PWD_MD5 未设置, real_ctx 不可用 (fail-close)")
                    self._real_ctx = None
                else:
                    ret, data = self._real_ctx.unlock_trade(password_md5=self._unlock_pwd_md5, is_unlock=True)
                    if ret != RET_OK:
                        logger.error(f"[DualBroker] real ctx 解锁失败: {data} — real_ctx 不可用 (fail-close)")
                        self._real_ctx = None
                    else:
                        self._discover_account(self._real_ctx, env="real")
                        logger.info("[DualBroker] real ctx 已解锁连接")
            except Exception as e:
                logger.error(f"[DualBroker] real ctx 连接失败: {e} — real_ctx 不可用 (fail-close)")
                self._real_ctx = None

            self._connected = True
            return True
        except Exception as e:
            logger.exception(f"[DualBroker] connect 异常: {e}")
            return False

    def _discover_account(self, ctx, env: str):
        """自动探测账户 ID (防 OpenD 重启后 ID 变化).

        2026-06-24: 美股模拟用实盘账户+TrdEnv.SIMULATE. 如果 SIMULATE 账户列表为空,
        fallback 到 REAL 账户 (Futu 支持 REAL acc_id + TrdEnv.SIMULATE 做模拟交易).
        """
        try:
            from futu import TrdEnv, RET_OK
            ret, data = ctx.get_acc_list()
            if ret != RET_OK:
                return
            target_env = TrdEnv.SIMULATE if env == "sim" else TrdEnv.REAL
            rows = data[data["trd_env"] == target_env]
            if rows.empty and env == "sim":
                # Fallback: 用 REAL 账户做模拟交易 (TrdEnv.SIMULATE + real acc_id)
                real_rows = data[data["trd_env"] == TrdEnv.REAL]
                if not real_rows.empty:
                    actual_id = str(real_rows.iloc[0]["acc_id"])
                    if not self.sim_account_id:
                        self.sim_account_id = actual_id
                    logger.info(f"[DualBroker] sim 账户 fallback 到 REAL 账户: {self.sim_account_id} (TrdEnv.SIMULATE)")
                    return
                logger.warning(f"[DualBroker] 未找到任何账户 (sim/real)")
                return
            if rows.empty:
                logger.warning(f"[DualBroker] 未找到 {env} 账户")
                return
            actual_id = str(rows.iloc[0]["acc_id"])
            configured = self.sim_account_id if env == "sim" else self.real_account_id
            if not configured:
                if env == "sim":
                    self.sim_account_id = actual_id
                else:
                    self.real_account_id = actual_id
                logger.info(f"[DualBroker] 自动探测 {env} 账户: {actual_id}")
        except Exception as e:
            logger.warning(f"[DualBroker] {env} 账户探测失败: {e}")

    def _ctx_and_acc(self, target_env: str):
        """返回 (ctx, acc_id, trd_env, lock) — target_env=sim|real."""
        from futu import TrdEnv
        if target_env == "real":
            if self._real_ctx is None:
                raise RuntimeError("[DualBroker] real ctx 不可用 (未解锁/连接失败) — 实盘下单被拒 (fail-close)")
            return self._real_ctx, self.real_account_id, TrdEnv.REAL, self._real_lock
        else:
            if self._sim_ctx is None:
                raise RuntimeError("[DualBroker] sim ctx 不可用 — 模拟下单被拒")
            return self._sim_ctx, self.sim_account_id, TrdEnv.SIMULATE, self._sim_lock

    # ── 风控兜底: 单笔名义本金硬上限 (30% equity) ──
    _NOTIONAL_HARD_CAP_PCT = 0.30

    def _notional_sanity_check(self, symbol: str, qty, price: float, is_close: bool = False) -> bool:
        """兜底检查: 单笔名义 ≤ equity × 30%. 平仓单放行, equity 不可用时 fail-close."""
        if is_close:
            return True
        if qty <= 0 or price <= 0:
            return True
        notional = float(qty) * float(price)
        equity = 0.0
        try:
            from app.risk.capital_safety import get_capital_safety_system
            _cap = get_capital_safety_system()
            if _cap._state.current_equity > 0:
                equity = _cap._state.current_equity
        except Exception:
            pass
        if equity <= 0:
            logger.error(f"🛑 [DualBroker] {symbol} 拒绝: equity 不可用, fail-close (名义=${notional:,.0f})")
            return False
        limit = equity * self._NOTIONAL_HARD_CAP_PCT
        if notional > limit:
            logger.error(
                f"🛑 [DualBroker] {symbol} 拒绝: 名义=${notional:,.0f} > 兜底上限=${limit:,.0f} "
                f"(净值${equity:,.0f}×{self._NOTIONAL_HARD_CAP_PCT:.0%})"
            )
            return False
        return True

    # ── 下单 ──────────────────────────────────────────────

    def submit_order(self, order, target_env: str = "sim") -> Dict[str, Any]:
        """提交订单到指定账户.

        Args:
            order: Order 对象 (app.core.models.Order) — 需 symbol/direction/quantity/price/order_type
            target_env: "sim" | "real"

        Returns:
            {"success": bool, "futu_order_id": str, "filled_price": float, "error": str}
        """
        try:
            from futu import TrdSide, OrderType as FutuOrderType, RET_OK
            from app.core.models import Direction, OrderType

            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)

            # 风控兜底: 单笔名义本金上限 (平仓单/市价单跳过)
            _order_meta = getattr(order, 'metadata', None) or {}
            _is_close = bool(_order_meta.get('is_exit', False)) if isinstance(_order_meta, dict) else False
            if not self._notional_sanity_check(order.symbol, order.quantity, order.price, is_close=_is_close):
                return {"success": False, "error": "dual_broker_notional_cap", "futu_order_id": ""}

            trd_side = TrdSide.BUY if order.direction == Direction.LONG else TrdSide.SELL
            futu_order_type = FutuOrderType.MARKET if order.order_type == OrderType.MARKET else FutuOrderType.NORMAL

            with lock:
                ret, data = ctx.place_order(
                    price=order.price, qty=order.quantity, code=order.symbol,
                    trd_side=trd_side, order_type=futu_order_type,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                )

            if ret != RET_OK:
                return {"success": False, "error": f"place_order 失败: {data}", "futu_order_id": ""}

            futu_id = str(data["order_id"].values[0])
            filled_price = self._query_filled_price(ctx, futu_id, trd_env, lock)

            logger.info(
                f"[DualBroker] 下单成功: {order.symbol} {order.direction} qty={order.quantity} "
                f"env={target_env} futu_id={futu_id} fill={filled_price}"
            )
            return {"success": True, "futu_order_id": futu_id, "filled_price": filled_price, "error": ""}

        except Exception as e:
            logger.exception(f"[DualBroker] submit_order 异常 {order.symbol}: {e}")
            return {"success": False, "error": str(e), "futu_order_id": ""}

    def close_position(self, symbol: str, qty: float, direction_held: str, target_env: str = "sim") -> Dict[str, Any]:
        """市价平仓 (持仓方向 → 反向市价单).

        2026-07-03: 平仓前查 broker 实际持仓量 (防 DB qty ≠ broker qty → "持仓不足"),
        平仓后验证 broker 已无仓 (防 fill=0 假平仓).

        Args:
            direction_held: "LONG" | "SHORT" — 持仓方向; 平仓用反向.

        Returns:
            {"success": bool, "futu_order_id": str, "filled_price": float,
             "price_source": str, "broker_qty_before": float, "broker_qty_after": float, "error": str}
        """
        try:
            from futu import TrdSide, OrderType as FutuOrderType, RET_OK
            from app.core.models import Order, Direction, OrderType

            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)

            # ── Fix 2: 平仓前查 broker 实际持仓量 ──
            broker_qty_before = 0.0
            with lock:
                ret_pos, pos_data = ctx.position_list_query(trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0)
            if ret_pos == RET_OK and pos_data is not None and not pos_data.empty:
                _matches = pos_data[pos_data["code"] == symbol]
                if not _matches.empty:
                    broker_qty_before = abs(float(_matches.iloc[0].get("qty", 0) or 0))

            if broker_qty_before < 0.5:
                logger.info(f"[DualBroker] {symbol} broker 无持仓 (qty={broker_qty_before}), 跳过平仓")
                return {"success": False, "error": "broker_no_position",
                        "filled_price": 0, "price_source": "broker_empty",
                        "broker_qty_before": 0, "broker_qty_after": 0}

            # 用 broker 实际持仓量平仓, 不用调用方传入的 qty (可能过期)
            close_qty = int(broker_qty_before)
            close_side = TrdSide.SELL if direction_held == "LONG" else TrdSide.BUY

            with lock:
                ret, data = ctx.place_order(
                    price=0.0, qty=close_qty, code=symbol,
                    trd_side=close_side, order_type=FutuOrderType.MARKET,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                )

            if ret != 0:
                return {"success": False, "error": f"平仓下单失败: {data}",
                        "filled_price": 0, "price_source": "order_rejected",
                        "broker_qty_before": broker_qty_before, "broker_qty_after": broker_qty_before}

            futu_id = str(data["order_id"].values[0])
            filled_price = self._query_filled_price(ctx, futu_id, trd_env, lock)

            # ── Fix 4: 平仓后验证 broker 已无仓 ──
            import time as _t
            _t.sleep(0.5)  # 给 broker 0.5s 处理
            broker_qty_after = broker_qty_before  # 默认假设没平掉
            with lock:
                ret_pos2, pos_data2 = ctx.position_list_query(trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0)
            if ret_pos2 == RET_OK and pos_data2 is not None and not pos_data2.empty:
                _matches2 = pos_data2[pos_data2["code"] == symbol]
                if not _matches2.empty:
                    broker_qty_after = abs(float(_matches2.iloc[0].get("qty", 0) or 0))

            # 确定价格来源
            if filled_price > 0:
                price_source = "broker_deal"
            elif broker_qty_after < 0.5:
                price_source = "broker_closed_no_price"  # 平了但价格查不到
            else:
                price_source = "broker_close_failed"  # 没平掉

            logger.info(
                f"[DualBroker] 平仓: {symbol} qty={close_qty} env={target_env} "
                f"fill={filled_price} src={price_source} "
                f"broker_qty: {broker_qty_before}→{broker_qty_after}"
            )
            return {"success": True, "futu_order_id": futu_id, "filled_price": filled_price,
                    "price_source": price_source,
                    "broker_qty_before": broker_qty_before, "broker_qty_after": broker_qty_after, "error": ""}

        except Exception as e:
            logger.exception(f"[DualBroker] close_position 异常 {symbol}: {e}")
            return {"success": False, "error": str(e), "filled_price": 0,
                    "price_source": "exception", "broker_qty_before": 0, "broker_qty_after": 0}

    def _query_filled_price(self, ctx, futu_order_id: str, trd_env, lock) -> float:
        """查订单成交均价 (市价单通常立即成交, 但 sim 可能有延迟).

        2026-07-03: 改用 history_deal_list_query 作为主路径 — order_list_query 在
        SIM 环境系统性返回 avg_price=0, 导致所有 sim 交易 exit_price 不可信.
        history_deal_list_query 返回真实成交记录的 price 字段, 可靠得多.
        order_list_query 降级为 fallback.
        """
        from futu import RET_OK
        import time
        from datetime import datetime

        # ── 主路径: history_deal_list_query (真实成交记录) ──
        for _attempt in range(3):
            try:
                now = datetime.now()
                start = now.strftime('%Y-%m-%d')
                end = now.strftime('%Y-%m-%d')
                with lock:
                    ret, data = ctx.history_deal_list_query(
                        start=start, end=end, trd_env=trd_env,
                    )
                if ret == RET_OK and data is not None and not data.empty:
                    for _, deal in data.iterrows():
                        deal_order_id = str(deal.get("order_id", ""))
                        if deal_order_id == futu_order_id:
                            price = float(deal.get("price", 0))
                            dealt_qty = float(deal.get("qty", 0) or 0)
                            if price > 0 and dealt_qty > 0:
                                return price
            except Exception as e:
                logger.debug(f"[DualBroker] history_deal_list_query 失败 {futu_order_id}: {e}")
            if _attempt < 2:
                time.sleep(0.5)

        # ── fallback: order_list_query (SIM 可能返回0) ──
        try:
            with lock:
                ret, data = ctx.order_list_query(order_id=futu_order_id, trd_env=trd_env)
            if ret == RET_OK and not data.empty:
                avg = data.iloc[0].get("avg_price", 0)
                dealt = data.iloc[0].get("dealt_qty", 0)
                if avg and float(avg) > 0 and float(dealt or 0) > 0:
                    return float(avg)
        except Exception as e:
            logger.debug(f"[DualBroker] order_list_query fallback 失败 {futu_order_id}: {e}")

        return 0.0

    def get_positions(self, target_env: str = "sim") -> list:
        """查询指定账户持仓."""
        try:
            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.position_list_query(trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0)
            if ret == 0 and not data.empty:
                return data.to_dict("records")
            return []
        except Exception as e:
            logger.warning(f"[DualBroker] get_positions {target_env} 失败: {e}")
            return []

    def place_protective_stop(self, symbol: str, qty: float, direction_held: str,
                              stop_price: float, target_env: str = "real") -> Dict[str, Any]:
        """挂 broker STOP 单作 live 兜底 (服务断连时仍触发止损).

        非行为性兜底: 正常退出由规则引擎 (monitor) 触发市价单; broker STOP 仅在
        服务挂掉时兜底. 富途模拟盘不支持 STOP → sim 跳过 (返回 skipped).

        Args:
            direction_held: "LONG" | "SHORT" (持仓方向, STOP 用反向)
            stop_price: 止损触发价
        """
        if target_env != "real":
            # 模拟盘不支持 STOP, 跳过 (规则引擎兜底)
            return {"success": True, "skipped": True, "reason": "sim 不支持 STOP, 由规则引擎兜底"}
        if not stop_price or stop_price <= 0:
            return {"success": True, "skipped": True, "reason": "无 stop_price"}
        try:
            from futu import TrdSide, OrderType as FutuOrderType, RET_OK
            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            # LONG 持仓 → SELL STOP; SHORT 持仓 → BUY STOP
            stop_side = TrdSide.SELL if direction_held == "LONG" else TrdSide.BUY
            with lock:
                ret, data = ctx.place_order(
                    price=stop_price, qty=qty, code=symbol,
                    trd_side=stop_side, order_type=FutuOrderType.STOP,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                )
            if ret != RET_OK:
                logger.warning(f"[DualBroker] live STOP 单失败 {symbol}: {data}")
                return {"success": False, "error": str(data)}
            futu_id = str(data["order_id"].values[0]) if not data.empty else ""
            logger.info(f"[DualBroker] live STOP 单已挂: {symbol} {stop_side} @ {stop_price} futu_id={futu_id}")
            return {"success": True, "futu_order_id": futu_id}
        except Exception as e:
            logger.warning(f"[DualBroker] place_protective_stop 异常 {symbol}: {e}")
            return {"success": False, "error": str(e)}

    def is_healthy(self, target_env: str = "sim") -> bool:
        ctx = self._real_ctx if target_env == "real" else self._sim_ctx
        if ctx is None:
            return False
        # 轻量健康检查: 查询账户列表
        try:
            from futu import RET_OK
            with (self._real_lock if target_env == "real" else self._sim_lock):
                ret, _ = ctx.get_acc_list()
            return ret == RET_OK
        except Exception:
            return False

    def disconnect(self):
        for ctx in (self._sim_ctx, self._real_ctx, self._quote_ctx):
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
        self._sim_ctx = self._real_ctx = self._quote_ctx = None
        self._connected = False
