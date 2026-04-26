"""
DEPRECATED: 此模块未在生产中使用.
FusionV2Strategy - 动态加载但无调用方.
保留仅为向后兼容, 将在未来版本移除.
请使用 app/strategy/confluence_v2.py 或 app/strategy/layered_integration.py
"""

import logging
from typing import Optional, Dict

from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.signal_engine.fusion_engine import get_fusion_engine, reset_fusion_engine
from app.limiter.rate_limiter import get_rate_limiter
from app.storage.database import get_database

logger = logging.getLogger("ftseek.strategy.fusion_v2")
logger.propagate = False  # 防止日志重复


class FusionV2Strategy(BaseStrategy):
    """
    融合策略 v2.1 - 模块化版本
    
    特性:
    - 完全模块化设计
    - 信号方向概率显示
    - 支持热插拔模块
    - 可配置权重和阈值
    """
    
    name = "fusion_v2"
    
    def __init__(self, config: dict = None):
        super().__init__(config)
        
        # 策略配置
        self.config = config or {}
        
        # 融合引擎配置 (模块化)
        engine_config = {
            'technical': {
                'weights': self.config.get('indicator_weights', {
                    'rsi': 0.25,
                    'macd': 0.25,
                    'ma': 0.20,
                    'volume': 0.15,
                    'bollinger': 0.15
                }),
                'rsi_period': self.config.get('rsi_period', 14),
                'rsi_overbought': self.config.get('rsi_overbought', 70),
                'rsi_oversold': self.config.get('rsi_oversold', 30),
            },
            'sentiment': {
                'timeout': self.config.get('news_timeout', 10),
                'min_news_count': self.config.get('min_news_count', 1),
                'lookback_days': self.config.get('news_lookback_days', 3),
            },
            'fusion': {
                'technical_weight': self.config.get('technical_weight', 0.6),
                'sentiment_weight': self.config.get('sentiment_weight', 0.4),
                'thresholds': {
                    'strong': self.config.get('threshold_strong', 0.8),
                    'recommended': self.config.get('threshold_recommended', 0.6),
                    'neutral': self.config.get('threshold_neutral', 0.4),
                }
            },
            'output': {
                'min_confidence': self.config.get('min_confidence', 0.4),
                'min_signal_level': self.config.get('min_signal_level', 'neutral'),
            }
        }
        
        # 初始化融合引擎 (模块化)
        self.fusion_engine = get_fusion_engine(engine_config)
        
        # 频率限制器
        self.rate_limiter = get_rate_limiter(
            per_stock_limit=self.config.get('per_stock_daily_limit', 3),
            global_limit=self.config.get('global_daily_limit', 20)
        )
        
        # 数据库
        self.db = get_database()
        
        # K 线缓存
        self._kline_cache: Dict[str, list] = {}
        
        logger.info(f"融合策略 v2.1 初始化完成 (模块化)")
        logger.info(f"  权重：技术{engine_config['fusion']['technical_weight']:.0%} + "
                   f"情绪{engine_config['fusion']['sentiment_weight']:.0%}")
        logger.info(f"  信号等级：{engine_config['output']['min_signal_level']}+")
    
    def _calculate_indicators(self, code: str, klines: list) -> Dict:
        """计算技术指标"""
        if len(klines) < 50:
            return {}
        
        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]
        volumes = [k['volume'] for k in klines]
        
        current_price = closes[-1]
        
        # 计算指标
        rsi = self._calc_rsi(closes, self.config.get('rsi_period', 14))
        macd, macd_signal, macd_hist = self._calc_macd(
            closes,
            self.config.get('macd_fast', 12),
            self.config.get('macd_slow', 26),
            self.config.get('macd_signal_period', 9)
        )
        ma_fast = self._calc_ma(closes, self.config.get('ma_fast_period', 10))
        ma_slow = self._calc_ma(closes, self.config.get('ma_slow_period', 30))
        bb_upper, bb_middle, bb_lower = self._calc_bollinger(
            closes,
            self.config.get('bb_period', 20),
            self.config.get('bb_std', 2.0)
        )
        volume_avg = sum(volumes[-20:]) / min(20, len(volumes))
        
        return {
            'current_price': current_price,
            'rsi': rsi,
            'macd': macd,
            'macd_signal': macd_signal,
            'macd_histogram': macd_hist,
            'ma_fast': ma_fast,
            'ma_slow': ma_slow,
            'volume': volumes[-1],
            'volume_avg': volume_avg,
            'bollinger_upper': bb_upper,
            'bollinger_middle': bb_middle,
            'bollinger_lower': bb_lower
        }
    
    def _calc_rsi(self, prices: list, period: int = 14) -> float:
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
    
    def _calc_ma(self, prices: list, period: int) -> float:
        """计算简单移动平均"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period
    
    def _calc_macd(self, prices: list, fast: int, slow: int, signal: int):
        """计算 MACD"""
        if len(prices) < slow + signal:
            return 0.0, 0.0, 0.0
        
        ema_fast = self._calc_ema(prices, fast)
        ema_slow = self._calc_ema(prices, slow)
        macd_line = ema_fast - ema_slow
        
        macd_values = []
        for i in range(slow, len(prices)):
            ef = self._calc_ema(prices[:i+1], fast)
            es = self._calc_ema(prices[:i+1], slow)
            macd_values.append(ef - es)
        
        if len(macd_values) < signal:
            return macd_line, 0.0, macd_line
        
        signal_line = self._calc_ema(macd_values, signal)
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
    
    def _calc_bollinger(self, prices: list, period: int, std_dev: float):
        """计算布林带"""
        if len(prices) < period:
            return prices[-1], prices[-1], prices[-1]
        
        middle = self._calc_ma(prices, period)
        variance = sum((p - middle) ** 2 for p in prices[-period:]) / period
        std = variance ** 0.5
        
        return middle + std_dev * std, middle, middle - std_dev * std
    
    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        """实时报价回调"""
        if not self.enabled:
            return None
        
        # 频率限制检查
        limit_result = self.rate_limiter.check(code, None)
        if not limit_result.allowed:
            logger.info(f"{code} 触发频率限制：{limit_result.reason}")
            return None
        
        # 获取价格
        current_price = quote.get('last_done', 0)
        if current_price <= 0:
            return None
        
        # 获取 K 线数据
        klines = self._kline_cache.get(code, [])
        if len(klines) < 50:
            return None
        
        # 计算指标
        indicators = self._calculate_indicators(code, klines)
        if not indicators:
            return None
        
        # 生成融合信号 (模块化引擎)
        stock_name = quote.get('name', code)
        result = self.fusion_engine.generate_signal(
            code=code,
            name=stock_name,
            price=current_price,
            indicators=indicators,
            strategy=self.name,
            quantity=100
        )
        
        if not result:
            return None
        
        # 转换为 Signal 对象
        signal = result.to_signal(expire_seconds=900)
        
        # 保存到数据库
        signal_data = {
            'id': signal.id,
            'code': signal.code,
            'name': signal.name,
            'action': signal.action.value,
            'price': signal.price,
            'quantity': signal.quantity,
            'strategy': signal.strategy,
            'technical_score': result.technical_score,
            'sentiment_score': result.sentiment_score,
            'confidence': result.confidence,
            'reason': signal.reason,
            'status': 'pending',
            'extra': {
                'direction': result.direction,
                'buy_probability': result.buy_probability,
                'sell_probability': result.sell_probability,
                'signal_level': result.signal_level
            }
        }
        self.db.save_signal(signal_data)
        
        logger.info(f"生成信号：{code} {result.direction} | "
                   f"BUY:{result.buy_probability:.1%} SELL:{result.sell_probability:.1%} | "
                   f"等级:{result.signal_level}")
        
        return signal
    
    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """K 线数据回调"""
        if not self.enabled:
            return None
        
        # 更新 K 线缓存
        if code not in self._kline_cache:
            self._kline_cache[code] = []
        
        self._kline_cache[code].append(kline)
        if len(self._kline_cache[code]) > 200:
            self._kline_cache[code] = self._kline_cache[code][-200:]
        
        # 只在日 K 线生成信号
        if kline_type != 'K_DAY':
            return None
        
        current_price = kline.get('close', 0)
        if current_price <= 0:
            return None
        
        indicators = self._calculate_indicators(code, self._kline_cache[code])
        if not indicators:
            return None
        
        stock_name = kline.get('name', code)
        result = self.fusion_engine.generate_signal(
            code=code,
            name=stock_name,
            price=current_price,
            indicators=indicators,
            strategy=self.name,
            quantity=100
        )
        
        if not result:
            return None
        
        signal = result.to_signal(expire_seconds=900)
        
        signal_data = {
            'id': signal.id,
            'code': signal.code,
            'name': signal.name,
            'action': signal.action.value,
            'price': signal.price,
            'quantity': signal.quantity,
            'strategy': signal.strategy,
            'technical_score': result.technical_score,
            'sentiment_score': result.sentiment_score,
            'confidence': result.confidence,
            'reason': signal.reason,
            'status': 'pending'
        }
        self.db.save_signal(signal_data)
        
        return signal
    
    def update_kline(self, code: str, kline: dict):
        """手动更新 K 线缓存"""
        if code not in self._kline_cache:
            self._kline_cache[code] = []
        
        self._kline_cache[code].append(kline)
        if len(self._kline_cache[code]) > 200:
            self._kline_cache[code] = self._kline_cache[code][-200:]
    
    def clear_cache(self, code: str = None):
        """清除 K 线缓存"""
        if code:
            self._kline_cache.pop(code, None)
        else:
            self._kline_cache.clear()
    
    def get_params(self) -> dict:
        """获取策略参数"""
        return {
            'technical_weight': self.config.get('technical_weight', 0.6),
            'sentiment_weight': self.config.get('sentiment_weight', 0.4),
            'min_confidence': self.config.get('min_confidence', 0.4),
            'rsi_period': self.config.get('rsi_period', 14),
            'macd_fast': self.config.get('macd_fast', 12),
            'macd_slow': self.config.get('macd_slow', 26),
            'ma_fast_period': self.config.get('ma_fast_period', 10),
            'ma_slow_period': self.config.get('ma_slow_period', 30),
        }
    
    def update_module_config(self, module_name: str, config: dict):
        """
        动态更新模块配置 (热插拔)
        
        Args:
            module_name: 模块名称 (technical/sentiment/fusion/output)
            config: 新配置
        """
        module = self.fusion_engine.get_module(module_name)
        if module:
            for key, value in config.items():
                if hasattr(module, key):
                    setattr(module, key, value)
            logger.info(f"模块配置已更新：{module_name}")
        else:
            logger.warning(f"未知模块：{module_name}")


# 策略注册
STRATEGY_CLASS = FusionV2Strategy
