"""
融合策略 v2 - 简化版

策略逻辑:
- 技术指标计算 (RSI/MACD/均线/成交量/布林带)
- 集成 Task 5 的情绪分析 (NewsAnalyzer)
- 融合公式：score = technical × 0.6 + sentiment × 0.4

统一接口:
- generate_signal(market_data) -> Optional[Signal]
- get_config() -> Dict
"""
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
import time
import yaml
import os

from app.strategy.base import BaseStrategy, Signal, SignalAction
# 新闻模块已移除 (2026-03-02)
# from app.analyzers.news_analyzer import NewsAnalyzer, AnalysisResult

logger = logging.getLogger("ftseek.strategies.fusion_v2")


@dataclass
class MarketData:
    """市场数据结构"""
    code: str
    name: str
    price: float
    volume: float
    klines: list  # K 线数据列表
    timestamp: float
    news: list = None  # 可选的新闻列表


class FusionV2Strategy(BaseStrategy):
    """
    融合策略 v2 - 简化版
    
    特性:
    - 技术指标 (RSI/MACD/MA/Volume/Bollinger)
    - 情绪分析 (大模型新闻分析)
    - 融合公式：score = technical × 0.6 + sentiment × 0.4
    - 可配置参数 (YAML)
    """
    
    name = "fusion_v2"
    
    def __init__(self, config: Dict = None):
        """
        初始化融合策略
        
        Args:
            config: 策略配置字典
        """
        super().__init__(config)
        
        # 从配置加载参数
        self.technical_weight = self.config.get("technical_weight", 0.6)
        self.sentiment_weight = self.config.get("sentiment_weight", 0.4)
        self.buy_threshold = self.config.get("buy_threshold", 0.65)
        
        # 技术指标参数
        self.rsi_period = self.config.get("rsi_period", 14)
        self.macd_fast = self.config.get("macd_fast", 12)
        self.macd_slow = self.config.get("macd_slow", 26)
        self.macd_signal_period = self.config.get("macd_signal_period", 9)
        self.ma_fast_period = self.config.get("ma_fast_period", 10)
        self.ma_slow_period = self.config.get("ma_slow_period", 30)
        self.bb_period = self.config.get("bb_period", 20)
        self.bb_std = self.config.get("bb_std", 2.0)
        
        # 信号阈值
        self.threshold_strong = self.config.get("threshold_strong", 0.8)
        self.threshold_recommended = self.config.get("threshold_recommended", 0.6)
        self.threshold_neutral = self.config.get("threshold_neutral", 0.4)
        
        # 初始化情绪分析器 (Task 5)
        # self.news_analyzer = NewsAnalyzer(timeout=5)  # 新闻模块已移除
        
        # K 线缓存
        self._kline_cache: Dict[str, list] = {}
        
        logger.info(f"融合策略 v2 初始化：技术{self.technical_weight:.0%} + "
                   f"情绪{self.sentiment_weight:.0%}, 买入阈值>{self.buy_threshold:.2f}")
    
    def _calculate_rsi(self, prices: list, period: int = 14) -> float:
        """计算 RSI"""
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
    
    def _calculate_macd(self, prices: list) -> tuple:
        """计算 MACD"""
        if len(prices) < self.macd_slow + self.macd_signal_period:
            return 0.0, 0.0, 0.0
        
        ema_fast = self._calc_ema(prices, self.macd_fast)
        ema_slow = self._calc_ema(prices, self.macd_slow)
        macd_line = ema_fast - ema_slow
        
        # 计算 MACD 序列用于信号线
        macd_values = []
        for i in range(self.macd_slow, len(prices)):
            ef = self._calc_ema(prices[:i+1], self.macd_fast)
            es = self._calc_ema(prices[:i+1], self.macd_slow)
            macd_values.append(ef - es)
        
        if len(macd_values) < self.macd_signal_period:
            return macd_line, 0.0, macd_line
        
        signal_line = self._calc_ema(macd_values, self.macd_signal_period)
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    def _calc_ema(self, prices: list, period: int) -> float:
        """计算指数移动平均"""
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    def _calculate_ma(self, prices: list, period: int) -> float:
        """计算简单移动平均"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period
    
    def _calculate_bollinger(self, prices: list) -> tuple:
        """计算布林带"""
        if len(prices) < self.bb_period:
            return prices[-1], prices[-1], prices[-1]
        
        middle = self._calculate_ma(prices, self.bb_period)
        variance = sum((p - middle) ** 2 for p in prices[-self.bb_period:]) / self.bb_period
        std = variance ** 0.5
        
        return middle + self.bb_std * std, middle, middle - self.bb_std * std
    
    def _calculate_technical_score(self, indicators: Dict) -> float:
        """
        计算技术指标综合评分 (-1 到 1)
        
        指标权重:
        - RSI: 25%
        - MACD: 25%
        - 均线：20%
        - 成交量：15%
        - 布林带：15%
        """
        scores = {}
        
        # RSI 评分
        rsi = indicators.get('rsi', 50)
        if rsi < 30:
            scores['rsi'] = 1.0 * (1 - rsi / 30)
        elif rsi > 70:
            scores['rsi'] = -1.0 * ((rsi - 70) / 30)
        else:
            scores['rsi'] = (50 - rsi) / 50
        
        # MACD 评分
        macd = indicators.get('macd', 0)
        macd_signal = indicators.get('macd_signal', 0)
        macd_hist = indicators.get('macd_histogram', 0)
        if macd > macd_signal and macd_hist > 0:
            scores['macd'] = 0.5 + 0.5 * min(1.0, macd_hist / max(abs(macd), 0.01))
        elif macd < macd_signal and macd_hist < 0:
            scores['macd'] = -0.5 - 0.5 * min(1.0, abs(macd_hist) / max(abs(macd), 0.01))
        else:
            scores['macd'] = macd_hist / max(abs(macd), 0.01) * 0.5
        
        # 均线评分
        price = indicators.get('price', 0)
        ma_fast = indicators.get('ma_fast', 0)
        ma_slow = indicators.get('ma_slow', 0)
        if ma_fast > 0 and ma_slow > 0:
            if price > ma_fast > ma_slow:
                scores['ma'] = 1.0
            elif price < ma_fast < ma_slow:
                scores['ma'] = -1.0
            elif ma_fast > ma_slow:
                scores['ma'] = 0.5
            else:
                scores['ma'] = -0.5
        else:
            scores['ma'] = 0.0
        
        # 成交量评分
        volume = indicators.get('volume', 0)
        volume_avg = indicators.get('volume_avg', 0)
        if volume_avg > 0:
            vol_ratio = volume / volume_avg
            if vol_ratio > 2.0:
                scores['volume'] = 1.0
            elif vol_ratio > 1.5:
                scores['volume'] = 0.5
            elif vol_ratio < 0.5:
                scores['volume'] = -0.5
            else:
                scores['volume'] = 0.0
        else:
            scores['volume'] = 0.0
        
        # 布林带评分
        price = indicators.get('price', 0)
        bb_upper = indicators.get('bb_upper', 0)
        bb_lower = indicators.get('bb_lower', 0)
        if bb_upper > 0 and bb_lower > 0:
            position = (price - bb_lower) / (bb_upper - bb_lower)
            if position < 0.1:
                scores['bollinger'] = 1.0
            elif position > 0.9:
                scores['bollinger'] = -1.0
            elif position < 0.3:
                scores['bollinger'] = 0.5
            elif position > 0.7:
                scores['bollinger'] = -0.5
            else:
                scores['bollinger'] = 0.0
        else:
            scores['bollinger'] = 0.0
        
        # 加权综合评分
        weights = {
            'rsi': 0.25,
            'macd': 0.25,
            'ma': 0.20,
            'volume': 0.15,
            'bollinger': 0.15
        }
        
        technical_score = sum(scores[k] * weights[k] for k in scores.keys())
        return max(-1.0, min(1.0, technical_score))
    
    def _calculate_sentiment_score(self, code: str) -> float:
        """
        计算情绪评分 (-1 到 1)
        
        使用 Task 5 的 NewsAnalyzer
        """
        try:
            # 使用新闻分析器分析
            # analysis = self.news_analyzer.analyze_stock(code)  # 新闻模块已移除
            
            if analysis is None:
                return 0.0
            
            # 将分析结果转换为 -1 到 1 的评分
            sentiment_map = {
                'bullish': 1.0,
                'neutral': 0.0,
                'bearish': -1.0
            }
            
            base_score = sentiment_map.get(analysis.sentiment, 0.0)
            
            # 根据影响程度调整
            impact_factor = analysis.impact_score / 10.0
            
            sentiment_score = base_score * impact_factor
            
            logger.debug(f"{code} 情绪分析：{analysis.sentiment}, "
                        f"影响度={analysis.impact_score}, 评分={sentiment_score:.2f}")
            
            return sentiment_score
            
        except Exception as e:
            logger.error(f"{code} 情绪分析失败：{e}")
            return 0.0
    
    def _generate_indicators(self, klines: list) -> Dict:
        """计算所有技术指标"""
        if len(klines) < 50:
            return {}
        
        closes = [k.get('close', 0) for k in klines]
        highs = [k.get('high', 0) for k in klines]
        lows = [k.get('low', 0) for k in klines]
        volumes = [k.get('volume', 0) for k in klines]
        
        current_price = closes[-1]
        
        macd_vals = self._calculate_macd(closes)
        bb_vals = self._calculate_bollinger(closes)
        
        return {
            'price': current_price,
            'rsi': self._calculate_rsi(closes, self.rsi_period),
            'macd': macd_vals[0],
            'macd_signal': macd_vals[1],
            'macd_histogram': macd_vals[2],
            'ma_fast': self._calculate_ma(closes, self.ma_fast_period),
            'ma_slow': self._calculate_ma(closes, self.ma_slow_period),
            'volume': volumes[-1],
            'volume_avg': sum(volumes[-20:]) / min(20, len(volumes)),
            'bb_upper': bb_vals[0],
            'bb_middle': bb_vals[1],
            'bb_lower': bb_vals[2]
        }
    
    def _determine_signal_level(self, score: float) -> str:
        """确定信号等级"""
        abs_score = abs(score)
        if abs_score >= self.threshold_strong:
            return 'strong'
        elif abs_score >= self.threshold_recommended:
            return 'recommended'
        elif abs_score >= self.threshold_neutral:
            return 'neutral'
        else:
            return 'weak'
    
    def generate_signal(self, market_data: MarketData) -> Optional[Signal]:
        """
        生成交易信号 (统一接口)
        
        Args:
            market_data: 市场数据
            
        Returns:
            Signal if triggered, None otherwise
        """
        if not self.enabled:
            return None
        
        code = market_data.code
        klines = market_data.klines
        
        if not klines or len(klines) < 50:
            return None
        
        # 更新 K 线缓存
        if code not in self._kline_cache:
            self._kline_cache[code] = []
        self._kline_cache[code].extend(klines[-10:])
        if len(self._kline_cache[code]) > 200:
            self._kline_cache[code] = self._kline_cache[code][-200:]
        
        # 计算技术指标
        indicators = self._generate_indicators(self._kline_cache[code])
        if not indicators:
            return None
        
        # 计算技术评分
        technical_score = self._calculate_technical_score(indicators)
        
        # 计算情绪评分 (Task 5)
        sentiment_score = self._calculate_sentiment_score(code)
        
        # 融合评分：score = technical × 0.6 + sentiment × 0.4
        final_score = (
            technical_score * self.technical_weight +
            sentiment_score * self.sentiment_weight
        )
        final_score = max(-1.0, min(1.0, final_score))
        
        # 置信度计算
        confidence = (
            abs(technical_score) * self.technical_weight +
            abs(sentiment_score) * self.sentiment_weight
        )
        
        # 确定信号等级
        signal_level = self._determine_signal_level(final_score)
        
        # 判断是否生成信号
        if abs(final_score) < self.threshold_neutral:
            logger.debug(f"{code} 信号太弱，跳过：score={final_score:.2f}")
            return None
        
        # 确定方向
        if final_score > 0:
            action = SignalAction.BUY
            direction = "BUY"
        else:
            action = SignalAction.SELL
            direction = "SELL"
        
        # 生成原因描述
        reason = (
            f"融合评分={final_score:.2f} | "
            f"技术分={technical_score:.2f} ({self.technical_weight:.0%}) + "
            f"情绪分={sentiment_score:.2f} ({self.sentiment_weight:.0%}) | "
            f"等级={signal_level}"
        )
        
        logger.info(f"[{code}] 生成融合信号：{direction} | "
                   f"score={final_score:.2f}, 等级={signal_level}")
        
        return Signal(
            code=code,
            name=market_data.name,
            action=action,
            price=market_data.price,
            quantity=100,
            reason=reason,
            strategy=self.name,
            extra={
                'technical_score': round(technical_score, 3),
                'sentiment_score': round(sentiment_score, 3),
                'final_score': round(final_score, 3),
                'confidence': round(confidence, 3),
                'signal_level': signal_level,
                'direction': direction,
                'indicators': {
                    'rsi': round(indicators.get('rsi', 0), 2),
                    'macd_signal': 'bullish' if indicators.get('macd', 0) > indicators.get('macd_signal', 0) else 'bearish'
                }
            }
        )
    
    def get_config(self) -> Dict:
        """获取策略配置 (统一接口)"""
        return {
            'name': self.name,
            'enabled': self.enabled,
            'technical_weight': self.technical_weight,
            'sentiment_weight': self.sentiment_weight,
            'buy_threshold': self.buy_threshold,
            'rsi_period': self.rsi_period,
            'macd_fast': self.macd_fast,
            'macd_slow': self.macd_slow,
            'ma_fast_period': self.ma_fast_period,
            'ma_slow_period': self.ma_slow_period
        }
    
    def update_config(self, config: Dict):
        """动态更新配置"""
        if 'technical_weight' in config:
            self.technical_weight = config['technical_weight']
        if 'sentiment_weight' in config:
            self.sentiment_weight = config['sentiment_weight']
        if 'buy_threshold' in config:
            self.buy_threshold = config['buy_threshold']
        if 'enabled' in config:
            self.enabled = config['enabled']
        
        logger.info(f"融合策略配置已更新：{self.get_config()}")
    
    def clear_cache(self, code: str = None):
        """清除 K 线缓存"""
        if code:
            self._kline_cache.pop(code, None)
        else:
            self._kline_cache.clear()


# 策略工厂函数
def create_strategy(config: Dict = None) -> FusionV2Strategy:
    """创建融合策略实例"""
    return FusionV2Strategy(config)


# 策略注册
STRATEGY_CLASS = FusionV2Strategy
