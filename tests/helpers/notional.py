"""测试夹具的**名义额**派生 (2026-11-03, 全链验收批)。

为什么需要这个共享 helper:
`playbook.admission.min_notional_usd` 在 2026-10-31 由 2500 抬到 **5500**；
2026-11-05 用户裁恢复供给后为 **2500 + max_fee_bps=11.54**，本 helper 同时跟随两项活配置。
(用户裁「P3上」, DEB-20261031-budget-design §8)。而**多个测试夹具把股数写死**,
造出的指令名义额 (如 100 股 × $50 = $5,000 / 40 股 × $100 = $4,000) 低于新地板
⇒ **落库时** admission 闸把方向改成 `SKIP`
(`skip_reason=NOTIONAL_TOO_SMALL(5000<5500)`) ⇒ `get_recent_instructions` 的
`WHERE direction != 'SKIP'` 查不到它 ⇒ `execute_instruction` / `_find_instruction_by_id`
恒返回「指令不存在」 ⇒ **守卫静默失效** (实测 35+ 条红, 同一根因)。

⇒ 夹具必须**跟着活配置走**: 用本 helper 派生股数, 以后再调阈值不会把守卫打红。
(仓内早有同型事故记录: `app/playbook/db.py:145-147` 记着 2026-10-05 US.GEO/US.ORCL
因 admission 改 SKIP 而死于「指令不存在」。)

用法:
    from tests.helpers.notional import qty_above_min_notional
    "position_size": qty_above_min_notional(50.0),
"""
from __future__ import annotations


def min_notional_usd() -> float:
    """当下生效的 admission 名义额地板 (读不到 ⇒ 0 = 不拦, 与 admission 的降级缺省同口径)。"""
    try:
        from app.playbook.admission import get_admission_config
        return float(get_admission_config().get("min_notional_usd") or 0)
    except Exception:
        return 0.0


def qty_above_min_notional(entry_price: float, *, margin_shares: int = 2) -> int:
    """按当下名义额与费用率门算足额股数，避免夹具落库即被 SKIP。"""
    price = float(entry_price)
    if price <= 0:
        return 1
    from app.playbook.admission import estimate_round_trip_fee, get_admission_config
    cfg = get_admission_config()
    floor = float(cfg.get("min_notional_usd") or 0)
    max_fee_bps = float(cfg.get("max_fee_bps") or 0)
    quantity = int(floor / price) + int(margin_shares)
    if max_fee_bps <= 0:
        return quantity

    def fee_gate_passes(candidate: int) -> bool:
        notional = candidate * price
        fee = estimate_round_trip_fee(candidate, price, "BUY")
        return notional > 0 and 10000.0 * fee / notional <= max_fee_bps

    if fee_gate_passes(quantity):
        return quantity
    high = max(quantity * 2, 200 + int(margin_shares))
    while not fee_gate_passes(high):
        quantity = high
        high *= 2
        if high > 1_000_000:
            raise ValueError(f"price={price} 在 max_fee_bps={max_fee_bps} 下无可行测试股数")
    while quantity + 1 < high:
        middle = (quantity + high) // 2
        if fee_gate_passes(middle):
            high = middle
        else:
            quantity = middle
    return high + int(margin_shares)
