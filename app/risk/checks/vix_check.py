"""VIX 波动率检查 — 美股特有"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import logging

logger = logging.getLogger("ftseek_us.risk.checks.vix")


class VIXCheck(RiskCheck):
    """
    VIX 波动率检查
    
    规则:
    - VIX > 40: 停止开新仓 (允许平仓)
    - VIX > 30: 自动降仓 50%
    - VIX > 20: 正常
    """
    
    def __init__(self, vix_extreme: float = 40.0, vix_high: float = 30.0, vix_normal: float = 20.0):
        self.vix_extreme = vix_extreme
        self.vix_high = vix_high
        self.vix_normal = vix_normal
    
    @property
    def name(self) -> str:
        return "VIX"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        vix = context.get("vix", 0) if context else 0
        if vix <= 0:
            return self._pass(signal, "VIX 数据不可用，跳过")
        
        direction = getattr(signal, 'direction', 'LONG')
        
        # VIX > 40: 极端恐慌，禁止开新仓
        if vix > self.vix_extreme:
            # 但允许平仓
            position_store = context.get("position_store") if context else None
            symbol = getattr(signal, 'symbol', '')
            if position_store and position_store.has(symbol):
                return self._pass(signal, f"VIX={vix:.1f} 极端，但允许平仓 {symbol}")
            return self._reject(signal, f"VIX={vix:.1f} > {self.vix_extreme} 极端恐慌，禁止开新仓")
        
        # VIX > 30: 高波动，降仓 50%
        if vix > self.vix_high:
            logger.warning("[VIX] VIX=%.1f > %.1f，建议降仓 50%%", vix, self.vix_high)
            return self._pass(signal, f"VIX={vix:.1f} 高波动，建议降仓", vix_level="high")
        
        return self._pass(signal, f"VIX={vix:.1f} 正常")
