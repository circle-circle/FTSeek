"""
融合策略 V3 - AI 增强版

在 FusionV2 基础上集成 AI 预测因子
"""
import logging
from typing import Optional, Dict
from .base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger("ftseek.strategy.fusion_v3")


class FusionV3Strategy(BaseStrategy):
    """融合策略 V3 - AI 增强版"""
    
    name = "fusion_v3"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.use_ai = config.get('use_ai', True) if config else True
        self.ai_weight = config.get('ai_weight', 0.3) if config else 0.3
        
        if self.use_ai:
            from ..factors.market_prediction_ai import AIPredictionFactor
            self.ai_predictor = AIPredictionFactor(config)
            logger.info(f"FusionV3 初始化：AI 权重={self.ai_weight}")
    
    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        """生成交易信号 (AI 增强版)"""
        if not self.enabled:
            return None
        
        # 简化实现：直接返回 AI 预测信号
        if self.use_ai and hasattr(self, 'ai_predictor'):
            data = {
                'klines': self._kline_cache.get(code, []) if hasattr(self, '_kline_cache') else [],
                'timestamp': quote.get('time', '')
            }
            
            ai_result = self.ai_predictor.calculate(code, data)
            
            if ai_result and ai_result.value != 0:
                action = SignalAction.BUY if ai_result.value > 0 else SignalAction.SELL
                confidence = abs(ai_result.value)
                
                return Signal(
                    code=code,
                    action=action,
                    confidence=confidence,
                    strategy=self.name,
                    metadata={
                        'ai_fused': True,
                        'ai_direction': ai_result.metadata.get('direction', ''),
                        'ai_reason': ai_result.metadata.get('reason', '')
                    }
                )
        
        return None
