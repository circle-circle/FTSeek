"""评分计算器"""
import logging
from typing import List, Dict, Optional
from .vote_collector import Vote
from .weight_calculator import WeightCalculator
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.consensus.scores")

class ScoreCalculator:
    """评分计算器"""
    
    def __init__(self, weight_calc: WeightCalculator):
        self.weight_calc = weight_calc
    
    def calculate_score(self, votes: List[Vote], market_regime: Optional[str] = None) -> float:
        if not votes:
            return 0.0
        score = 0.0
        for vote in votes:
            weight = self.weight_calc.calculate_weight(vote.strategy, market_regime)
            direction = 1 if vote.action == SignalAction.BUY else -1
            score += weight * direction
        return score
