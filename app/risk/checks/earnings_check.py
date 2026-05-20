"""财报检查 — 财报发布前 24h 禁止开新仓"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any, Optional
import logging
import time

logger = logging.getLogger("ftseek_us.risk.checks.earnings")


class EarningsCheck(RiskCheck):
    """
    财报感知检查
    
    规则:
    - 财报前 24h: 禁止开新仓 (允许平仓)
    - 财报后 1h: 放大信号权重 (波动率提升)
    """
    
    def __init__(self, pre_earnings_hours: int = 24, post_earnings_hours: int = 1):
        self.pre_earnings_hours = pre_earnings_hours
        self.post_earnings_hours = post_earnings_hours
        self._earnings_cache: Dict[str, float] = {}  # symbol -> earnings_timestamp
    
    @property
    def name(self) -> str:
        return "Earnings"
    
    def set_earnings(self, symbol: str, earnings_timestamp: float):
        """设置财报时间"""
        self._earnings_cache[symbol] = earnings_timestamp
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        symbol = getattr(signal, 'symbol', '')
        earnings_time = self._earnings_cache.get(symbol)
        
        if not earnings_time:
            return self._pass(signal, "无财报数据")
        
        now = time.time()
        hours_before = (earnings_time - now) / 3600
        hours_after = (now - earnings_time) / 3600
        
        # 财报前 24h
        if 0 < hours_before <= self.pre_earnings_hours:
            position_store = context.get("position_store") if context else None
            if position_store and position_store.has(symbol):
                return self._pass(signal, f"财报前 {hours_before:.1f}h，允许平仓")
            return self._reject(signal, f"财报前 {hours_before:.1f}h，禁止开新仓")
        
        # 财报后 1h: 标记波动率提升
        if 0 < hours_after <= self.post_earnings_hours:
            return self._pass(signal, f"财报后 {hours_after:.1f}h，波动率提升", post_earnings=True)
        
        return self._pass(signal, f"财报前 {hours_before:.1f}h，正常")
