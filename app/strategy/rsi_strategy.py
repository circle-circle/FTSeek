"""
RSI 策略
基于相对强弱指标的超买超卖交易策略
TradingView 风格：支持动态参数调整
"""
import logging
from collections import defaultdict
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, rsi
from app import config

logger = logging.getLogger("ftseek.strategy.rsi")
logger.propagate = False  # 防止日志重复


class RSIStrategy(BaseStrategy):
    name = "rsi"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("rsi", {}))
        self.period = self.config.get("period", 14)
        
        # 做空优化 (2026-03-15): 从 70 降至 65，更早发现超买
        self.overbought = self.config.get("overbought", 65)
        self.oversold = self.config.get("oversold", 30)
        
        # 分离做多/做空参数
        self.long_oversold = self.config.get("long_oversold", self.oversold)
        self.short_overbought = self.config.get("short_overbought", self.overbought)
        
        self.exit_overbought = self.config.get("exit_overbought", 50)
        self.exit_oversold = self.config.get("exit_oversold", 50)
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_rsi = {}  # code -> last rsi value
        self._state = {}     # code -> "overbought" | "oversold" | None

    def update_params(self, params: dict):
        """动态更新策略参数"""
        if "period" in params:
            self.period = int(params["period"])
        if "overbought" in params:
            self.overbought = float(params["overbought"])
        if "oversold" in params:
            self.oversold = float(params["oversold"])
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        logger.info(f"RSI 参数已更新: period={self.period}, OB={self.overbought}, OS={self.oversold}")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "period": self.period,
            "overbought": self.overbought,
            "oversold": self.oversold,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)

        closes = list(buf.close)
        rsi_val = rsi(closes, self.period)
        if rsi_val is None:
            return None

        prev_rsi = self._prev_rsi.get(code)
        self._prev_rsi[code] = rsi_val
        state = self._state.get(code)

        # 超卖区上穿 → 买入信号
        if prev_rsi is not None and prev_rsi <= self.oversold and rsi_val > self.oversold:
            self._state[code] = "recovering"
            qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK
            logger.info(f"[{code}] RSI 超卖反转: {prev_rsi:.1f} -> {rsi_val:.1f}")
            return Signal(
                code=code,
                name=kline.get("name", code),
                action=SignalAction.BUY,
                price=kline.get("close", 0),
                quantity=qty,
                reason=f"RSI({self.period})={rsi_val:.1f} 从超卖区({self.oversold})反转上穿",
                strategy=self.name,
                extra={"rsi": round(rsi_val, 2), "prev_rsi": round(prev_rsi, 2)},
            )

        # 超买区下穿 → 卖出信号
        if prev_rsi is not None and prev_rsi >= self.overbought and rsi_val < self.overbought:
            self._state[code] = "declining"
            qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK
            logger.info(f"[{code}] RSI 超买回落: {prev_rsi:.1f} -> {rsi_val:.1f}")
            return Signal(
                code=code,
                name=kline.get("name", code),
                action=SignalAction.SELL,
                price=kline.get("close", 0),
                quantity=qty,
                reason=f"RSI({self.period})={rsi_val:.1f} 从超买区({self.overbought})回落下穿",
                strategy=self.name,
                extra={"rsi": round(rsi_val, 2), "prev_rsi": round(prev_rsi, 2)},
            )

        return None
