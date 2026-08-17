"""
DualBrokerAdapter — 双账户 broker (模拟 + 实盘) per-call 路由.

三态共存架构 (Phase 1, 2026-06-24): 同一进程内同时持有富途模拟账户 + 实盘账户两个
OpenSecTradeContext, submit_order/close_position 按 target_env 路由到对应账户.

用途:
  - playbook futu_simulate 模式: 真实下单到模拟账户 (PLAYBOOK_SIM), OKR 量真实成交
  - playbook live 模式 (Phase 2, 需 graduation): 真实下单到实盘账户
  - 手动交易仍走 manual_order_service 独立 ctx (实盘), 不经此组件

设计:
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
            from futu import OpenSecTradeContext, TrdMarket, RET_OK

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
                    # B4 (2026-07-19): 透传 client_order_id 到 broker 侧留痕 (崩溃后可审计/可定位, 非真幂等).
                    remark=getattr(order, 'client_order_id', '') or None,
                )

            if ret != RET_OK:
                return {"success": False, "error": f"place_order 失败: {data}", "futu_order_id": ""}

            futu_id = str(data["order_id"].values[0])
            filled_price, filled_qty = self._query_fill(ctx, futu_id, trd_env, lock)

            if filled_qty > 0 and filled_qty < float(order.quantity):
                logger.warning(
                    f"[DualBroker] 部分成交: {order.symbol} 下单 {order.quantity} 实际成交 {filled_qty} "
                    f"env={target_env} futu_id={futu_id}"
                )

            logger.info(
                f"[DualBroker] 下单成功: {order.symbol} {order.direction} qty={order.quantity} "
                f"env={target_env} futu_id={futu_id} fill={filled_price} filled_qty={filled_qty}"
            )
            return {"success": True, "futu_order_id": futu_id, "filled_price": filled_price,
                    "filled_qty": filled_qty, "error": ""}

        except Exception as e:
            logger.exception(f"[DualBroker] submit_order 异常 {order.symbol}: {e}")
            return {"success": False, "error": str(e), "futu_order_id": ""}

    def close_position(self, symbol: str, qty: float, direction_held: str,
                       target_env: str = "sim", limit_price: float = 0.0,
                       limit_timeout_s: float = 30.0) -> Dict[str, Any]:
        """平仓 (持仓方向 → 反向单). 默认市价; limit_price>0 时挂限价单.

        根因修复 (2026-07-07): 订单状态是成交的唯一真相来源.
        旧实现用 position_list_query (持仓列表) 验证平仓 — SIM 环境持仓列表更新延迟可达 29s,
        3s 轮询不够 → 假阴性 broker_close_failed → 下一轮 broker_no_position → BROKER_GHOST_CLOSE.
        现改为用 order_list_query 的 order_status 验证 (FILLED_ALL = 全部成交),
        与持仓列表更新延迟无关, 从源头消灭整类问题.

        限价平仓 (2026-08-17, 用户裁"净保本包含费用"): SL_BREAKEVEN 市价平仓的 fill
        系统性落回保本线下方 (D4 后 31 笔 24 笔净亏, 越位≈费用本身 — 轮询间隙漂移吃掉
        cushion). 限价单@净保本线使成交价物理上 ≥ 保本线, 净保本由订单类型保证.
        limit_timeout_s 内未成交 → 撤单降级市价 (最坏=现状, 无卡单风险).

        Args:
            direction_held: "LONG" | "SHORT" — 持仓方向; 平仓用反向.
            limit_price: >0 时限价平仓 (LONG→SELL LIMIT, SHORT→BUY LIMIT); 0=市价.
            limit_timeout_s: 限价单等待上限, 超时撤单转市价.

        Returns:
            {"success": bool, "futu_order_id": str, "filled_price": float,
             "price_source": str, "broker_qty_before": float, "broker_qty_after": float, "error": str}
        """
        try:
            from futu import TrdSide, OrderType as FutuOrderType, RET_OK, OrderStatus
            from app.core.models import Order, Direction, OrderType
            import time as _t

            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)

            # ── 平仓前查 broker 实际持仓量 ──
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

            # ── 净保本限价腿 (2026-08-17, 用户裁"保本单真正净保本包含费用") ──
            # SL_BREAKEVEN 市价平仓的 fill 系统性落回保本线下方 (D4 后 31 笔 24 笔净亏,
            #   越位≈费用本身: 20s 轮询间隙的价格漂移把 fee cushion 精确吃掉).
            # 限价单@净保本线: LONG→SELL LIMIT@entry×(1+费率), SHORT→BUY LIMIT@entry×(1−费率),
            #   成交价物理上 ≥(空: ≤) 保本线 → 净保本由订单类型保证, 不靠轮询追价.
            # timeout 未成交 → 撤单降级市价 (最坏=现状, 无卡单风险). SIM 支持 LIMIT(NORMAL).
            _limit_dealt = 0.0
            _limit_fill_px = 0.0  # 限价腿已成交部分的均价 (部分成交降级时加权记账用)
            if limit_price and float(limit_price) > 0 and close_qty > 0:
                _limit_px = round(float(limit_price), 2)
                with lock:
                    ret_l, data_l = ctx.place_order(
                        price=_limit_px, qty=close_qty, code=symbol,
                        trd_side=close_side, order_type=FutuOrderType.NORMAL,  # LIMIT
                        trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                        remark=f"pb_be_close_{symbol}_{target_env}",  # B4: 净保本平仓单留痕
                    )
                if ret_l == 0 and data_l is not None and not data_l.empty:
                    _lid = str(data_l["order_id"].values[0])
                    _n_polls = max(1, int(float(limit_timeout_s) / 0.5))
                    _l_fill = 0.0
                    for _lw in range(_n_polls):
                        _t.sleep(0.5)
                        try:
                            with lock:
                                _ret_lo, _lod = ctx.order_list_query(order_id=_lid, trd_env=trd_env)
                            if _ret_lo == RET_OK and _lod is not None and not _lod.empty:
                                _lrow = _lod.iloc[0]
                                _lstatus = str(_lrow.get("order_status", ""))
                                _limit_dealt = float(_lrow.get("dealt_qty", 0) or 0)
                                if "FILLED_ALL" in _lstatus or _limit_dealt >= close_qty:
                                    try:
                                        _l_fill = float(_lrow.get("dealt_avg_price", 0) or 0)
                                    except Exception:
                                        _l_fill = 0.0
                                    if _l_fill <= 0:
                                        _l_fill = self._query_filled_price(ctx, _lid, trd_env, lock)
                                    _ba = broker_qty_before - _limit_dealt if _limit_dealt > 0 else broker_qty_before
                                    logger.info(
                                        f"[DualBroker] 平仓(限价@净保本): {symbol} qty={close_qty} env={target_env} "
                                        f"limit={_limit_px} fill={_l_fill} src=broker_limit_deal "
                                        f"broker_qty: {broker_qty_before}→{_ba}"
                                    )
                                    return {"success": True, "futu_order_id": _lid,
                                            "filled_price": _l_fill, "price_source": "broker_limit_deal",
                                            "broker_qty_before": broker_qty_before, "broker_qty_after": _ba,
                                            "order_filled": True, "dealt_qty": _limit_dealt, "error": ""}
                                if any(_x in _lstatus for _x in ("FAILED", "CANCELLED", "DELETED", "TIMEOUT", "REJECTED")):
                                    logger.warning(f"[DualBroker] {symbol} 限价平仓订单失败: status={_lstatus}")
                                    break
                        except Exception as _le:
                            logger.debug(f"[DualBroker] {symbol} 限价单查询异常: {_le}")
                    # ── 未成交: 撤单 + 竞态复查 + 降级市价 ──
                    try:
                        from futu import ModifyOrderOp
                        with lock:
                            ctx.modify_order(modify_order_op=ModifyOrderOp.CANCEL, order_id=_lid,
                                             qty=0, price=0, trd_env=trd_env,
                                             acc_id=int(acc_id) if acc_id else 0)
                    except Exception as _ce:
                        logger.warning(f"[DualBroker] {symbol} 撤限价单异常 (继续降级): {_ce}")
                    # 竞态复查: 撤单请求与成交之间的窗口 — 若已全部成交, 直接按限价成交返回
                    try:
                        with lock:
                            _ret_lq, _lq = ctx.order_list_query(order_id=_lid, trd_env=trd_env)
                        if _ret_lq == RET_OK and _lq is not None and not _lq.empty:
                            _lqr = _lq.iloc[0]
                            _limit_dealt = float(_lqr.get("dealt_qty", 0) or 0)
                            if "FILLED_ALL" in str(_lqr.get("order_status", "")) or _limit_dealt >= close_qty:
                                try:
                                    _l_fill = float(_lqr.get("dealt_avg_price", 0) or 0)
                                except Exception:
                                    _l_fill = 0.0
                                if _l_fill <= 0:
                                    _l_fill = self._query_filled_price(ctx, _lid, trd_env, lock)
                                logger.info(
                                    f"[DualBroker] 平仓(限价@净保本·撤单竞态全成): {symbol} fill={_l_fill}"
                                )
                                return {"success": True, "futu_order_id": _lid,
                                        "filled_price": _l_fill, "price_source": "broker_limit_deal",
                                        "broker_qty_before": broker_qty_before,
                                        "broker_qty_after": broker_qty_before - _limit_dealt,
                                        "order_filled": True, "dealt_qty": _limit_dealt, "error": ""}
                    except Exception as _lqe:
                        logger.debug(f"[DualBroker] {symbol} 撤单后复查异常: {_lqe}")
                    logger.info(
                        f"[DualBroker] {symbol} 限价平仓单 {limit_timeout_s:.0f}s 未成交 "
                        f"(dealt={_limit_dealt}), 撤单降级市价 (净保本兜底路径)"
                    )
                    if _limit_dealt > 0:
                        # 部分成交: 市价腿只平剩余 (持仓列表有延迟, 用订单 dealt 推算更准)
                        _limit_fill_px = self._query_filled_price(ctx, _lid, trd_env, lock)
                        close_qty = int(broker_qty_before - _limit_dealt)
                        if close_qty <= 0:
                            _l_fill = self._query_filled_price(ctx, _lid, trd_env, lock)
                            return {"success": True, "futu_order_id": _lid,
                                    "filled_price": _l_fill, "price_source": "broker_limit_deal",
                                    "broker_qty_before": broker_qty_before, "broker_qty_after": 0,
                                    "order_filled": True, "dealt_qty": _limit_dealt, "error": ""}
                else:
                    logger.warning(f"[DualBroker] {symbol} 限价平仓下单失败, 降级市价: {data_l}")

            with lock:
                ret, data = ctx.place_order(
                    price=0.0, qty=close_qty, code=symbol,
                    trd_side=close_side, order_type=FutuOrderType.MARKET,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                    remark=f"pb_close_{symbol}_{target_env}",  # B4: 平仓单留痕
                )
            if ret != 0:
                return {"success": False, "error": f"平仓下单失败: {data}",
                        "filled_price": 0, "price_source": "order_rejected",
                        "broker_qty_before": broker_qty_before, "broker_qty_after": broker_qty_before}

            futu_id = str(data["order_id"].values[0])

            # ── 根因修复: 用订单状态验证成交, 不用持仓列表 ──
            # order_list_query 返回 order_status + dealt_qty, 与持仓列表更新延迟无关.
            # FILLED_ALL = 全部成交; FILLED_PART = 部分成交.
            # 最多等待 30s (60×0.5s), 覆盖 SIM 环境 ~29s 延迟.
            order_filled = False
            dealt_qty = 0.0
            filled_price = 0.0

            for _wait in range(60):  # 最多 30s (60×0.5s)
                _t.sleep(0.5)
                # 查订单状态
                try:
                    with lock:
                        ret_od, od_data = ctx.order_list_query(order_id=futu_id, trd_env=trd_env)
                    if ret_od == RET_OK and od_data is not None and not od_data.empty:
                        _row = od_data.iloc[0]
                        _status = str(_row.get("order_status", ""))
                        dealt_qty = float(_row.get("dealt_qty", 0) or 0)
                        # FILLED_ALL 或 dealt_qty >= close_qty = 成交完成
                        if "FILLED_ALL" in _status or dealt_qty >= close_qty:
                            order_filled = True
                            # 2026-07-27 (DEB-entry-fill 同款对称修复): 本行里就有 dealt_avg_price
                            #   真成交均价, 旧版只读 status/qty 丢价 → 事后 _query_filled_price
                            #   (sim 主路径 history_deal 不支持) 失败率 41% → 41% 平仓价被报价兜底。
                            if filled_price <= 0:
                                try:
                                    _davg = float(_row.get("dealt_avg_price", 0) or 0)
                                    if _davg > 0:
                                        filled_price = _davg
                                except Exception:
                                    pass
                            break
                        # 订单失败/取消 = 不可恢复
                        if any(_x in _status for _x in ("FAILED", "CANCELLED", "DELETED", "TIMEOUT", "REJECTED")):
                            logger.warning(f"[DualBroker] {symbol} 平仓订单失败: status={_status}")
                            break
                except Exception as _od_err:
                    logger.debug(f"[DualBroker] {symbol} order_list_query 异常: {_od_err}")
                # 同时尝试获取成交价 (可能在等待期间成交了)
                if filled_price <= 0:
                    filled_price = self._query_filled_price(ctx, futu_id, trd_env, lock)

            # 订单成交后再查一次成交价 (确保拿到最终价)
            if order_filled and filled_price <= 0:
                filled_price = self._query_filled_price(ctx, futu_id, trd_env, lock)

            # 限价腿部分成交 + 市价腿平剩余 → 按全部已成交量的加权均价记账 (2026-08-17).
            #   dealt_qty 此处仅为市价腿成交; _limit_dealt/_limit_fill_px 是限价腿.
            _total_dealt = dealt_qty + _limit_dealt
            if order_filled and _limit_dealt > 0 and filled_price > 0 and _limit_fill_px > 0 and _total_dealt > 0:
                _wavg = (_limit_fill_px * _limit_dealt + filled_price * dealt_qty) / _total_dealt
                logger.info(
                    f"[DualBroker] {symbol} 两腿平仓加权均价: limit腿 {_limit_dealt}@{_limit_fill_px} + "
                    f"market腿 {dealt_qty}@{filled_price} → {_wavg:.4f}"
                )
                filled_price = round(_wavg, 4)
                dealt_qty = _total_dealt

            # 计算剩余持仓 (仅用于日志/审计, 不作为成交判定依据)
            broker_qty_after = broker_qty_before - dealt_qty if dealt_qty > 0 else broker_qty_before

            # 确定价格来源
            if order_filled and filled_price > 0:
                price_source = "broker_deal"
            elif order_filled and filled_price <= 0:
                price_source = "broker_closed_no_price"  # 成交了但价格查不到
            else:
                price_source = "broker_close_failed"  # 订单未成交

            logger.info(
                f"[DualBroker] 平仓: {symbol} qty={close_qty} env={target_env} "
                f"fill={filled_price} src={price_source} order_filled={order_filled} "
                f"dealt={dealt_qty} broker_qty: {broker_qty_before}→{broker_qty_after}"
            )
            return {"success": True, "futu_order_id": futu_id, "filled_price": filled_price,
                    "price_source": price_source,
                    "broker_qty_before": broker_qty_before, "broker_qty_after": broker_qty_after,
                    "order_filled": order_filled, "dealt_qty": dealt_qty, "error": ""}

        except Exception as e:
            logger.exception(f"[DualBroker] close_position 异常 {symbol}: {e}")
            return {"success": False, "error": str(e), "filled_price": 0,
                    "price_source": "exception", "broker_qty_before": 0, "broker_qty_after": 0}

    def _query_filled_price(self, ctx, futu_order_id: str, trd_env, lock) -> float:
        """查订单成交均价 (兼容旧调用, 只取价格). 内部委托 _query_fill."""
        price, _qty = self._query_fill(ctx, futu_order_id, trd_env, lock)
        return price

    def _query_fill(self, ctx, futu_order_id: str, trd_env, lock):
        """查订单成交均价 + 实际成交数量 → (price, dealt_qty).

        2026-07-03: 改用 history_deal_list_query 作为主路径 — order_list_query 在
        SIM 环境系统性返回 avg_price=0, 导致所有 sim 交易 exit_price 不可信.
        history_deal_list_query 返回真实成交记录的 price 字段, 可靠得多.
        order_list_query 降级为 fallback.

        2026-07-21 更正: 上述"SIM 系统性返回 avg_price=0"系误判 — 真根因是 fallback
        字段名笔误 (读 avg_price, 富途真实字段为 dealt_avg_price); 且 history_deal_list_query
        在 SIM 环境平台侧不支持 (实测报错"模拟交易不支持成交数据"), 主路径 sim 恒失败.
        本日已修复 fallback 字段名, sim 真实成交均价 (dealt_avg_price) 可正常获取.

        2026-07-19: 同时返回真实成交量 (dealt_qty), 供上游识别部分成交 —
            此前只取价格, 部分成交时上游按下单量记账 → 幽灵仓.
            同一 order_id 可能有多笔成交, price 用最后一笔, qty 累加.
        """
        from futu import RET_OK
        import time
        from datetime import datetime, timezone, timedelta
        from app.utils.timezone import now_et as _now_et

        # ── 主路径: history_deal_list_query (真实成交记录) ──
        # 2026-07-16 P0 修复: 用 ET 时区查询当日成交, 而非服务器 CST 时间.
        #   CST 比 ET 快 12 小时, 用 CST 查询可能导致跨日查不到当日成交.
        #   富途 API 按 ET 交易日返回, start/end 应是 ET 日期.
        # 2026-07-19: 改用 now_et() (DST 正确), 原硬编码 -4 在冬令时 (EST=-5) 错标日期.
        # 2026-07-21: SIM 环境主路径恒失败 (平台不支持, 见 docstring) → 仅试 1 次,
        #   省 2 次无效重试 (~1-2s), 让 30s 订单确认窗口更多留给 fallback 生效.
        _max_attempts = 1 if str(trd_env).endswith("SIMULATE") else 3
        for _attempt in range(_max_attempts):
            try:
                _et_dt = _now_et()
                start = _et_dt.strftime('%Y-%m-%d')
                end = _et_dt.strftime('%Y-%m-%d')
                with lock:
                    ret, data = ctx.history_deal_list_query(
                        start=start, end=end, trd_env=trd_env,
                    )
                if ret == RET_OK and data is not None and not data.empty:
                    _last_price = 0.0
                    _sum_qty = 0.0
                    for _, deal in data.iterrows():
                        deal_order_id = str(deal.get("order_id", ""))
                        if deal_order_id == futu_order_id:
                            price = float(deal.get("price", 0))
                            dealt_qty = float(deal.get("qty", 0) or 0)
                            if price > 0 and dealt_qty > 0:
                                _last_price = price
                                _sum_qty += dealt_qty
                    if _last_price > 0 and _sum_qty > 0:
                        return _last_price, _sum_qty
            except Exception as e:
                logger.debug(f"[DualBroker] history_deal_list_query 失败 {futu_order_id}: {e}")
            if _attempt < 2:
                time.sleep(0.5)

        # ── fallback: order_list_query (SIM 可能返回0) ──
        try:
            with lock:
                ret, data = ctx.order_list_query(order_id=futu_order_id, trd_env=trd_env)
            if ret == RET_OK and not data.empty:
                # 2026-07-21 根因修复: 富途成交均价字段是 dealt_avg_price (avg_price 系笔误,
                #   永远得 0 → fallback 失效 → sim 全部交易用 bid/ask 兜底记账, 价格系统性失真).
                #   保留旧键作末位兼容. 回滚: 恢复单行 .get("avg_price", 0).
                avg = data.iloc[0].get("dealt_avg_price", 0) or data.iloc[0].get("avg_price", 0)
                dealt = data.iloc[0].get("dealt_qty", 0)
                if avg and float(avg) > 0 and float(dealt or 0) > 0:
                    return float(avg), float(dealt)
        except Exception as e:
            logger.debug(f"[DualBroker] order_list_query fallback 失败 {futu_order_id}: {e}")

        return 0.0, 0.0

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

    def get_short_pool(self, code_list, target_env: str = "sim") -> Dict[str, Dict[str, Any]]:
        """借券池/费率/可空状态快照 (futu get_margin_ratio; 2026-07-30 实测 SIM/REAL 均可查).

        Returns {code: {"pool": 股数|None, "fee_rate": 年率%|None, "shortable": bool|None}}.
        任何异常/空结果返回 {} — 是否放行由调用方决定 (门默认 fail-open)。
        """
        def _f(v):
            try:
                f = float(v)
                return f if f == f else None  # NaN → None
            except (TypeError, ValueError):
                return None
        try:
            ctx, _acc_id, _trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.get_margin_ratio(code_list=list(code_list))
            if ret != 0 or data is None or data.empty:
                return {}
            out = {}
            for _, row in data.iterrows():
                code = str(row.get("code", ""))
                if not code:
                    continue
                sp = row.get("is_short_permit")
                out[code] = {
                    "pool": _f(row.get("short_pool_remain")),
                    "fee_rate": _f(row.get("short_fee_rate")),
                    "shortable": (bool(sp) if sp is not None and sp == sp else None),
                }
            return out
        except Exception as e:
            logger.debug(f"[DualBroker] get_short_pool 失败 (调用方 fail-open): {e}")
            return {}

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
                    remark=f"pb_stop_{symbol}_{target_env}",  # B4: STOP 单留痕
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
        for ctx in (self._sim_ctx, self._real_ctx):
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
        self._sim_ctx = self._real_ctx = None
        self._connected = False
