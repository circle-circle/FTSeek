"""entry_reference → entry_price 单一数值解析 (轨道A 阶段0, 2026-07-10)

职责分离原则: LLM 只给 entry_reference (枚举类别, 如 "回踩支撑进场"), 代码在此把类别
解析成【真实价位】。generate 路径与 DA (Phase 2 接线) 共用同一函数, 杜绝各算各的漂移。

设计:
- 核心枚举映射 + 复用 generate 路径同款可成交价 (BUY=ask / SHORT=bid), 与
  instruction_generator L301-311 语义一致 (单一真源)。
- 需外部引擎的位 (support/resistance/vwap/orb) 不在本模块 import 重引擎, 而由调用方
  注入 `levels` (pivotal_detector / orb / atr_provider 的产出), 保持低耦合、可测。
- 任何引用源缺失 → 安全回退 market → 回退 price; 都取不到才返回 (0.0, "no_price")。

本阶段【不接线】: 仅提供函数 + 单测, 行为零变更。Phase 2 才在 adapter/generate 调用。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# LLM 允许输出的 entry_reference 枚举 (小而明确, 禁自由文本)
ALLOWED_ENTRY_REFERENCES = [
    "market",                     # 市价 (可成交价): BUY=ask / SHORT=bid
    "pullback_to_support",        # 回踩支撑 (LONG=支撑/MA20; SHORT=阻力/MA20)
    "breakout_above_resistance",  # 突破阻力 (LONG=阻力; SHORT=支撑)
    "vwap",                       # 回到 VWAP
    "ma20",                       # 回到 MA20
    "ma50",                       # 回到 MA50
    "orb",                        # 开盘区间边界 (LONG=ORB 高; SHORT=ORB 低)
]


def _is_short(direction: str) -> bool:
    return any(k in (direction or "").upper() for k in ("SHORT", "SELL"))


def _f(v: Any) -> float:
    try:
        x = float(v or 0)
        return x if x > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _market_price(tech: Any, is_short: bool) -> float:
    """可成交价 — 与 instruction_generator L301-311 同源: SHORT 用 bid, 其余用 ask, 回退 price。"""
    ask = _f(getattr(tech, "ask", 0))
    bid = _f(getattr(tech, "bid", 0))
    price = _f(getattr(tech, "price", 0))
    if is_short:
        return bid or price
    return ask or price


def resolve_entry_reference(
    direction: str,
    entry_reference: str,
    tech: Any,
    *,
    levels: Optional[Dict[str, float]] = None,
) -> Tuple[float, str]:
    """把 LLM 给的 entry_reference (类别) 解析成真实入场价。

    Args:
        direction: "BUY"/"LONG" 或 "SELL_SHORT"/"SHORT"。
        entry_reference: ALLOWED_ENTRY_REFERENCES 之一; 未知按 market 处理。
        tech: TechnicalData-like, 需 .ask/.bid/.price, 可选 .ma20/.ma50。
        levels: 可选, 调用方注入的引擎位 {support, resistance, vwap, orb_high, orb_low}。

    Returns:
        (entry_price, source_str)。任何引用源缺失均回退 market; 全无价 → (0.0, "no_price")。
    """
    is_short = _is_short(direction)
    lv = levels or {}
    ref = (entry_reference or "market").strip().lower()

    market = _market_price(tech, is_short)

    def _fallback(reason: str) -> Tuple[float, str]:
        if market > 0:
            return round(market, 2), f"{ref}->market({reason})"
        return 0.0, "no_price"

    if ref == "market":
        return (round(market, 2), "market") if market > 0 else (0.0, "no_price")

    if ref == "pullback_to_support":
        # LONG 回踩支撑; SHORT 回抽阻力。缺显式位则用 MA20 近似。
        cand = _f(lv.get("resistance")) if is_short else _f(lv.get("support"))
        cand = cand or _f(getattr(tech, "ma20", 0))
        return (round(cand, 2), f"{ref}={'resistance' if is_short else 'support'}/ma20") if cand > 0 else _fallback("no_level")

    if ref == "breakout_above_resistance":
        # LONG 突破阻力; SHORT 跌破支撑。
        cand = _f(lv.get("support")) if is_short else _f(lv.get("resistance"))
        return (round(cand, 2), f"{ref}={'support' if is_short else 'resistance'}") if cand > 0 else _fallback("no_level")

    if ref == "vwap":
        cand = _f(lv.get("vwap"))
        return (round(cand, 2), "vwap") if cand > 0 else _fallback("no_vwap")

    if ref == "ma20":
        cand = _f(getattr(tech, "ma20", 0))
        return (round(cand, 2), "ma20") if cand > 0 else _fallback("no_ma20")

    if ref == "ma50":
        cand = _f(getattr(tech, "ma50", 0))
        return (round(cand, 2), "ma50") if cand > 0 else _fallback("no_ma50")

    if ref == "orb":
        cand = _f(lv.get("orb_low")) if is_short else _f(lv.get("orb_high"))
        return (round(cand, 2), f"{ref}={'orb_low' if is_short else 'orb_high'}") if cand > 0 else _fallback("no_orb")

    # 未知枚举 → market 兜底 (不抛异常, 不返回 0)
    return _fallback("unknown_ref")
