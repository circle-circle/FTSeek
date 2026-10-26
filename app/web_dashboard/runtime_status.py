"""交易页运行时状态与能力判定 (纯函数).

本模块只回答一个 UI 前必须知道的问题: **现在哪些动作有足够可信的数据支撑?**
它不执行下单、不改闸门、不替代后端 validator, 只把连接/数据来源/新鲜度汇总成
一份可展示、可测试的 runtime contract。

Fail-safe 口径:
  - 券商未连接或状态未知: 所有写动作关闭;
  - 持仓来源未确认或来自 DB fallback: 开仓/减仓/改保护关闭;
  - 订单来源未确认或来自审计 DB: 撤单关闭;
  - 持仓报价降级: 禁止新开仓, 但不阻断减仓;
  - 现有手动闸门 blocks_now: 禁止新开仓 (减仓/撤单仍由后端裁决)。

``runtime.capabilities`` 是前端提示层, 后端 validator/executor 仍是最终防线。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

_BROKER_STATES = {"connected", "degraded", "disconnected", "unknown"}
_DATA_STATES = {"live", "degraded", "unknown"}
_MARKET_STATES = {"regular", "pre", "after", "closed", "unknown"}


def map_market_phase(phase: Any) -> str:
    """把项目内部 phase 归一成交易契约的 5 态。"""
    value = str(phase or "").strip().lower()
    if value in {"regular", "trading", "opening_drive", "morning", "midday",
                 "afternoon", "closing_drive"}:
        return "regular"
    if value in {"pre", "premarket", "pre_market", "pre-market"}:
        return "pre"
    if value in {"after", "after_hours", "after-hours", "postmarket", "post_market"}:
        return "after"
    if value in {"closed", "weekend", "overnight", "holiday"}:
        return "closed"
    return "unknown"


def derive_positions_state(
    positions_raw: Sequence[Mapping[str, Any]] | None,
    authoritative: bool,
) -> str:
    """持仓数据是否来自本轮券商权威应答。"""
    rows = list(positions_raw or [])
    if any(str(p.get("data_source") or "") == "db_fallback" for p in rows):
        return "degraded"
    if authoritative:
        return "live"
    return "unknown"


def derive_quotes_state(positions: Sequence[Mapping[str, Any]] | None) -> str:
    """持仓现价新鲜度。无持仓时返回 unknown, 不冒充行情正常。"""
    rows = list(positions or [])
    if not rows:
        return "unknown"
    bases = [str(p.get("price_basis") or "none") for p in rows]
    if all(b == "live" for b in bases):
        return "live"
    return "degraded"


def derive_orders_state(orders_meta: Mapping[str, Any] | None) -> str:
    """订单查询是否来自券商, 以及是否发生了审计 DB fallback。"""
    meta = dict(orders_meta or {})
    source = str(meta.get("source") or "none")
    broker_ok = bool(meta.get("broker_ok"))
    if source == "audit_db":
        return "degraded"
    if broker_ok:
        return "live"
    return "unknown"


def derive_broker_state(
    *,
    connect_flag: bool,
    account_known: bool,
    positions_state: str,
    orders_state: str,
) -> str:
    """综合连接标志与各数据面证据, 避免只信 ``_connected`` 假在线。"""
    if not connect_flag:
        return "disconnected"
    evidence = sum((bool(account_known), positions_state == "live", orders_state == "live"))
    if positions_state == "degraded" or orders_state == "degraded":
        return "degraded" if evidence else "unknown"
    if evidence == 3:
        return "connected"
    if evidence:
        return "degraded"
    return "unknown"


def _account_state(account_known: bool, account_scope: Any) -> str:
    if not account_known:
        return "unknown"
    scope = str(account_scope or "").strip().lower()
    if scope in {"real", "live"}:
        return "real"
    if scope in {"sim", "simulate", "simulated"}:
        return "sim"
    return "unknown"


def build_runtime_status(
    *,
    connect_flag: bool,
    account_known: bool,
    account_scope: Any,
    positions_raw: Sequence[Mapping[str, Any]] | None,
    positions: Sequence[Mapping[str, Any]] | None,
    positions_authoritative: bool,
    orders_meta: Mapping[str, Any] | None,
    gates: Mapping[str, Any] | None,
    market_phase: Any,
    runtime_as_of: str,
    data_as_of: str | None = None,
) -> dict:
    """生成 ``/api/trade/snapshot`` 的 ``runtime`` 字段。"""
    positions_state = derive_positions_state(positions_raw, positions_authoritative)
    quotes_state = derive_quotes_state(positions)
    orders_state = derive_orders_state(orders_meta)
    broker_state = derive_broker_state(
        connect_flag=connect_flag,
        account_known=account_known,
        positions_state=positions_state,
        orders_state=orders_state,
    )
    account_state = _account_state(account_known, account_scope)
    market_state = map_market_phase(market_phase)
    blocks = list((gates or {}).get("blocks_now") or [])

    writable_broker = broker_state in {"connected", "degraded"}
    positions_writable = writable_broker and positions_state == "live"
    place = (
        positions_writable
        and account_known
        and quotes_state != "degraded"
        and not blocks
    )
    reduce = positions_writable
    cancel = writable_broker and orders_state == "live"
    modify_stop = positions_writable

    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason and reason not in reasons:
            reasons.append(reason)

    if broker_state == "disconnected":
        add("券商连接不可用；当前只读，仅展示本地台账与最近缓存")
    elif broker_state == "unknown":
        add("券商状态未确认；交易动作已锁定")
    elif broker_state == "degraded":
        add("部分券商数据降级；仅开放有数据证据的动作")

    if positions_state == "degraded":
        add("持仓来自本地 DB 兜底；开仓、减仓与保护修改已锁定")
    elif positions_state == "unknown":
        add("持仓来源未确认；开仓、减仓与保护修改已锁定")

    if not account_known:
        add("账户资金不可用；禁止新开仓")

    if orders_state == "degraded":
        add("订单来自本地审计库；撤单已锁定，状态可能滞后")
    elif orders_state == "unknown":
        add("订单状态未确认；撤单已锁定")

    if quotes_state == "degraded":
        add("持仓报价非实时；禁止新开仓，减仓仍可用")

    for block in blocks:
        add(str(block))

    return {
        "broker": broker_state if broker_state in _BROKER_STATES else "unknown",
        "account": account_state,
        "quotes": quotes_state if quotes_state in _DATA_STATES else "unknown",
        "positions": positions_state if positions_state in _DATA_STATES else "unknown",
        "orders": orders_state if orders_state in _DATA_STATES else "unknown",
        "market": market_state if market_state in _MARKET_STATES else "unknown",
        "as_of": data_as_of or None,
        "runtime_as_of": runtime_as_of,
        "capabilities": {
            "place": bool(place),
            "reduce": bool(reduce),
            "cancel": bool(cancel),
            "modify_stop": bool(modify_stop),
        },
        "reasons": reasons,
    }
