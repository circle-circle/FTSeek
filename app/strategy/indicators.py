"""
技术指标计算库
提供 TradingView 风格的技术指标计算，供所有策略模块复用
"""
import math
import threading
from collections import deque
from typing import List, Optional, Tuple


class IndicatorBuffer:
    """滑动窗口指标缓冲区，自动管理历史数据 (线程安全: 2026-04-19)"""

    def __init__(self, maxlen: int = 500):
        self.close = deque(maxlen=maxlen)
        self.high = deque(maxlen=maxlen)
        self.low = deque(maxlen=maxlen)
        self.volume = deque(maxlen=maxlen)
        self.open = deque(maxlen=maxlen)
        self._lock = threading.Lock()  # P1 2026-04-19: 线程安全锁

    def push(self, o: float, h: float, l: float, c: float, v: float = 0):
        with self._lock:
            self.open.append(o)
            self.high.append(h)
            self.low.append(l)
            self.close.append(c)
            self.volume.append(v)

    def push_kline(self, kline: dict):
        self.push(
            o=kline.get("open", kline.get("close", 0)),
            h=kline.get("high", kline.get("close", 0)),
            l=kline.get("low", kline.get("close", 0)),
            c=kline.get("close", 0),
            v=kline.get("volume", 0),
        )

    def snapshot(self) -> dict:
        """线程安全地获取当前缓冲区快照"""
        with self._lock:
            return {
                "open": list(self.open),
                "high": list(self.high),
                "low": list(self.low),
                "close": list(self.close),
                "volume": list(self.volume),
            }

    @property
    def size(self) -> int:
        with self._lock:
            return len(self.close)


# ============================================================
#  基础计算
# ============================================================
def sma(data, period: int) -> Optional[float]:
    """Simple Moving Average"""
    if len(data) < period:
        return None
    return sum(list(data)[-period:]) / period


def ema(data, period: int, prev_ema: Optional[float] = None) -> Optional[float]:
    """Exponential Moving Average (递推)"""
    if len(data) < 1:
        return None
    if prev_ema is None:
        if len(data) < period:
            return None
        return sum(list(data)[-period:]) / period
    k = 2.0 / (period + 1)
    return data[-1] * k + prev_ema * (1 - k)


def ema_series(data_list: list, period: int) -> list:
    """计算完整 EMA 序列"""
    if len(data_list) < period:
        return []
    result = [sum(data_list[:period]) / period]
    k = 2.0 / (period + 1)
    for i in range(period, len(data_list)):
        result.append(data_list[i] * k + result[-1] * (1 - k))
    return result


# ============================================================
#  RSI (Relative Strength Index)
# ============================================================
def rsi(closes: list, period: int = 14) -> Optional[float]:
    """
    RSI 计算 (Wilder's smoothing)
    返回 0-100 之间的值
    """
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    if len(gains) < period:
        return None

    # 初始平均
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ============================================================
#  MACD (Moving Average Convergence Divergence)
# ============================================================
def macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Optional[Tuple[float, float, float]]:
    """
    MACD 计算
    返回 (macd_line, signal_line, histogram)
    """
    if len(closes) < slow + signal:
        return None

    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)

    # 对齐长度
    offset = slow - fast
    if offset < 0 or len(fast_ema) <= offset:
        return None

    macd_line_series = []
    for i in range(len(slow_ema)):
        macd_line_series.append(fast_ema[i + offset] - slow_ema[i])

    if len(macd_line_series) < signal:
        return None

    signal_ema = ema_series(macd_line_series, signal)
    if not signal_ema:
        return None

    macd_val = macd_line_series[-1]
    signal_val = signal_ema[-1]
    hist_val = macd_val - signal_val

    return (macd_val, signal_val, hist_val)


# ============================================================
#  Bollinger Bands
# ============================================================
def bollinger_bands(closes: list, period: int = 20, std_dev: float = 2.0
                    ) -> Optional[Tuple[float, float, float]]:
    """
    布林带计算
    返回 (upper, middle, lower)
    """
    if len(closes) < period:
        return None

    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)

    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return (upper, middle, lower)


def bollinger_bandwidth(upper: float, lower: float, middle: float) -> float:
    """布林带宽度 (衡量波动性)"""
    if middle == 0:
        return 0
    return (upper - lower) / middle


def bollinger_pctb(close: float, upper: float, lower: float) -> float:
    """布林带 %B (价格在带内的相对位置, 0=下轨, 1=上轨)"""
    if upper == lower:
        return 0.5
    return (close - lower) / (upper - lower)


# ============================================================
#  SuperTrend
# ============================================================
def atr(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """Average True Range"""
    if len(closes) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    # Wilder's smoothing
    atr_val = sum(true_ranges[:period]) / period
    for i in range(period, len(true_ranges)):
        atr_val = (atr_val * (period - 1) + true_ranges[i]) / period

    return atr_val


def supertrend(highs: list, lows: list, closes: list,
               period: int = 10, multiplier: float = 3.0
               ) -> Optional[Tuple[float, str]]:
    """
    SuperTrend 指标
    返回 (supertrend_value, direction) direction = "up" | "down"
    """
    if len(closes) < period + 2:
        return None

    n = len(closes)
    atr_vals = []
    # 计算 ATR 序列
    initial_atr = sum(
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, period + 1)
    ) / period
    atr_vals = [0] * (period + 1)
    atr_vals[period] = initial_atr
    for i in range(period + 1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        atr_vals.append((atr_vals[-1] * (period - 1) + tr) / period)

    # SuperTrend 计算
    upper_band = [0.0] * n
    lower_band = [0.0] * n
    supertrend_val = [0.0] * n
    direction = [1] * n  # 1 = up, -1 = down

    for i in range(period, n):
        hl2 = (highs[i] + lows[i]) / 2
        upper_band[i] = hl2 + multiplier * atr_vals[i]
        lower_band[i] = hl2 - multiplier * atr_vals[i]

        if i > period:
            if lower_band[i] > lower_band[i - 1] or closes[i - 1] < lower_band[i - 1]:
                pass
            else:
                lower_band[i] = lower_band[i - 1]

            if upper_band[i] < upper_band[i - 1] or closes[i - 1] > upper_band[i - 1]:
                pass
            else:
                upper_band[i] = upper_band[i - 1]

            if supertrend_val[i - 1] == upper_band[i - 1]:
                direction[i] = -1 if closes[i] > upper_band[i] else -1 if closes[i] == upper_band[i] else 1
                if closes[i] > upper_band[i]:
                    direction[i] = 1
                else:
                    direction[i] = -1
            else:
                if closes[i] < lower_band[i]:
                    direction[i] = -1
                else:
                    direction[i] = 1

        supertrend_val[i] = lower_band[i] if direction[i] == 1 else upper_band[i]

    dir_str = "up" if direction[-1] == 1 else "down"
    return (supertrend_val[-1], dir_str)


# ============================================================
#  VWAP (Volume Weighted Average Price)
# ============================================================
def vwap(highs: list, lows: list, closes: list, volumes: list) -> Optional[float]:
    """
    VWAP 计算 (当日或窗口内)
    """
    if not closes or not volumes or len(closes) != len(volumes):
        return None

    total_pv = 0
    total_vol = 0
    for i in range(len(closes)):
        typical = (highs[i] + lows[i] + closes[i]) / 3
        total_pv += typical * volumes[i]
        total_vol += volumes[i]

    if total_vol == 0:
        return None
    return total_pv / total_vol


# ============================================================
#  Stochastic RSI
# ============================================================
def stoch_rsi(closes: list, rsi_period: int = 14, stoch_period: int = 14,
              k_smooth: int = 3, d_smooth: int = 3
              ) -> Optional[Tuple[float, float]]:
    """
    Stochastic RSI
    返回 (%K, %D) 均在 0-100 范围
    """
    # 先算 RSI 序列
    needed = rsi_period + stoch_period + k_smooth + d_smooth + 10
    if len(closes) < needed:
        return None

    rsi_values = []
    for i in range(rsi_period + 1, len(closes) + 1):
        r = rsi(closes[:i], rsi_period)
        if r is not None:
            rsi_values.append(r)

    if len(rsi_values) < stoch_period:
        return None

    # Stochastic of RSI
    stoch_values = []
    for i in range(stoch_period - 1, len(rsi_values)):
        window = rsi_values[i - stoch_period + 1:i + 1]
        lo = min(window)
        hi = max(window)
        if hi == lo:
            stoch_values.append(50.0)
        else:
            stoch_values.append((rsi_values[i] - lo) / (hi - lo) * 100)

    if len(stoch_values) < k_smooth:
        return None

    # %K = SMA of stoch
    k_values = []
    for i in range(k_smooth - 1, len(stoch_values)):
        k_values.append(sum(stoch_values[i - k_smooth + 1:i + 1]) / k_smooth)

    if len(k_values) < d_smooth:
        return None

    # %D = SMA of %K
    d_val = sum(k_values[-d_smooth:]) / d_smooth
    return (k_values[-1], d_val)


# ============================================================
#  ADX (Average Directional Index)
# ============================================================
def adx(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """
    ADX 指标 (趋势强度，不含方向)
    返回 0-100，>25 表示强趋势
    """
    if len(closes) < period * 2 + 1:
        return None

    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # Wilder's smoothing
    atr_val = sum(tr_list[:period]) / period
    plus_di_smooth = sum(plus_dm[:period]) / period
    minus_di_smooth = sum(minus_dm[:period]) / period

    dx_values = []

    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period
        plus_di_smooth = (plus_di_smooth * (period - 1) + plus_dm[i]) / period
        minus_di_smooth = (minus_di_smooth * (period - 1) + minus_dm[i]) / period

        if atr_val > 0:
            plus_di = 100 * plus_di_smooth / atr_val
            minus_di = 100 * minus_di_smooth / atr_val
        else:
            plus_di = minus_di = 0

        di_sum = plus_di + minus_di
        if di_sum > 0:
            dx_values.append(100 * abs(plus_di - minus_di) / di_sum)
        else:
            dx_values.append(0)

    if len(dx_values) < period:
        return None

    adx_val = sum(dx_values[:period]) / period
    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period

    return adx_val


# ============================================================
#  P1 修复: 增量缓存集成 (2026-04-11)
# ============================================================

def rsi_incremental(
    code: str,
    close: float,
    period: int = 14,
    historical_closes: Optional[list] = None
) -> Optional[float]:
    """
    RSI 增量计算 (P1 优化)
    
    首次调用需要历史数据初始化，后续只需传入最新收盘价
    性能: O(1) vs O(n) 传统计算
    
    Args:
        code: 股票代码
        close: 最新收盘价
        period: RSI 周期
        historical_closes: 历史收盘价序列 (仅首次需要)
    
    Returns:
        RSI 值 (0-100) 或 None (数据不足)
    """
    from app.strategy.indicator_cache import indicator_cache
    
    state = indicator_cache.get_or_create(code)
    
    if not state.rsi_initialized:
        if historical_closes is None:
            return None
        return indicator_cache.initialize_rsi(code, historical_closes, period)
    else:
        return indicator_cache.update_rsi(code, close, period, state)


def macd_incremental(
    code: str,
    close: float,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    historical_closes: Optional[list] = None
) -> Optional[Tuple[float, float, float]]:
    """
    MACD 增量计算 (P1 优化)
    
    Args:
        code: 股票代码
        close: 最新收盘价
        fast/signal/slow: MACD 参数
        historical_closes: 历史收盘价序列 (仅首次需要)
    
    Returns:
        (macd_line, signal_line, histogram) 或 None
    """
    from app.strategy.indicator_cache import indicator_cache
    
    state = indicator_cache.get_or_create(code)
    
    if state.ema_fast is None:
        if historical_closes is None:
            return None
        return indicator_cache.initialize_macd(code, historical_closes, fast, slow, signal)
    else:
        return indicator_cache.update_macd(code, close, fast, slow, signal, state)


# ============================================================
#  波动率自适应 (用于布林带 σ 动态调整)
# ============================================================

def volatility_factor(
    highs: list, lows: list, closes: list, period: int = 20
) -> Optional[float]:
    """计算波动率因子 (平均真实波幅 / 收盘价)

    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        period: 计算窗口

    Returns:
        波动率因子 (ATRP)，None 如果数据不足
    """
    if len(highs) < period + 1 or len(closes) < period:
        return None

    true_ranges = []
    for i in range(-period, 0):
        h = highs[i]
        l = lows[i]
        c_prev = closes[i - 1] if i > -len(closes) else closes[i]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        true_ranges.append(tr)

    atr = sum(true_ranges) / period
    avg_close = sum(closes[-period:]) / period

    if avg_close <= 0:
        return None

    return atr / avg_close


def volatility_adaptive_multiplier(
    current_vf: float, baseline_vf: float, alpha: float = 1.5
) -> float:
    """根据波动率动态调整布林带 σ 倍数

    Args:
        current_vf: 当前波动率因子
        baseline_vf: 基准波动率因子 (中位数)
        alpha: 调节系数 (1.0=不调节, >1.0=高波动时放宽)

    Returns:
        σ 倍数调整系数
    """
    if baseline_vf <= 0 or current_vf <= 0:
        return 1.0

    ratio = current_vf / baseline_vf
    # 高波动时放宽布林带 (增大 σ), 低波动时收紧
    return max(0.5, min(3.0, 1.0 + alpha * (ratio - 1.0)))


# ============================================================
#  动量因子 (Momentum / Rate of Change)
# ============================================================

def momentum(closes: list, period: int = 10) -> Optional[float]:
    """计算动量因子（Rate of Change, ROC）

    ROC = (当前价 / N日前价 - 1) × 100

    Args:
        closes: 收盘价序列（按时间递增）
        period: 回看周期，默认10（港股/美股常用10日动量）

    Returns:
        动量百分比（正=上涨动量，负=下跌动量），None 如果数据不足
    """
    if len(closes) < period + 1:
        return None
    prev_close = closes[-(period + 1)]
    curr_close = closes[-1]
    if prev_close <= 0:
        return None
    return (curr_close / prev_close - 1.0) * 100.0


def multi_period_momentum(closes: list, periods: list = None) -> dict:
    """计算多周期动量（一次返回多个周期）

    Args:
        closes: 收盘价序列
        periods: 周期列表，默认 [5, 10, 20]

    Returns:
        {period: momentum_value_or_None}
    """
    if periods is None:
        periods = [5, 10, 20]
    result = {}
    for p in periods:
        result[p] = momentum(closes, p)
    return result
