"""决策生成器 - 根据评分生成决策"""
import logging
from typing import Optional, Tuple, List
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.consensus.decision")

class DecisionMaker:
    """决策生成器"""
    
    def __init__(self, threshold: float = 1.5):
        self.threshold = threshold
    
    def make_decision(self, score: float) -> Tuple[Optional[SignalAction], str]:
        """
        根据评分生成决策
        
        Args:
            score: 加权评分
        
        Returns:
            (决策动作，原因)
        """
        if score >= self.threshold:
            logger.info(f"评分 {score:.2f} >= {self.threshold} → BUY")
            return SignalAction.BUY, f"评分 {score:.2f} 超过阈值 {self.threshold}"
        elif score <= -self.threshold:
            logger.info(f"评分 {score:.2f} <= -{self.threshold} → SELL")
            return SignalAction.SELL, f"评分 {score:.2f} 低于阈值 -{self.threshold}"
        else:
            logger.debug(f"评分 {score:.2f} 在阈值内 → HOLD")
            return None, f"评分 {score:.2f} 未达阈值"
