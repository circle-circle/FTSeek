#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓位降级策略 (Position Degradation Policy)

职责:
当数据质量下降时，定义对现有仓位的具体收缩策略。
核心原则: 数据盲区不是继续持有的理由，而是收缩风险的理由。

三层防御:
- PARTIAL  (部分缺失)  -> 收紧止损，禁止开新仓
- DEGRADED (严重缺失)  -> 强制保本，禁止加仓
- BLIND    (完全不可用) -> 逐步减仓 -> 市价全平
"""

import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict


class DataQuality(Enum):
    """数据质量等级 (与 futu_margin_monitor.DataQuality 共享定义)"""
    FULL = "FULL"           # 完整数据，所有规则可用
    PARTIAL = "PARTIAL"     # 部分数据缺失，降级到保守规则
    DEGRADED = "DEGRADED"   # 严重缺失，仅保留保命逻辑
    BLIND = "BLIND"         # 数据完全不可用


logger = logging.getLogger("ftseek.risk.position_degradation")


class PositionAction(Enum):
    """对现有仓位的动作指令"""
    HOLD = "HOLD"                      # 正常持有
    TIGHTEN_STOP = "TIGHTEN_STOP"      # 收紧止损线 20%
    FORCE_BREAKEVEN = "FORCE_BREAKEVEN" # 强制保本止损
    REDUCE_25 = "REDUCE_25"            # 减仓 25%
    REDUCE_50 = "REDUCE_50"            # 减仓 50%
    CLOSE_ALL = "CLOSE_ALL"            # 市价全平
    NO_ADD = "NO_ADD"                  # 禁止加仓


@dataclass
class DegradationRule:
    """
    每个数据质量等级对应的仓位动作规则。
    """
    quality: DataQuality
    # 对现有仓位的动作
    position_action: PositionAction
    # 是否允许开新仓
    allow_new_positions: bool
    # 是否允许加仓
    allow_adding: bool
    # 止损线收紧比例 (0 = 不收紧, 0.2 = 收紧 20%)
    stop_loss_tighten_ratio: float
    # 降级持续时间超过此值后触发更严格动作 (秒)
    escalate_after_seconds: float
    # 升级后的动作
    escalate_action: Optional[PositionAction]
    # 描述
    description: str


# 默认降级规则
# 注意: 数据盲区不是继续持有的理由，而是收缩风险的理由
DEFAULT_DEGRADATION_RULES: Dict[DataQuality, DegradationRule] = {
    DataQuality.FULL: DegradationRule(
        quality=DataQuality.FULL,
        position_action=PositionAction.HOLD,
        allow_new_positions=True,
        allow_adding=True,
        stop_loss_tighten_ratio=0.0,
        escalate_after_seconds=0,
        escalate_action=None,
        description="数据完整，正常交易",
    ),
    DataQuality.PARTIAL: DegradationRule(
        quality=DataQuality.PARTIAL,
        position_action=PositionAction.TIGHTEN_STOP,
        allow_new_positions=False,
        allow_adding=False,
        stop_loss_tighten_ratio=0.20,  # 收紧 20%
        escalate_after_seconds=1800,   # 30 分钟后升级
        escalate_action=PositionAction.FORCE_BREAKEVEN,
        description="数据部分缺失 -> 收紧止损，禁止新开仓",
    ),
    DataQuality.DEGRADED: DegradationRule(
        quality=DataQuality.DEGRADED,
        position_action=PositionAction.FORCE_BREAKEVEN,
        allow_new_positions=False,
        allow_adding=False,
        stop_loss_tighten_ratio=0.0,  # 直接保本
        escalate_after_seconds=600,   # 10 分钟后升级
        escalate_action=PositionAction.REDUCE_50,
        description="数据严重缺失 -> 强制保本，禁止加仓",
    ),
    DataQuality.BLIND: DegradationRule(
        quality=DataQuality.BLIND,
        position_action=PositionAction.REDUCE_25,
        allow_new_positions=False,
        allow_adding=False,
        stop_loss_tighten_ratio=0.0,
        escalate_after_seconds=300,   # 5 分钟后升级到减仓 50%
        escalate_action=PositionAction.REDUCE_50,
        description="数据完全不可用 -> 逐步减仓，最终市价全平",
    ),
}


class PositionDegradationPolicy:
    """
    仓位降级策略引擎 -- 根据数据质量决定对现有仓位的动作。
    
    用法:
        policy = PositionDegradationPolicy()
        policy.update_quality(DataQuality.DEGRADED)
        rule = policy.get_current_rule()
        action = rule.position_action  # FORCE_BREAKEVEN
    """
    
    def __init__(self, rules: Optional[Dict[DataQuality, DegradationRule]] = None):
        self.rules = rules or DEFAULT_DEGRADATION_RULES
        self._current_quality = DataQuality.FULL
        self._quality_since: float = time.time()
        self._escalation_triggered: bool = False
        
        logger.info(
            f"仓位降级策略已启动 | {len(self.rules)} 条规则 | "
            f"核心原则: 数据盲区 = 收缩风险"
        )
    
    def update_quality(self, quality: DataQuality):
        """
        更新数据质量，触发对应的仓位动作。
        如果降级持续时间超过阈值，自动升级到更严格动作。
        """
        # 如果质量变化，重置计时器
        if quality != self._current_quality:
            self._current_quality = quality
            self._quality_since = time.time()
            self._escalation_triggered = False
            
            rule = self.rules.get(quality)
            logger.info(
                f"[仓位降级] 数据质量 -> {quality.value} | "
                f"动作: {rule.position_action.value} | "
                f"规则: {rule.description}"
            )
        
        # 检查是否应该升级
        self._check_escalation()
    
    def _check_escalation(self):
        """检查降级是否持续太久，需要升级到更严格动作"""
        if self._escalation_triggered:
            return
        
        rule = self.rules.get(self._current_quality)
        if not rule or not rule.escalate_action:
            return
        
        elapsed = time.time() - self._quality_since
        if elapsed >= rule.escalate_after_seconds:
            self._escalation_triggered = True
            logger.warning(
                f"[仓位降级升级] {self._current_quality.value} 持续 {elapsed:.0f}s "
                f"> {rule.escalate_after_seconds}s -> "
                f"升级动作: {rule.escalate_action.value}"
            )
    
    def get_current_rule(self) -> DegradationRule:
        """获取当前规则（考虑升级后的动作）"""
        rule = self.rules.get(self._current_quality)
        if not rule:
            return self.rules[DataQuality.FULL]
        
        if self._escalation_triggered and rule.escalate_action:
            # 返回升级后的规则副本
            escalated = DegradationRule(
                quality=rule.quality,
                position_action=rule.escalate_action,
                allow_new_positions=False,
                allow_adding=False,
                stop_loss_tighten_ratio=rule.stop_loss_tighten_ratio,
                escalate_after_seconds=0,
                escalate_action=None,
                description=f"[升级] {rule.description}",
            )
            return escalated
        
        return rule
    
    def get_position_action(self) -> PositionAction:
        """获取当前应对现有仓位的动作"""
        return self.get_current_rule().position_action
    
    def should_block_new_positions(self) -> bool:
        """是否应该阻止开新仓"""
        return not self.get_current_rule().allow_new_positions
    
    def should_block_adding(self) -> bool:
        """是否应该阻止加仓"""
        return not self.get_current_rule().allow_adding
    
    def get_stop_loss_tighten_ratio(self) -> float:
        """获取止损线收紧比例"""
        return self.get_current_rule().stop_loss_tighten_ratio
    
    def get_status(self) -> Dict:
        """获取当前策略状态"""
        rule = self.get_current_rule()
        elapsed = time.time() - self._quality_since
        
        return {
            "quality": self._current_quality.value,
            "position_action": rule.position_action.value,
            "allow_new_positions": rule.allow_new_positions,
            "allow_adding": rule.allow_adding,
            "stop_loss_tighten_ratio": rule.stop_loss_tighten_ratio,
            "escalation_triggered": self._escalation_triggered,
            "duration_seconds": elapsed,
            "description": rule.description,
        }
    
    def reset(self):
        """重置策略状态"""
        self._current_quality = DataQuality.FULL
        self._quality_since = time.time()
        self._escalation_triggered = False
        logger.info("仓位降级策略已重置")
