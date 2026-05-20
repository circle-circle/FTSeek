"""组合式止盈止损配置 (P0 强化版)"""
from dataclasses import dataclass
from typing import Dict
from enum import Enum


class StrategyType(Enum):
    """策略类型"""
    SHORT_TERM = "SHORT_TERM"    # 短线 (1-5 天)
    MID_TERM = "MID_TERM"        # 中线 (1-4 周)
    LONG_TERM = "LONG_TERM"      # 长线 (1-6 个月)
    SHORT = "SHORT"              # 卖空交易


@dataclass
class ExitConfig:
    """组合式止盈止损配置 (P0 强化版)
    
    P0 强化规则:
    - 卖空止损：+8% (入场价 × 1.08)
    - 做多止损：-5% 至 -15% (根据策略类型)
      - 短线：-5%
      - 中线：-8%
      - 长线：-15%
    - 监控间隔：30 秒 (原 5 分钟)
    """
    
    # P0 强化：卖空止损 +8%
    short_stop_loss: float = 0.08  # 卖空止损 +8%
    
    # P0 强化：做多止损根据策略类型
    long_stop_loss_short_term: float = -0.05   # 短线 -5%
    long_stop_loss_mid_term: float = -0.08     # 中线 -8%
    long_stop_loss_long_term: float = -0.15    # 长线 -15%
    
    # 分批止盈 (价格目标 + 平仓比例)
    take_profit_1: float = 0.08   # +8%
    take_profit_ratio_1: float = 0.30  # 平 30%
    
    take_profit_2: float = 0.15   # +15%
    take_profit_ratio_2: float = 0.30  # 平 30%
    
    take_profit_3: float = 0.25   # +25%
    take_profit_ratio_3: float = 0.40  # 平 40%
    
    # 移动止损
    trailing_stop_atr_mult: float = 2.0  # ATR 倍数
    
    # 时间止损
    time_stop_hours: int = 72
    
    # P0 强化：监控配置
    monitor_interval_seconds: int = 30  # 监控间隔 30 秒
    
    # P0 强化：执行重试配置
    max_retry_count: int = 3  # 最大重试次数
    retry_interval_seconds: int = 5  # 重试间隔
    
    def get_stop_loss(self, strategy_type: StrategyType, direction: str = "LONG") -> float:
        """
        获取止损比例 (P0 强化)
        
        Args:
            strategy_type: 策略类型
            direction: 方向 (LONG/SHORT)
            
        Returns:
            止损比例 (正数表示上涨幅度，负数表示下跌幅度)
        """
        if direction == "SHORT":
            # 卖空止损：+8%
            return self.short_stop_loss
        else:
            # 做多止损：根据策略类型
            if strategy_type == StrategyType.SHORT_TERM:
                return self.long_stop_loss_short_term
            elif strategy_type == StrategyType.MID_TERM:
                return self.long_stop_loss_mid_term
            elif strategy_type == StrategyType.LONG_TERM:
                return self.long_stop_loss_long_term
            else:
                # 默认使用中线止损
                return self.long_stop_loss_mid_term
    
    def get_stop_loss_price(self, entry_price: float, strategy_type: StrategyType, 
                            direction: str = "LONG") -> float:
        """
        计算止损价格 (P0 强化)
        
        Args:
            entry_price: 入场价
            strategy_type: 策略类型
            direction: 方向 (LONG/SHORT)
            
        Returns:
            止损价格
        """
        stop_loss_pct = self.get_stop_loss(strategy_type, direction)
        
        if direction == "SHORT":
            # 卖空止损 = 入场价 × (1 + 止损%)
            return entry_price * (1 + stop_loss_pct)
        else:
            # 做多止损 = 入场价 × (1 + 止损%) (止损%为负数)
            return entry_price * (1 + stop_loss_pct)
    
    def get_exit_levels(self, entry_price: float, strategy_type: StrategyType = StrategyType.MID_TERM,
                        direction: str = "LONG") -> Dict[str, float]:
        """获取退出价位 (P0 强化版)"""
        stop_loss = self.get_stop_loss_price(entry_price, strategy_type, direction)
        
        return {
            "stop_loss": stop_loss,
            "take_profit_1": entry_price * (1 + self.take_profit_1),
            "take_profit_2": entry_price * (1 + self.take_profit_2),
            "take_profit_3": entry_price * (1 + self.take_profit_3),
        }
    
    def get_exit_ratios(self) -> Dict[str, float]:
        """获取平仓比例"""
        return {
            "take_profit_1": self.take_profit_ratio_1,
            "take_profit_2": self.take_profit_ratio_2,
            "take_profit_3": self.take_profit_ratio_3,
        }
