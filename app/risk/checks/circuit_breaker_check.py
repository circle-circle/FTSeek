"""熔断器检查 — 策略/品种/系统三级熔断"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any


class CircuitBreakerCheck(RiskCheck):
    """检查熔断器状态"""
    
    @property
    def name(self) -> str:
        return "CircuitBreaker"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        risk_engine = context.get("risk_engine") if context else None
        if not risk_engine:
            return self._pass(signal, "无风控引擎，跳过熔断检查")
        
        # 系统熔断
        if hasattr(risk_engine, 'system_breaker') and risk_engine.system_breaker.is_triggered():
            return self._reject(signal, "系统熔断器已触发")
        
        # 策略熔断
        if hasattr(risk_engine, 'strategy_breaker'):
            strategy_id = getattr(signal, 'strategy_id', '')
            if risk_engine.strategy_breaker.is_triggered(strategy_id):
                return self._reject(signal, f"策略熔断 (strategy_id={strategy_id})")
        
        # 品种熔断
        if hasattr(risk_engine, 'symbol_breaker'):
            symbol = getattr(signal, 'symbol', '')
            if risk_engine.symbol_breaker.is_frozen(symbol):
                return self._reject(signal, f"品种已冻结 ({symbol})")
        
        return self._pass(signal, "熔断器正常")
