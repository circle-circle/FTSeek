"""
止损策略模块 (Stop Loss Strategies)

支持多种止损方法：
- 固定百分比止损
- ATR 止损
- SuperTrend 止损
- 移动止损
- 时间止损
- 分级止损
- 突发新闻快速止盈 (2026-04-08 新增)
"""
import logging
from typing import Optional, Tuple, Dict
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.risk.stop_loss")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


class StopLossType(Enum):
    """止损类型"""
    FIXED_PERCENT = "fixed_percent"      # 固定百分比
    ATR = "atr"                          # ATR 止损
    SUPERTREND = "supertrend"            # SuperTrend 止损
    TRAILING = "trailing"                # 移动止损
    TIME = "time"                        # 时间止损
    TIERED = "tiered"                    # 分级止损
    BREAKING_NEWS = "breaking_news"      # 突发新闻快速止盈 (2026-04-08)


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
        default_stop_percent: float = 0.03,  # 2026-04-16 对齐 config.yaml stop_loss.default_percent (3%)
        atr_multiplier: float = 2.0,         # ATR 乘数
        trailing_percent: float = 0.03,      # 移动止损百分比
        max_holding_periods: int = 20,       # 最大持仓周期
        # 2026-04-08 新增：突发新闻快速止盈参数
        breaking_news_take_profit: float = 0.02,  # 快速止盈 2%
        breaking_news_trailing: float = 0.005,    # 追踪止盈 0.5%
        breaking_news_stop_loss: float = 0.02,    # 快速止损 2%
        breaking_news_max_minutes: int = 30,      # 最长持仓 30 分钟
    ):
        """
        初始化止损计算器

        Args:
            default_stop_percent: 默认止损百分比
            atr_multiplier: ATR 止损乘数
            trailing_percent: 移动止损百分比
            max_holding_periods: 最大持仓周期 (时间止损)
            breaking_news_take_profit: 突发新闻快速止盈百分比
            breaking_news_trailing: 突发新闻追踪止盈百分比
            breaking_news_stop_loss: 突发新闻快速止损百分比
            breaking_news_max_minutes: 突发新闻最长持仓时间 (分钟)
        """
        self.default_stop_percent = default_stop_percent
        self.atr_multiplier = atr_multiplier
        self.trailing_percent = trailing_percent
        self.max_holding_periods = max_holding_periods

        # 2026-04-08 新增：突发新闻快速止盈参数
        self.breaking_news_take_profit = breaking_news_take_profit
        self.breaking_news_trailing = breaking_news_trailing
        self.breaking_news_stop_loss = breaking_news_stop_loss
        self.breaking_news_max_minutes = breaking_news_max_minutes

        logger.info(
            f"止损计算器已初始化 | 默认止损={default_stop_percent:.1%} | "
            f"ATR 乘数={atr_multiplier} | "
            f"突发新闻止盈={breaking_news_take_profit:.1%} | "
            f"突发新闻追踪={breaking_news_trailing:.1%}"
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
        breaking_news_config: Optional[Dict] = None,
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
            breaking_news_config: 突发新闻配置 (可选，包含 is_breaking, sentiment_score 等)

        Returns:
            StopLossLevel 止损水平
        """
        # 2026-04-08 新增：突发新闻快速止盈检查
        if breaking_news_config and breaking_news_config.get('is_breaking', False):
            return self._breaking_news_exit(entry_price, direction, breaking_news_config)

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

    def _breaking_news_exit(
        self,
        entry_price: float,
        direction: str,
        breaking_news_config: Optional[Dict] = None,
    ) -> StopLossLevel:
        """
        突发新闻快速止盈（2026-04-08 新增）

        逻辑:
        1. 快速止盈：盈利 2% 即走（原 4%）
        2. 紧密追踪：回调 0.5% 即止盈（原 1.5%）
        3. 快速止损：-2% 止损（原 -3%）
        4. 时间退出：30 分钟内未止盈则平仓

        Args:
            entry_price: 入场价格
            direction: 交易方向 ("BUY" or "SELL")
            breaking_news_config: 突发新闻配置
                - is_breaking: 是否突发新闻
                - sentiment_score: 情绪得分
                - news_age_minutes: 新闻年龄

        Returns:
            StopLossLevel 止损/止盈水平
        """
        # 获取配置（支持动态覆盖）
        take_profit_pct = self.breaking_news_take_profit
        trailing_pct = self.breaking_news_trailing
        stop_loss_pct = self.breaking_news_stop_loss
        max_minutes = self.breaking_news_max_minutes

        # 根据情绪得分调整参数（情绪越强，止盈越紧）
        if breaking_news_config:
            sentiment = breaking_news_config.get('sentiment_score', 0.5)
            if sentiment > 0.8:
                # 极强情绪：更紧止盈（1.5%）
                take_profit_pct = 0.015
                trailing_pct = 0.003
            elif sentiment > 0.6:
                # 强情绪：标准止盈（2%）
                take_profit_pct = 0.02
                trailing_pct = 0.005
            else:
                # 一般情绪：放宽止盈（3%）
                take_profit_pct = 0.03
                trailing_pct = 0.01

        if direction == "BUY":
            # 做多：止盈在上，止损在下
            take_profit_price = entry_price * (1 + take_profit_pct)
            stop_loss_price = entry_price * (1 - stop_loss_pct)
            # 初始追踪止损设在入场价
            trailing_stop_price = entry_price
        else:  # SELL
            # 做空：止盈在下，止损在上
            take_profit_price = entry_price * (1 - take_profit_pct)
            stop_loss_price = entry_price * (1 + stop_loss_pct)
            trailing_stop_price = entry_price

        return StopLossLevel(
            price=stop_loss_price,  # 初始返回止损价
            type="breaking_news",
            distance=abs(entry_price - stop_loss_price),
            distance_percent=stop_loss_pct,
            notes=(
                f"突发新闻快速止盈 | "
                f"止盈={take_profit_price:.2f} (+{take_profit_pct:.1%}) | "
                f"追踪={trailing_pct:.1%} | "
                f"止损={stop_loss_price:.2f} (-{stop_loss_pct:.1%}) | "
                f"最长持仓={max_minutes}分钟"
            ),
            # 扩展字段：存储止盈和追踪信息
            triggered=False,
        )

    def check_breaking_news_exit(
        self,
        stop_level: StopLossLevel,
        current_price: float,
        direction: str,
        entry_time: Optional[str] = None,
        current_time: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        检查突发新闻止盈/止损是否触发（2026-04-08 新增）

        Args:
            stop_level: 止损水平（包含止盈信息）
            current_price: 当前价格
            direction: 交易方向
            entry_time: 入场时间
            current_time: 当前时间

        Returns:
            (已触发，触发原因)
        """
        if stop_level.type != "breaking_news":
            return False, ""

        # 解析 notes 获取止盈价
        notes = stop_level.notes
        take_profit_price = None
        try:
            for part in notes.split(" | "):
                if part.startswith("止盈="):
                    take_profit_price = float(part.split("=")[1].split(" ")[0])
                    break
        except Exception as e:
            logger.debug(f"[解析止盈价] notes内容无法解析，跳过止盈检查: {e}")

        if take_profit_price is None:
            return False, ""

        reason = ""
        triggered = False

        if direction == "BUY":
            # 做多：达到止盈价 → 触发
            if current_price >= take_profit_price:
                triggered = True
                reason = f"达到止盈价 {take_profit_price:.2f}"
                stop_level.trigger_price = current_price
            # 跌破止损价 → 触发
            elif current_price <= stop_level.price:
                triggered = True
                reason = f"跌破止损价 {stop_level.price:.2f}"
                stop_level.trigger_price = current_price
        else:  # SELL
            # 做空：达到止盈价 → 触发
            if current_price <= take_profit_price:
                triggered = True
                reason = f"达到止盈价 {take_profit_price:.2f}"
                stop_level.trigger_price = current_price
            # 突破止损价 → 触发
            elif current_price >= stop_level.price:
                triggered = True
                reason = f"突破止损价 {stop_level.price:.2f}"
                stop_level.trigger_price = current_price

        # 时间退出检查
        if not triggered and entry_time and current_time:
            try:
                from datetime import datetime
                entry_dt = datetime.fromisoformat(entry_time)
                current_dt = datetime.fromisoformat(current_time)
                holding_minutes = (current_dt - entry_dt).total_seconds() / 60

                if holding_minutes >= self.breaking_news_max_minutes:
                    triggered = True
                    reason = f"持仓超时 {holding_minutes:.0f}分钟 >= {self.breaking_news_max_minutes}分钟"
                    stop_level.trigger_price = current_price
            except Exception as e:
                logger.debug(f"时间检查失败：{e}")

        if triggered:
            stop_level.triggered = True
            stop_level.trigger_time = current_time

        return triggered, reason

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
