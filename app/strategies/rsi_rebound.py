"""
RSI 反弹策略 (RSI Rebound)

策略逻辑:
- RSI < 25 → 买入信号 (超卖反弹)
- RSI > 75 → 卖出信号 (超买回落)
- 持仓时间 ≤ 3 天

统一接口:
- generate_signal(market_data) -> Optional[Signal]
- get_config() -> Dict
"""
import logging
from collections import defaultdict
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import time
import yaml
import os

from app.strategy.base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger("ftseek.strategies.rsi_rebound")


@dataclass
class MarketData:
    """市场数据结构"""
    code: str
    name: str
    price: float
    volume: float
    klines: list  # K 线数据列表
    timestamp: float


@dataclass
class PositionInfo:
    """持仓信息"""
    code: str
    entry_time: float
    entry_price: float
    signal_id: str


class RSIReboundStrategy(BaseStrategy):
    """
    RSI 反弹策略
    
    特性:
    - RSI 超卖 (<25) → 买入
    - RSI 超买 (>75) → 卖出
    - 持仓时间限制 (≤3 天)
    - 可配置参数 (YAML)
    """
    
    name = "rsi_rebound"
    
    def __init__(self, config: Dict = None):
        """
        初始化 RSI 反弹策略
        
        Args:
            config: 策略配置字典
        """
        super().__init__(config)
        
        # 从配置加载参数
        self.rsi_period = self.config.get("rsi_period", 14)
        self.oversold = self.config.get("oversold", 25)
        self.overbought = self.config.get("overbought", 75)
        self.max_holding_days = self.config.get("max_holding_days", 3)
        self.min_confidence = self.config.get("min_confidence", 0.5)
        
        # K 线缓存：code -> list of closes
        self._price_cache: Dict[str, list] = defaultdict(list)
        
        # 持仓跟踪：code -> PositionInfo
        self._positions: Dict[str, PositionInfo] = {}
        
        # 前一日 RSI 值：code -> rsi
        self._prev_rsi: Dict[str, float] = {}
        
        logger.info(f"RSI 反弹策略初始化：周期={self.rsi_period}, "
                   f"超卖<{self.oversold}, 超买>{self.overbought}, "
                   f"最大持仓={self.max_holding_days}天")
    
    def _calculate_rsi(self, prices: list, period: int = 14) -> Optional[float]:
        """
        计算 RSI 指标
        
        Args:
            prices: 收盘价列表
            period: RSI 周期
            
        Returns:
            RSI 值 (0-100), or None if insufficient data
        """
        if len(prices) < period + 1:
            return None
        
        # 计算价格变化
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        
        # 分离涨跌
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _update_price_cache(self, code: str, klines: list):
        """更新价格缓存"""
        closes = [k.get('close', 0) for k in klines if k.get('close', 0) > 0]
        
        if not closes:
            return
        
        # 保留足够的历史数据
        self._price_cache[code].extend(closes)
        max_history = self.rsi_period + 30
        if len(self._price_cache[code]) > max_history:
            self._price_cache[code] = self._price_cache[code][-max_history:]
    
    def _check_holding_time(self, code: str) -> bool:
        """
        检查持仓时间
        
        Returns:
            True if holding time <= max_holding_days, False otherwise
        """
        if code not in self._positions:
            return True
        
        position = self._positions[code]
        holding_seconds = time.time() - position.entry_time
        holding_days = holding_seconds / (24 * 3600)
        
        return holding_days <= self.max_holding_days
    
    def _get_holding_days(self, code: str) -> float:
        """获取当前持仓天数"""
        if code not in self._positions:
            return 0
        
        position = self._positions[code]
        holding_seconds = time.time() - position.entry_time
        return holding_seconds / (24 * 3600)
    
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
        
        if not klines or len(klines) < self.rsi_period + 1:
            return None
        
        # 更新价格缓存
        self._update_price_cache(code, klines)
        
        # 获取价格序列
        prices = self._price_cache[code]
        if len(prices) < self.rsi_period + 1:
            return None
        
        # 计算 RSI
        current_rsi = self._calculate_rsi(prices, self.rsi_period)
        if current_rsi is None:
            return None
        
        # 获取最新 K 线
        latest_kline = klines[-1]
        current_price = latest_kline.get('close', 0)
        current_time = latest_kline.get('timestamp', time.time())
        
        if current_price <= 0:
            return None
        
        # 获取前一日 RSI
        prev_rsi = self._prev_rsi.get(code)
        
        # 检查持仓时间
        can_trade = self._check_holding_time(code)
        
        # 买入信号：RSI 从超卖区上穿
        if prev_rsi is not None and prev_rsi <= self.oversold and current_rsi > self.oversold:
            if can_trade:
                confidence = min(1.0, (current_rsi - self.oversold) / 25 + 0.5)
                if confidence >= self.min_confidence:
                    logger.info(f"[{code}] RSI 超卖反弹：{prev_rsi:.1f} -> {current_rsi:.1f}")
                    
                    signal = Signal(
                        code=code,
                        name=market_data.name,
                        action=SignalAction.BUY,
                        price=current_price,
                        quantity=100,
                        reason=f"RSI({self.rsi_period})={current_rsi:.1f} 从超卖区 ({self.oversold}) 反弹",
                        strategy=self.name,
                        extra={
                            'rsi': round(current_rsi, 2),
                            'prev_rsi': round(prev_rsi, 2),
                            'confidence': confidence,
                            'signal_type': 'oversold_rebound'
                        }
                    )
                    
                    # 记录持仓
                    self._positions[code] = PositionInfo(
                        code=code,
                        entry_time=current_time,
                        entry_price=current_price,
                        signal_id=signal.id
                    )
                    
                    return signal
        
        # 卖出信号：RSI 从超买区下穿
        if prev_rsi is not None and prev_rsi >= self.overbought and current_rsi < self.overbought:
            confidence = min(1.0, (self.overbought - current_rsi) / 25 + 0.5)
            if confidence >= self.min_confidence:
                logger.info(f"[{code}] RSI 超买回落：{prev_rsi:.1f} -> {current_rsi:.1f}")
                
                signal = Signal(
                    code=code,
                    name=market_data.name,
                    action=SignalAction.SELL,
                    price=current_price,
                    quantity=100,
                    reason=f"RSI({self.rsi_period})={current_rsi:.1f} 从超买区 ({self.overbought}) 回落",
                    strategy=self.name,
                    extra={
                        'rsi': round(current_rsi, 2),
                        'prev_rsi': round(prev_rsi, 2),
                        'confidence': confidence,
                        'signal_type': 'overbought_decline'
                    }
                )
                
                # 清除持仓记录
                self._positions.pop(code, None)
                
                return signal
        
        # 强制卖出：持仓时间超过限制
        if code in self._positions and not can_trade:
            holding_days = self._get_holding_days(code)
            logger.info(f"[{code}] 持仓时间超限 ({holding_days:.1f}天), 强制卖出")
            
            signal = Signal(
                code=code,
                name=market_data.name,
                action=SignalAction.SELL,
                price=current_price,
                quantity=100,
                reason=f"持仓时间超限 ({holding_days:.1f}天 > {self.max_holding_days}天), 强制平仓",
                strategy=self.name,
                extra={
                    'rsi': round(current_rsi, 2),
                    'holding_days': round(holding_days, 2),
                    'signal_type': 'time_limit_exit'
                }
            )
            
            # 清除持仓记录
            self._positions.pop(code, None)
            
            return signal
        
        # 更新前一日 RSI
        self._prev_rsi[code] = current_rsi
        
        return None
    
    def get_config(self) -> Dict:
        """获取策略配置 (统一接口)"""
        return {
            'name': self.name,
            'enabled': self.enabled,
            'rsi_period': self.rsi_period,
            'oversold': self.oversold,
            'overbought': self.overbought,
            'max_holding_days': self.max_holding_days,
            'min_confidence': self.min_confidence
        }
    
    def update_config(self, config: Dict):
        """动态更新配置"""
        if 'rsi_period' in config:
            self.rsi_period = config['rsi_period']
        if 'oversold' in config:
            self.oversold = config['oversold']
        if 'overbought' in config:
            self.overbought = config['overbought']
        if 'max_holding_days' in config:
            self.max_holding_days = config['max_holding_days']
        if 'min_confidence' in config:
            self.min_confidence = config['min_confidence']
        if 'enabled' in config:
            self.enabled = config['enabled']
        
        logger.info(f"RSI 反弹策略配置已更新：{self.get_config()}")
    
    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取当前持仓"""
        return self._positions.copy()
    
    def clear_cache(self, code: str = None):
        """清除缓存"""
        if code:
            self._price_cache.pop(code, None)
            self._prev_rsi.pop(code, None)
        else:
            self._price_cache.clear()
            self._prev_rsi.clear()
    
    def clear_positions(self, code: str = None):
        """清除持仓记录"""
        if code:
            self._positions.pop(code, None)
        else:
            self._positions.clear()


# 策略工厂函数
def create_strategy(config: Dict = None) -> RSIReboundStrategy:
    """创建 RSI 反弹策略实例"""
    return RSIReboundStrategy(config)


# 策略注册
STRATEGY_CLASS = RSIReboundStrategy
