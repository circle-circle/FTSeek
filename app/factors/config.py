"""
因子库配置

通过环境变量控制因子启用和权重
"""
import os
from typing import Dict
from dataclasses import dataclass


@dataclass
class FactorConfig:
    """因子配置"""
    enabled: bool = True
    weight: float = 1.0
    params: Dict = None
    
    @classmethod
    def from_env(cls) -> 'FactorConfig':
        """从环境变量加载配置"""
        return cls(
            enabled=os.getenv('FTSEEK_ENABLE_ALTERNATIVE_FACTORS', 'false').lower() == 'true',
            weight=float(os.getenv('FTSEEK_FACTOR_WEIGHT', '1.0')),
            params={}
        )


# 默认因子权重配置
DEFAULT_FACTOR_WEIGHTS = {
    'northbound': 0.3,
    'institutional': 0.3,
    'sentiment': 0.3,
    'esg': 0.1
}
