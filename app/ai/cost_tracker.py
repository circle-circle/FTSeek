"""
AI 成本监控模块

追踪 API 调用成本和预算
"""
import os
import time
import logging
from typing import Dict
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.ai.cost_tracker")


@dataclass
class CostStats:
    """成本统计"""
    daily_calls: int = 0
    daily_cost: float = 0.0
    monthly_calls: int = 0
    monthly_cost: float = 0.0
    last_reset: str = field(default_factory=lambda: time.strftime('%Y-%m-%d'))
    
    def to_dict(self) -> Dict:
        return {
            'daily_calls': self.daily_calls,
            'daily_cost': self.daily_cost,
            'monthly_calls': self.monthly_calls,
            'monthly_cost': self.monthly_cost,
            'last_reset': self.last_reset
        }


class CostTracker:
    """成本追踪器"""
    
    def __init__(self):
        self.daily_budget = float(os.getenv('AI_DAILY_BUDGET', '100'))
        self.monthly_budget = float(os.getenv('AI_MONTHLY_BUDGET', '3000'))
        self.stats = CostStats()
        self._check_budget_reset()
    
    def _check_budget_reset(self):
        """检查是否需要重置日预算"""
        today = time.strftime('%Y-%m-%d')
        if self.stats.last_reset != today:
            logger.info(f"重置日预算：{today}")
            self.stats.daily_calls = 0
            self.stats.daily_cost = 0.0
            self.stats.last_reset = today
    
    def record_call(self, tokens_used: int, cost: float):
        """记录 API 调用"""
        self._check_budget_reset()
        
        self.stats.daily_calls += 1
        self.stats.daily_cost += cost
        self.stats.monthly_calls += 1
        self.stats.monthly_cost += cost
        
        # 检查预算
        if self.stats.daily_cost > self.daily_budget:
            logger.warning(f"⚠️ 日预算超支：{self.stats.daily_cost:.2f} > {self.daily_budget}")
        
        if self.stats.monthly_cost > self.monthly_budget:
            logger.warning(f"⚠️ 月预算超支：{self.stats.monthly_cost:.2f} > {self.monthly_budget}")
        
        logger.debug(f"API 调用：tokens={tokens_used}, cost=¥{cost:.4f}")
    
    def can_call(self) -> bool:
        """检查是否可以调用 API"""
        self._check_budget_reset()
        
        if self.stats.daily_cost >= self.daily_budget:
            logger.warning(f"日预算已用尽：{self.stats.daily_cost:.2f}/{self.daily_budget}")
            return False
        
        if self.stats.monthly_cost >= self.monthly_budget:
            logger.warning(f"月预算已用尽：{self.stats.monthly_cost:.2f}/{self.monthly_budget}")
            return False
        
        return True
    
    def get_status(self) -> Dict:
        """获取成本状态"""
        self._check_budget_reset()
        
        return {
            'daily': {
                'calls': self.stats.daily_calls,
                'cost': self.stats.daily_cost,
                'budget': self.daily_budget,
                'remaining': self.daily_budget - self.stats.daily_cost,
                'usage_percent': (self.stats.daily_cost / self.daily_budget * 100) if self.daily_budget > 0 else 0
            },
            'monthly': {
                'calls': self.stats.monthly_calls,
                'cost': self.stats.monthly_cost,
                'budget': self.monthly_budget,
                'remaining': self.monthly_budget - self.stats.monthly_cost,
                'usage_percent': (self.stats.monthly_cost / self.monthly_budget * 100) if self.monthly_budget > 0 else 0
            }
        }
    
    def reset_stats(self):
        """重置统计"""
        self.stats = CostStats()


# 全局单例
_cost_tracker = None

def get_cost_tracker() -> CostTracker:
    """获取成本追踪器单例"""
    global _cost_tracker
    if _cost_tracker is None:
        _cost_tracker = CostTracker()
    return _cost_tracker
