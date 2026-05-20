"""冷却期检查 — 同股/同策略冷却"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import time


class CoolDownCheck(RiskCheck):
    """检查信号冷却期"""
    
    def __init__(self, cooldown_seconds: int = 120):
        self.cooldown_seconds = cooldown_seconds
        self._last_signal: Dict[str, float] = {}  # symbol -> timestamp
    
    @property
    def name(self) -> str:
        return "CoolDown"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        symbol = getattr(signal, 'symbol', '')
        now = time.time()
        last_time = self._last_signal.get(symbol, 0)
        
        if now - last_time < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - last_time)
            return self._reject(signal, f"冷却中 (剩余 {remaining:.0f}s)")
        
        self._last_signal[symbol] = now
        return self._pass(signal, "冷却通过")
