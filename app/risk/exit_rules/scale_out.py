"""
FTSeek-US Scale Out 规则 (规则 2.5) — 锯齿止盈升级版

Task #62 (2026-05-29):
  - 触发阈值降低 (+5% / +10% / +18% → +1.5% / +3.0% / +5.0%)，更早锁定利润
  - 每次部分减仓后，剩余仓位的止损线**单向上移** (LONG 只升不降, SHORT 只降不升)
  - 最后 25% 仓位标记 trailing_remainder=true, 交由 Trailing Stop 接管
  - 返回 dict 中新增 partial_close_pct / new_stop_pct / stop_price 字段, 供
    orchestrator 在执行减仓的同时把 trail_state 的止损线推到新水位

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


def _load_tiers_from_config() -> List[Dict[str, float]]:
    """从 unified_config 读取 tiers (SSOT), 失败退化为默认"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        if cfg is None:
            return list(DEFAULT_TIERS)
        raw = cfg.get_dotted("exit_policy.scale_out.tiers", None)
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
    except Exception as e:
        logger.debug(f"[scale_out] 读取 exit_policy.scale_out.tiers 失败, 使用默认: {e}")
        return list(DEFAULT_TIERS)


def _trailing_remainder_enabled() -> bool:
    """读取 exit_policy.scale_out.trailing_remainder, 默认 True"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        if cfg is None:
            return True
        return bool(cfg.get_dotted("exit_policy.scale_out.trailing_remainder", True))
    except Exception:
        return True


def _calc_new_stop_price(direction: str, entry_price: float, new_stop_pct: float) -> float:
    """根据方向把 new_stop_pct 折算为绝对价
    LONG : entry * (1 + new_stop_pct)  → 止损上移
    SHORT: entry * (1 - new_stop_pct)  → 止损下移
    """
    if entry_price <= 0:
        return 0.0
    if direction == "LONG":
        return entry_price * (1.0 + float(new_stop_pct))
    return entry_price * (1.0 - float(new_stop_pct))


def _is_more_protective(direction: str, candidate: float, current: float) -> bool:
    """判断候选止损是否比当前更紧 (单向移动)
    LONG : 价格越高越紧 (上移)
    SHORT: 价格越低越紧 (下移)
    """
    if candidate <= 0:
        return False
    if current <= 0:
        return True
    if direction == "LONG":
        return candidate > current
    return candidate < current


def check_scale_out(position, current_price, trail_state, direction):
    """锯齿止盈判定逻辑 (Task #62)

    分级减仓 + 剩余仓位止损单向上移; 最后一档触发后置 trailing_remainder
    标志, 后续由 Trailing Stop 规则接管。
    """
    entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
    if entry_price <= 0 or current_price <= 0:
        return None

    tiers = _load_tiers_from_config()
    if not tiers:
        return None

    # 当前已实现的 (有向) 收益率
    if direction == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price

    triggered: List[int] = list(trail_state.get("triggered_tiers", []) or [])
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

        # 触发判定: 收益率达标
        if pnl_pct < trigger_pct:
            # 触发是"达到任一未触发档"即触发，但要按层级顺序由低到高
            # 顺序遍历，遇到首个未达成的层级立即停止 (避免越级跳触)
            break

        # 计算减仓股数 (以原始仓位为基准, 与既有协议一致)
        close_qty = max(1, int(original_size * close_pct))
        if close_qty <= 0:
            continue

        # 计算并应用单向上移的新止损
        candidate_stop = _calc_new_stop_price(direction, entry_price, new_stop_pct)
        current_floor = float(trail_state.get("scale_out_stop_floor", 0) or 0)
        if _is_more_protective(direction, candidate_stop, current_floor):
            trail_state["scale_out_stop_floor"] = candidate_stop
            applied_stop = candidate_stop
        else:
            # 已有更紧的止损, 不反向调整, 但仍要返回当前 floor 给 orchestrator
            applied_stop = current_floor or candidate_stop

        # 记录已触发档
        triggered.append(i)
        trail_state["triggered_tiers"] = triggered

        # 是否进入 trailing_remainder (最后一档触发后)
        is_last_tier = (i == len(tiers) - 1)
        trailing_remainder = bool(is_last_tier and _trailing_remainder_enabled())
        if trailing_remainder:
            trail_state["trailing_remainder"] = True

        sign = "+" if direction == "LONG" else "+"  # pnl_pct 在 SHORT 已转正
        return {
            "reason": "SCALE_OUT",
            "reason_cn": (
                f"锯齿止盈 Tier {i+1}/{len(tiers)} "
                f"({sign}{pnl_pct*100:.2f}% ≥ {trigger_pct*100:.2f}%)"
            ),
            "exit_price": current_price,
            "close_qty": close_qty,
            "close_ratio": close_pct,
            "partial_close_pct": close_pct,
            "new_stop_pct": new_stop_pct,
            "stop_price": applied_stop,
            "trailing_remainder": trailing_remainder,
            "description": (
                f"锯齿止盈 Tier {i+1}: pnl={pnl_pct*100:.2f}% ≥ "
                f"{trigger_pct*100:.2f}%, 减仓 {close_qty}/{original_size} "
                f"({close_pct:.0%}), 剩余止损上移至 entry"
                f"{'+' if direction == 'LONG' else '-'}{new_stop_pct*100:.2f}% "
                f"= {applied_stop:.4f}"
                + (" [trailing_remainder]" if trailing_remainder else "")
            ),
        }

    return None


class ScaleOutRule(BaseExitRule):
    """锯齿止盈规则 (Task #62 升级版)"""

    rule_name = "SCALE_OUT"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_scale_out(
            position,
            market_data["current_price"],
            trail_state,
            market_data["direction"],
        )
