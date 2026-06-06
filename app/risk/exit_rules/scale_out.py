"""
FTSeek-US Scale Out 规则 (规则 2.5) — 锯齿止盈升级版

Task #62 (2026-05-29):
  - 触发阈值降低 (+5% / +10% / +18% → +1.5% / +3.0% / +5.0%)，更早锁定利润
  - 每次部分减仓后，剩余仓位的止损线**单向上移** (LONG 只升不降, SHORT 只降不升)
  - 最后 25% 仓位标记 trailing_remainder=true, 交由 Trailing Stop 接管
  - 返回 dict 中新增 partial_close_pct / new_stop_pct / stop_price 字段, 供
    orchestrator 在执行减仓的同时把 trail_state 的止损线推到新水位

Phase 3 (2026-05-31):
  - 新增 ATR-relative 止盈模式 (tier_mode: atr), 按 profit_atr 分级触发
  - 保留 pct 模式向后兼容 (tier_mode: pct)
  - ATR 模式默认 4 档: 0.5/1.0/2.0/3.0 ATR, 含 ratchet SL + trailing_remainder

接口约定 (与 ExitRuleRegistry / _execute_exit 兼容):
  返回 None 表示不触发; 返回 dict 触发, 字段:
    - reason / reason_cn / exit_price / description / close_ratio / close_qty
    - partial_close_pct  (== close_ratio, 任务文档约定字段名)
    - new_stop_pct       (相对 entry 的止损偏移, LONG 为正、SHORT 为正绝对值)
    - stop_price         (新止损绝对价)
    - trailing_remainder (bool, 最后一档触发后置 true)
"""
import logging
from typing import Optional, Dict, Any, List

from .base import BaseExitRule

logger = logging.getLogger("ftseek.exit_monitor")


# Task #62 默认配置 (与 unified_config.yaml: exit_policy.scale_out.tiers 对齐)
DEFAULT_TIERS: List[Dict[str, float]] = [
    {"trigger_pct": 0.015, "close_pct": 0.25, "new_stop_pct": 0.005},
    {"trigger_pct": 0.030, "close_pct": 0.25, "new_stop_pct": 0.015},
    {"trigger_pct": 0.050, "close_pct": 0.25, "new_stop_pct": 0.030},
]

# Phase 3 (2026-05-31): ATR-relative 默认分档
DEFAULT_ATR_TIERS: List[Dict[str, Any]] = [
    {"profit_atr": 0.5, "close_pct": 15, "ratchet_sl_pct": 0.3},   # Tier 0: 微利锁定
    {"profit_atr": 1.0, "close_pct": 25, "ratchet_sl_pct": 0.5},   # Tier 1: 落袋为安
    {"profit_atr": 2.0, "close_pct": 30, "ratchet_sl_pct": 1.0},   # Tier 2: 趋势利润
    {"profit_atr": 3.0, "close_pct": 30, "ratchet_sl_pct": 1.5,    # Tier 3: 留底仓
     "trailing_remainder": True},
]


def _load_scale_out_config() -> Dict[str, Any]:
    """Load scale-out config from exit_policy.yaml via ExitPolicyGuardian.

    P0 fix (2026-05-31): The previous get_dotted("exit_policy.scale_out.*") approach
    read from unified_config, but the data lives in exit_policy.yaml.
    """
    try:
        from app.risk.exit_policy_guardian import ExitPolicyGuardian
        guardian = ExitPolicyGuardian()
        guardian.load()
        policy = guardian.policy or {}
        return policy.get("exit_rules", {}).get("scale_out", {})
    except Exception as e:
        logger.debug(f"[scale_out] Failed to load config: {e}")
        return {}


def _load_tiers_from_config() -> List[Dict[str, float]]:
    """从 exit_policy.yaml 读取 pct tiers, 失败退化为默认"""
    cfg = _load_scale_out_config()
    raw = cfg.get("tiers") if cfg else None
    if not raw or not isinstance(raw, list):
        return list(DEFAULT_TIERS)
    out: List[Dict[str, float]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        try:
            tp = float(t.get("trigger_pct", 0) or 0)
            cp = float(t.get("close_pct", 0) or 0)
            ns = float(t.get("new_stop_pct", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tp <= 0 or cp <= 0:
            continue
        out.append({"trigger_pct": tp, "close_pct": cp, "new_stop_pct": ns})
    return out or list(DEFAULT_TIERS)


def _load_atr_tiers_from_config() -> List[Dict[str, Any]]:
    """从 exit_policy.yaml 读取 ATR tiers, 失败退化为 DEFAULT_ATR_TIERS"""
    cfg = _load_scale_out_config()
    raw = cfg.get("atr_tiers") if cfg else None
    if not raw or not isinstance(raw, list):
        return list(DEFAULT_ATR_TIERS)
    out: List[Dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        try:
            pa = float(t.get("profit_atr", 0) or 0)
            cp = float(t.get("close_pct", 0) or 0)
            rs = float(t.get("ratchet_sl_pct", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pa <= 0 or cp <= 0:
            continue
        tier: Dict[str, Any] = {"profit_atr": pa, "close_pct": cp, "ratchet_sl_pct": rs}
        if t.get("trailing_remainder"):
            tier["trailing_remainder"] = True
        out.append(tier)
    return out or list(DEFAULT_ATR_TIERS)


def _get_tier_mode() -> str:
    """读取 exit_policy.yaml scale_out.tier_mode, 默认 'atr'"""
    cfg = _load_scale_out_config()
    mode = cfg.get("tier_mode", "atr") if cfg else "atr"
    return str(mode or "atr").lower()


def _trailing_remainder_enabled() -> bool:
    """读取 exit_policy.yaml scale_out.trailing_remainder, 默认 True"""
    cfg = _load_scale_out_config()
    return bool(cfg.get("trailing_remainder", True)) if cfg else True


def _calc_new_stop_price(direction: str, entry_price: float, new_stop_pct: float) -> float:
    if entry_price <= 0:
        return 0.0
    if direction == "LONG":
        return entry_price * (1.0 + float(new_stop_pct))
    return entry_price * (1.0 - float(new_stop_pct))


def _is_more_protective(direction: str, candidate: float, current: float) -> bool:
    if candidate <= 0:
        return False
    if current <= 0:
        return True
    if direction == "LONG":
        return candidate > current
    return candidate < current


def check_scale_out(position, current_price, trail_state, direction, market_data=None):
    """锯齿止盈判定逻辑 (Task #62 + Phase 3 ATR 模式)

    Phase 3 (2026-05-31): 支持 ATR-relative 模式, 当 market_data 含 ATR 时
    按 ATR 倍数分档; 否则回退到 pct 百分比模式。
    
    2026-06-06: 根因修复 — 不再依赖 trail_state 追踪已触发 tier
    - 用 position.is_partially_reduced (DB持久化) 判断是否已 scale_out
    - 加 PnL 方向校验: 实际亏损时不触发止盈
    """
    entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
    if entry_price <= 0 or current_price <= 0:
        return None

    # ── 防护 1: 已部分减仓 → 不再触发 (替代 trail_state["triggered_tiers"]) ──
    if getattr(position, "is_partially_reduced", False):
        return None

    # ── 防护 2: PnL 方向校验 — 实际亏损时不触发止盈 ──
    # 根因: position_sync 可能把 entry_price 设为 broker avg cost,
    #        effective_atr 极小时 profit_atr 虚高, 导致 PnL<0 仍触发
    if direction == "SHORT":
        actual_pnl_pct = (entry_price - current_price) / entry_price
    else:
        actual_pnl_pct = (current_price - entry_price) / entry_price
    if actual_pnl_pct <= 0:
        return None  # 实际亏损，不应该触发止盈

    # Phase 3: ATR-relative profit calculation
    profit_atr = None
    atr = 0.0
    md = market_data or {}
    atr = float(md.get("atr") or md.get("effective_atr", 0) or 0)
    if atr and atr > 0 and entry_price > 0:
        if direction == "SHORT":
            profit_atr = (entry_price - current_price) / atr
        else:
            profit_atr = (current_price - entry_price) / atr

    mode = _get_tier_mode()
    if mode == "atr" and profit_atr is not None:
        return _check_scale_out_atr(position, current_price, trail_state, direction,
                                     entry_price, profit_atr, atr)
    else:
        return _check_scale_out_pct(position, current_price, trail_state, direction, entry_price)


def _check_scale_out_pct(position, current_price, trail_state, direction, entry_price):
    """pct 百分比模式 (向后兼容, Task #62 原始逻辑)"""
    tiers = _load_tiers_from_config()
    if not tiers:
        return None

    if direction == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price

    triggered = list(trail_state.get("triggered_tiers", []) or [])
    original_size = trail_state.get("scale_out_original_size", position.size)
    trail_state["scale_out_original_size"] = original_size

    for i, tier in enumerate(tiers):
        if i in triggered:
            continue
        trigger_pct = float(tier.get("trigger_pct", 0) or 0)
        close_pct = float(tier.get("close_pct", 0) or 0)
        new_stop_pct = float(tier.get("new_stop_pct", 0) or 0)
        if trigger_pct <= 0 or close_pct <= 0:
            continue
        if pnl_pct < trigger_pct:
            break
        close_qty = max(1, int(original_size * close_pct))
        if close_qty <= 0:
            continue
        candidate_stop = _calc_new_stop_price(direction, entry_price, new_stop_pct)
        current_floor = float(trail_state.get("scale_out_stop_floor", 0) or 0)
        if _is_more_protective(direction, candidate_stop, current_floor):
            trail_state["scale_out_stop_floor"] = candidate_stop
            applied_stop = candidate_stop
        else:
            applied_stop = current_floor or candidate_stop
        triggered.append(i)
        trail_state["triggered_tiers"] = triggered
        is_last_tier = (i == len(tiers) - 1)
        trailing_remainder = bool(is_last_tier and _trailing_remainder_enabled())
        if trailing_remainder:
            trail_state["trailing_remainder"] = True
        symbol = getattr(position, "symbol", "?")
        logger.info(
            "[scale_out] %s %s Tier %d/%d fired | mode=pct | pnl_pct=%.2f%% | "
            "close %.0f%% | ratchet_sl=%.4f | trailing_remainder=%s",
            symbol, direction, i+1, len(tiers),
            pnl_pct * 100, close_pct, applied_stop, trailing_remainder
        )
        sign = "+" if direction == "LONG" else "+"
        return {
            "reason": "SCALE_OUT",
            "reason_cn": f"锯齿止盈 Tier {i+1}/{len(tiers)} ({sign}{pnl_pct*100:.2f}% ≥ {trigger_pct*100:.2f}%)",
            "exit_price": current_price,
            "close_qty": close_qty,
            "close_ratio": close_pct,
            "partial_close_pct": close_pct,
            "new_stop_pct": new_stop_pct,
            "stop_price": applied_stop,
            "trailing_remainder": trailing_remainder,
            "set_partially_reduced": True,  # 2026-06-06: 通知调用方标记已减仓
            "description": (
                f"锯齿止盈 Tier {i+1}: pnl={pnl_pct*100:.2f}% ≥ {trigger_pct*100:.2f}%, "
                f"减仓 {close_qty}/{original_size} ({close_pct:.0%}), "
                f"剩余止损上移至 entry{'+' if direction == 'LONG' else '-'}{new_stop_pct*100:.2f}% = {applied_stop:.4f}"
                + (" [trailing_remainder]" if trailing_remainder else "")
            ),
            "metadata": {
                "tier": i + 1,
                "total_tiers": len(tiers),
                "mode": "pct",
                "trigger_value": round(pnl_pct, 4),
                "close_pct": close_pct,
                "ratchet_sl": applied_stop,
                "trailing_remainder": trailing_remainder,
            },
        }
    return None


def _check_scale_out_atr(position, current_price, trail_state, direction,
                          entry_price, profit_atr, atr):
    """ATR-relative 模式 (Phase 3, 2026-05-31)"""
    atr_tiers = _load_atr_tiers_from_config()
    if not atr_tiers:
        return None

    triggered = list(trail_state.get("triggered_tiers", []) or [])
    original_size = trail_state.get("scale_out_original_size", position.size)
    trail_state["scale_out_original_size"] = original_size

    for i, tier in enumerate(atr_tiers):
        if i in triggered:
            continue
        tier_profit_atr = float(tier.get("profit_atr", 0) or 0)
        close_pct = float(tier.get("close_pct", 0) or 0)
        ratchet_sl_pct = float(tier.get("ratchet_sl_pct", 0) or 0)
        if tier_profit_atr <= 0 or close_pct <= 0:
            continue
        if profit_atr < tier_profit_atr:
            break
        close_ratio = close_pct / 100.0
        close_qty = max(1, int(original_size * close_ratio))
        if close_qty <= 0:
            continue
        if direction == "LONG":
            candidate_stop = entry_price + ratchet_sl_pct * atr
        else:
            candidate_stop = entry_price - ratchet_sl_pct * atr
        current_floor = float(trail_state.get("scale_out_stop_floor", 0) or 0)
        if _is_more_protective(direction, candidate_stop, current_floor):
            trail_state["scale_out_stop_floor"] = candidate_stop
            applied_stop = candidate_stop
        else:
            applied_stop = current_floor or candidate_stop
        triggered.append(i)
        trail_state["triggered_tiers"] = triggered
        is_last_tier = (i == len(atr_tiers) - 1)
        tier_trailing = bool(tier.get("trailing_remainder", False))
        trailing_remainder = bool((is_last_tier or tier_trailing) and _trailing_remainder_enabled())
        if trailing_remainder:
            trail_state["trailing_remainder"] = True
        symbol = getattr(position, "symbol", "?")
        logger.info(
            "[scale_out] %s %s Tier %d/%d fired | mode=atr | profit_atr=%.2f | "
            "close %.0f%% | ratchet_sl=%.4f | trailing_remainder=%s",
            symbol, direction, i+1, len(atr_tiers),
            profit_atr, close_pct, applied_stop, trailing_remainder
        )
        return {
            "reason": "SCALE_OUT",
            "reason_cn": f"ATR锯齿止盈 Tier {i+1}/{len(atr_tiers)} (profit={profit_atr:.2f}ATR ≥ {tier_profit_atr}ATR)",
            "exit_price": current_price,
            "close_qty": close_qty,
            "close_ratio": close_ratio,
            "partial_close_pct": close_ratio,
            "new_stop_pct": ratchet_sl_pct * atr / entry_price if entry_price > 0 else 0,
            "stop_price": applied_stop,
            "trailing_remainder": trailing_remainder,
            "set_partially_reduced": True,  # 2026-06-06: 通知调用方标记已减仓
            "description": (
                f"ATR锯齿止盈 Tier {i+1}: profit={profit_atr:.2f}ATR ≥ {tier_profit_atr}ATR, "
                f"减仓 {close_qty}/{original_size} ({close_pct:.0f}%), "
                f"ratchet SL={applied_stop:.4f} (entry {'+'if direction=='LONG' else '-'}{ratchet_sl_pct}×ATR)"
                + (" [trailing_remainder]" if trailing_remainder else "")
            ),
            "metadata": {
                "tier": i + 1,
                "total_tiers": len(atr_tiers),
                "mode": "atr",
                "trigger_value": round(profit_atr, 3),
                "tier_profit_atr": tier_profit_atr,
                "close_pct": close_pct,
                "ratchet_sl": applied_stop,
                "ratchet_sl_pct": ratchet_sl_pct,
                "atr": atr,
                "trailing_remainder": trailing_remainder,
            },
        }
    return None


class ScaleOutRule(BaseExitRule):
    """锯齿止盈规则 (Task #62 升级版 + Phase 3 ATR 模式)"""

    rule_name = "SCALE_OUT"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_scale_out(
            position,
            market_data["current_price"],
            trail_state,
            market_data["direction"],
            market_data=market_data,
        )
