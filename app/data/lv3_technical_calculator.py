"""
富途 LV3 技术指标计算器

从 Futu LV3 K 线数据计算常用技术指标 (纯 pandas/numpy 实现, 无外部依赖)。

支持的指标:
- MA (移动平均线): MA5, MA10, MA20, MA50, MA200
- RSI (相对强弱指标): RSI-14
- MACD (指数平滑异同平均线): MACD, Signal, Histogram
- ATR (平均真实波幅): ATR-14
- VWAP (成交量加权平均价格)
- BOLL (布林带): Upper (bb_upper), Middle, Lower (bb_lower)
- KDJ (随机指标): K, D, J
- 成交量指标: Volume MA5, Volume MA20
- 趋势判断: 基于均线排列

用法:
    calculator = LV3TechnicalCalculator()
    indicators = calculator.calculate_from_klines(klines)
"""

import logging
from typing import Dict, Any, Optional, List
import numpy as np
import pandas as pd

logger = logging.getLogger("ftseek_us.lv3_technical")


class LV3TechnicalCalculator:
    """从 LV3 K 线数据计算技术指标"""

    def __init__(self):
        self._cache: Dict[str, Dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = 60  # 技术指标缓存 60 秒

    def calculate_from_klines(self, klines: List[Dict]) -> Optional[Dict[str, Any]]:
        """
        从 K 线数据计算完整技术指标集

        Args:
            klines: K 线数据列表, 每条包含:
                {
                    "time": str,
                    "open": float,
                    "high": float,
                    "low": float,
                    "close": float,
                    "volume": float,
                }

        Returns:
            技术指标字典或 None
        """
        if not klines or len(klines) < 30:
            logger.warning(f"[LV3技术指标] K 线数据不足 ({len(klines) if klines else 0} 条), 需要至少 30 条")
            return None

        try:
            df = self._klines_to_dataframe(klines)
            if df is None or len(df) < 30:
                return None

            result = {}

            # 最新价格
            result["price"] = float(df["close"].iloc[-1])
            result["open"] = float(df["open"].iloc[-1])
            result["high"] = float(df["high"].iloc[-1])
            result["low"] = float(df["low"].iloc[-1])
            result["volume"] = float(df["volume"].iloc[-1])

            # 计算各项指标
            self._calc_ma(df, result)
            self._calc_rsi(df, result)
            self._calc_macd(df, result)
            self._calc_atr(df, result)
            self._calc_vwap(df, result)
            self._calc_boll(df, result)
            self._calc_supertrend(df, result)
            self._calc_kdj(df, result)
            self._calc_volume_ma(df, result)
            self._calc_trend(df, result)
            self._calc_volatility(df, result)
            self._calc_change_percent(df, result)

            return result

        except Exception as e:
            logger.error(f"[LV3技术指标] 计算异常: {e}", exc_info=True)
            return None

    def _klines_to_dataframe(self, klines: List[Dict]) -> Optional[pd.DataFrame]:
        """将 K 线列表转换为 DataFrame"""
        try:
            df = pd.DataFrame(klines)
            # 统一时间字段名 (Push 缓存用 time_key, HTTP 缓存用 time)
            if "time" not in df.columns and "time_key" in df.columns:
                df = df.rename(columns={"time_key": "time"})
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df.sort_values("time").reset_index(drop=True)
            return df if len(df) >= 30 else None
        except Exception as e:
            logger.error(f"[LV3技术指标] DataFrame 转换失败: {e}")
            return None

    def _calc_ma(self, df: pd.DataFrame, result: Dict):
        """计算移动平均线"""
        for period in [5, 10, 20, 50]:
            if len(df) >= period:
                ma = df["close"].rolling(window=period).mean().iloc[-1]
                result[f"ma{period}"] = round(float(ma), 2)

    def _calc_rsi(self, df: pd.DataFrame, result: Dict, period: int = 14):
        """计算 RSI (相对强弱指标)"""
        if len(df) < period + 1:
            result["rsi"] = 50.0
            return

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=period).mean().iloc[-1]
        avg_loss = loss.rolling(window=period).mean().iloc[-1]

        if avg_loss == 0:
            result["rsi"] = 100.0
        else:
            rs = avg_gain / avg_loss
            result["rsi"] = round(float(100 - (100 / (1 + rs))), 2)

    def _calc_macd(self, df: pd.DataFrame, result: Dict,
                   fast: int = 12, slow: int = 26, signal: int = 9):
        """计算 MACD"""
        if len(df) < slow + signal:
            result["macd"] = 0.0
            result["macd_signal"] = 0.0
            result["macd_hist"] = 0.0
            return

        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()

        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        result["macd"] = round(float(macd_line.iloc[-1]), 4)
        result["macd_signal"] = round(float(signal_line.iloc[-1]), 4)
        result["macd_hist"] = round(float(histogram.iloc[-1]), 4)

    def _calc_atr(self, df: pd.DataFrame, result: Dict, period: int = 14):
        """计算 ATR (平均真实波幅)"""
        if len(df) < period + 1:
            result["atr"] = 0.0
            result["atr_percent"] = 0.0
            return

        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean().iloc[-1]

        price = result.get("price", 0)
        result["atr"] = round(float(atr), 2)
        result["atr_percent"] = round(float(atr / price * 100), 2) if price > 0 else 0.0

    def _calc_vwap(self, df: pd.DataFrame, result: Dict):
        """
        计算 VWAP (成交量加权平均价格)
        使用当日所有 K 线数据: VWAP = cum(typical_price × volume) / cum(volume)
        typical_price = (high + low + close) / 3
        """
        if len(df) < 2:
            result["vwap"] = result.get("price", 0)
            return

        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_tp_vol = (tp * df["volume"]).cumsum()
        cum_vol = df["volume"].cumsum()

        if cum_vol.iloc[-1] > 0:
            result["vwap"] = round(float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1]), 2)
        else:
            result["vwap"] = result.get("price", 0)

    def _calc_boll(self, df: pd.DataFrame, result: Dict, period: int = 20, std_dev: int = 2):
        """计算布林带"""
        if len(df) < period:
            return

        middle = df["close"].rolling(window=period).mean().iloc[-1]
        std = df["close"].rolling(window=period).std().iloc[-1]

        result["bb_upper"] = round(float(middle + std_dev * std), 2)
        result["bb_middle"] = round(float(middle), 2)
        result["bb_lower"] = round(float(middle - std_dev * std), 2)
        # 兼容旧字段名 (boll_*)
        result["boll_upper"] = result["bb_upper"]
        result["boll_middle"] = result["bb_middle"]
        result["boll_lower"] = result["bb_lower"]

    def _calc_supertrend(self, df: pd.DataFrame, result: Dict, period: int = 10, multiplier: float = 3.0):
        """计算 SuperTrend 指标

        SuperTrend = (High + Low) / 2 ± multiplier × ATR
        趋势方向: 收盘价 > SuperTrend 下轨 → UP, 收盘价 < SuperTrend 上轨 → DOWN
        """
        if len(df) < period + 1:
            result["supertrend_direction"] = ""
            result["supertrend_value"] = 0.0
            return

        high = df["high"]
        low = df["low"]
        close = df["close"]

        # ATR (EMA 版本)
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=period, adjust=False).mean()

        hl2 = (high + low) / 2.0
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        # 逐 bar 计算 SuperTrend
        n = len(df)
        supertrend = pd.Series(0.0, index=df.index)
        direction = pd.Series("", index=df.index)

        for i in range(1, n):
            prev_st = supertrend.iloc[i - 1]
            prev_dir = direction.iloc[i - 1]

            if prev_dir == "":
                # 初始化: 第一个有效 bar 默认从 upper band 开始
                if close.iloc[i] > upper_band.iloc[i]:
                    supertrend.iloc[i] = lower_band.iloc[i]
                    direction.iloc[i] = "UP"
                else:
                    supertrend.iloc[i] = upper_band.iloc[i]
                    direction.iloc[i] = "DOWN"
            elif prev_dir == "UP":
                if close.iloc[i] > supertrend.iloc[i - 1]:
                    # 维持 UP: supertrend 取 max(当前 lower_band, 前值)
                    supertrend.iloc[i] = max(lower_band.iloc[i], prev_st)
                    direction.iloc[i] = "UP"
                else:
                    # 翻转为 DOWN
                    supertrend.iloc[i] = upper_band.iloc[i]
                    direction.iloc[i] = "DOWN"
            else:  # DOWN
                if close.iloc[i] < supertrend.iloc[i - 1]:
                    # 维持 DOWN: supertrend 取 min(当前 upper_band, 前值)
                    supertrend.iloc[i] = min(upper_band.iloc[i], prev_st)
                    direction.iloc[i] = "DOWN"
                else:
                    # 翻转为 UP
                    supertrend.iloc[i] = lower_band.iloc[i]
                    direction.iloc[i] = "UP"

        result["supertrend_direction"] = str(direction.iloc[-1])
        result["supertrend_value"] = round(float(supertrend.iloc[-1]), 2)

    def _calc_kdj(self, df: pd.DataFrame, result: Dict, period: int = 9):
        """计算 KDJ (随机指标)"""
        if len(df) < period:
            result["kdj_k"] = 50.0
            result["kdj_d"] = 50.0
            result["kdj_j"] = 50.0
            return

        low_min = df["low"].rolling(window=period).min()
        high_max = df["high"].rolling(window=period).max()

        rsv = (df["close"] - low_min) / (high_max - low_min) * 100

        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        j = 3 * k - 2 * d

        result["kdj_k"] = round(float(k.iloc[-1]), 2)
        result["kdj_d"] = round(float(d.iloc[-1]), 2)
        result["kdj_j"] = round(float(j.iloc[-1]), 2)

    def _calc_volume_ma(self, df: pd.DataFrame, result: Dict):
        """计算成交量均线"""
        for period in [5, 20]:
            if len(df) >= period:
                vol_ma = df["volume"].rolling(window=period).mean().iloc[-1]
                result[f"vol_ma{period}"] = round(float(vol_ma), 0)

        # 量比 (当日成交量 / 5 日均量)
        if "vol_ma5" in result and result["vol_ma5"] > 0:
            result["volume_ratio"] = round(result["volume"] / result["vol_ma5"], 2)
        else:
            result["volume_ratio"] = 1.0

    def _calc_trend(self, df: pd.DataFrame, result: Dict):
        """判断趋势方向"""
        ma5 = result.get("ma5", 0)
        ma10 = result.get("ma10", 0)
        ma20 = result.get("ma20", 0)
        ma50 = result.get("ma50", 0)
        price = result.get("price", 0)

        # 多头排列: price > MA5 > MA10 > MA20
        if price > ma5 > ma10 > ma20 > 0:
            result["trend"] = "UP"
            result["trend_strength"] = "STRONG"
        elif ma5 > ma10 > ma20 > 0 and price > ma20:
            result["trend"] = "UP"
            result["trend_strength"] = "MODERATE"
        elif price < ma5 < ma10 < ma20 and ma20 > 0:
            result["trend"] = "DOWN"
            result["trend_strength"] = "STRONG"
        elif ma5 < ma10 < ma20 and price < ma20 and ma20 > 0:
            result["trend"] = "DOWN"
            result["trend_strength"] = "MODERATE"
        else:
            result["trend"] = "NEUTRAL"
            result["trend_strength"] = "WEAK"

        # MACD 趋势确认
        macd = result.get("macd", 0)
        macd_hist = result.get("macd_hist", 0)
        if macd > 0 and macd_hist > 0:
            result["macd_trend"] = "BULLISH"
        elif macd < 0 and macd_hist < 0:
            result["macd_trend"] = "BEARISH"
        else:
            result["macd_trend"] = "NEUTRAL"

    def _calc_volatility(self, df: pd.DataFrame, result: Dict):
        """计算波动率"""
        if len(df) < 20:
            result["volatility"] = 0.02
            result["avg_volatility"] = 0.02
            return

        returns = df["close"].pct_change().dropna()
        recent_vol = returns.tail(20).std()
        avg_vol = returns.std()

        result["volatility"] = round(float(recent_vol), 4)
        result["avg_volatility"] = round(float(avg_vol), 4)

    def _calc_change_percent(self, df: pd.DataFrame, result: Dict):
        """计算涨跌幅"""
        if len(df) < 2:
            result["change_percent"] = 0.0
            return

        prev_close = df["close"].iloc[-2]
        curr_price = result.get("price", 0)

        if prev_close > 0:
            result["change_percent"] = round((curr_price - prev_close) / prev_close * 100, 2)
        else:
            result["change_percent"] = 0.0

        # 计算多周期涨跌幅
        for period in [5, 10, 20]:
            if len(df) > period:
                old_price = df["close"].iloc[-period - 1]
                if old_price > 0:
                    result[f"change_{period}d_pct"] = round((curr_price - old_price) / old_price * 100, 2)
