"""
布林带策略
基于 Bollinger Bands 的均值回归 + 突破策略
支持两种模式: reversion (均值回归) / breakout (突破追踪)
"""
import logging
from collections import defaultdict
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, bollinger_bands, bollinger_pctb
from app import config

logger = logging.getLogger("ftseek.strategy.bollinger")


class BollingerStrategy(BaseStrategy):
    name = "bollinger"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("bollinger", {}))
        self.period = self.config.get("period", 20)
        self.std_dev = self.config.get("std_dev", 2.0)
        self.mode = self.config.get("mode", "reversion")  # "reversion" | "breakout"
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_pctb = {}  # code -> previous %B

    def update_params(self, params: dict):
        if "period" in params:
            self.period = int(params["period"])
        if "std_dev" in params:
            self.std_dev = float(params["std_dev"])
        if "mode" in params:
            self.mode = params["mode"]
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        logger.info(f"Bollinger 参数已更新: period={self.period}, std={self.std_dev}, mode={self.mode}")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "period": self.period,
            "std_dev": self.std_dev,
            "mode": self.mode,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)
        closes = list(buf.close)

        bb = bollinger_bands(closes, self.period, self.std_dev)
        if bb is None:
            return None

        upper, middle, lower = bb
        close = kline.get("close", 0)
        pctb = bollinger_pctb(close, upper, lower)

        prev_pctb = self._prev_pctb.get(code)
        self._prev_pctb[code] = pctb

        if prev_pctb is None:
            return None

        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        if self.mode == "reversion":
            # 均值回归: 触及下轨后回升 → 买, 触及上轨后回落 → 卖
            if prev_pctb <= 0 and pctb > 0:
                logger.info(f"[{code}] BB 下轨反弹: %B {prev_pctb:.2f} -> {pctb:.2f}")
                return Signal(
                    code=code, name=kline.get("name", code),
                    action=SignalAction.BUY, price=close, quantity=qty,
                    reason=f"BB({self.period},{self.std_dev}) 触及下轨反弹, 价格={close:.2f}, 下轨={lower:.2f}, %B={pctb:.2f}",
                    strategy=self.name,
                    extra={"upper": round(upper, 2), "middle": round(middle, 2), "lower": round(lower, 2), "pctb": round(pctb, 2)},
                )
            if prev_pctb >= 1 and pctb < 1:
                logger.info(f"[{code}] BB 上轨回落: %B {prev_pctb:.2f} -> {pctb:.2f}")
                return Signal(
                    code=code, name=kline.get("name", code),
                    action=SignalAction.SELL, price=close, quantity=qty,
                    reason=f"BB({self.period},{self.std_dev}) 触及上轨回落, 价格={close:.2f}, 上轨={upper:.2f}, %B={pctb:.2f}",
                    strategy=self.name,
                    extra={"upper": round(upper, 2), "middle": round(middle, 2), "lower": round(lower, 2), "pctb": round(pctb, 2)},
                )

        elif self.mode == "breakout":
            # 突破模式: 突破上轨 → 追涨买入, 跌破下轨 → 追跌卖出
            if prev_pctb < 1 and pctb >= 1:
                logger.info(f"[{code}] BB 突破上轨: %B {prev_pctb:.2f} -> {pctb:.2f}")
                return Signal(
                    code=code, name=kline.get("name", code),
                    action=SignalAction.BUY, price=close, quantity=qty,
                    reason=f"BB({self.period},{self.std_dev}) 突破上轨, 价格={close:.2f} > 上轨={upper:.2f}",
                    strategy=self.name,
                    extra={"upper": round(upper, 2), "middle": round(middle, 2), "lower": round(lower, 2), "pctb": round(pctb, 2)},
                )
            if prev_pctb > 0 and pctb <= 0:
                logger.info(f"[{code}] BB 跌破下轨: %B {prev_pctb:.2f} -> {pctb:.2f}")
                return Signal(
                    code=code, name=kline.get("name", code),
                    action=SignalAction.SELL, price=close, quantity=qty,
                    reason=f"BB({self.period},{self.std_dev}) 跌破下轨, 价格={close:.2f} < 下轨={lower:.2f}",
                    strategy=self.name,
                    extra={"upper": round(upper, 2), "middle": round(middle, 2), "lower": round(lower, 2), "pctb": round(pctb, 2)},
                )

        return None
