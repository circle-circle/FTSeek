"""
FTSeek-US Dual Risk Level Merger

融合富途保证金风控 (硬) + 水位回撤风控 (软)，取更严格者作为最终风控等级。

设计原则:
  1. 双重防御: 硬风控 (不爆仓) + 软风控 (不大亏)
  2. 取更严格者: 任一触发预警即收紧策略
  3. 纯函数，可单测

作者: FTSeek Team
日期: 2026-05-02
"""

import logging
from typing import Dict, Any, Optional, Tuple
from enum import Enum

logger = logging.getLogger("ftseek.risk_merger")


# ============================================================
#  统一风险等级
# ============================================================

class UnifiedRiskLevel(Enum):
    """统一风险等级 (富途 + 水位融合)"""
    SAFE = "safe"             # 双安全: 保证金充足 + 回撤正常
    MODERATE = "moderate"     # 适中: 有杠杆/小回撤但可控
    WARNING = "warning"       # 预警: 流动性不足或回撤 5-10%
    DANGER = "danger"         # 危险: 强平风险或回撤 10-20%
    BLACKOUT = "blackout"     # 熔断: 停止一切开仓


# 风险等级排序 (数字越大越严格)
RISK_RANK = {
    "safe": 0,
    "moderate": 1,
    "warning": 2,
    "danger": 3,
    "blackout": 4,
}


def merge_risk_levels(
    futu_level: str,
    water_level: str,
) -> Tuple[UnifiedRiskLevel, str, Dict[str, Any]]:
    """
    融合富途保证金风控 + 水位回撤风控，取更严格者。

    Args:
        futu_level: 富途风险等级 ("safe"|"moderate"|"warning"|"danger")
        water_level: 水位回撤等级 ("green"|"yellow"|"orange"|"red"|"blackout")

    Returns:
        (统一等级, 原因, 详情字典)
    """
    # 水位映射到统一刻度
    water_map = {"green": 0, "yellow": 1, "orange": 2, "red": 3, "blackout": 4}
    futu_map = {"safe": 0, "moderate": 1, "warning": 2, "danger": 3}

    w_rank = water_map.get(water_level, 0)
    f_rank = futu_map.get(futu_level, 0)

    # 取更严格者
    merged_rank = max(f_rank, w_rank)

    # 映射回统一等级
    unified_map = {0: UnifiedRiskLevel.SAFE, 1: UnifiedRiskLevel.MODERATE,
                   2: UnifiedRiskLevel.WARNING, 3: UnifiedRiskLevel.DANGER,
                   4: UnifiedRiskLevel.BLACKOUT}

    result_level = unified_map.get(merged_rank, UnifiedRiskLevel.SAFE)

    # 生成原因
    reasons = []
    if f_rank >= w_rank and futu_level != "safe":
        reasons.append(f"富途: {futu_level}")
    if w_rank >= f_rank and water_level != "green":
        reasons.append(f"水位: {water_level}")
    if not reasons:
        reasons.append("双安全")

    detail = {
        "futu_level": futu_level,
        "futu_rank": f_rank,
        "water_level": water_level,
        "water_rank": w_rank,
        "merged_rank": merged_rank,
        "dominant": "futu" if f_rank >= w_rank else "water",
    }

    return result_level, " | ".join(reasons), detail


def get_risk_action(level: UnifiedRiskLevel) -> Dict[str, Any]:
    """
    根据统一风险等级返回对应的操作建议。

    Returns:
        操作字典: {can_open, max_positions, max_risk_mult, require_catalyst, ...}
    """
    actions = {
        UnifiedRiskLevel.SAFE: {
            "can_open": True,
            "max_positions_override": None,  # 不限制 (由 tier 决定)
            "risk_mult": 1.0,
            "require_catalyst": False,
            "allow_short": True,
            "description": "正常交易",
        },
        UnifiedRiskLevel.MODERATE: {
            "can_open": True,
            "max_positions_override": 5,  # 最多 5 只
            "risk_mult": 0.7,             # 风险系数降至 70%
            "require_catalyst": True,     # 需要新闻催化剂
            "allow_short": True,
            "description": "适度收紧",
        },
        UnifiedRiskLevel.WARNING: {
            "can_open": True,
            "max_positions_override": 2,  # 最多 2 只
            "risk_mult": 0.4,             # 风险系数降至 40%
            "require_catalyst": True,
            "allow_short": False,         # 禁止做空
            "description": "预警收紧",
        },
        UnifiedRiskLevel.DANGER: {
            "can_open": False,
            "max_positions_override": 0,
            "risk_mult": 0.0,
            "require_catalyst": True,
            "allow_short": False,
            "description": "禁止开新仓",
        },
        UnifiedRiskLevel.BLACKOUT: {
            "can_open": False,
            "max_positions_override": 0,
            "risk_mult": 0.0,
            "require_catalyst": True,
            "allow_short": False,
            "description": "熔断 — 停止一切交易",
        },
    }
    return actions[level]
