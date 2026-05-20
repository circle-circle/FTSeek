"""回撤检查 — 日内回撤限制"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any


class DrawdownCheck(RiskCheck):
    """检查日内回撤是否超限"""
    
    @property
    def name(self) -> str:
        return "Drawdown"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        risk_engine = context.get("risk_engine") if context else None
        if not risk_engine or not hasattr(risk_engine, 'drawdown_controller'):
            return self._pass(signal, "无回撤控制器，跳过")
        
        if risk_engine.drawdown_controller.is_daily_limit_breached():
            return self._reject(signal, "日内回撤熔断")
        
        return self._pass(signal, "回撤正常")
