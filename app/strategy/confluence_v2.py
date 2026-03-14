"""
多因子融合策略 (Multi-Factor Confluence) v2.0 - 市场状态增强版

在原有基础上增加:
- 市场状态感知 (Market Regime Awareness)
- 动态因子权重 (Dynamic Factor Weights)
- 波动率调整 (Volatility Adjustment)

支持的因子：RSI / MACD / Bollinger / SuperTrend / MA Cross / Volume
信号模式：至少 N 个因子同时确认才触发（可配置）
"""
import logging
from collections import defaultdict
from typing import Optional, Dict, List
from .base import BaseStrategy, Signal, SignalAction
from .indicators import (
    IndicatorBuffer, rsi, macd, bollinger_bands, bollinger_pctb,
    supertrend, sma, ema, atr, adx
)
from .market_regime import market_regime_detector, MarketRegimeType
from app import config

logger = logging.getLogger("ftseek.strategy.confluence")


class ConfluenceStrategyV2(BaseStrategy):
    """多因子融合策略 v2.0 - 市场状态增强版"""
    name = "confluence_v2"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("confluence", {}))
        
        # 基础参数
        self.min_factors = self.config.get("min_factors", 3)
        self.use_market_regime = self.config.get("use_market_regime", True)
        self.use_dynamic_weights = self.config.get("use_dynamic_weights", True)
        
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
        
        # 市场状态相关的动态权重
        self.regime_weights = {
            MarketRegimeType.STRONG_TREND_UP: {
                "supertrend": 1.5, "macd": 1.3, "rsi": 0.7, "bollinger": 0.8,
            },
            MarketRegimeType.STRONG_TREND_DOWN: {
                "supertrend": 1.5, "macd": 1.3, "rsi": 0.7, "bollinger": 0.8,
            },
            MarketRegimeType.RANGE_BOUND: {
                "rsi": 1.5, "bollinger": 1.5, "supertrend": 0.6, "macd": 0.7,
            },
            MarketRegimeType.HIGH_VOLATILITY: {
                "bollinger": 1.3, "supertrend": 0.8, "rsi": 0.9,
            },
            MarketRegimeType.LOW_VOLATILITY: {
                "bollinger": 1.2,  # 布林带收窄可能突破
            },
        }
        
        # 内部状态
        self._buffers = defaultdict(lambda: IndicatorBuffer())
        self._prev_state: Dict[str, Dict] = defaultdict(dict)
        
        logger.info(
            f"Confluence V2 已初始化 | 市场状态={self.use_market_regime} | "
            f"动态权重={self.use_dynamic_weights}"
        )

    def update_params(self, params: dict):
        """动态更新参数"""
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
                elif key in ("use_market_regime", "use_dynamic_weights"):
                    setattr(self, key, bool(val))
        logger.info(f"Confluence V2 参数已更新")

    def get_params(self) -> dict:
        return {
            "name": self.name, "enabled": self.enabled,
            "min_factors": self.min_factors,
            "use_market_regime": self.use_market_regime,
            "use_dynamic_weights": self.use_dynamic_weights,
            "enabled_factors": self.enabled_factors,
        }

    def _get_market_regime(self, code: str):
        """获取当前市场状态"""
        if not self.use_market_regime:
            return None
        
        try:
            regime = market_regime_detector.detect(code)
            return regime
        except Exception as e:
            logger.debug(f"获取市场状态失败：{e}")
            return None

    def _get_factor_weight(self, factor: str, regime) -> float:
        """获取因子权重 (考虑市场状态)"""
        base_weight = 1.0
        
        if not self.use_dynamic_weights or regime is None:
            return base_weight
        
        # 根据市场状态获取权重
        regime_weight_map = self.regime_weights.get(regime.regime_type, {})
        return regime_weight_map.get(factor, base_weight)

    def _evaluate_factors(self, code: str, kline: dict, regime) -> Dict[str, Dict]:
        """
        评估各因子方向和权重
        
        返回 {factor_name: {"direction": 1/-1/0, "weight": float, "reason": str}}
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
                direction = 0
                reason = ""
                
                if prev_rsi is not None:
                    if prev_rsi <= self.rsi_oversold and rsi_val > self.rsi_oversold:
                        direction = 1
                        reason = f"RSI 超卖反转 ({prev_rsi:.1f}->{rsi_val:.1f})"
                    elif prev_rsi >= self.rsi_overbought and rsi_val < self.rsi_overbought:
                        direction = -1
                        reason = f"RSI 超买回落 ({prev_rsi:.1f}->{rsi_val:.1f})"
                    elif rsi_val < self.rsi_oversold:
                        direction = 1
                        reason = f"RSI 超卖区 ({rsi_val:.1f})"
                    elif rsi_val > self.rsi_overbought:
                        direction = -1
                        reason = f"RSI 超买区 ({rsi_val:.1f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("rsi", regime)
                    factors["rsi"] = {"direction": direction, "weight": weight, "reason": reason}

        # MACD
        if "macd" in self.enabled_factors:
            macd_result = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
            if macd_result:
                macd_line, signal_line, histogram = macd_result
                prev_hist = prev.get("macd_hist")
                prev["macd_hist"] = histogram
                
                direction = 0
                reason = ""
                
                if histogram > 0:
                    direction = 1
                    reason = f"MACD 金叉 (hist={histogram:.4f})"
                elif histogram < 0:
                    direction = -1
                    reason = f"MACD 死叉 (hist={histogram:.4f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("macd", regime)
                    factors["macd"] = {"direction": direction, "weight": weight, "reason": reason}

        # Bollinger Bands
        if "bollinger" in self.enabled_factors:
            bb = bollinger_bands(closes, self.bb_period, self.bb_std)
            if bb:
                upper, middle, lower = bb
                close = closes[-1]
                pctb = bollinger_pctb(close, upper, lower)
                
                direction = 0
                reason = ""
                
                if pctb <= 0.1:
                    direction = 1
                    reason = f"布林带下轨 (%B={pctb:.2f})"
                elif pctb >= 0.9:
                    direction = -1
                    reason = f"布林带上轨 (%B={pctb:.2f})"
                
                if direction != 0:
                    weight = self._get_factor_weight("bollinger", regime)
                    factors["bollinger"] = {"direction": direction, "weight": weight, "reason": reason}

        # SuperTrend
        if "supertrend" in self.enabled_factors and buf.size > self.st_period + 5:
            st_result = supertrend(highs, lows, closes, self.st_period, self.st_multiplier)
            if st_result:
                st_val, direction_str = st_result
                direction = 1 if direction_str == "up" else -1
                reason = f"SuperTrend {direction_str}"
                
                weight = self._get_factor_weight("supertrend", regime)
                factors["supertrend"] = {"direction": direction, "weight": weight, "reason": reason}

        # MA Cross
        if "ma_cross" in self.enabled_factors:
            fast_ma = sma(buf.close, self.ma_fast)
            slow_ma = sma(buf.close, self.ma_slow)
            if fast_ma is not None and slow_ma is not None:
                direction = 1 if fast_ma > slow_ma else -1
                reason = f"MA 交叉 (快={fast_ma:.2f}, 慢={slow_ma:.2f})"
                
                weight = self._get_factor_weight("ma_cross", regime)
                factors["ma_cross"] = {"direction": direction, "weight": weight, "reason": reason}

        # Volume
        if "volume" in self.enabled_factors and len(volumes) > 20:
            avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 20 else sum(volumes[:-1]) / max(len(volumes) - 1, 1)
            if avg_vol > 0 and volumes[-1] > avg_vol * self.vol_ratio:
                # 放量确认当前方向
                if closes[-1] > closes[-2] if len(closes) > 1 else True:
                    direction = 1
                    reason = f"放量上涨 (vol={volumes[-1]/avg_vol:.2f}x)"
                else:
                    direction = -1
                    reason = f"放量下跌 (vol={volumes[-1]/avg_vol:.2f}x)"
                
                weight = self._get_factor_weight("volume", regime)
                factors["volume"] = {"direction": direction, "weight": weight, "reason": reason}

        return factors

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        if not self.enabled:
            return None

        buf = self._buffers[code]
        buf.push_kline(kline)

        # 检查数据是否足够
        min_periods = max(
            self.macd_slow + self.macd_signal,
            self.bb_period,
            self.st_period + 5,
            self.ma_slow,
        ) + 5
        
        if buf.size < min_periods:
            return None

        # 获取市场状态
        regime = self._get_market_regime(code)
        
        # 评估因子
        factors = self._evaluate_factors(code, kline, regime)
        if not factors:
            return None

        # 计算加权得分
        bullish_score = sum(
            f["weight"] for f in factors.values() if f["direction"] > 0
        )
        bearish_score = sum(
            f["weight"] for f in factors.values() if f["direction"] < 0
        )

        close = kline.get("close", 0)
        qty = config.DEFAULT_ORDER_QTY_US if code.startswith("US.") else config.DEFAULT_ORDER_QTY_HK

        # ADX 过滤
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        adx_val = adx(highs, lows, closes, 14)
        
        # 根据市场状态调整最小因子数
        effective_min_factors = self.min_factors
        if regime and self.use_market_regime:
            if regime.regime_type == MarketRegimeType.RANGE_BOUND:
                effective_min_factors = max(2, self.min_factors - 1)  # 震荡市降低要求
            elif regime.regime_type in (MarketRegimeType.STRONG_TREND_UP, MarketRegimeType.STRONG_TREND_DOWN):
                effective_min_factors = self.min_factors  # 趋势市保持标准
        
        # 生成信号
        if bullish_score >= effective_min_factors and (adx_val is None or adx_val >= self.adx_threshold):
            bullish_factors = [k for k, v in factors.items() if v["direction"] > 0]
            factor_details = "\n".join(
                f"  - {k}: {v['reason']} (权重{v['weight']:.1f})"
                for k, v in factors.items() if v["direction"] > 0
            )
            
            logger.info(
                f"[{code}] 多因子共振看多 | 得分={bullish_score:.1f} | "
                f"市场={regime.regime_type.value if regime else 'N/A'}"
            )
            
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.BUY, price=close, quantity=qty,
                reason=f"多因子共振买入 (得分={bullish_score:.1f}):\n{factor_details}",
                strategy=self.name,
                extra={
                    "factors": factors,
                    "bullish_score": bullish_score,
                    "bearish_score": bearish_score,
                    "adx": round(adx_val, 1) if adx_val else None,
                    "market_regime": regime.regime_type.value if regime else None,
                },
            )

        if bearish_score >= effective_min_factors and (adx_val is None or adx_val >= self.adx_threshold):
            bearish_factors = [k for k, v in factors.items() if v["direction"] < 0]
            factor_details = "\n".join(
                f"  - {k}: {v['reason']} (权重{v['weight']:.1f})"
                for k, v in factors.items() if v["direction"] < 0
            )
            
            logger.info(
                f"[{code}] 多因子共振看空 | 得分={bearish_score:.1f} | "
                f"市场={regime.regime_type.value if regime else 'N/A'}"
            )
            
            return Signal(
                code=code, name=kline.get("name", code),
                action=SignalAction.SELL, price=close, quantity=qty,
                reason=f"多因子共振卖出 (得分={bearish_score:.1f}):\n{factor_details}",
                strategy=self.name,
                extra={
                    "factors": factors,
                    "bullish_score": bullish_score,
                    "bearish_score": bearish_score,
                    "adx": round(adx_val, 1) if adx_val else None,
                    "market_regime": regime.regime_type.value if regime else None,
                },
            )

        return None
