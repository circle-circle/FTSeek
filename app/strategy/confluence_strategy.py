"""
多因子融合策略 (Multi-Factor Confluence)
TradingView 风格的多指标共振策略
只在多个指标同时确认时才产生信号，大幅降低假信号

支持的因子: RSI / MACD / Bollinger / SuperTrend / MA Cross / Volume
信号模式: 至少 N 个因子同时确认才触发（可配置）
"""
import logging
from collections import defaultdict
from typing import Optional, Dict, List
from .base import BaseStrategy, Signal, SignalAction
from .indicators import (
    IndicatorBuffer, rsi, macd, bollinger_bands, bollinger_pctb,
    supertrend, sma, ema, atr, adx
)
from app import config

logger = logging.getLogger("ftseek.strategy.confluence")
logger.propagate = False  # 防止日志重复


class ConfluenceStrategy(BaseStrategy):
    name = "confluence"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("confluence", {}))
        # 最少需要几个因子同向确认
        self.min_factors = self.config.get("min_factors", 3)
        # 各因子参数
        self.rsi_period = self.config.get("rsi_period", 14)
        self.rsi_overbought = self.config.get("rsi_overbought", 70)
        self.rsi_oversold = self.config.get("rsi_oversold", 30)
        self.macd_fast = self.config.get("macd_fast", 12)
        self.macd_slow = self.config.get("macd_slow", 26)
        self.macd_signal = self.config.get("macd_signal", 9)
        self.bb_period = self.config.get("bb_period", 20)
        self.bb_std = self.config.get("bb_std", 2.0)
        self.st_period = self.config.get("st_period", 10)
        self.st_multiplier = self.config.get("st_multiplier", 3.0)
        self.ma_fast = self.config.get("ma_fast", 10)
        self.ma_slow = self.config.get("ma_slow", 30)
        self.vol_ratio = self.config.get("vol_ratio", 1.5)
        self.adx_threshold = self.config.get("adx_threshold", 25)
        # 启用的因子
        self.enabled_factors = self.config.get("enabled_factors",
            ["rsi", "macd", "bollinger", "supertrend", "ma_cross", "volume"])
        # 内部状态
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_state: Dict[str, Dict] = defaultdict(dict)

    def update_params(self, params: dict):
        for key, val in params.items():
            if hasattr(self, key):
                if key in ("min_factors", "rsi_period", "macd_fast", "macd_slow",
                           "macd_signal", "bb_period", "st_period", "ma_fast", "ma_slow"):
                    setattr(self, key, int(val))
                elif key in ("rsi_overbought", "rsi_oversold", "bb_std", "st_multiplier",
                             "vol_ratio", "adx_threshold"):
                    setattr(self, key, float(val))
                elif key == "enabled_factors":
                    setattr(self, key, val if isinstance(val, list) else val.split(","))
                elif key == "enabled":
                    self.enabled = bool(val)
        logger.info(f"Confluence 参数已更新: min_factors={self.min_factors}, factors={self.enabled_factors}")

    def get_params(self) -> dict:
        return {
            "name": self.name, "enabled": self.enabled,
            "min_factors": self.min_factors,
            "enabled_factors": self.enabled_factors,
            "rsi": {"period": self.rsi_period, "overbought": self.rsi_overbought, "oversold": self.rsi_oversold},
            "macd": {"fast": self.macd_fast, "slow": self.macd_slow, "signal": self.macd_signal},
            "bollinger": {"period": self.bb_period, "std": self.bb_std},
            "supertrend": {"period": self.st_period, "multiplier": self.st_multiplier},
            "ma_cross": {"fast": self.ma_fast, "slow": self.ma_slow},
            "volume": {"ratio": self.vol_ratio},
            "adx_threshold": self.adx_threshold,
        }

    def _evaluate_factors(self, code: str, kline: dict) -> Dict[str, int]:
        """
        评估各因子方向
        返回 {factor_name: direction}  direction: 1=看多, -1=看空, 0=中性
        """
        buf = self._buffers[code]
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        volumes = list(buf.volume)
        prev = self._prev_state[code]
        factors = {}

        # RSI
        if "rsi" in self.enabled_factors:
            rsi_val = rsi(closes, self.rsi_period)
            if rsi_val is not None:
                prev_rsi = prev.get("rsi")
                prev["rsi"] = rsi_val
                if prev_rsi is not None:
                    if prev_rsi <= self.rsi_oversold and rsi_val > self.rsi_oversold:
                        factors["rsi"] = 1
                    elif prev_rsi >= self.rsi_overbought and rsi_val < self.rsi_overbought:
                        factors["rsi"] = -1
                    elif rsi_val < self.rsi_oversold:
                        factors["rsi"] = 1  # 超卖区偏多
                    elif rsi_val > self.rsi_overbought:
                        factors["rsi"] = -1  # 超买区偏空
                    else:
                        factors["rsi"] = 0

        # MACD
        if "macd" in self.enabled_factors:
            macd_result = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
            if macd_result:
                macd_line, signal_line, histogram = macd_result
                prev_hist = prev.get("macd_hist")
                prev["macd_hist"] = histogram
                if histogram > 0:
                    factors["macd"] = 1
                elif histogram < 0:
                    factors["macd"] = -1
                else:
                    factors["macd"] = 0

        # Bollinger Bands
        if "bollinger" in self.enabled_factors:
            bb = bollinger_bands(closes, self.bb_period, self.bb_std)
            if bb:
                upper, middle, lower = bb
                close = closes[-1]
                pctb = bollinger_pctb(close, upper, lower)
                if pctb <= 0.1:
                    factors["bollinger"] = 1  # 接近下轨，看多
                elif pctb >= 0.9:
                    factors["bollinger"] = -1  # 接近上轨，看空
                elif pctb < 0.5:
                    factors["bollinger"] = 1
                else:
                    factors["bollinger"] = -1

        # SuperTrend
        if "supertrend" in self.enabled_factors and buf.size > self.st_period + 5:
            st_result = supertrend(highs, lows, closes, self.st_period, self.st_multiplier)
            if st_result:
                st_val, direction = st_result
                prev_dir = prev.get("st_dir")
                prev["st_dir"] = direction
                factors["supertrend"] = 1 if direction == "up" else -1

        # MA Cross
        if "ma_cross" in self.enabled_factors:
            fast_ma = sma(buf.close, self.ma_fast)
            slow_ma = sma(buf.close, self.ma_slow)
            if fast_ma is not None and slow_ma is not None:
                factors["ma_cross"] = 1 if fast_ma > slow_ma else -1

        # Volume
        if "volume" in self.enabled_factors and len(volumes) > 20:
            avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 20 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
            if avg_vol > 0 and volumes[-1] > avg_vol * self.vol_ratio:
                # 放量确认当前方向
                if closes[-1] > closes[-2] if len(closes) > 1 else True:
                    factors["volume"] = 1
                else:
                    factors["volume"] = -1
            else:
                factors["volume"] = 0

        return factors

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)

        if buf.size < max(self.macd_slow + self.macd_signal, self.bb_period, self.st_period + 5, self.ma_slow) + 5:
            return None

        factors = self._evaluate_factors(code, kline)
        if not factors:
            return None

        # 统计看多/看空因子数
        bullish = [k for k, v in factors.items() if v > 0]
        bearish = [k for k, v in factors.items() if v < 0]

        close = kline.get("close", 0)
        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        # ADX 过滤: 只在趋势足够强时触发
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        adx_val = adx(highs, lows, closes, 14)
        adx_filter = adx_val is None or adx_val >= self.adx_threshold

        if len(bullish) >= self.min_factors and adx_filter:
            factor_desc = ", ".join(bullish)
            logger.info(f"[{code}] 多因子共振看多: {factor_desc} ({len(bullish)}/{len(factors)})")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.BUY, price=close, quantity=qty,
                reason=f"多因子共振买入({len(bullish)}/{self.min_factors}): [{factor_desc}]" +
                       (f", ADX={adx_val:.1f}" if adx_val else ""),
                strategy=self.name,
                extra={"factors": factors, "bullish": bullish, "adx": round(adx_val, 1) if adx_val else None},
            )

        if len(bearish) >= self.min_factors and adx_filter:
            factor_desc = ", ".join(bearish)
            logger.info(f"[{code}] 多因子共振看空: {factor_desc} ({len(bearish)}/{len(factors)})")
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.SELL, price=close, quantity=qty,
                reason=f"多因子共振卖出({len(bearish)}/{self.min_factors}): [{factor_desc}]" +
                       (f", ADX={adx_val:.1f}" if adx_val else ""),
                strategy=self.name,
                extra={"factors": factors, "bearish": bearish, "adx": round(adx_val, 1) if adx_val else None},
            )

        return None
