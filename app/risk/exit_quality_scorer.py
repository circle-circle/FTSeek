"""
FTSeek-US 退出规则质量评分体系 (Exit Quality Scorer)

从"先到先得"改为"最优规则优先"的核心评分模块。
当多条退出规则同时触发时，计算每条规则的综合质量分，选择评分最高的执行。

评分维度:
  1. pnl_quality (0-30): 当前 PnL 相对 ATR 的比值（正PnL=高分）
  2. thesis_match (0-25): 退出原因是否匹配入场逻辑（高匹配=高分）
  3. timing_quality (0-25): 距离目标持有时间的完成度
  4. rule_priority (0-20): 规则本身的基础优先级（配置项）

输出综合评分 0-100。

设计原则:
  - Pre-Close（盘末强平）优先级最高，不参与评分竞争，直接执行
  - 只有一个规则触发时，直接执行（无需评分）
  - 多规则触发时，评分最高的规则执行
"""
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

import yaml

logger = logging.getLogger("ftseek.exit_quality_scorer")

# ============================================================
#  配置加载
# ============================================================

_DEFAULT_RULE_PRIORITIES = {
    "trailing_stop": 70,     # P0 (2026-06-07): 90→70, scale_out 止盈优先于 trailing 止损
    "scale_out": 90,         # P0 (2026-06-07): 80→90, 锁利优先 — scale_out 是唯一正EV退出
    "day_trade": 80,         # DAY_TRADE_TP 100%WR, 仅次于 scale_out
    "flash_exit": 50,
    "pre_close": 100,
}

# 退出原因 → 规则标识映射
_REASON_TO_RULE = {
    "FLASH_EXIT": "flash_exit",
    "DAY_TRADE_SL": "day_trade",
    "DAY_TRADE_TP": "day_trade",
    "DAY_TRADE_TP_TIER1": "day_trade",
    "DAY_TRADE_TP_TIER2": "day_trade",
    "DAY_TRADE_TP_TIER3": "day_trade",
    "DAY_TRADE_TIME_DECAY": "day_trade",
    "DAY_TRADE_PROFIT_LOCK": "day_trade",
    "DAY_TRADE_TIMEOUT": "day_trade",  # 2026-06-03: 新增超时退出映射
    "SCALE_OUT": "scale_out",
    "CHANDELIER_TRAIL": "trailing_stop",
    "SL_HIT": "trailing_stop",
    "ATR_STOP": "trailing_stop",
    "BREAKEVEN_STOP": "trailing_stop",
    "STOP_LOSS": "trailing_stop",
    "TIME_STOP": "time_stop",
    "TIME_EXIT": "time_exit",            # 2026-06-21: 时间退出
    "PRE_CLOSE_EXIT": "pre_close",
    "PRE_CLOSE_LOSS_FLAT": "pre_close",
    "PRE_CLOSE_HALF_EXIT": "pre_close",
    "PRE_CLOSE_QUARTER_EXIT": "pre_close",
}


def _load_rule_priorities() -> Dict[str, int]:
    """从 exit_policy.yaml 加载规则优先级配置"""
    try:
        policy_path = Path(__file__).resolve().parent / "exit_policy.yaml"
        if policy_path.exists():
            with open(policy_path, "r", encoding="utf-8") as f:
                policy = yaml.safe_load(f)
            priorities = policy.get("rule_priorities", {})
            if priorities:
                return {k: int(v) for k, v in priorities.items()}
    except Exception as e:
        logger.debug(f"[ExitQualityScorer] 加载 rule_priorities 失败, 使用默认值: {e}")
    return _DEFAULT_RULE_PRIORITIES.copy()


# ============================================================
#  ExitQualityScorer
# ============================================================

class ExitQualityScorer:
    """退出规则质量评分器

    输入: 单个退出建议 + 持仓上下文
    输出: 0-100 综合质量分
    """

    def __init__(self):
        self._priorities = _load_rule_priorities()

    def score(
        self,
        exit_result: Dict[str, Any],
        position: Any,
        current_price: float,
        atr: float,
        pnl_pct: float,
        held_minutes: float,
        direction: str,
    ) -> float:
        """计算退出建议的综合质量分

        Args:
            exit_result: 退出规则返回的 dict (含 reason, exit_price 等)
            position: Position 对象
            current_price: 当前价格
            atr: 有效 ATR (session-aware)
            pnl_pct: 当前 PnL 百分比 (有向, 盈利>0)
            held_minutes: 已持仓分钟数
            direction: "LONG" | "SHORT"

        Returns:
            0-100 综合评分
        """
        reason = exit_result.get("reason", "UNKNOWN")

        # Pre-Close 直接返回满分 (不参与评分竞争)
        rule_key = _REASON_TO_RULE.get(reason, "")
        if rule_key == "pre_close":
            return 100.0

        # 计算四个维度分数
        pnl_score = self._score_pnl_quality(pnl_pct, atr, current_price, position, direction)
        thesis_score = self._score_thesis_match(reason, position)
        timing_score = self._score_timing_quality(held_minutes, position)
        priority_score = self._score_rule_priority(reason)

        total = pnl_score + thesis_score + timing_score + priority_score
        return min(100.0, max(0.0, total))

    def _score_pnl_quality(
        self, pnl_pct: float, atr: float, current_price: float, position: Any, direction: str
    ) -> float:
        """PnL 质量评分 (0-30)

        逻辑:
        - profit_atr >= 2.0: 满分 30
        - profit_atr >= 1.0: 20-30 线性
        - profit_atr >= 0.0: 10-20 线性
        - profit_atr < 0 (亏损): 0-10 按亏损深度递减
        """
        entry_price = getattr(position, "avg_entry_price", 0)
        if entry_price <= 0 or atr <= 0:
            return 15.0  # 无法计算时给中间分

        # 计算 profit_atr (有向)
        if direction == "LONG":
            profit_atr = (current_price - entry_price) / atr
        else:
            profit_atr = (entry_price - current_price) / atr

        if profit_atr >= 2.0:
            return 30.0
        elif profit_atr >= 1.0:
            # 1.0 → 20, 2.0 → 30
            return 20.0 + (profit_atr - 1.0) * 10.0
        elif profit_atr >= 0.0:
            # 0.0 → 10, 1.0 → 20
            return 10.0 + profit_atr * 10.0
        else:
            # -2.0 → 0, 0.0 → 10
            return max(0.0, 10.0 + profit_atr * 5.0)

    def _score_thesis_match(self, reason: str, position: Any) -> float:
        """入场逻辑匹配评分 (0-25)

        逻辑:
        - 止盈类退出 (SCALE_OUT, DAY_TRADE_TP*): 高分
        - 追踪止损 (CHANDELIER_TRAIL) 有利润锁定: 中高分
        - 止损 (SL_HIT, FLASH_EXIT): 低分
        - 时间止损: 中低分 (信号失效)
        """
        # 基于退出原因分类
        profit_exits = {"SCALE_OUT", "DAY_TRADE_TP", "DAY_TRADE_TP_TIER1",
                        "DAY_TRADE_TP_TIER2", "DAY_TRADE_TP_TIER3"}
        trail_exits = {"CHANDELIER_TRAIL", "BREAKEVEN_STOP"}
        stop_exits = {"SL_HIT", "ATR_STOP", "STOP_LOSS", "DAY_TRADE_SL", "FLASH_EXIT"}
        time_exits = {"TIME_STOP", "DAY_TRADE_TIME_DECAY", "DAY_TRADE_TIMEOUT",
                      "TIME_EXIT"}

        if reason in profit_exits:
            # 止盈退出 — 完美匹配入场逻辑
            return 25.0
        elif reason in trail_exits:
            # 追踪止损 — 入场逻辑部分完成
            return 20.0
        elif reason in time_exits:
            # 时间止损 — 信号失效，低匹配
            return 10.0
        elif reason in stop_exits:
            # 止损退出 — 入场逻辑失败
            return 5.0
        else:
            return 12.0  # 未知规则给中间分

    def _score_timing_quality(self, held_minutes: float, position: Any) -> float:
        """持仓时间完成度评分 (0-25)

        逻辑:
        - 理想持仓时间: 60-480 分钟 (1-8小时, 美股日内)
        - 持仓时间在理想范围内: 高分
        - 持仓过短 (<30min): 低分 (信号可能未充分发展)
        - 持仓过长 (>24h): 中等分 (可能错过最佳退出点)
        """
        # 目标持有分钟数 (从 position metadata 或默认值)
        metadata = getattr(position, "metadata", None) or {}
        target_minutes = float(metadata.get("target_hold_minutes", 240))  # 默认 4 小时

        if target_minutes <= 0:
            target_minutes = 240.0

        # 完成度 = held / target, clamp to [0, 2]
        completion = min(held_minutes / target_minutes, 2.0) if target_minutes > 0 else 1.0

        if completion >= 0.8 and completion <= 1.5:
            # 理想范围: 80%-150% 完成度
            return 25.0
        elif completion > 1.5:
            # 超时持仓: 1.5→2.0 线性衰减 25→15
            return 25.0 - (completion - 1.5) / 0.5 * 10.0
        elif completion >= 0.5:
            # 50%-80%: 线性增长 15→25
            return 15.0 + (completion - 0.5) / 0.3 * 10.0
        elif completion >= 0.2:
            # 20%-50%: 线性增长 5→15
            return 5.0 + (completion - 0.2) / 0.3 * 10.0
        else:
            # <20%: 过早退出
            return max(0.0, completion / 0.2 * 5.0)

    def _score_rule_priority(self, reason: str) -> float:
        """规则基础优先级评分 (0-20)

        从配置文件加载的规则优先级，归一化到 0-20 范围。
        """
        rule_key = _REASON_TO_RULE.get(reason, "")
        raw_priority = self._priorities.get(rule_key, 50)  # 未知规则默认 50
        # 原始范围 0-100 → 归一化到 0-20
        return min(20.0, max(0.0, raw_priority / 100.0 * 20.0))


# ============================================================
#  全局单例
# ============================================================

_scorer: Optional[ExitQualityScorer] = None


def get_exit_quality_scorer() -> ExitQualityScorer:
    """获取 ExitQualityScorer 单例"""
    global _scorer
    if _scorer is None:
        _scorer = ExitQualityScorer()
    return _scorer
