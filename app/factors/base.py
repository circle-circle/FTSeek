"""
因子基类定义
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict
import logging

logger = logging.getLogger("ftseek.factors")


@dataclass
class FactorResult:
    """因子计算结果"""
    code: str
    factor_name: str
    value: float
    timestamp: str
    metadata: Optional[Dict] = None
    
    def __post_init__(self):
        # 验证因子值在合理范围
        if not -10.0 <= self.value <= 10.0:
            logger.warning(f"因子值超出范围：{self.factor_name}={self.value}")


class BaseFactor(ABC):
    """因子基类"""
    
    name: str = "base_factor"
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)
    
    @abstractmethod
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """
        计算因子值
        
        Args:
            code: 股票代码
            data: 市场数据 (K 线/成交量等)
        
        Returns:
            FactorResult 或 None (数据不足时)
        """
        pass
    
    def validate_data(self, data: Dict, min_periods: int = 50) -> bool:
        """验证数据是否满足计算要求"""
        if not data:
            return False
        
        # 检查 K 线数据
        klines = data.get('klines', [])
        if len(klines) < min_periods:
            return False
        
        return True
