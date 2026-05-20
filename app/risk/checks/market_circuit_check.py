"""大盘熔断检查 — 美股 L1/L2/L3 熔断"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import logging

logger = logging.getLogger("ftseek_us.risk.checks.market_circuit")


class MarketCircuitCheck(RiskCheck):
    """
    美股大盘熔断检查
    
    L1: S&P 500 -7% → 暂停 15 分钟
    L2: S&P 500 -13% → 暂停 15 分钟
    L3: S&P 500 -20% → 全天停止
    """
    
    def __init__(self, level_1: float = -0.07, level_2: float = -0.13, level_3: float = -0.20):
        self.level_1 = level_1
        self.level_2 = level_2
        self.level_3 = level_3
    
    @property
    def name(self) -> str:
        return "MarketCircuit"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        spy_change = context.get("spy_change_pct", 0) if context else 0
        
        if spy_change <= self.level_3:
            return self._reject(signal, f"大盘 L3 熔断 (SPY {spy_change:.1%})，全天停止")
        
        if spy_change <= self.level_2:
            return self._reject(signal, f"大盘 L2 熔断 (SPY {spy_change:.1%})，暂停 15 分钟")
        
        if spy_change <= self.level_1:
            return self._reject(signal, f"大盘 L1 熔断 (SPY {spy_change:.1%})，暂停 15 分钟")
        
        return self._pass(signal, f"SPY {spy_change:.1%} 正常")
