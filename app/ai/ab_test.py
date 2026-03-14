"""
A/B 测试配置 - 小流量测试
"""
import os
from dataclasses import dataclass
from typing import Dict

@dataclass
class ABTestConfig:
    """A/B 测试配置"""
    enabled: bool = True
    control_ratio: float = 0.9  # 对照组 90%
    experiment_ratio: float = 0.1  # 实验组 10%
    ai_weight: float = 0.3
    min_signals_per_day: int = 20
    
    def should_use_ai(self, stock_code: str) -> bool:
        """判断是否使用 AI (基于股票代码哈希)"""
        import hashlib
        hash_val = int(hashlib.md5(stock_code.encode()).hexdigest(), 16)
        return (hash_val % 100) < (self.experiment_ratio * 100)
    
    def to_dict(self) -> Dict:
        return {
            'enabled': self.enabled,
            'control_ratio': self.control_ratio,
            'experiment_ratio': self.experiment_ratio,
            'ai_weight': self.ai_weight,
            'min_signals_per_day': self.min_signals_per_day
        }

# 全局配置
ab_test_config = ABTestConfig()

def get_ab_test_config() -> ABTestConfig:
    """获取 A/B 测试配置"""
    return ab_test_config
