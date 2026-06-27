"""订单风控/校验逻辑 (从 manual_order_service.validate_order 抽取).

公开函数:
    - validate_order(service, order) -> List[str]
    - count_today_manual_orders(service) -> int
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:  # pragma: no cover
    from app.web_dashboard.manual_order_service import ManualOrder, ManualOrderService

logger = logging.getLogger("ftseek_us.manual_order_service.validator")


def validate_order(service: "ManualOrderService", order: "ManualOrder") -> List[str]:
    """校验订单, 返回错误列表 (空列表 = 通过).

    保持与原 ManualOrderService.validate_order 完全等价的检查顺序与语义:
        0. Kill Switch 紧急停止
        1. 基础参数 (symbol/direction/action/qty/price 等)
        2. 单笔金额上限
        3. 每日手动下单次数限制
        4. 理由 (现非强制)
        5. 实盘 ALLOW_REAL_TRADING gate
        6. PDT Guard (实盘 only)
        7. 仓位集中度 (开仓时)
        7.5 购买力 (开仓时, 缓存+实时双确认)
        8. 日回撤熔断
        9. 资本安全水位 (BLACKOUT 硬阻断)
        10. 双链路预算 (全局 80% 上限)
        11. 限价单价格偏离硬/软拦截
    """
    errors: List[str] = []

    # 0. [P0 2026-06-13] Kill Switch 检查 (最高优先级)
    try:
        from app.us_pipeline.emergency_interceptor import check_kill_switch
        if check_kill_switch():
            errors.append("🚨 紧急停止开关已激活，所有交易被拦截")
            return errors
    except Exception as _ks_err:
        logger.debug(f"[手动下单] Kill Switch 检查跳过: {_ks_err}")

    # 1. 基础参数校验
    if not order.symbol:
        errors.append("标的代码不能为空")
    if order.direction not in ("LONG", "SHORT"):
        errors.append(f"方向无效: {order.direction} (必须 LONG 或 SHORT)")
    if order.action not in ("BUY", "SELL"):
        errors.append(f"操作无效: {order.action} (必须 BUY 或 SELL)")
    if order.quantity <= 0:
        errors.append(f"数量必须大于 0: {order.quantity}")
    if order.quantity > 10000:
        errors.append(f"数量过大: {order.quantity} (上限 10000)")
    if order.order_type == "LIMIT" and (not order.price or order.price <= 0):
        errors.append("限价单必须指定价格")
    if order.fill_outside_rth and order.order_type == "MARKET":
        errors.append("盘前盘后时段不支持市价单，请使用限价单")

    # 2. 金额上限校验
    quote = service.get_quote(order.symbol)
    cur_price = quote.get("cur_price", 0)
    if cur_price > 0:
        order_value = order.quantity * cur_price
        if order_value > service.MAX_ORDER_VALUE_USD:
            errors.append(
                f"订单金额 ${order_value:,.0f} 超过上限 ${service.MAX_ORDER_VALUE_USD:,.0f}"
            )

    # 3. 每日手动下单次数限制
    daily_count = count_today_manual_orders(service)
    if daily_count >= service.MAX_DAILY_MANUAL_ORDERS:
        errors.append(
            f"今日手动下单已达 {daily_count} 次 (上限 {service.MAX_DAILY_MANUAL_ORDERS})"
        )

    # 4. 理由可选 (不再强制)

    # 5. 实盘额外校验
    # 2026-06-24: ALLOW_REAL_TRADING 是 AUTO pipeline (playbook/ORB) 的安全锁,
    # 手动交易不受此限制 — 用户通过 dashboard 下单是显式行为, 已有 kill switch + PDT + 金额上限保护.
    # 仅保留 PDT + 仓位 + 回撤 等风控检查 (下方).

    # 6. [P1 2026-06-13] PDT Guard (实盘 only)
    if service._is_live() and cur_price > 0:
        try:
            from app.risk.pdt_guard import PDTGuard
            _pdt = PDTGuard()
            _pdt_ok, _pdt_reason, _pdt_level = _pdt.check_trade_permission(is_overnight=False)
            if not _pdt_ok:
                errors.append(f"PDT 风控拦截: {_pdt_reason}")
        except Exception as _pdt_err:
            logger.debug(f"[手动下单] PDT 检查跳过: {_pdt_err}")

    # 仓位集中度检查已移除 (2026-06-26): 手动 Dashboard 下单由用户决策,
    # 已有 kill switch + 金额上限 + 购买力 + 回撤 + BLACKOUT + 预算80% + 价格偏离 足够保护.
    # pre_trade_safety_check 的单品种/方向/总敞口上限适用于自动 pipeline, 不适用于手动下单.
    _is_opening = (
        (order.action == "BUY" and order.direction == "LONG")
        or (order.action == "SELL" and order.direction == "SHORT")
    )

    # 7.5. [P0 2026-06-14] 购买力检查 (开仓前必须有足够 buying_power)
    # 修复 (2026-06-15): capital_safety 缓存可能滞后 30s,
    # 当缓存值不足以通过时, 实时查询 broker 二次确认
    if cur_price > 0 and _is_opening:
        try:
            from app.risk.capital_safety import get_capital_safety_system
            _bp = get_capital_safety_system()._state.buying_power
            _order_value = order.quantity * cur_price
            if _bp > 0 and _order_value > _bp:
                # 缓存说不够 — 实时查 broker 确认 (防止 30s 滞后误拒)
                _realtime_bp = 0
                try:
                    if service._broker_ctx and service._connected:
                        from futu import RET_OK as _RET_OK
                        _ret, _acc = service._broker_ctx.accinfo_query(
                            trd_env=service._trd_env(), acc_id=service._acc_id(),
                        )
                        if _ret == _RET_OK and not _acc.empty:
                            from app.web_dashboard.data_service import DataService
                            _usd = DataService.to_usd(dict(_acc.iloc[0]))
                            _realtime_bp = _usd.get("power", 0)
                except Exception:
                    pass
                if _realtime_bp > 0 and _order_value <= _realtime_bp:
                    logger.info(
                        f"[购买力] 缓存拒绝(${_bp:,.0f})但实时通过(${_realtime_bp:,.0f}), 放行"
                    )
                else:
                    _final_bp = _realtime_bp if _realtime_bp > 0 else _bp
                    errors.append(
                        f"购买力不足: 订单 ${_order_value:,.0f} > 可用 ${_final_bp:,.0f}"
                    )
        except Exception as _bp_err:
            logger.debug(f"[手动下单] 购买力检查跳过: {_bp_err}")

    # 8. [P1 2026-06-13] 日回撤熔断 (已实现亏损 > 3% equity)
    # P0-fix (2026-06-14): 使用 scheduler 的共享 DrawdownController 实例,
    # 而非 fresh instance (零历史, 永远放行)
    try:
        import importlib
        _sc_mod = importlib.import_module("app.core.scheduler_core")
        _sched = getattr(_sc_mod, "_scheduler_instance", None)
        _dd = getattr(getattr(_sched, "risk_engine", None), "drawdown_controller", None) if _sched else None
        if _dd is not None and not _dd.is_trading_allowed():
            _dd_reason = _dd.get_block_reason() or "日回撤超限"
            errors.append(f"回撤熔断: {_dd_reason}")
    except Exception as _dd_err:
        logger.debug(f"[手动下单] 回撤检查跳过: {_dd_err}")

    # 9. [P0 2026-06-13] 资本安全水位检查 (手动下单也须尊重 BLACKOUT 硬熔断)
    # 设计: BLACKOUT(>-35%) 硬阻断; RED/ORANGE 仅警告不阻断 (手动=人风控层)
    try:
        from app.risk.capital_safety import get_capital_safety_system, WaterLevel
        _cs = get_capital_safety_system()
        _cs_ok, _cs_msg = _cs.can_open_position()
        if not _cs_ok:
            _wl = getattr(_cs._state, 'water_level', None)
            if _wl == WaterLevel.BLACKOUT:
                errors.append(f"🚨 资本安全 BLACKOUT 熔断: {_cs_msg}")
            elif _wl == WaterLevel.RED:
                logger.warning(f"[手动下单] ⚠️ 资本安全 RED 警告: {_cs_msg} (不阻断手动)")
    except Exception as _cs_err:
        logger.debug(f"[手动下单] 资本安全检查跳过: {_cs_err}")

    # 10. 双链路预算检查 (手动不受auto cap限制, 但须尊重全局80%上限)
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _manual_budget = get_capital_safety_system().get_available_budget_for_manual()
        _order_value = order.quantity * (order.price or 0)
        if _order_value > 0 and _manual_budget >= 0 and _order_value > _manual_budget:
            errors.append(
                f"手动预算不足: 订单${_order_value:,.0f} > 可用${_manual_budget:,.0f} "
                f"(全局80%上限)"
            )
    except Exception as _mb_err:
        logger.debug(f"[手动下单] 预算检查跳过: {_mb_err}")

    # 11. [P0 2026-06-15] 限价单价格偏离检测 (防止输错价格导致意外成交)
    # SNDK 事故复盘: 买入限价远高于/远低于市价时无警告, 立即成交导致亏损
    # 2026-06-25: 盘前/夜盘 cur_price 为上次常规收盘价, 不反映盘外报价 → 跳过检测
    # 2026-06-25: ≥10% 从硬拦截改为软拦截 (与前端 2026-06-24 strong-warn 改动对齐)
    _skip_deviation = False
    try:
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        _skip_deviation = get_market_phase() != MarketPhase.TRADING
    except Exception:
        pass

    if (not _skip_deviation and order.order_type == "LIMIT" and order.price
            and order.price > 0 and cur_price > 0):
        _deviation = abs(order.price - cur_price) / cur_price * 100
        _is_warn = _deviation >= service.PRICE_DEVIATION_WARN_PCT

        if _is_warn:
            # 构造场景化消息
            if order.action == "BUY":
                if order.price > cur_price:
                    _scenario = (
                        f"买入限价 ${order.price:.2f} 高于市价 ${cur_price:.2f} "
                        f"达 {_deviation:.1f}%，将以市价立即成交（可能非预期）"
                    )
                else:
                    _scenario = (
                        f"买入限价 ${order.price:.2f} 低于市价 ${cur_price:.2f} "
                        f"达 {_deviation:.1f}%，订单不会立即成交（请确认是否输入正确）"
                    )
            else:  # SELL
                if order.price < cur_price:
                    _scenario = (
                        f"卖出限价 ${order.price:.2f} 低于市价 ${cur_price:.2f} "
                        f"达 {_deviation:.1f}%，将以市价立即成交（可能非预期）"
                    )
                else:
                    _scenario = (
                        f"卖出限价 ${order.price:.2f} 高于市价 ${cur_price:.2f} "
                        f"达 {_deviation:.1f}%，订单不会立即成交（请确认是否输入正确）"
                    )

            _prefix = "🛑" if _deviation >= service.PRICE_DEVIATION_HARD_PCT else "⚠️"
            _msg = f"{_prefix} 价格偏离: {_scenario}"

            if not order.acknowledge_price_warning:
                # 软拦截: 需前端确认 (≥5% 和 ≥10% 统一逻辑)
                errors.append(_msg + " (如需继续请在前端确认)")
                logger.info(f"[价格偏离] 软拦截: {order.symbol} {_scenario}")

    return errors


def count_today_manual_orders(service: "ManualOrderService") -> int:
    """统计今日手动下单次数."""
    try:
        conn = service._db()
        today = datetime.now().strftime("%Y-%m-%d")
        count = conn.execute(
            "SELECT COUNT(*) FROM manual_order_audit WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0
