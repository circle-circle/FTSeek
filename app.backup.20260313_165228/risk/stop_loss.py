"""
止损策略模块 (Stop Loss Strategies)

支持多种止损方法：
- 固定百分比止损
- ATR 止损
- SuperTrend 止损
- 移动止损
- 时间止损
- 分级止损
"""
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.risk.stop_loss")


class StopLossType(Enum):
    """止损类型"""
    FIXED_PERCENT = "fixed_percent"      # 固定百分比
    ATR = "atr"                          # ATR 止损
    SUPERTREND = "supertrend"            # SuperTrend 止损
    TRAILING = "trailing"                # 移动止损
    TIME = "time"                        # 时间止损
    TIERED = "tiered"                    # 分级止损


@dataclass
class StopLossLevel:
    """止损水平"""
    price: float                # 止损价格
    type: str                   # 止损类型
    distance: float             # 距离入场价的距离
    distance_percent: float     # 距离百分比
    triggered: bool = False     # 是否已触发
    trigger_price: Optional[float] = None  # 触发价格
    trigger_time: Optional[str] = None     # 触发时间
    notes: str = ""             # 备注说明


class StopLossCalculator:
    """
    止损计算器
    
    根据入场价和策略参数计算止损位
    """

    def __init__(
        self,
        default_stop_percent: float = 0.05,  # 默认 5% 止损
        atr_multiplier: float = 2.0,         # ATR 乘数
        trailing_percent: float = 0.03,      # 移动止损百分比
        max_holding_periods: int = 20,       # 最大持仓周期
    ):
        """
        初始化止损计算器
        
        Args:
            default_stop_percent: 默认止损百分比
            atr_multiplier: ATR 止损乘数
            trailing_percent: 移动止损百分比
            max_holding_periods: 最大持仓周期 (时间止损)
        """
        self.default_stop_percent = default_stop_percent
        self.atr_multiplier = atr_multiplier
        self.trailing_percent = trailing_percent
        self.max_holding_periods = max_holding_periods

        logger.info(
            f"止损计算器已初始化 | 默认止损={default_stop_percent:.1%} | "
            f"ATR 乘数={atr_multiplier}"
        )

    def calculate(
        self,
        entry_price: float,
        direction: str,  # "BUY" or "SELL"
        method: StopLossType = StopLossType.FIXED_PERCENT,
        atr_value: Optional[float] = None,
        supertrend_value: Optional[float] = None,
        highest_price: Optional[float] = None,
        lowest_price: Optional[float] = None,
        entry_time: Optional[str] = None,
        current_time: Optional[str] = None,
    ) -> StopLossLevel:
        """
        计算止损位
        
        Args:
            entry_price: 入场价格
            direction: 交易方向 ("BUY" or "SELL")
            method: 止损方法
            atr_value: ATR 值 (用于 ATR 止损)
            supertrend_value: SuperTrend 值 (用于 SuperTrend 止损)
            highest_price: 最高价 (用于移动止损)
            lowest_price: 最低价 (用于移动止损)
            entry_time: 入场时间
            current_time: 当前时间
            
        Returns:
            StopLossLevel 止损水平
        """
        if method == StopLossType.FIXED_PERCENT:
            return self._fixed_percent(entry_price, direction)
        elif method == StopLossType.ATR:
            return self._atr(entry_price, direction, atr_value)
        elif method == StopLossType.SUPERTREND:
            return self._supertrend(entry_price, direction, supertrend_value)
        elif method == StopLossType.TRAILING:
            return self._trailing(entry_price, direction, highest_price, lowest_price)
        elif method == StopLossType.TIME:
            return self._time(entry_price, direction, entry_time, current_time)
        else:
            return self._fixed_percent(entry_price, direction)

    def _fixed_percent(
        self,
        entry_price: float,
        direction: str,
    ) -> StopLossLevel:
        """固定百分比止损"""
        if direction == "BUY":
            stop_price = entry_price * (1 - self.default_stop_percent)
        else:  # SELL
            stop_price = entry_price * (1 + self.default_stop_percent)

        distance = abs(entry_price - stop_price)
        distance_percent = distance / entry_price

        return StopLossLevel(
            price=stop_price,
            type="fixed_percent",
            distance=distance,
            distance_percent=distance_percent,
        )

    def _atr(
        self,
        entry_price: float,
        direction: str,
        atr_value: Optional[float],
    ) -> StopLossLevel:
        """ATR 止损"""
        if atr_value is None or atr_value <= 0:
            return self._fixed_percent(entry_price, direction)

        stop_distance = atr_value * self.atr_multiplier

        if direction == "BUY":
            stop_price = entry_price - stop_distance
        else:  # SELL
            stop_price = entry_price + stop_distance

        distance = abs(entry_price - stop_price)
        distance_percent = distance / entry_price

        return StopLossLevel(
            price=stop_price,
            type="atr",
            distance=distance,
            distance_percent=distance_percent,
            notes=f"ATR={atr_value:.2f}, 乘数={self.atr_multiplier}",
        )

    def _supertrend(
        self,
        entry_price: float,
        direction: str,
        supertrend_value: Optional[float],
    ) -> StopLossLevel:
        """SuperTrend 止损"""
        if supertrend_value is None or supertrend_value <= 0:
            return self._fixed_percent(entry_price, direction)

        # SuperTrend 本身就是动态止损位
        stop_price = supertrend_value
        distance = abs(entry_price - stop_price)
        distance_percent = distance / entry_price

        return StopLossLevel(
            price=stop_price,
            type="supertrend",
            distance=distance,
            distance_percent=distance_percent,
            notes=f"SuperTrend={supertrend_value:.2f}",
        )

    def _trailing(
        self,
        entry_price: float,
        direction: str,
        highest_price: Optional[float],
        lowest_price: Optional[float],
    ) -> StopLossLevel:
        """移动止损"""
        if direction == "BUY":
            # 做多：从最高价回撤
            if highest_price is None:
                highest_price = entry_price
            
            stop_price = highest_price * (1 - self.trailing_percent)
            
            # 止损只能上移，不能下移
            min_stop = entry_price * (1 - self.default_stop_percent)
            stop_price = max(stop_price, min_stop)
        else:  # SELL
            # 做空：从最低价反弹
            if lowest_price is None:
                lowest_price = entry_price
            
            stop_price = lowest_price * (1 + self.trailing_percent)
            
            # 止损只能下移，不能上移
            max_stop = entry_price * (1 + self.default_stop_percent)
            stop_price = min(stop_price, max_stop)

        distance = abs(entry_price - stop_price)
        distance_percent = distance / entry_price

        return StopLossLevel(
            price=stop_price,
            type="trailing",
            distance=distance,
            distance_percent=distance_percent,
            notes=f"移动止损={self.trailing_percent:.1%}",
        )

    def _time(
        self,
        entry_price: float,
        direction: str,
        entry_time: Optional[str],
        current_time: Optional[str],
    ) -> StopLossLevel:
        """时间止损"""
        # 时间止损不改变止损价，只是判断是否应该退出
        # 这里返回固定止损，由外部判断时间
        stop_level = self._fixed_percent(entry_price, direction)
        stop_level.type = "time"
        stop_level.notes = f"最大持仓={self.max_holding_periods}周期"
        return stop_level

    def check_stop_loss(
        self,
        stop_level: StopLossLevel,
        current_price: float,
        direction: str,
    ) -> bool:
        """
        检查是否触发止损
        
        Args:
            stop_level: 止损水平
            current_price: 当前价格
            direction: 交易方向
            
        Returns:
            True 表示已触发止损
        """
        if direction == "BUY":
            # 做多：价格跌破止损位
            triggered = current_price <= stop_level.price
        else:  # SELL
            # 做空：价格突破止损位
            triggered = current_price >= stop_level.price

        if triggered:
            stop_level.triggered = True
            stop_level.trigger_price = current_price

        return triggered

    def update_trailing_stop(
        self,
        stop_level: StopLossLevel,
        current_price: float,
        direction: str,
    ) -> StopLossLevel:
        """
        更新移动止损位
        
        Args:
            stop_level: 当前止损水平
            current_price: 当前价格
            direction: 交易方向
            
        Returns:
            更新后的止损水平
        """
        if direction == "BUY":
            # 做多：价格上涨时上移止损
            new_stop = current_price * (1 - self.trailing_percent)
            if new_stop > stop_level.price:
                stop_level.price = new_stop
        else:  # SELL
            # 做空：价格下跌时下移止损
            new_stop = current_price * (1 + self.trailing_percent)
            if new_stop < stop_level.price:
                stop_level.price = new_stop

        return stop_level


class TieredStopLoss:
    """
    分级止损策略
    
    将仓位分成多份，每份使用不同的止损策略
    """

    def __init__(self, n_tiers: int = 3):
        """
        初始化分级止损
        
        Args:
            n_tiers: 分级数量
        """
        self.n_tiers = n_tiers
        self.calculator = StopLossCalculator()

        logger.info(f"分级止损已初始化 | 分级数={n_tiers}")

    def calculate_tiers(
        self,
        entry_price: float,
        total_quantity: int,
        direction: str,
        atr_value: Optional[float] = None,
    ) -> list:
        """
        计算各级止损
        
        Args:
            entry_price: 入场价格
            total_quantity: 总数量
            direction: 交易方向
            atr_value: ATR 值
            
        Returns:
            各级止损配置列表
        """
        quantity_per_tier = total_quantity // self.n_tiers
        tiers = []

        # 第 1 级：紧止损 (1.5x ATR)
        stop1 = self.calculator.calculate(
            entry_price, direction,
            StopLossType.ATR,
            atr_value=atr_value * 1.5 if atr_value else None,
        )
        tiers.append({
            "tier": 1,
            "quantity": quantity_per_tier,
            "stop_price": stop1.price,
            "stop_type": "atr_1.5x",
            "risk_percent": stop1.distance_percent,
        })

        # 第 2 级：中等止损 (2.5x ATR)
        stop2 = self.calculator.calculate(
            entry_price, direction,
            StopLossType.ATR,
            atr_value=atr_value * 2.5 if atr_value else None,
        )
        tiers.append({
            "tier": 2,
            "quantity": quantity_per_tier,
            "stop_price": stop2.price,
            "stop_type": "atr_2.5x",
            "risk_percent": stop2.distance_percent,
        })

        # 第 3 级：宽止损 (3.5x ATR 或 SuperTrend)
        stop3 = self.calculator.calculate(
            entry_price, direction,
            StopLossType.ATR,
            atr_value=atr_value * 3.5 if atr_value else None,
        )
        tiers.append({
            "tier": 3,
            "quantity": total_quantity - 2 * quantity_per_tier,  # 剩余数量
            "stop_price": stop3.price,
            "stop_type": "atr_3.5x",
            "risk_percent": stop3.distance_percent,
        })

        return tiers


# 全局单例
stop_loss_calculator = StopLossCalculator()
