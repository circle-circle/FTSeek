"""
MACD 策略
基于 MACD 柱状图翻转 + 零轴交叉的趋势跟踪策略
TradingView 风格：支持动态参数调整
"""
import logging
from collections import defaultdict
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, macd
from app import config

logger = logging.getLogger("ftseek.strategy.macd")
logger.propagate = False  # 防止日志重复


class MACDStrategy(BaseStrategy):
    name = "macd"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("macd", {}))
        self.fast_period = self.config.get("fast_period", 12)
        self.slow_period = self.config.get("slow_period", 26)
        self.signal_period = self.config.get("signal_period", 9)
        self.signal_mode = self.config.get("signal_mode", "histogram")  # "histogram" | "zero_cross" | "both"
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_hist = {}  # code -> previous histogram value
        self._prev_macd = {}  # code -> previous macd line value

    def update_params(self, params: dict):
        if "fast_period" in params:
            self.fast_period = int(params["fast_period"])
        if "slow_period" in params:
            self.slow_period = int(params["slow_period"])
        if "signal_period" in params:
            self.signal_period = int(params["signal_period"])
        if "signal_mode" in params:
            self.signal_mode = params["signal_mode"]
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        logger.info(f"MACD 参数已更新: {self.fast_period}/{self.slow_period}/{self.signal_period} mode={self.signal_mode}")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "signal_period": self.signal_period,
            "signal_mode": self.signal_mode,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)
        closes = list(buf.close)

        result = macd(closes, self.fast_period, self.slow_period, self.signal_period)
        if result is None:
            return None

        macd_line, signal_line, histogram = result
        prev_hist = self._prev_hist.get(code)
        prev_macd = self._prev_macd.get(code)
        self._prev_hist[code] = histogram
        self._prev_macd[code] = macd_line

        if prev_hist is None:
            return None

        close = kline.get("close", 0)
        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        # 柱状图翻转信号
        if self.signal_mode in ("histogram", "both"):
            # 柱状图从负转正 → 买入
            if prev_hist < 0 and histogram >= 0:
                logger.info(f"[{code}] MACD 柱状图金叉: hist {prev_hist:.4f} -> {histogram:.4f}")
                return Signal(
                    code=code,
                    name=kline.get("name", code),
                    action=SignalAction.BUY,
                    price=close,
                    quantity=qty,
                    reason=f"MACD({self.fast_period},{self.slow_period},{self.signal_period}) 柱状图金叉, MACD={macd_line:.4f}, Signal={signal_line:.4f}",
                    strategy=self.name,
                    extra={"macd": round(macd_line, 4), "signal": round(signal_line, 4), "histogram": round(histogram, 4)},
                )
            # 柱状图从正转负 → 卖出
            if prev_hist > 0 and histogram <= 0:
                logger.info(f"[{code}] MACD 柱状图死叉: hist {prev_hist:.4f} -> {histogram:.4f}")
                return Signal(
                    code=code,
                    name=kline.get("name", code),
                    action=SignalAction.SELL,
                    price=close,
                    quantity=qty,
                    reason=f"MACD({self.fast_period},{self.slow_period},{self.signal_period}) 柱状图死叉, MACD={macd_line:.4f}, Signal={signal_line:.4f}",
                    strategy=self.name,
                    extra={"macd": round(macd_line, 4), "signal": round(signal_line, 4), "histogram": round(histogram, 4)},
                )

        # MACD 零轴交叉信号
        if self.signal_mode in ("zero_cross", "both") and prev_macd is not None:
            if prev_macd < 0 and macd_line >= 0:
                logger.info(f"[{code}] MACD 上穿零轴: {prev_macd:.4f} -> {macd_line:.4f}")
                return Signal(
                    code=code,
                    name=kline.get("name", code),
                    action=SignalAction.BUY,
                    price=close,
                    quantity=qty,
                    reason=f"MACD 上穿零轴, MACD={macd_line:.4f}",
                    strategy=self.name,
                    extra={"macd": round(macd_line, 4), "zero_cross": "bullish"},
                )
            if prev_macd > 0 and macd_line <= 0:
                logger.info(f"[{code}] MACD 下穿零轴: {prev_macd:.4f} -> {macd_line:.4f}")
                return Signal(
                    code=code,
                    name=kline.get("name", code),
                    action=SignalAction.SELL,
                    price=close,
                    quantity=qty,
                    reason=f"MACD 下穿零轴, MACD={macd_line:.4f}",
                    strategy=self.name,
                    extra={"macd": round(macd_line, 4), "zero_cross": "bearish"},
                )

        return None
