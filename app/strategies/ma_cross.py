"""
均线交叉策略 (MA Cross)

策略逻辑:
- MA5 上穿 MA20 → 买入信号 (金叉)
- MA5 下穿 MA20 → 卖出信号 (死叉)
- 成交量过滤：成交量 > 1.5 倍均量才触发信号

统一接口:
- generate_signal(market_data) -> Optional[Signal]
- get_config() -> Dict
"""
import logging
from collections import defaultdict, deque
from typing import Optional, Dict, Any
from dataclasses import dataclass
import yaml
import os

from app.strategy.base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger("ftseek.strategies.ma_cross")


@dataclass
class MarketData:
    """市场数据结构"""
    code: str
    name: str
    price: float
    volume: float
    klines: list  # K 线数据列表
    timestamp: float


class MACrossStrategy(BaseStrategy):
    """
    均线交叉策略
    
    特性:
    - MA 快线上穿慢线 → 买入
    - MA 快线下穿慢线 → 卖出
    - 成交量过滤 (>1.5 倍均量)
    - 可配置参数 (YAML)
    """
    
    name = "ma_cross"
    
    def __init__(self, config: Dict = None):
        """
        初始化均线交叉策略
        
        Args:
            config: 策略配置字典
        """
        super().__init__(config)
        
        # 从配置加载参数
        self.fast_period = self.config.get("fast_ma", 5)
        self.slow_period = self.config.get("slow_ma", 20)
        self.volume_filter = self.config.get("volume_filter", 1.5)
        self.min_confidence = self.config.get("min_confidence", 0.5)
        
        # K 线缓存：code -> deque of klines
        self._kline_cache: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.slow_period + 10)
        )
        
        # 状态跟踪：code -> "golden" / "death" / None
        self._prev_state: Dict[str, Optional[str]] = {}
        
        # 成交量缓存：code -> deque of volumes
        self._volume_cache: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=20)
        )
        
        logger.info(f"均线交叉策略初始化：MA{self.fast_period}/{self.slow_period}, "
                   f"成交量过滤>{self.volume_filter}x")
    
    def _calculate_ma(self, prices: list, period: int) -> Optional[float]:
        """计算简单移动平均"""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period
    
    def _calculate_volume_avg(self, volumes: list, period: int = 20) -> float:
        """计算平均成交量"""
        if len(volumes) < period:
            return sum(volumes) / len(volumes) if volumes else 0
        return sum(volumes[-period:]) / period
    
    def _update_cache(self, code: str, kline: Dict):
        """更新 K 线和成交量缓存"""
        self._kline_cache[code].append(kline)
        volume = kline.get('volume', 0)
        if volume > 0:
            self._volume_cache[code].append(volume)
    
    def _check_volume_filter(self, code: str, current_volume: float) -> bool:
        """
        成交量过滤检查
        
        Returns:
            True if volume > threshold, False otherwise
        """
        volumes = list(self._volume_cache[code])
        if len(volumes) < 5:
            return True  # 数据不足时不过滤
        
        avg_volume = self._calculate_volume_avg(volumes, 20)
        if avg_volume <= 0:
            return True
        
        volume_ratio = current_volume / avg_volume
        return volume_ratio >= self.volume_filter
    
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
        
        if not klines or len(klines) < self.slow_period:
            return None
        
        # 更新缓存
        for kline in klines[-10:]:  # 更新最近 10 条 K 线
            self._update_cache(code, kline)
        
        # 获取最新 K 线
        latest_kline = klines[-1]
        current_price = latest_kline.get('close', 0)
        current_volume = latest_kline.get('volume', 0)
        
        if current_price <= 0:
            return None
        
        # 成交量过滤
        if not self._check_volume_filter(code, current_volume):
            logger.debug(f"{code} 成交量不足，跳过信号")
            return None
        
        # 提取收盘价
        closes = [k.get('close', 0) for k in self._kline_cache[code]]
        if len(closes) < self.slow_period:
            return None
        
        # 计算 MA
        fast_ma = self._calculate_ma(closes, self.fast_period)
        slow_ma = self._calculate_ma(closes, self.slow_period)
        
        if fast_ma is None or slow_ma is None:
            return None
        
        # 判断状态
        prev_state = self._prev_state.get(code)
        
        if fast_ma > slow_ma:
            current_state = "golden"
        elif fast_ma < slow_ma:
            current_state = "death"
        else:
            current_state = prev_state
        
        # 检测金叉：从 death 变为 golden
        if prev_state == "death" and current_state == "golden":
            confidence = min(1.0, (fast_ma - slow_ma) / slow_ma + 0.5)
            if confidence >= self.min_confidence:
                logger.info(f"[{code}] 均线金叉：MA{self.fast_period}={fast_ma:.2f} > "
                           f"MA{self.slow_period}={slow_ma:.2f}")
                
                return Signal(
                    code=code,
                    name=market_data.name,
                    action=SignalAction.BUY,
                    price=current_price,
                    quantity=100,
                    reason=f"MA{self.fast_period}({fast_ma:.2f}) 上穿 MA{self.slow_period}({slow_ma:.2f}), "
                           f"金叉买入，成交量{current_volume:.0f}",
                    strategy=self.name,
                    extra={
                        'fast_ma': fast_ma,
                        'slow_ma': slow_ma,
                        'volume': current_volume,
                        'confidence': confidence
                    }
                )
        
        # 检测死叉：从 golden 变为 death
        if prev_state == "golden" and current_state == "death":
            confidence = min(1.0, (slow_ma - fast_ma) / slow_ma + 0.5)
            if confidence >= self.min_confidence:
                logger.info(f"[{code}] 均线死叉：MA{self.fast_period}={fast_ma:.2f} < "
                           f"MA{self.slow_period}={slow_ma:.2f}")
                
                return Signal(
                    code=code,
                    name=market_data.name,
                    action=SignalAction.SELL,
                    price=current_price,
                    quantity=100,
                    reason=f"MA{self.fast_period}({fast_ma:.2f}) 下穿 MA{self.slow_period}({slow_ma:.2f}), "
                           f"死叉卖出，成交量{current_volume:.0f}",
                    strategy=self.name,
                    extra={
                        'fast_ma': fast_ma,
                        'slow_ma': slow_ma,
                        'volume': current_volume,
                        'confidence': confidence
                    }
                )
        
        # 更新状态
        self._prev_state[code] = current_state
        
        return None
    
    def get_config(self) -> Dict:
        """获取策略配置 (统一接口)"""
        return {
            'name': self.name,
            'enabled': self.enabled,
            'fast_ma': self.fast_period,
            'slow_ma': self.slow_period,
            'volume_filter': self.volume_filter,
            'min_confidence': self.min_confidence
        }
    
    def update_config(self, config: Dict):
        """动态更新配置"""
        if 'fast_ma' in config:
            self.fast_period = config['fast_ma']
        if 'slow_ma' in config:
            self.slow_period = config['slow_ma']
        if 'volume_filter' in config:
            self.volume_filter = config['volume_filter']
        if 'min_confidence' in config:
            self.min_confidence = config['min_confidence']
        if 'enabled' in config:
            self.enabled = config['enabled']
        
        logger.info(f"均线交叉策略配置已更新：{self.get_config()}")
    
    def clear_cache(self, code: str = None):
        """清除缓存"""
        if code:
            self._kline_cache.pop(code, None)
            self._volume_cache.pop(code, None)
            self._prev_state.pop(code, None)
        else:
            self._kline_cache.clear()
            self._volume_cache.clear()
            self._prev_state.clear()


# 策略工厂函数
def create_strategy(config: Dict = None) -> MACrossStrategy:
    """创建均线交叉策略实例"""
    return MACrossStrategy(config)


# 策略注册
STRATEGY_CLASS = MACrossStrategy
