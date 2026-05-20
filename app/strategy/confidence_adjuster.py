#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
置信度调整器 (Confidence Adjuster) - 2026-04-09

统一管理置信度的计算和调整，解决原有问题：
1. 多次乘法降权导致指数衰减 (×0.5 × 0.5 × 0.5 = 0.125)
2. raw_confidence 和 final_confidence 不区分
3. 调整原因不可追踪

核心改进:
- 区分 raw_confidence（初始计算值）和 final_confidence（最终值）
- 降权采用加法而非乘法（避免指数衰减）
- 记录所有调整步骤和原因，支持审计
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum

logger = logging.getLogger("ftseek.confidence_adjuster")
logger.propagate = False


class AdjustmentType(str, Enum):
    """调整类型"""
    PENALTY = "penalty"       # 惩罚（降低置信度）
    BOOST = "boost"           # 增强（提高置信度）


@dataclass
class ConfidenceAdjustment:
    """单次置信度调整记录"""
    name: str                           # 调整名称
    adjustment_type: AdjustmentType     # 调整类型
    value: float                        # 调整值（加法，正=增强，负=惩罚）
    reason: str                         # 调整原因
    before: float                       # 调整前置信度
    after: float                        # 调整后置信度


@dataclass
class ConfidenceResult:
    """置信度计算结果"""
    raw_confidence: float               # 初始计算值
    final_confidence: float             # 最终值
    adjustments: List[ConfidenceAdjustment] = field(default_factory=list)
    
    @property
    def total_penalty(self) -> float:
        """总惩罚量"""
        return sum(a.value for a in self.adjustments if a.adjustment_type == AdjustmentType.PENALTY)
    
    @property
    def total_boost(self) -> float:
        """总增强量"""
        return sum(a.value for a in self.adjustments if a.adjustment_type == AdjustmentType.BOOST)
    
    @property
    def audit_trail(self) -> List[Dict]:
        """获取审计日志"""
        return [
            {
                "name": a.name,
                "type": a.adjustment_type.value,
                "value": round(a.value, 3),
                "reason": a.reason,
                "before": round(a.before, 3),
                "after": round(a.after, 3),
            }
            for a in self.adjustments
        ]


class ConfidenceAdjuster:
    """
    置信度调整器
    
    使用方式:
        adjuster = ConfidenceAdjuster(raw_confidence=0.65)
        adjuster.apply("counter_trend", -0.10, "短期BUY与日线down反向")
        adjuster.apply("duplicate_signal", -0.08, "300s内同方向重复信号")
        adjuster.apply("quality_low", -0.05, "质量分=0.6")
        result = adjuster.get_result()
    
    规则:
    1. 每次调整采用加法（-0.10 表示降低0.10）
    2. 最终置信度不低于 MIN_CONFIDENCE(0.05)，不高于 MAX_CONFIDENCE(1.0)
    3. 单次惩罚不超过 MAX_SINGLE_PENALTY(0.15)
    4. 累计惩罚不超过 MAX_TOTAL_PENALTY(0.40)
    """
    
    # 常量
    MIN_CONFIDENCE = 0.05       # 最低置信度（不要降到0，否则任何信号都无法通过）
    MAX_CONFIDENCE = 1.0        # 最高置信度
    MAX_SINGLE_PENALTY = 0.15   # 单次最大惩罚
    MAX_TOTAL_PENALTY = 0.40    # 累计最大惩罚
    MAX_SINGLE_BOOST = 0.10     # 单次最大增强
    MAX_TOTAL_BOOST = 0.20      # 累计最大增强
    
    # 预定义的调整值（统一管理，避免各处硬编码不同数值）
    # 2026-04-13 修复：单策略信号置信度约 0.35-0.41，原 -0.08 会压到门槛以下
    # 导致 duplicate_signal → 置信度<0.35 → 拒绝 → 连续拒绝 → 熔断器触发 的恶性循环
    PRESETS = {
        "counter_trend": -0.10,         # 多周期反向降权
        "duplicate_signal": -0.04,      # 300s内同方向重复 (原-0.08, 降低避免压垮单策略信号)
        "quality_low": -0.05,           # 质量分低于1.0
        "quality_very_low": -0.10,      # 质量分低于0.5
        "mtf_aligned": 0.05,            # 多周期对齐增强
        "mtf_strong_aligned": 0.08,     # 多周期强对齐增强
        "breaking_news": 0.06,          # 突发新闻增强
        "high_agreement": 0.05,         # 高度共识增强
    }
    
    def __init__(self, raw_confidence: float):
        self.raw_confidence = max(0.0, min(1.0, raw_confidence))
        self.current_confidence = self.raw_confidence
        self.adjustments: List[ConfidenceAdjustment] = []
        self._total_penalty = 0.0
        self._total_boost = 0.0
    
    def apply(self, name: str, value: float, reason: str) -> 'ConfidenceAdjuster':
        """
        应用一次置信度调整
        
        Args:
            name: 调整名称（建议使用 PRESETS 中的键名）
            value: 调整值（负=惩罚，正=增强）
            reason: 调整原因
        
        Returns:
            self（支持链式调用）
        """
        before = self.current_confidence
        
        if value < 0:
            # 惩罚
            value = max(value, -self.MAX_SINGLE_PENALTY)  # 单次限制
            remaining_penalty = self.MAX_TOTAL_PENALTY - self._total_penalty
            value = max(value, -remaining_penalty)  # 累计限制
            
            self._total_penalty += abs(value)
            adjustment_type = AdjustmentType.PENALTY
        elif value > 0:
            # 增强
            value = min(value, self.MAX_SINGLE_BOOST)  # 单次限制
            remaining_boost = self.MAX_TOTAL_BOOST - self._total_boost
            value = min(value, remaining_boost)  # 累计限制
            
            self._total_boost += value
            adjustment_type = AdjustmentType.BOOST
        else:
            return self  # 值为0，不调整
        
        # 应用调整
        self.current_confidence = max(
            self.MIN_CONFIDENCE,
            min(self.MAX_CONFIDENCE, self.current_confidence + value)
        )
        
        adjustment = ConfidenceAdjustment(
            name=name,
            adjustment_type=adjustment_type,
            value=value,
            reason=reason,
            before=before,
            after=self.current_confidence,
        )
        self.adjustments.append(adjustment)
        
        logger.debug(
            f"[置信度] {name}: {before:.3f} → {self.current_confidence:.3f} "
            f"({value:+.3f}, {reason})"
        )
        
        return self
    
    def apply_preset(self, name: str, reason: str) -> 'ConfidenceAdjuster':
        """
        使用预定义的调整值
        
        Args:
            name: PRESETS 中的键名
            reason: 调整原因
        """
        value = self.PRESETS.get(name, 0.0)
        return self.apply(name, value, reason)
    
    def get_result(self) -> ConfidenceResult:
        """获取置信度计算结果"""
        return ConfidenceResult(
            raw_confidence=round(self.raw_confidence, 3),
            final_confidence=round(self.current_confidence, 3),
            adjustments=self.adjustments,
        )
    
    @property
    def final_value(self) -> float:
        """获取最终置信度值"""
        return round(self.current_confidence, 3)
    
    def should_reject(self, min_confidence: float = 0.45) -> bool:  # 2026-05-19: 提高门槛 (原0.35)
        """检查最终置信度是否低于最低阈值"""
        return self.current_confidence < min_confidence
