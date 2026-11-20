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

    def __init__(self, *, exit_ledger_path=None):
        self._exit_ledger_path = exit_ledger_path
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

    def _notional_sanity_check(self, symbol: str, qty, price: float, is_close: bool = False,
                               target_env: str = "sim", reference_price: float = 0.0) -> bool:
        """兜底检查: 单笔名义 ≤ equity × 30%. 平仓单放行, 参考价/equity 不可用时 fail-close.

        2026-10-17: MARKET 单 order.price=0.0 曾在此直接 return True ⇒ 30% 硬上限与
        equity fail-close 对市价单完全失效。市价单必须带 reference_price (下单前
        reprice/盘口价) 才能计算名义; 两者都缺 ⇒ fail-close, 不再放行。
        """
        if is_close:
            return True
        if qty <= 0:
            logger.error(f"🛑 [DualBroker] {symbol} 拒绝: qty={qty} 非法 (名义无法计算)")
            return False
        _ref_price = float(price or 0)
        if _ref_price <= 0:
            _ref_price = float(reference_price or 0)
        if _ref_price <= 0:
            _ref_price = self._estimate_price_for_cap(symbol)
        if _ref_price <= 0:
            logger.error(
                f"🛑 [DualBroker] {symbol} 拒绝: 市价单缺少参考价, 名义上限无法评估 "
                f"(order.price=0, reference_price=0, 行情不可用) — fail-close"
            )
            return False
        notional = float(qty) * _ref_price
        equity = 0.0
        try:
            from app.risk.capital_safety import get_capital_safety_system_for_arm
            _cap = get_capital_safety_system_for_arm(target_env)
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

    def _estimate_price_for_cap(self, symbol: str) -> float:
        """为市价单兜底门禁估价: 只 peek 已注册 router, 不在下单热路径新建连接."""
        try:
            from app.data import market_data_router as _mdr
            router = getattr(_mdr, "_shared_router", None)
            if router is None:
                return 0.0
            snap = router.get_quote(symbol)
            if isinstance(snap, dict):
                for key in ("price", "last_price", "close", "last"):
                    try:
                        value = float(snap.get(key) or 0)
                    except (TypeError, ValueError) as error:
                        logger.warning(
                            f"[DualBroker] {symbol} 快照字段 {key} 无法转价格: {error}",
                            exc_info=True)
                        continue
                    if value > 0:
                        return value
            return 0.0
        except Exception as error:
            logger.debug(f"[DualBroker] {symbol} 市价单估价失败: {error}")
            return 0.0

    # ── 下单 ──────────────────────────────────────────────

    def submit_order(self, order, target_env: str = "sim") -> Dict[str, Any]:
        """提交订单；保留旧字段，并用 submit_state 区分可否安全重试。

        submit_state 为 ACCEPTED / DEFINITIVE_REJECT / UNKNOWN。只要已取得 order id，
        后续成交查询异常也仍返回 ACCEPTED 且保留该 id。
        """
        _place_attempted = False
        _accepted_id = ""
        try:
            from futu import TrdSide, OrderType as FutuOrderType, RET_OK
            from app.core.models import Direction, OrderType

            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            _order_meta = getattr(order, 'metadata', None) or {}
            _is_close = bool(_order_meta.get('is_exit', False)) if isinstance(_order_meta, dict) else False
            _ref_price = 0.0
            if isinstance(_order_meta, dict):
                try:
                    _ref_price = float(_order_meta.get('reference_price') or 0)
                except (TypeError, ValueError):
                    _ref_price = 0.0
            if not self._notional_sanity_check(
                    order.symbol, order.quantity, order.price,
                    is_close=_is_close, target_env=target_env,
                    reference_price=_ref_price):
                return {"success": False, "submit_state": "DEFINITIVE_REJECT",
                        "error": "dual_broker_notional_cap", "futu_order_id": ""}

            # P0-2 stage 1 (DEB-20261025): REAL 下单前只读记录券商容量与同标的
            # 活跃单。影子模块内部吞掉所有异常, 不拦截、不改返回值; 第二阶段
            # 的有限拒绝须等样本裁决后另行开启。
            if target_env == "real":
                try:
                    from app.execution.broker_capacity_shadow import (
                        record_order_capacity_shadow,
                    )
                    record_order_capacity_shadow(
                        ctx,
                        acc_id=acc_id,
                        trd_env=trd_env,
                        lock=lock,
                        order=order,
                        target_env=target_env,
                        is_exit=_is_close,
                    )
                except Exception as _capacity_error:
                    logger.warning(
                        "[DualBroker] capacity shadow failed (order path unchanged) "
                        f"{order.symbol}: {_capacity_error}",
                        exc_info=True,
                    )

            trd_side = TrdSide.BUY if order.direction == Direction.LONG else TrdSide.SELL
            futu_order_type = FutuOrderType.MARKET if order.order_type == OrderType.MARKET else FutuOrderType.NORMAL

            _place_attempted = True
            # R04 (2026-09-16, REVIEW-20260916-full-session-audit §7.1, 用户批准):
            #   显式声明 RTH-only + DAY — 消除订单意图/成交时段语义不确定 (默认 Session.NONE
            #   可能被 broker 解释为延长时段成交). 未来启用 ETH 再显式 session=ETH+限价单.
            from futu import TimeInForce, Session
            with lock:
                ret, data = ctx.place_order(
                    price=order.price, qty=order.quantity, code=order.symbol,
                    trd_side=trd_side, order_type=futu_order_type,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                    time_in_force=TimeInForce.DAY, session=Session.RTH,
                    remark=getattr(order, 'client_order_id', '') or None,
                )
            try:
                _candidate_id = str(data["order_id"].values[0]).strip()
                if _candidate_id.lower() not in ("", "none", "nan"):
                    _accepted_id = _candidate_id
            except Exception:
                pass
            try:
                _broker_status = str(data["order_status"].values[0] or "")
            except Exception:
                _broker_status = ""

            if ret != RET_OK and not _accepted_id:
                # A failed response after submission does not prove zero execution.
                return {"success": False, "submit_state": "UNKNOWN",
                        "broker_order_status": _broker_status,
                        "error": f"place_order 失败: {data}", "futu_order_id": ""}
            if not _accepted_id:
                logger.error(f"[DualBroker] broker 已响应成功但 order_id 不可读 {order.symbol}")
                return {"success": False, "submit_state": "UNKNOWN",
                        "broker_order_status": _broker_status,
                        "error": "broker order_id missing", "futu_order_id": "",
                        "filled_price": 0.0, "filled_qty": 0.0}
            if ret != RET_OK:
                logger.warning(
                    f"[DualBroker] broker 返回非成功但已给 order id，按已受理锁定 "
                    f"{order.symbol} futu_id={_accepted_id}"
                )

            try:
                filled_price, filled_qty = self._query_fill(ctx, _accepted_id, trd_env, lock)
            except Exception as exc:
                logger.warning(
                    f"[DualBroker] 下单已受理但成交查询异常 {order.symbol} "
                    f"futu_id={_accepted_id}: {exc}",
                    exc_info=True,
                )
                filled_price, filled_qty = 0.0, 0.0

            if filled_qty > 0 and filled_qty < float(order.quantity):
                logger.warning(
                    f"[DualBroker] 部分成交: {order.symbol} 下单 {order.quantity} 实际成交 {filled_qty} "
                    f"env={target_env} futu_id={_accepted_id}"
                )
            logger.info(
                f"[DualBroker] 下单已受理: {order.symbol} {order.direction} qty={order.quantity} "
                f"env={target_env} futu_id={_accepted_id} fill={filled_price} filled_qty={filled_qty}"
            )
            return {"success": True, "submit_state": "ACCEPTED", "futu_order_id": _accepted_id,
                    "broker_order_status": _broker_status,
                    "filled_price": filled_price, "filled_qty": filled_qty, "error": ""}

        except Exception as e:
            logger.exception(f"[DualBroker] submit_order 异常 {order.symbol}: {e}")
            state = "ACCEPTED" if _accepted_id else ("UNKNOWN" if _place_attempted else "DEFINITIVE_REJECT")
            return {"success": bool(_accepted_id), "submit_state": state, "error": str(e),
                    "futu_order_id": _accepted_id, "broker_order_status": "",
                    "filled_price": 0.0, "filled_qty": 0.0}

    def cancel_order(self, futu_order_id: str, target_env: str = "sim") -> Dict[str, Any]:
        """撤单 (尽力而为) — [DEB-20260901 选项1②] 入场限价路由 TTL 到期撤单用。

        与 close_position 限价腿同款 modify_order(CANCEL) 语义。撤单请求与成交
        之间存在竞态窗 — 调用方须在撤单后复查订单状态 (可能已成交)。

        Returns: {"success": bool, "error": str}
        """
        try:
            from futu import ModifyOrderOp, RET_OK
            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL, order_id=str(futu_order_id),
                    qty=0, price=0, trd_env=trd_env,
                    acc_id=int(acc_id) if acc_id else 0,
                )
            if ret != RET_OK:
                return {"success": False, "error": f"modify_order CANCEL 失败: {data}"}
            return {"success": True, "error": ""}
        except Exception as e:
            logger.warning(f"[DualBroker] cancel_order 异常 {futu_order_id}: {e}")
            return {"success": False, "error": str(e)}

    def _exit_lifecycle(self):
        from app.execution.exit_lifecycle import ExitLedger, ExitLifecycle
        path = getattr(self, '_exit_ledger_path', None)
        if path is None:
            from app.core.paths import get_exit_ledger_db
            path = get_exit_ledger_db()
        return ExitLifecycle(self, ExitLedger(path), getattr(self, '_exit_clock', None))

    def close_position(self, symbol: str, qty: float, direction_held: str,
                       target_env: str = "sim", limit_price: float = 0.0,
                       limit_timeout_s: float = 8.0, session: str = "RTH", *,
                       exit_owner: dict = None,
                       exit_decision: dict = None) -> Dict[str, Any]:
        """Close only the owned quantity through a durable, recoverable send gate."""
        from app.execution.exit_lifecycle import held
        if not isinstance(exit_owner, dict) or not isinstance(exit_decision, dict):
            return held('exit_identity_required', owner=exit_owner, decision=exit_decision)
        try:
            return self._exit_lifecycle().request(symbol, qty, direction_held, target_env,
                limit_price, limit_timeout_s, exit_owner, exit_decision, session=session)
        except Exception:
            logger.exception('[DualBroker] durable close failed symbol=%s', symbol)
            return held('exit_ledger_or_identity_error', owner=exit_owner, decision=exit_decision)

    def pending_closes(self) -> list:
        """Enumerate all unsettled accounts/environments without constructing contexts."""
        return self._exit_lifecycle().ledger.list_unsettled()

    def resume_close(self, intent_id: str, *, allow_submit: bool = True) -> Dict[str, Any]:
        from app.execution.exit_lifecycle import held
        try:
            return self._exit_lifecycle().resume(intent_id, allow_submit=allow_submit)
        except Exception:
            logger.exception('[DualBroker] durable resume failed intent=%s', intent_id)
            result = held('exit_ledger_error')
            result['exit_intent_id'] = intent_id
            return result

    def release_exit_intent(self, intent_id: str, *, reason: str,
                            evidence: dict | None = None,
                            kind: str = "manual_release") -> tuple:
        """Archive and delete one durable exit intent without submitting an order."""
        try:
            lifecycle = self._exit_lifecycle()
            return lifecycle.ledger.archive_and_delete(
                intent_id, reason=reason, now=lifecycle.clock.time(),
                evidence=evidence or {}, kind=kind)
        except Exception:
            logger.exception('[DualBroker] exit intent release failed intent=%s', intent_id)
            return None, 'exit_release_error'

    def acknowledge_close(self, intent_id: str, receipt: dict) -> bool:
        try:
            lifecycle = self._exit_lifecycle()
            intent = lifecycle.ledger.get(intent_id)
            if not intent:
                return False
            account = getattr(self, 'real_account_id' if intent['target_env'] == 'real' else 'sim_account_id', '')
            if str(account) != intent['account_id']:
                return False
            return lifecycle.ledger.ack_booked(intent_id, receipt, lifecycle.clock.time())
        except Exception:
            logger.exception('[DualBroker] exit acknowledgement failed intent=%s', intent_id)
            return False

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
            同一 order_id 可能有多笔成交, price 按数量加权, qty 累加.
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
                    _notional = 0.0
                    _sum_qty = 0.0
                    for _, deal in data.iterrows():
                        deal_order_id = str(deal.get("order_id", ""))
                        if deal_order_id == futu_order_id:
                            price = float(deal.get("price", 0))
                            dealt_qty = float(deal.get("qty", 0) or 0)
                            if price > 0 and dealt_qty > 0:
                                _notional += price * dealt_qty
                                _sum_qty += dealt_qty
                    if _notional > 0 and _sum_qty > 0:
                        return _notional / _sum_qty, _sum_qty
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

    def query_order_fill(self, futu_order_id: str, target_env: str = "real"):
        """Return broker fill evidence for a submitted order.

        This is read-only and is used to reconcile broker-side protective STOP
        fills after the entry ledger is already open.
        """
        ctx, _acc_id, trd_env, lock = self._ctx_and_acc(target_env)
        return self._query_fill(ctx, str(futu_order_id), trd_env, lock)

    def query_order_status(self, futu_order_id: str, target_env: str = "real") -> Dict[str, Any]:
        """Return read-only broker order identity/status evidence."""
        try:
            from futu import RET_OK
            ctx, _acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.order_list_query(
                    order_id=str(futu_order_id),
                    trd_env=trd_env,
                    refresh_cache=True,
                )
            if ret != RET_OK or data is None or data.empty:
                return {"success": False, "error": str(data)}
            row = data.iloc[0]
            fields = (
                "order_id", "code", "trd_side", "order_type", "order_status",
                "qty", "dealt_qty", "dealt_avg_price", "price", "aux_price",
                "time_in_force", "fill_outside_rth", "remark",
                # [RFC-169 复查] 入场时段回填需要券商订单时间 (ET 墙钟)
                "create_time", "updated_time",
            )
            return {
                "success": True,
                **{key: row.get(key) for key in fields if key in data.columns},
            }
        except Exception as error:
            logger.warning(
                "[DualBroker] query_order_status failed %s env=%s: %s",
                futu_order_id, target_env, error, exc_info=True,
            )
            return {"success": False, "error": str(error)}

    def query_order_fees(self, futu_order_ids: list[str], target_env: str = "real") -> Dict[str, Any]:
        """Return read-only broker fee evidence for submitted orders."""
        try:
            from futu import RET_OK
            order_ids = [str(order_id) for order_id in futu_order_ids if str(order_id or "").strip()]
            if not order_ids:
                return {"success": False, "error": "no order ids"}
            ctx, _acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.order_fee_query(
                    order_id_list=order_ids,
                    trd_env=trd_env,
                )
            if ret != RET_OK or data is None or data.empty:
                return {"success": False, "error": str(data)}
            rows = data.to_dict("records")
            fees = {
                str(row.get("order_id")): float(row.get("fee_amount") or 0)
                for row in rows
                if row.get("order_id") is not None
            }
            return {
                "success": True,
                "fees": fees,
                "rows": rows,
            }
        except Exception as error:
            logger.warning(
                "[DualBroker] query_order_fees failed env=%s: %s",
                target_env, error, exc_info=True,
            )
            return {"success": False, "error": str(error)}

    def get_positions(self, target_env: str = "sim") -> list:
        """查询指定账户持仓."""
        result = self.query_positions(target_env)
        return result["positions"] if result.get("success") else []

    def query_positions(self, target_env: str = "sim", *, refresh_cache: bool = False) -> Dict[str, Any]:
        """Return broker positions plus explicit query success evidence.

        ``get_positions`` keeps its historical list return for existing callers.
        Protective-stop recovery needs to distinguish "zero positions" from
        "broker query failed", so it uses this evidence-returning variant.
        """
        try:
            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            query = {
                "trd_env": trd_env,
                "acc_id": int(acc_id) if acc_id else 0,
            }
            if refresh_cache:
                query["refresh_cache"] = True
            with lock:
                ret, data = ctx.position_list_query(**query)
            if ret == 0 and data is not None and not data.empty:
                # 2026-09-21 全链路复查修复 (P0-2): 与 broker_adapter.get_positions()
                # 同口径 — REAL 综合账户可能返回港股等跨市场持仓, 按 code 前缀隔离美股行。
                # 消费方 (playbook 按 US.XXX 精确匹配) 在 SIM 下行为零变化。
                positions = [
                    r for r in data.to_dict("records")
                    if str(r.get("code", "")).startswith("US.")
                ]
                return {"success": True, "positions": positions, "error": ""}
            if ret == 0:
                return {"success": True, "positions": [], "error": ""}
            return {"success": False, "positions": [], "error": str(data)}
        except Exception as e:
            logger.warning(f"[DualBroker] get_positions {target_env} 失败: {e}")
            return {"success": False, "positions": [], "error": str(e)}

    def query_symbol_orders(self, symbol: str, target_env: str = "real") -> Dict[str, Any]:
        """Return same-symbol broker orders for read-only safety checks."""
        try:
            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.order_list_query(
                    code=str(symbol), trd_env=trd_env,
                    acc_id=int(acc_id) if acc_id else 0,
                    refresh_cache=True,
                )
            if ret != 0 or data is None:
                return {"success": False, "orders": [], "error": str(data)}
            orders = [] if data.empty else data.to_dict("records")
            return {"success": True, "orders": orders, "error": ""}
        except Exception as error:
            logger.warning(
                "[DualBroker] query_symbol_orders failed %s env=%s: %s",
                symbol, target_env, error, exc_info=True,
            )
            return {"success": False, "orders": [], "error": str(error)}

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

    def get_account_info(self, target_env: str = "sim") -> Dict[str, Any]:
        """Query and USD-normalize one account's Futu funds snapshot."""
        try:
            from futu import RET_OK
            from app.execution.futu_accounting import normalize_futu_account

            ctx, acc_id, trd_env, lock = self._ctx_and_acc(target_env)
            with lock:
                ret, data = ctx.accinfo_query(
                    trd_env=trd_env,
                    acc_id=int(acc_id) if acc_id else 0,
                    refresh_cache=True,
                )
            if ret != RET_OK or data is None or data.empty:
                logger.warning("[DualBroker] account info query failed env=%s ret=%s", target_env, ret)
                return {}

            raw = dict(data.iloc[0])
            account = normalize_futu_account(raw)
            account["_target_env"] = target_env
            return account
        except Exception as e:
            logger.warning("[DualBroker] account info query exception env=%s: %s", target_env, e,
                           exc_info=True)
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
            # 2026-10-06 P0 (首笔 LIVE 探针实测事故): 富途 STOP 单触发价必须走
            #   aux_price — 只传 price 会被拒「下单缺少必要参数触发价格」
            #   (仓内 8/5 已实证, scripts/probe_sim_stop_20260805.py:44 + 看板
            #   executor.py:1236 同款写法); price=0 = 触发后市价成交。
            #   time_in_force=GTC: 默认 DAY 会在当日收盘失效, 而 W_WEEK 持仓
            #   最多持 5 个交易日, 兜底必须跨日有效 (同看板 executor.py:1236)。
            from futu import TimeInForce as _TIF
            with lock:
                ret, data = ctx.place_order(
                    price=0, qty=qty, code=symbol,
                    trd_side=stop_side, order_type=FutuOrderType.STOP,
                    trd_env=trd_env, acc_id=int(acc_id) if acc_id else 0,
                    time_in_force=_TIF.GTC,
                    aux_price=stop_price,  # 触发价 (Futu STOP 单必须用 aux_price)
                    trail_spread=0,  # 非追踪止损
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
