"""
AI 市场预测因子

基于 Bailian API 预测股票短期走势
"""
import logging
from typing import Optional, Dict
from .base import BaseFactor, FactorResult
from app.ai.base import AIService

logger = logging.getLogger("ftseek.factors.market_prediction_ai")


class AIPredictionFactor(BaseFactor):
    """AI 市场预测因子"""
    
    name = "market_prediction_ai"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.ai_service = AIService(provider=config.get('provider', 'bailian') if config else 'bailian')
        self.cache = {}
        self.cache_ttl = 3600
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算 AI 市场预测因子"""
        import time
        
        context = self._build_context(code, data)
        cache_key = f"prediction_{code}_{hash(str(context))}"
        
        if cache_key in self.cache:
            cached_result, timestamp = self.cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return cached_result
        
        response = self.ai_service.predict_market(code, context)
        
        if not response.success:
            return None
        
        prediction = response.data.get('prediction', {})
        direction = prediction.get('direction', '震荡')
        confidence = prediction.get('confidence', 50)
        
        direction_map = {'上涨': 1, '震荡': 0, '下跌': -1}
        score = direction_map.get(direction, 0) * (confidence / 100)
        
        result = FactorResult(
            code=code,
            factor_name='market_prediction_ai',
            value=score,
            timestamp=data.get('timestamp', ''),
            metadata={
                'direction': direction,
                'confidence': confidence,
                'reason': prediction.get('reason', ''),
                'ai_provider': self.ai_service.provider.name
            }
        )
        
        self.cache[cache_key] = (result, time.time())
        return result
    
    def _build_context(self, code: str, data: Dict) -> Dict:
        """构建预测上下文"""
        klines = data.get('klines', [])
        
        if len(klines) >= 50:
            closes = [k['close'] for k in klines]
            rsi = self._calc_rsi(closes, 14)
            macd_signal = self._calc_macd_signal(closes)
            ma_trend = '上涨' if closes[-1] > sum(closes[-20:])/20 else '下跌'
        else:
            rsi = 50
            macd_signal = '中性'
            ma_trend = '不明'
        
        return {
            'code': code,
            'indicators': {'rsi': rsi, 'macd_signal': macd_signal, 'ma_trend': ma_trend},
            'capital_flow': data.get('capital_flow', {}),
            'sentiment': data.get('sentiment', {})
        }
    
    def _calc_rsi(self, prices: list, period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _calc_macd_signal(self, prices: list) -> str:
        if len(prices) < 35:
            return '中性'
        ema12 = sum(prices[-12:]) / 12
        ema26 = sum(prices[-26:]) / 26
        macd = ema12 - ema26
        if macd > 0:
            return '金叉'
        elif macd < 0:
            return '死叉'
        return '中性'
