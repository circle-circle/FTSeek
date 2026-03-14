"""
均线交叉策略
MA快线上穿慢线 -> 买入信号; 快线下穿慢线 -> 卖出信号
"""
import logging
from collections import defaultdict, deque
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from app import config

logger = logging.getLogger("ftseek.strategy.ma_cross")


class MACrossStrategy(BaseStrategy):
    name = "ma_cross"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("ma_cross", {}))
        self.fast_period = self.config.get("fast_period", 5)
        self.slow_period = self.config.get("slow_period", 20)
        # code -> deque of close prices
        self._prices = defaultdict(lambda: deque(maxlen=self.slow_period + 5))
        self._prev_state = {}  # code -> "golden" / "death" / None

    def _ma(self, prices, period):
        if len(prices) < period:
            return None
        return sum(list(prices)[-period:]) / period

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        close = kline.get("close", 0)
        if close <= 0:
            return None

        self._prices[code].append(close)

        fast_ma = self._ma(self._prices[code], self.fast_period)
        slow_ma = self._ma(self._prices[code], self.slow_period)

        if fast_ma is None or slow_ma is None:
            return None

        prev = self._prev_state.get(code)

        if fast_ma > slow_ma:
            current = "golden"
        elif fast_ma < slow_ma:
            current = "death"
        else:
            current = prev

        self._prev_state[code] = current

        # 金叉: 从death变为golden
        if prev == "death" and current == "golden":
            logger.info(f"[{code}] 均线金叉 MA{self.fast_period}={fast_ma:.2f} > MA{self.slow_period}={slow_ma:.2f}")
            return Signal(
                code=code,
                name=kline.get("name", code),
                action=SignalAction.BUY,
                price=close,
                quantity=config.DEFAULT_ORDER_QTY,
                reason=f"MA{self.fast_period}({fast_ma:.2f}) 上穿 MA{self.slow_period}({slow_ma:.2f}), 金叉买入",
                strategy=self.name,
            )

        # 死叉: 从golden变为death
        if prev == "golden" and current == "death":
            logger.info(f"[{code}] 均线死叉 MA{self.fast_period}={fast_ma:.2f} < MA{self.slow_period}={slow_ma:.2f}")
            return Signal(
                code=code,
                name=kline.get("name", code),
                action=SignalAction.SELL,
                price=close,
                quantity=config.DEFAULT_ORDER_QTY,
                reason=f"MA{self.fast_period}({fast_ma:.2f}) 下穿 MA{self.slow_period}({slow_ma:.2f}), 死叉卖出",
                strategy=self.name,
            )

        return None
