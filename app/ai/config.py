"""
AI 特性开关配置

通过环境变量控制 AI 功能
"""
import os
from dataclasses import dataclass
from typing import Dict


@dataclass
class AIConfig:
    """AI 配置"""
    enabled: bool = False
    provider: str = 'bailian'
    weight: float = 0.3
    daily_budget: float = 100.0
    monthly_budget: float = 3000.0
    cache_ttl: int = 3600
    requests_per_minute: int = 10
    
    @classmethod
    def from_env(cls) -> 'AIConfig':
        """从环境变量加载配置"""
        return cls(
            enabled=os.getenv('FTSEEK_ENABLE_AI', 'false').lower() == 'true',
            provider=os.getenv('AI_PROVIDER', 'bailian'),
            weight=float(os.getenv('FTSEEK_AI_WEIGHT', '0.3')),
            daily_budget=float(os.getenv('AI_DAILY_BUDGET', '100')),
            monthly_budget=float(os.getenv('AI_MONTHLY_BUDGET', '3000')),
            cache_ttl=int(os.getenv('AI_CACHE_TTL', '3600')),
            requests_per_minute=int(os.getenv('AI_REQUESTS_PER_MINUTE', '10'))
        )
    
    def to_dict(self) -> Dict:
        return {
            'enabled': self.enabled,
            'provider': self.provider,
            'weight': self.weight,
            'daily_budget': self.daily_budget,
            'monthly_budget': self.monthly_budget,
            'cache_ttl': self.cache_ttl,
            'requests_per_minute': self.requests_per_minute
        }


# 便捷函数
def is_ai_enabled() -> bool:
    """检查 AI 功能是否启用"""
    return os.getenv('FTSEEK_ENABLE_AI', 'false').lower() == 'true'


def get_ai_provider() -> str:
    """获取 AI Provider"""
    return os.getenv('AI_PROVIDER', 'bailian')


def get_ai_weight() -> float:
    """获取 AI 权重"""
    return float(os.getenv('FTSEEK_AI_WEIGHT', '0.3'))
