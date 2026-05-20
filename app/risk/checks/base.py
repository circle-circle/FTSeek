"""
FTSeek-US 风控检查基类

所有风控检查必须继承此基类，实现 check() 方法。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import logging
import time

logger = logging.getLogger("ftseek_us.risk.checks")


@dataclass
class CheckResult:
    """单次检查结果"""
    passed: bool
    check_name: str
    reason: str = ""
    signal_symbol: str = ""
    signal_direction: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def is_pass(self) -> bool:
        return self.passed
    
    @property
    def is_reject(self) -> bool:
        return not self.passed


class RiskCheck(ABC):
    """
    风控检查基类
    
    每个检查独立可测试，按顺序执行。
    任一检查拒绝即终止 Pipeline。
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """检查名称"""
        pass
    
    @abstractmethod
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        """
        执行检查
        
        Args:
            signal: 信号对象 (SignalScore)
            context: 上下文信息 (价格、VIX、大盘等)
        
        Returns:
            CheckResult
        """
        pass
    
    def _pass(self, signal, reason: str = "", **kwargs) -> CheckResult:
        return CheckResult(
            passed=True,
            check_name=self.name,
            reason=reason or f"{self.name} passed",
            signal_symbol=getattr(signal, 'symbol', ''),
            signal_direction=getattr(signal, 'direction', ''),
            metadata=kwargs,
        )
    
    def _reject(self, signal, reason: str, **kwargs) -> CheckResult:
        logger.warning("[风控] %s 拒绝 | %s %s | 原因: %s", 
                       self.name, getattr(signal, 'symbol', ''), 
                       getattr(signal, 'direction', ''), reason)
        return CheckResult(
            passed=False,
            check_name=self.name,
            reason=reason,
            signal_symbol=getattr(signal, 'symbol', ''),
            signal_direction=getattr(signal, 'direction', ''),
            metadata=kwargs,
        )
