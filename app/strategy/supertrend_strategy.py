"""
SuperTrend 策略
基于 ATR 的自适应趋势跟踪策略，TradingView 上最受欢迎的趋势指标之一
方向翻转时产生信号
"""
import logging
from collections import defaultdict
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, supertrend
from app import config

logger = logging.getLogger("ftseek.strategy.supertrend")


class SuperTrendStrategy(BaseStrategy):
    name = "supertrend"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("supertrend", {}))
        self.atr_period = self.config.get("atr_period", 10)
        self.multiplier = self.config.get("multiplier", 3.0)
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_direction = {}  # code -> "up" | "down"

    def update_params(self, params: dict):
        if "atr_period" in params:
            self.atr_period = int(params["atr_period"])
        if "multiplier" in params:
            self.multiplier = float(params["multiplier"])
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        logger.info(f"SuperTrend 参数已更新: ATR period={self.atr_period}, multiplier={self.multiplier}")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "atr_period": self.atr_period,
            "multiplier": self.multiplier,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)

        if buf.size < self.atr_period + 5:
            return None

        result = supertrend(
            list(buf.high), list(buf.low), list(buf.close),
            self.atr_period, self.multiplier,
        )
        if result is None:
            return None

        st_value, direction = result
        prev_dir = self._prev_direction.get(code)
        self._prev_direction[code] = direction

        if prev_dir is None or prev_dir == direction:
            return None

        close = kline.get("close", 0)
        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        # 方向从 down → up: 买入
        if prev_dir == "down" and direction == "up":
            logger.info(f"[{code}] SuperTrend 翻多: ST={st_value:.2f}, price={close:.2f}")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.BUY, price=close, quantity=qty,
                reason=f"SuperTrend({self.atr_period},{self.multiplier}) 翻多, ST={st_value:.2f}, 价格={close:.2f}",
                strategy=self.name,
                extra={"supertrend": round(st_value, 2), "direction": direction},
            )

        # 方向从 up → down: 卖出
        if prev_dir == "up" and direction == "down":
            logger.info(f"[{code}] SuperTrend 翻空: ST={st_value:.2f}, price={close:.2f}")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.SELL, price=close, quantity=qty,
                reason=f"SuperTrend({self.atr_period},{self.multiplier}) 翻空, ST={st_value:.2f}, 价格={close:.2f}",
                strategy=self.name,
                extra={"supertrend": round(st_value, 2), "direction": direction},
            )

        return None
