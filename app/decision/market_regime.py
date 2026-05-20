"""
市场状态识别器

识别当前市场状态：趋势/震荡/高波动
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.models import MarketRegime, TradeEvent
from app.core.events import EventStore

logger = logging.getLogger(__name__)


class MarketRegimeIdentifier:
    """
    市场状态识别器
    
    基于多个指标识别市场状态
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        self.last_regime = MarketRegime.RANGE
        self.last_check_time = None
        
        # 状态识别阈值
        self.thresholds = {
            "trend_threshold": 0.02,      # 趋势阈值 2%
            "volatility_high": 0.03,      # 高波动阈值 3%
            "volatility_low": 0.01,       # 低波动阈值 1%
            "range_bound": 0.015,         # 震荡区间阈值 1.5%
        }
    
    def identify(self, market_data: Dict[str, Any]) -> MarketRegime:
        """
        识别市场状态
        
        Args:
            market_data: 市场数据
                - spy_price: 标普 500 价格
                - spy_ma20: 20 日均线
                - spy_ma50: 50 日均线
                - vix: VIX 指数
                - atr: 平均真实波幅
                - price_change_5d: 5 日涨跌幅
        
        Returns:
            MarketRegime
        """
        # 1. 趋势判断
        is_uptrend = self._is_uptrend(market_data)
        is_downtrend = self._is_downtrend(market_data)
        
        # 2. 波动率判断
        is_high_vol = self._is_high_volatility(market_data)
        is_low_vol = self._is_low_volatility(market_data)
        
        # 3. 综合判断
        regime = self._determine_regime(is_uptrend, is_downtrend, is_high_vol, is_low_vol, market_data)
        
        # 状态变化时记录事件
        if regime != self.last_regime:
            logger.info(f"市场状态变化：{self.last_regime.value} -> {regime.value}")
            self.event_store.append(
                TradeEvent(
                    event_type="market_regime_changed",
                    data={
                        "old_regime": self.last_regime.value,
                        "new_regime": regime.value,
                        "is_uptrend": is_uptrend,
                        "is_downtrend": is_downtrend,
                        "is_high_vol": is_high_vol,
                        "is_low_vol": is_low_vol,
                    },
                )
            )
            self.last_regime = regime
        
        self.last_check_time = datetime.now()
        return regime
    
    def _is_uptrend(self, data: Dict[str, Any]) -> bool:
        """判断是否上涨趋势"""
        spy_price = data.get("spy_price", 0)
        spy_ma20 = data.get("spy_ma20", 0)
        spy_ma50 = data.get("spy_ma50", 0)
        price_change_5d = data.get("price_change_5d", 0)
        
        # 价格在均线上方 + 5 日上涨
        if spy_price > spy_ma20 > spy_ma50 and price_change_5d > self.thresholds["trend_threshold"]:
            return True
        return False
    
    def _is_downtrend(self, data: Dict[str, Any]) -> bool:
        """判断是否下跌趋势"""
        spy_price = data.get("spy_price", 0)
        spy_ma20 = data.get("spy_ma20", 0)
        spy_ma50 = data.get("spy_ma50", 0)
        price_change_5d = data.get("price_change_5d", 0)
        
        # 价格在均线下方 + 5 日下跌
        if spy_price < spy_ma20 < spy_ma50 and price_change_5d < -self.thresholds["trend_threshold"]:
            return True
        return False
    
    def _is_high_volatility(self, data: Dict[str, Any]) -> bool:
        """判断是否高波动"""
        vix = data.get("vix", 20)
        atr = data.get("atr", 0)
        avg_atr = data.get("avg_atr", 0)
        
        # VIX > 25 或 ATR 显著放大
        if vix > 25 or (avg_atr > 0 and atr > avg_atr * 1.5):
            return True
        return False
    
    def _is_low_volatility(self, data: Dict[str, Any]) -> bool:
        """判断是否低波动"""
        vix = data.get("vix", 20)
        atr = data.get("atr", 0)
        avg_atr = data.get("avg_atr", 0)
        
        # VIX < 15 或 ATR 显著缩小
        if vix < 15 or (avg_atr > 0 and atr < avg_atr * 0.7):
            return True
        return False
    
    def _determine_regime(
        self,
        is_uptrend: bool,
        is_downtrend: bool,
        is_high_vol: bool,
        is_low_vol: bool,
        data: Dict[str, Any],
    ) -> MarketRegime:
        """综合判断市场状态"""
        
        # 高波动优先
        if is_high_vol:
            return MarketRegime.HIGH_VOL
        
        # 趋势判断
        if is_uptrend:
            return MarketRegime.TREND_UP
        elif is_downtrend:
            return MarketRegime.TREND_DOWN
        
        # 低波动
        if is_low_vol:
            return MarketRegime.LOW_VOL
        
        # 默认震荡
        return MarketRegime.RANGE
    
    def get_regime_params(self, regime: MarketRegime) -> Dict[str, Any]:
        """
        获取市场状态对应的参数
        
        不同市场状态使用不同的风控和交易参数
        """
        params = {
            MarketRegime.TREND_UP: {
                "confidence_threshold": 0.45,
                "position_ratio": 0.50,
                "stop_loss": 0.06,
                "take_profit": 0.15,
                "cooldown_seconds": 60,
            },
            MarketRegime.TREND_DOWN: {
                "confidence_threshold": 0.70,
                "position_ratio": 0.25,
                "stop_loss": 0.04,
                "take_profit": 0.10,
                "cooldown_seconds": 300,
            },
            MarketRegime.RANGE: {
                "confidence_threshold": 0.60,
                "position_ratio": 0.30,
                "stop_loss": 0.05,
                "take_profit": 0.08,
                "cooldown_seconds": 180,
            },
            MarketRegime.HIGH_VOL: {
                "confidence_threshold": 0.75,
                "position_ratio": 0.15,
                "stop_loss": 0.08,
                "take_profit": 0.20,
                "cooldown_seconds": 300,
            },
            MarketRegime.LOW_VOL: {
                "confidence_threshold": 0.50,
                "position_ratio": 0.40,
                "stop_loss": 0.04,
                "take_profit": 0.10,
                "cooldown_seconds": 120,
            },
        }
        
        return params.get(regime, params[MarketRegime.RANGE])
