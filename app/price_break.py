"""
价格阈值突破策略
当股价突破预设的上限或下限时产生信号
"""
import logging
from typing import Optional
from .base import BaseStrategy, Signal, SignalAction
from app import config

logger = logging.getLogger("ftseek.strategy.price_break")
logger.propagate = False


class PriceBreakStrategy(BaseStrategy):
    name = "price_break"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("price_break", {}))
        self.thresholds = self.config.get("thresholds", {})
        # 记录已触发状态，避免重复提醒
        self._triggered = {}  # code -> {"upper": bool, "lower": bool}

    def _get_qty(self, code: str) -> int:
        """根据市场返回合适的下单数量"""
        if code.startswith("US."):
            return config.DEFAULT_ORDER_QTY_US
        return config.DEFAULT_ORDER_QTY_HK

    def _get_currency(self, code: str) -> str:
        return "$" if code.startswith("US.") else "HK$"

    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        if not self.enabled:
            return None

        threshold = self.thresholds.get(code)
        if not threshold:
            return None

        last_price = quote.get("last_price", 0)
        if last_price <= 0:
            return None

        triggered = self._triggered.setdefault(code, {"upper": False, "lower": False})

        upper = threshold.get("upper")
        lower = threshold.get("lower")
        currency = self._get_currency(code)
        qty = self._get_qty(code)

        # 突破上限 -> 考虑卖出 / 追涨
        if upper and last_price >= upper and not triggered["upper"]:
            triggered["upper"] = True
            triggered["lower"] = False  # 重置下限触发
            logger.info(f"[{code}] 突破上限 {upper}, 当前价 {last_price}")
            return Signal(
                code=code,
                name=quote.get("name", code),
                action=SignalAction.BUY,
                price=last_price,
                quantity=qty,
                reason=f"价格 {currency}{last_price} 突破上限 {currency}{upper}",
                strategy=self.name,
                expire_at=0,
            )

        # 跌破下限 -> 考虑买入 / 止损
        if lower and last_price <= lower and not triggered["lower"]:
            triggered["lower"] = True
            triggered["upper"] = False
            logger.info(f"[{code}] 跌破下限 {lower}, 当前价 {last_price}")
            return Signal(
                code=code,
                name=quote.get("name", code),
                action=SignalAction.SELL,
                price=last_price,
                quantity=qty,
                reason=f"价格 {currency}{last_price} 跌破下限 {currency}{lower}",
                strategy=self.name,
                expire_at=0,
            )

        # 回到区间内，重置触发状态
        if upper and lower and lower < last_price < upper:
            triggered["upper"] = False
            triggered["lower"] = False

        return None
