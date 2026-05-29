"""
FTSeek-US Graduated Stop 规则 (Task #61)

分级减仓止损 — 按 ATR 跌幅分级减仓，控制单笔最大回撤。

设计:
    亏损阶段        | 动作         | 保留仓位
    -0.5x ATR      | 减仓 25%     | 75%
    -1.0x ATR      | 再减仓 25%   | 50%
    -1.5x ATR      | 全平         | 0%

特性:
    1. 每级仅触发一次 (使用 trail_state["graduated_triggered_levels"] 去重)
    2. 两级之间最少间隔 cooldown_seconds=120 (防止快速连续触发)
    3. 优先返回最深一级 (满足条件的最低 trigger_atr)
    4. 配置从 unified_config.yaml `exit_policy.graduated_stop` 读取，缺失时使用默认值
    5. 状态记录与 trailing_stop 共用 trail_state dict (orchestrator 负责持久化)

返回 dict 字段:
    - triggered:         True
    - reason:            "GRADUATED_STOP_L1" / "L2" / "L3"
    - reason_cn:         中文描述
    - exit_price:        当前价 (执行参考)
    - stop_price:        触发价 (entry_price + signed_trigger_atr * atr)
    - partial_close_pct: 0.25 / 0.25 / 1.0  (该级配置的减仓比例)
    - close_ratio:       相对当前持仓的减仓比例 (供 order_executor 使用)
    - close_qty:         绝对股数 (基于原始持仓 × reduce_pct)
    - level:             触发的级别索引 (0/1/2)
    - description:       详细日志文本

注：本规则仅检测亏损方向（profit_atr_signed < 0）；
    依赖外部已计算好的 profit_atr_signed (有向：盈利>0，亏损<0)。
"""
import logging
from typing import Optional, Dict, Any, List

from .base import BaseExitRule

logger = logging.getLogger("ftseek.exit_monitor")


# ----------------------------------------------------------------------
# 默认配置 (fallback)
# ----------------------------------------------------------------------
DEFAULT_LEVELS: List[Dict[str, float]] = [
    {"trigger_atr": -0.5, "reduce_pct": 0.25},
    {"trigger_atr": -1.0, "reduce_pct": 0.25},
    {"trigger_atr": -1.5, "reduce_pct": 1.0},
]
DEFAULT_COOLDOWN: float = 120.0


def _load_graduated_cfg(policy) -> Dict[str, Any]:
    """从 policy_guardian.policy 或 unified_config.yaml 读取分级止损配置。

    优先级:
      1. policy.policy["exit_policy"]["graduated_stop"]
      2. policy.policy["exit_rules"]["graduated_stop"] (向后兼容路径)
      3. unified_config.yaml `exit_policy.graduated_stop` (经 get_config().get_dotted)
      4. DEFAULT_LEVELS / DEFAULT_COOLDOWN
    """
    cfg: Dict[str, Any] = {}

    # 1) 通过 policy_guardian 注入
    try:
        if policy is not None and hasattr(policy, "policy"):
            policy_dict = policy.policy if isinstance(policy.policy, dict) else {}
            cfg = (
                policy_dict.get("exit_policy", {}).get("graduated_stop", {})
                or policy_dict.get("exit_rules", {}).get("graduated_stop", {})
                or {}
            )
    except Exception as e:
        logger.debug(f"[graduated_stop] 从 policy 读取配置失败: {e}")
        cfg = {}

    # 2) Fallback: unified_config.yaml
    if not cfg:
        try:
            from app.core.config_container import get_config
            cfg_obj = get_config()
            cfg = cfg_obj.get_dotted("exit_policy.graduated_stop", {}) or {}
        except Exception as e:
            logger.debug(f"[graduated_stop] 从 unified_config 读取失败: {e}")
            cfg = {}

    # 3) 字段级 fallback
    enabled = bool(cfg.get("enabled", True))
    raw_levels = cfg.get("levels") or []
    if not isinstance(raw_levels, list) or not raw_levels:
        raw_levels = DEFAULT_LEVELS

    # 规范化并按 trigger_atr 升序 (最负的在前)
    levels: List[Dict[str, float]] = []
    for lv in raw_levels:
        if not isinstance(lv, dict):
            continue
        try:
            trigger_atr = float(lv.get("trigger_atr"))
            reduce_pct = float(lv.get("reduce_pct"))
        except (TypeError, ValueError):
            continue
        if reduce_pct <= 0 or reduce_pct > 1:
            continue
        levels.append({"trigger_atr": trigger_atr, "reduce_pct": reduce_pct})

    if not levels:
        levels = list(DEFAULT_LEVELS)
    # 按 trigger_atr 降序 (例如 -0.5, -1.0, -1.5) —
    # L1 对应最浅亏损, L3 对应最深亏损, 与设计规格 (L1=-0.5/L2=-1.0/L3=-1.5) 对齐。
    levels.sort(key=lambda x: x["trigger_atr"], reverse=True)

    try:
        cooldown = float(cfg.get("cooldown_seconds", DEFAULT_COOLDOWN))
        if cooldown < 0:
            cooldown = DEFAULT_COOLDOWN
    except (TypeError, ValueError):
        cooldown = DEFAULT_COOLDOWN

    return {
        "enabled": enabled,
        "levels": levels,
        "cooldown_seconds": cooldown,
    }


def check_graduated_stop(
    position,
    current_price: float,
    entry_price: float,
    atr: float,
    profit_atr_signed: float,
    trail_state: Dict[str, Any],
    now_ts: float,
    direction: str,
    policy=None,
) -> Optional[Dict[str, Any]]:
    """分级止损判定 (与 trailing_stop.check_* 同风格的纯函数)

    Args:
        position:           Position 对象 (需要 .symbol / .size)
        current_price:      当前价
        entry_price:        平均成本
        atr:                ATR (绝对值, > 0)
        profit_atr_signed:  有向 profit/ATR (盈利 > 0, 亏损 < 0)
        trail_state:        trail_state dict (规则会写入触发记录)
        now_ts:             当前 epoch 秒
        direction:          "LONG" / "SHORT"
        policy:             ExitPolicyGuardian 或 None

    Returns:
        None: 不触发
        Dict: 触发，结构见模块 docstring
    """
    # 基础类型校验
    if not isinstance(current_price, (int, float)) or current_price <= 0:
        return None
    if not isinstance(entry_price, (int, float)) or entry_price <= 0:
        return None
    if not isinstance(atr, (int, float)) or atr <= 0:
        return None
    if profit_atr_signed is None:
        return None

    # 仅在亏损方向触发
    if profit_atr_signed >= 0:
        return None

    cfg = _load_graduated_cfg(policy)
    if not cfg["enabled"]:
        return None

    levels: List[Dict[str, float]] = cfg["levels"]
    cooldown: float = cfg["cooldown_seconds"]

    # 已触发级别集合 (持久化在 trail_state)
    triggered: List[int] = list(trail_state.get("graduated_triggered_levels", []) or [])
    last_trigger_ts = float(trail_state.get("graduated_last_trigger_ts", 0.0) or 0.0)

    # 冷却检查
    if last_trigger_ts > 0 and (now_ts - last_trigger_ts) < cooldown:
        logger.debug(
            f"[graduated_stop] {getattr(position, 'symbol', '?')} 冷却中 "
            f"({now_ts - last_trigger_ts:.0f}s < {cooldown:.0f}s), 跳过"
        )
        return None

    # 选择"最深一级且未触发"的级别。
    # levels 已按 trigger_atr 降序 (L1=-0.5 在前, L3=-1.5 在后)；
    # 顺序遍历, 持续更新 chosen_idx 至最后一个满足条件的索引,
    # 即 profit_atr_signed 达到的最深级别 (允许跳级直接到 L3)。
    chosen_idx: Optional[int] = None
    chosen_level: Optional[Dict[str, float]] = None
    for idx, lv in enumerate(levels):
        if profit_atr_signed <= lv["trigger_atr"] and idx not in triggered:
            chosen_idx = idx
            chosen_level = lv
            # 不 break —— 继续向更深级别探测

    if chosen_idx is None or chosen_level is None:
        return None

    reduce_pct = float(chosen_level["reduce_pct"])
    trigger_atr = float(chosen_level["trigger_atr"])

    # 原始持仓大小 (与 scale_out 同机制，首次触发时锚定)
    try:
        cur_size = int(getattr(position, "size", 0) or 0)
    except (TypeError, ValueError):
        cur_size = 0
    original_size = int(trail_state.get("graduated_original_size", cur_size) or cur_size)
    if original_size <= 0:
        original_size = cur_size
    trail_state["graduated_original_size"] = original_size

    # 计算关闭股数 (基于原始持仓)；L3 直接全平当前剩余
    if reduce_pct >= 1.0:
        close_qty = max(1, cur_size) if cur_size > 0 else 0
    else:
        close_qty = max(1, int(original_size * reduce_pct)) if original_size > 0 else 0
        # 不超过当前剩余
        if cur_size > 0:
            close_qty = min(close_qty, cur_size)

    # close_ratio 相对当前剩余持仓 (供 order_executor 兼容)
    if cur_size > 0:
        close_ratio = min(1.0, close_qty / cur_size) if reduce_pct < 1.0 else 1.0
    else:
        close_ratio = reduce_pct

    # 触发价 (entry +/- 有向 ATR 偏移)
    if direction == "LONG":
        stop_price = entry_price + trigger_atr * atr  # trigger_atr<0 → 下方
    else:
        stop_price = entry_price - trigger_atr * atr  # 做空：上行为亏损

    # 写入状态
    triggered.append(chosen_idx)
    trail_state["graduated_triggered_levels"] = triggered
    trail_state["graduated_last_trigger_ts"] = float(now_ts)

    level_label = f"L{chosen_idx + 1}"
    reason_code = f"GRADUATED_STOP_{level_label}"
    reason_cn = (
        f"分级止损 {level_label} "
        f"(profit_atr={profit_atr_signed:+.2f}≤{trigger_atr:+.2f}, 减仓 {reduce_pct:.0%})"
    )
    description = (
        f"分级止损触发 {level_label}: profit_atr={profit_atr_signed:+.2f}, "
        f"trigger={trigger_atr:+.2f} ATR, entry={entry_price:.2f}, "
        f"current={current_price:.2f}, stop={stop_price:.2f}, "
        f"reduce_pct={reduce_pct:.0%}, close_qty={close_qty}/{cur_size}"
    )

    logger.warning(
        f"[graduated_stop] {getattr(position, 'symbol', '?')} {direction} "
        f"{level_label} triggered | {description}"
    )

    return {
        "triggered": True,
        "reason": reason_code,
        "reason_cn": reason_cn,
        "exit_price": float(current_price),
        "stop_price": float(stop_price),
        "partial_close_pct": reduce_pct,
        "close_ratio": float(close_ratio),
        "close_qty": int(close_qty),
        "level": int(chosen_idx),
        "description": description,
    }


class GraduatedStopRule(BaseExitRule):
    """分级止损规则 — 按 ATR 跌幅分级减仓代替全平

    与 ExitRuleRegistry 接口兼容 (BaseExitRule.should_exit)。
    Task #64 将完成注册集成；本任务仅提供规则实现。
    """

    rule_name = "GRADUATED_STOP"

    def should_exit(
        self,
        position,
        market_data: Dict[str, Any],
        trail_state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        return check_graduated_stop(
            position=position,
            current_price=market_data["current_price"],
            entry_price=market_data["entry_price"],
            atr=market_data.get("effective_atr", market_data.get("atr", 0.0)),
            profit_atr_signed=market_data.get("profit_atr_signed", 0.0),
            trail_state=trail_state,
            now_ts=market_data.get("now_ts", 0.0),
            direction=market_data.get("direction", "LONG"),
            policy=market_data.get("policy_guardian"),
        )


__all__ = [
    "DEFAULT_LEVELS",
    "DEFAULT_COOLDOWN",
    "check_graduated_stop",
    "GraduatedStopRule",
]
