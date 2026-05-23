"""
辩议论题定义

TopicType: 议题类型枚举
DebateTopic: 结构化议题 (驱动整个辩论)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TopicType(str, Enum):
    """议题类型"""
    FACTOR_PARAM = "factor_param"              # 因子参数调优
    EXIT_PROFILE = "exit_profile"              # 止损止盈调整
    FILTER_ADDITION = "filter_addition"        # 新增过滤条件
    FILTER_REMOVAL = "filter_removal"          # 移除过滤条件
    STRATEGY_VIABILITY = "strategy_viability"  # 策略存废
    CROSS_MARKET = "cross_market"              # 跨市场迁移
    FAILURE_DIAGNOSIS = "failure_diagnosis"    # 失败归因
    CUSTOM = "custom"                          # 自定义议题


@dataclass
class DebateTopic:
    """
    辩议论题 — 驱动整个辩论的结构化输入
    
    Attributes:
        topic_id: 唯一标识
        topic_type: 议题类型
        market: 适用市场 "US" | "HK" | "BOTH"
        title: 人类可读标题
        description: 详细背景
        evidence: 支撑数据 (回测统计/交易明细/日志)
        affected_components: 受影响的组件 ["factor:SSS", "exit:stable_15m"]
        injection_targets: 注入目标声明
    """
    topic_id: str
    topic_type: TopicType
    market: str = "US"
    title: str = ""
    description: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    affected_components: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    injection_targets: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "topic_type": self.topic_type.value,
            "market": self.market,
            "title": self.title,
            "description": self.description,
            "evidence": self.evidence,
            "affected_components": self.affected_components,
            "injection_targets": self.injection_targets,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DebateTopic:
        return cls(
            topic_id=data["topic_id"],
            topic_type=TopicType(data["topic_type"]),
            market=data.get("market", "US"),
            title=data.get("title", ""),
            description=data.get("description", ""),
            evidence=data.get("evidence", {}),
            affected_components=data.get("affected_components", []),
            context=data.get("context", {}),
            injection_targets=data.get("injection_targets", []),
        )
