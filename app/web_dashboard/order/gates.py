"""手动交易闸门只读快照 (2026-09-08, REVIEW-20260908 批1 B1).

背景: `order/validator.py::validate_order` 的闸门只在**拒单后**才可见 —— 实测
`manual_order_audit` 83 条手动单里 13 条被拒 (15.7%), 其中 kill switch / 资本水位 /
日回撤熔断 / 手动预算 / 单笔金额上限 / 当日单数上限 六类都是"后端早已知道、
前端零呈现", 用户只能靠一次失败往返得知 (2026-06-30 连续 3 次撞同一道闸)。

本模块把同一批判据**只读地**聚合给交易页预检条 (`/api/trade/gates`)。

纪律:
  - 纯只读: 不改任何阈值/判据, 不新增拦截, 不写库;
  - 与 validator 同源: 每项注明对应 validator 步骤, 禁止在此另立判据;
  - 读取失败一律 fail-open 且标 unknown: 呈现层不得因为自己读不到就宣称"被阻断"
    (否则会把依赖故障变成假拦截, 与 trading_day_check 的 fail-open 纪律一致);
  - **不含 PDT**: 2026-08-02 用户裁决关闭 (富途证券(香港)账户不适用 FINRA PDT —
    `app/risk/pdt_guard.py:347`, `config/unified_config.yaml` risk.pdt_guard.enabled=false);
    2026-09-08 用户复述"美股 PDT 限制条件在当前富途中不存在"。UI 呈现一个不存在的
    约束 = 纯噪声, 故本模块不输出任何 PDT 字段 (钉子:
    tests/web_dashboard/test_trade_ui_gates_contract.py)。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek_us.web_dashboard.order.gates")


def _kill_switch() -> Optional[bool]:
    """validator §0 同源: 紧急停止开关 (激活即拦截一切手动单)."""
    try:
        from app.us_pipeline.emergency_interceptor import check_kill_switch
        return bool(check_kill_switch())
    except Exception as err:  # pragma: no cover - 依赖缺失时 fail-open
        logger.debug(f"[gates] kill switch 读取跳过: {err}")
        return None


def _capital_safety() -> Dict[str, Any]:
    """validator §9/§10 同源: 资本安全水位 + 手动预算余量.

    注意口径: 手动单只在 BLACKOUT 被硬阻断 (validator §9 对 RED 仅 logger.warning),
    故 water_blocks_manual 只在 BLACKOUT 为 True; RED/锁定/fail-safe 走 water_msg 警告。
    """
    out: Dict[str, Any] = {
        "water_level": "unknown",
        "water_blocks_manual": False,
        "water_msg": "",
        "manual_budget_usd": None,
    }
    try:
        from app.risk.capital_safety import WaterLevel, get_capital_safety_system
        cs = get_capital_safety_system()
        allowed, msg = cs.can_open_position()
        wl = getattr(getattr(cs, "_state", None), "water_level", None)
        if isinstance(wl, WaterLevel):
            out["water_level"] = wl.value
            out["water_blocks_manual"] = (wl == WaterLevel.BLACKOUT)
        out["water_msg"] = "" if allowed else str(msg or "")
        try:
            out["manual_budget_usd"] = round(float(cs.get_available_budget_for_manual()), 2)
        except Exception as err:  # pragma: no cover
            logger.debug(f"[gates] 手动预算读取跳过: {err}")
    except Exception as err:  # pragma: no cover
        logger.debug(f"[gates] 资本安全读取跳过: {err}")
    return out


def _drawdown() -> Dict[str, Any]:
    """validator §8 同源: 日回撤熔断 (共享 DrawdownController 实例, 非 fresh)."""
    out: Dict[str, Any] = {"drawdown_halt": False, "drawdown_reason": ""}
    try:
        sched_mod = importlib.import_module("app.core.scheduler_core")
        sched = getattr(sched_mod, "_scheduler_instance", None)
        controller = getattr(getattr(sched, "risk_engine", None),
                             "drawdown_controller", None) if sched else None
        if controller is not None and not controller.is_trading_allowed():
            out["drawdown_halt"] = True
            out["drawdown_reason"] = controller.get_block_reason() or "日回撤超限"
    except Exception as err:  # pragma: no cover
        logger.debug(f"[gates] 回撤熔断读取跳过: {err}")
    return out


def _quotas(service: Any) -> Dict[str, Any]:
    """validator §2/§3/§11 同源: 单笔金额上限 + 当日手动单额度 + 限价偏离阈值.

    阈值随服务常量下发, 前端不再硬编码 5%/10% (原 trade.js 两处写死)。
    """
    out: Dict[str, Any] = {
        "max_order_value_usd": None,
        "today_manual_orders": None,
        "max_daily_manual_orders": None,
        "price_deviation_warn_pct": None,
        "price_deviation_hard_pct": None,
    }
    if service is None:
        return out
    # 量纲分型: 金额/次数 = int 绝对值, 阈值 = float 百分数 (5.0 而非 0.05)
    for key, attr, caster in (
        ("max_order_value_usd", "MAX_ORDER_VALUE_USD", int),
        ("max_daily_manual_orders", "MAX_DAILY_MANUAL_ORDERS", int),
        ("price_deviation_warn_pct", "PRICE_DEVIATION_WARN_PCT", float),
        ("price_deviation_hard_pct", "PRICE_DEVIATION_HARD_PCT", float),
    ):
        try:
            value = getattr(service, attr, None)
            out[key] = caster(value) if value is not None else None
        except Exception:  # pragma: no cover
            out[key] = None
    try:
        from app.web_dashboard.order.validator import count_today_manual_orders
        out["today_manual_orders"] = int(count_today_manual_orders(service))
    except Exception as err:  # pragma: no cover
        logger.debug(f"[gates] 当日手动单计数跳过: {err}")
    return out


def collect_manual_gates(service: Any = None) -> Dict[str, Any]:
    """聚合手动下单闸门快照 (只读, 供 /api/trade/gates 与前端预检条消费).

    `blocks_now` = 此刻会拒绝**任何**手动单的全局闸门文案 (空 = 无全局阻断);
    与订单相关的额度类 (单笔金额/购买力/预算) 由前端按本单金额比对, 不在此预判。
    """
    if service is None:
        try:
            from app.web_dashboard.manual_order_service import get_manual_order_service
            service = get_manual_order_service()
        except Exception as err:  # pragma: no cover
            logger.debug(f"[gates] manual_order_service 不可用: {err}")
            service = None

    gates: Dict[str, Any] = {}
    kill = _kill_switch()
    gates["kill_switch_active"] = bool(kill)
    gates["kill_switch_known"] = kill is not None
    gates.update(_capital_safety())
    gates.update(_drawdown())
    gates.update(_quotas(service))

    blocks: List[str] = []
    if gates["kill_switch_active"]:
        blocks.append("🚨 紧急停止开关已激活，所有交易被拦截")
    if gates["water_blocks_manual"]:
        blocks.append(f"🚨 资本安全 BLACKOUT 熔断{('：' + gates['water_msg']) if gates['water_msg'] else ''}")
    if gates["drawdown_halt"]:
        blocks.append(f"回撤熔断: {gates['drawdown_reason']}")
    max_daily = gates.get("max_daily_manual_orders")
    today = gates.get("today_manual_orders")
    if max_daily is not None and today is not None and today >= max_daily:
        blocks.append(f"今日手动下单已达 {today} 次 (上限 {int(max_daily)})")
    gates["blocks_now"] = blocks

    warnings: List[str] = []
    if not gates["water_blocks_manual"] and gates["water_msg"]:
        warnings.append(f"资本安全: {gates['water_msg']}")
    if gates["water_level"] in ("yellow", "orange", "red"):
        warnings.append(f"水位 {gates['water_level'].upper()} — 手动单不被硬拦, 但自动侧已降仓/禁开")
    gates["warnings_now"] = warnings
    return gates
