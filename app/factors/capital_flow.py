"""
资金流因子模块

基于价格和成交量计算资金流向指标
"""
import logging
from typing import Optional, Dict
from .base import BaseFactor, FactorResult

logger = logging.getLogger("ftseek.factors.capital_flow")
logger.propagate = False  # 防止日志重复


class CapitalFlowFactors(BaseFactor):
    """资金流因子"""
    
    name = "capital_flow"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        self.mfi_period = self.config.get('mfi_period', 14)
        self.flow_window = self.config.get('flow_window', 5)
    
    def calculate(self, code: str, data: Dict) -> Optional[FactorResult]:
        """计算资金流因子"""
        if not self.validate_data(data):
            return None
        
        klines = data['klines']
        mfi = self._calc_mfi(klines, self.mfi_period)
        flow_trend = self._calc_flow_trend(klines, self.flow_window)
        
        # 综合评分 (-1 到 1)
        score = (mfi - 50) / 50 * 0.5 + flow_trend * 0.5
        
        return FactorResult(
            code=code,
            factor_name='capital_flow',
            value=score,
            timestamp=klines[-1]['date'],
            metadata={'mfi': mfi, 'flow_trend': flow_trend}
        )
    
    def _calc_mfi(self, klines: list, period: int = 14) -> float:
        """计算 MFI (资金流量指标)"""
        if len(klines) < period + 1:
            return 50.0
        
        positive_flow = []
        negative_flow = []
        
        for i in range(1, len(klines)):
            typical_price = (klines[i]['high'] + klines[i]['low'] + klines[i]['close']) / 3
            raw_money_flow = typical_price * klines[i]['volume']
            
            if klines[i]['close'] > klines[i-1]['close']:
                positive_flow.append(raw_money_flow)
                negative_flow.append(0)
            else:
                positive_flow.append(0)
                negative_flow.append(raw_money_flow)
        
        if len(positive_flow) >= period:
            avg_positive = sum(positive_flow[-period:]) / period
            avg_negative = sum(negative_flow[-period:]) / period
            
            if avg_negative == 0:
                return 100.0
            
            money_ratio = avg_positive / avg_negative
            mfi = 100 - (100 / (1 + money_ratio))
            return mfi
        
        return 50.0
    
    def _calc_flow_trend(self, klines: list, window: int = 5) -> float:
        """计算资金流趋势"""
        if len(klines) < window:
            return 0.0
        
        inflow_days = 0
        outflow_days = 0
        
        for i in range(-window, 0):
            price_change = klines[i]['close'] - klines[i-1]['close']
            volume_change = klines[i]['volume'] - klines[i-1]['volume']
            
            if price_change > 0 and volume_change > 0:
                inflow_days += 1
            elif price_change < 0 and volume_change > 0:
                outflow_days += 1
        
        if inflow_days + outflow_days == 0:
            return 0.0
        
        return (inflow_days - outflow_days) / (inflow_days + outflow_days)
