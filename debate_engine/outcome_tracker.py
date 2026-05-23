"""
结论回填与持续学习 (Outcome Tracking)

追踪辩论结论注入后的实际效果:
- 注入后 7 天 — 首次检查
- 注入后 20 天 — 正式评估
- 注入后 60 天 — 长期验证
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from debate_engine.knowledge import DebateKnowledgeBase, Outcome
from debate_engine.verdict import Injection

logger = logging.getLogger(__name__)


class OutcomeTracker:
    """
    追踪辩论结论注入后的实际效果
    
    Usage:
        tracker = OutcomeTracker(knowledge_base)
        outcomes = tracker.check_outcomes(days_threshold=7)
        report = tracker.generate_learning_report()
    """
    
    def __init__(self, kb: Optional[DebateKnowledgeBase] = None):
        self.kb = kb or DebateKnowledgeBase()
    
    def record_manual_outcome(self, debate_id: str, injection_id: str,
                               metric_name: str, value_before: float,
                               value_after: float, observation_days: int,
                               observation_trades: int, verdict: str,
                               note: str = "") -> Outcome:
        """手动记录回填结果"""
        outcome = Outcome(
            outcome_id=str(uuid.uuid4())[:8],
            debate_id=debate_id,
            injection_id=injection_id,
            metric_name=metric_name,
            value_before=value_before,
            value_after=value_after,
            observation_days=observation_days,
            observation_trades=observation_trades,
            verdict=verdict,
            note=note,
            recorded_at=datetime.now().isoformat(),
        )
        self.kb.add_outcome(outcome)
        return outcome
    
    def generate_learning_report(self) -> Dict[str, Any]:
        """
        生成学习报告 — 辩论引擎的自我进化
        
        包含:
        - 历史辩论胜率 (结论被验证的比例)
        - 各模型的预测准确率
        - 高效结论的特征
        - 建议的辩论策略调整
        """
        stats = self.kb.stats
        
        report = {
            "generated_at": datetime.now().isoformat(),
            "summary": stats,
            "confirmation_rate": (
                stats["confirmed"] / max(stats["total_outcomes"], 1)
            ),
            "refutation_rate": (
                stats["refuted"] / max(stats["total_outcomes"], 1)
            ),
            "lesson_validation_rate": (
                stats["validated_lessons"] / max(stats["total_lessons"], 1)
            ),
            "recommendations": self._generate_recommendations(stats),
        }
        
        return report
    
    def _generate_recommendations(self, stats: Dict[str, int]) -> List[str]:
        """基于统计生成建议"""
        recs = []
        
        if stats["total_debates"] < 5:
            recs.append("辩论次数较少，建议增加辩论频率以积累更多历史数据")
        
        if stats["total_lessons"] > 0:
            val_rate = stats["validated_lessons"] / stats["total_lessons"]
            if val_rate < 0.3:
                recs.append("教训验证率较低，建议增加回填追踪频率")
        
        if stats["total_outcomes"] > 0:
            conf_rate = stats["confirmed"] / stats["total_outcomes"]
            if conf_rate < 0.4:
                recs.append("结论确认率<40%，辩论质量可能需要提升 (检查 prompt 模板)")
            elif conf_rate > 0.8:
                recs.append("结论确认率>80%，辩论引擎表现优秀")
        
        return recs
    
    def list_pending_injections(self) -> List[Dict[str, Any]]:
        """列出已注入但尚未评估的 injection"""
        # 简化: 返回所有 active overrides
        from debate_engine.verdict import InjectionApplier
        applier = InjectionApplier()
        return applier.list_active()
