"""
辩论数据模型

定义多空辩论的核心数据结构
"""
from dataclasses import dataclass, field
from typing import List
from datetime import datetime


@dataclass
class DebateArgument:
    """单轮辩论论点"""
    side: str  # "BULL" or "BEAR"
    core_thesis: str  # 核心论点(50字内)
    supporting_points: List[str]  # 支撑论据(最多3条)
    risk_factors: List[str]  # 风险因素
    confidence: float  # 该侧信心度 (0.0-1.0)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "side": self.side,
            "core_thesis": self.core_thesis,
            "supporting_points": self.supporting_points,
            "risk_factors": self.risk_factors,
            "confidence": round(self.confidence, 2),
            "timestamp": self.timestamp,
        }


@dataclass
class DebateRound:
    """单轮辩论记录"""
    round_number: int
    bull_argument: DebateArgument
    bear_argument: DebateArgument
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "round_number": self.round_number,
            "bull": self.bull_argument.to_dict(),
            "bear": self.bear_argument.to_dict(),
            "timestamp": self.timestamp,
        }


@dataclass
class DebateSummary:
    """辩论总结"""
    bull_case: DebateArgument  # 多头最终观点
    bear_case: DebateArgument  # 空头最终观点
    divergence_score: float  # 分歧度 0-1 (越高表示分歧越大)
    consensus_points: List[str]  # 共识点
    key_risks: List[str]  # 关键风险
    recommendation: str  # "STRONG_BULL" / "WEAK_BULL" / "NEUTRAL" / "WEAK_BEAR" / "STRONG_BEAR"
    debate_rounds: List[DebateRound] = field(default_factory=list)  # 辩论过程记录
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "bull_case": self.bull_case.to_dict(),
            "bear_case": self.bear_case.to_dict(),
            "divergence_score": round(self.divergence_score, 2),
            "consensus_points": self.consensus_points,
            "key_risks": self.key_risks,
            "recommendation": self.recommendation,
            "rounds": [r.to_dict() for r in self.debate_rounds],
            "timestamp": self.timestamp,
        }
