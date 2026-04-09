"""
SuperTrend 策略
基于 ATR 的自适应趋势跟踪策略，TradingView 上最受欢迎的趋势指标之一
方向翻转时产生信号

优化于 2026-03-27:
- 添加市场状态感知 (MarketAwareMixin)
- 震荡市中自动调整乘数，降低假信号
- 强趋势中避免逆势信号
"""
import logging
from collections import defaultdict
from typing import Optional, Tuple
from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, supertrend
from .market_aware import MarketAwareMixin
from app import config

logger = logging.getLogger("ftseek.strategy.supertrend")
logger.propagate = False  # 防止日志重复


class SuperTrendStrategy(BaseStrategy, MarketAwareMixin):
    """
    SuperTrend 趋势跟踪策略
    
    特点:
    - 基于ATR的自适应止损线
    - 趋势市表现优秀，震荡市假信号多
    - 集成市场状态感知，自动调整参数
    """
    name = "supertrend"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("supertrend", {}))
        self.atr_period = self.config.get("atr_period", 10)
        
        # 基准乘数 (会根据市场状态动态调整)
        self.base_multiplier = self.config.get("multiplier", 2.5)
        self.multiplier = self.base_multiplier
        
        # 分离做多/做空参数
        self.long_multiplier = self.config.get("long_multiplier", 3.0)
        self.short_multiplier = self.config.get("short_multiplier", 2.5)
        
        # 震荡市调整系数
        self.range_adjust_factor = self.config.get("range_adjust_factor", 1.2)
        
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_direction = {}  # code -> "up" | "down"
        self._regime_warning_logged = set()  # 避免重复日志

    def update_params(self, params: dict):
        if "atr_period" in params:
            self.atr_period = int(params["atr_period"])
        if "multiplier" in params:
            self.base_multiplier = float(params["multiplier"])
            self.multiplier = self.base_multiplier
        if "range_adjust_factor" in params:
            self.range_adjust_factor = float(params["range_adjust_factor"])
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        logger.info(f"SuperTrend 参数已更新: ATR period={self.atr_period}, multiplier={self.base_multiplier}")

    def get_params(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "atr_period": self.atr_period,
            "base_multiplier": self.base_multiplier,
            "range_adjust_factor": self.range_adjust_factor,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)

        if buf.size < self.atr_period + 5:
            return None

        # === 市场状态感知 ===
        regime = self.get_market_regime(code)
        
        # 根据市场状态调整乘数
        if regime:
            from .market_regime import MarketRegimeType
            
            # 震荡市: 增大乘数，降低灵敏度
            if regime.regime_type == MarketRegimeType.RANGE_BOUND:
                self.multiplier = self.base_multiplier * self.range_adjust_factor
                if code not in self._regime_warning_logged:
                    logger.info(f"[{code}] 震荡市检测，SuperTrend 乘数调整: {self.base_multiplier:.1f} -> {self.multiplier:.1f}")
                    self._regime_warning_logged.add(code)
            else:
                self.multiplier = self.base_multiplier
                self._regime_warning_logged.discard(code)
        else:
            self.multiplier = self.base_multiplier

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

        # === 信号生成 (带市场状态过滤) ===
        
        # 方向从 down → up: 买入
        if prev_dir == "down" and direction == "up":
            # 强下跌趋势中避免做多
            if regime:
                from .market_regime import TrendDirection
                if regime.trend_direction == TrendDirection.STRONG_DOWN:
                    logger.info(f"[{code}] ⚠️ 强下跌趋势中忽略 SuperTrend 翻多信号")
                    return None
            
            logger.info(f"[{code}] SuperTrend 翻多: ST={st_value:.2f}, price={close:.2f}")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.BUY, price=close, quantity=qty,
                reason=f"SuperTrend({self.atr_period},{self.multiplier:.1f}) 翻多, ST={st_value:.2f}, 价格={close:.2f}",
                strategy=self.name,
                extra={
                    "supertrend": round(st_value, 2),
                    "direction": direction,
                    "market_regime": regime.regime_type.value if regime else None,
                    "adjusted_multiplier": round(self.multiplier, 1),
                },
            )

        # 方向从 up → down: 卖出
        if prev_dir == "up" and direction == "down":
            # 强上涨趋势中避免做空
            if regime:
                from .market_regime import TrendDirection
                if regime.trend_direction == TrendDirection.STRONG_UP:
                    logger.info(f"[{code}] ⚠️ 强上涨趋势中忽略 SuperTrend 翻空信号")
                    return None
            
            logger.info(f"[{code}] SuperTrend 翻空: ST={st_value:.2f}, price={close:.2f}")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.SELL, price=close, quantity=qty,
                reason=f"SuperTrend({self.atr_period},{self.multiplier:.1f}) 翻空, ST={st_value:.2f}, 价格={close:.2f}",
                strategy=self.name,
                extra={
                    "supertrend": round(st_value, 2),
                    "direction": direction,
                    "market_regime": regime.regime_type.value if regime else None,
                    "adjusted_multiplier": round(self.multiplier, 1),
                },
            )

        return None