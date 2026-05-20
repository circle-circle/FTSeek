"""置信度检查 — 信号置信度门槛"""

from app.risk.checks.base import RiskCheck, CheckResult
from app.core.config_defaults import CFG
from typing import Dict, Any


class ConfidenceCheck(RiskCheck):
    """检查信号置信度是否达到最低门槛"""
    
    def __init__(self, min_confidence: float = None, min_score: int = 50):
        # P1 2026-05-12 审计修复: 默认值走 CFG.MIN_CONFIDENCE, 不再硬编码 0.50
        self.min_confidence = min_confidence if min_confidence is not None else CFG.MIN_CONFIDENCE
        self.min_score = min_score
    
    @property
    def name(self) -> str:
        return "Confidence"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        confidence = getattr(signal, 'confidence', 0)
        strength = getattr(signal, 'strength', 0)
        
        if confidence < self.min_confidence:
            return self._reject(signal, f"置信度 {confidence:.2f} < {self.min_confidence}")
        
        # strength 方向检查: LONG 看 strength, SHORT 看 (100-strength)
        direction = getattr(signal, 'direction', 'LONG')
        if direction == "LONG" and strength < self.min_score:
            return self._reject(signal, f"信号强度 {strength} < {self.min_score}")
        elif direction == "SHORT" and (100 - strength) < self.min_score:
            return self._reject(signal, f"做空强度 {100-strength} < {self.min_score}")
        
        return self._pass(signal, f"confidence={confidence:.2f}, strength={strength}")
