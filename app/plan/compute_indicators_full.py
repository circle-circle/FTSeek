"""完整技术指标计算与解读模块

对标 deep research 报告的 "指标汇总表"（8 指标 + 趋势/动量/波动/量价 4 维度解读）。
输入: OHLCV DataFrame (Futu K-line 或 CSV)
输出: 结构化 dict（指标值 + 解读 + 关键价位 + 趋势判断）

指标清单 (与 deep research 一致):
  1. close_10_ema  — 短期趋势/入场节奏
  2. close_50_sma  — 中期趋势
  3. close_200_sma — 长期趋势基准
  4. macd + signal — 动量交叉/强弱
  5. rsi (14)      — 超买超卖/动量位置
  6. atr (14)      — 波动/止损尺度
  7. boll (20,2)   — 波动带/突破
  8. vwma (20)     — 量价趋势确认
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 指标计算函数
# ═══════════════════════════════════════════════════════════

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = _ema(close, fast)
    ema_slow = _ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def _bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = _sma(close, window)
    std = close.rolling(window=window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def _vwma(close: pd.Series, volume: pd.Series, window: int = 20) -> pd.Series:
    pv = close * volume
    return pv.rolling(window=window).sum() / volume.rolling(window=window).sum()


# ═══════════════════════════════════════════════════════════
# 关键价位提取
# ═══════════════════════════════════════════════════════════

def _extract_key_levels(df: pd.DataFrame, indicators: Dict) -> Dict:
    """从 OHLCV 数据提取关键支撑/阻力价位"""
    close = df["close"].iloc[-1]

    # 52 周高低
    if len(df) >= 250:
        high_52w = float(df["high"].iloc[-250:].max())
        low_52w = float(df["low"].iloc[-250:].min())
    else:
        high_52w = float(df["high"].max())
        low_52w = float(df["low"].min())

    # YTD (近似: 取最近约250个交易日起点)
    ytd_start_idx = max(0, len(df) - 250)
    ytd_change = (close - float(df["close"].iloc[ytd_start_idx])) / float(df["close"].iloc[ytd_start_idx]) * 100

    # 近期 swing high/low (20日)
    recent_20 = df.tail(20)
    swing_high = float(recent_20["high"].max())
    swing_low = float(recent_20["low"].min())

    # 5 日高低
    recent_5 = df.tail(5)
    high_5d = float(recent_5["high"].max())
    low_5d = float(recent_5["low"].min())

    return {
        "current_price": round(close, 2),
        "week52_high": round(high_52w, 2),
        "week52_low": round(low_52w, 2),
        "ytd_change": round(ytd_change, 2),
        "swing_high_20d": round(swing_high, 2),
        "swing_low_20d": round(swing_low, 2),
        "high_5d": round(high_5d, 2),
        "low_5d": round(low_5d, 2),
        "dist_from_52w_high": round((close - high_52w) / high_52w * 100, 2),
    }


# ═══════════════════════════════════════════════════════════
# 趋势判断
# ═══════════════════════════════════════════════════════════

def _judge_trend(close: float, ema10: float, sma50: float, sma200: float) -> Dict:
    """判断长/中/短期趋势及排列状态"""
    long_trend = "多头" if close > sma200 else "空头"
    mid_trend = "多头" if close > sma50 else "空头"
    short_trend = "偏多" if close > ema10 else "偏空"

    # 均线排列
    if ema10 > sma50 > sma200:
        alignment = "完美多头排列"
    elif ema10 < sma50 < sma200:
        alignment = "完美空头排列"
    elif close > sma50 and sma50 > sma200:
        alignment = "中期多头"
    elif close < sma50 and sma50 < sma200:
        alignment = "中期空头"
    else:
        alignment = "交叉/过渡"

    # 金叉/死叉
    golden_cross = sma50 > sma200
    death_cross = sma50 < sma200

    return {
        "long": long_trend,
        "mid": mid_trend,
        "short": short_trend,
        "alignment": alignment,
        "golden_cross": golden_cross,
        "death_cross": death_cross,
        "summary": f"{alignment} ({'10EMA' if ema10 > sma50 else '10EMA'}>{'<'}50SMA>{'<'}200SMA)"
    }


def _judge_momentum(macd_val: float, macd_signal: float, rsi: float) -> Dict:
    """判断动量状态"""
    macd_positive = macd_val > 0
    macd_above_signal = macd_val > macd_signal
    macd_hist = macd_val - macd_signal

    # RSI 区间
    if rsi >= 70:
        rsi_zone = "超买"
    elif rsi >= 60:
        rsi_zone = "中性偏多"
    elif rsi >= 40:
        rsi_zone = "中性"
    elif rsi >= 30:
        rsi_zone = "中性偏空"
    else:
        rsi_zone = "超卖"

    return {
        "macd": round(macd_val, 4),
        "macd_signal": round(macd_signal, 4),
        "macd_histogram": round(macd_hist, 4),
        "macd_positive": macd_positive,
        "macd_above_signal": macd_above_signal,
        "rsi": round(rsi, 2),
        "rsi_zone": rsi_zone,
        "summary": f"MACD {'正' if macd_positive else '负'}值{'(多头)' if macd_positive else '(空头)'} · RSI {rsi:.1f} {rsi_zone}"
    }


def _judge_volatility(atr_val: float, close: float, boll_upper: float, boll_mid: float, boll_lower: float) -> Dict:
    """判断波动状态"""
    atr_pct = atr_val / close * 100 if close > 0 else 0
    annualized_hv = atr_pct * np.sqrt(252)

    # 布林位置
    boll_range = boll_upper - boll_lower
    if boll_range > 0:
        boll_position = (close - boll_lower) / boll_range
    else:
        boll_position = 0.5

    # 波动等级
    if atr_pct >= 5:
        vol_level = "极高"
    elif atr_pct >= 3:
        vol_level = "高"
    elif atr_pct >= 1.5:
        vol_level = "中等"
    else:
        vol_level = "低"

    return {
        "atr": round(atr_val, 4),
        "atr_pct": round(atr_pct, 2),
        "annualized_hv": round(annualized_hv, 1),
        "boll_upper": round(boll_upper, 2),
        "boll_mid": round(boll_mid, 2),
        "boll_lower": round(boll_lower, 2),
        "boll_position": round(boll_position, 3),
        "vol_level": vol_level,
        "summary": f"ATR {atr_val:.2f}(≈{atr_pct:.1f}%/日) · 波动{vol_level} · 布林位置 {boll_position:.0%}"
    }


def _judge_volume(vwma_val: float, close: float, volume_ratio: Optional[float]) -> Dict:
    """判断量价关系"""
    price_above_vwma = close > vwma_val
    vol_confirm = price_above_vwma  # 价格在 VWMA 之上 = 量价确认上涨

    vol_status = "量价确认" if vol_confirm else "量价背离"
    if volume_ratio is not None:
        if volume_ratio >= 1.5:
            vol_status += " · 放量"
        elif volume_ratio >= 1.0:
            vol_status += " · 正常量"
        else:
            vol_status += " · 缩量"

    return {
        "vwma": round(vwma_val, 2),
        "price_above_vwma": price_above_vwma,
        "volume_ratio": volume_ratio,
        "vol_confirm": vol_confirm,
        "summary": f"VWMA {vwma_val:.2f} · {vol_status}"
    }


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def compute_indicators_full(
    df: pd.DataFrame,
    volume_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """从 OHLCV DataFrame 计算完整 8 指标并生成结构化解读。

    Args:
        df: DataFrame with columns: open, high, low, close, volume
            (至少需要 200 行数据以计算 200SMA)
        volume_ratio: 可选的当日量比 (当日成交量 / 20日均量)

    Returns:
        结构化 dict，含：
        - indicators: 各指标原始数值
        - trend: 趋势判断
        - momentum: 动量判断
        - volatility: 波动判断
        - volume: 量价判断
        - key_levels: 关键价位
        - indicators_table: 对标 deep research 的 8 行指标汇总表
        - overall_verdict: 综合判断 (偏多/偏空/中性 + 强度)
    """
    if df is None or len(df) < 30:
        return {"error": "数据不足 (需至少30行 OHLCV)", "indicators": {}}

    # 确保列名统一小写
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("open", "high", "low", "close", "volume"):
            col_map[c] = cl
    if col_map:
        df = df.rename(columns=col_map)

    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return {"error": f"缺少列: {missing}", "indicators": {}}

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)
    cur = float(close.iloc[-1])

    # ── 计算指标 ──
    ema10 = _ema(close, 10)
    sma50 = _sma(close, 50)
    sma200 = _sma(close, 200) if len(df) >= 200 else _sma(close, min(50, len(df)))
    macd_line, signal_line, hist = _macd(close)
    rsi = _rsi(close, 14)
    atr = _atr(high, low, close, 14)
    boll_upper, boll_mid, boll_lower = _bollinger(close, 20, 2.0)
    vwma = _vwma(close, volume, 20)

    # 取最后一行值
    indicators = {
        "close_10_ema": round(float(ema10.iloc[-1]), 2) if not pd.isna(ema10.iloc[-1]) else None,
        "close_50_sma": round(float(sma50.iloc[-1]), 2) if not pd.isna(sma50.iloc[-1]) else None,
        "close_200_sma": round(float(sma200.iloc[-1]), 2) if not pd.isna(sma200.iloc[-1]) else None,
        "macd": round(float(macd_line.iloc[-1]), 4) if not pd.isna(macd_line.iloc[-1]) else None,
        "macd_signal": round(float(signal_line.iloc[-1]), 4) if not pd.isna(signal_line.iloc[-1]) else None,
        "rsi": round(float(rsi.iloc[-1]), 2) if not pd.isna(rsi.iloc[-1]) else None,
        "atr": round(float(atr.iloc[-1]), 4) if not pd.isna(atr.iloc[-1]) else None,
        "boll_upper": round(float(boll_upper.iloc[-1]), 2) if not pd.isna(boll_upper.iloc[-1]) else None,
        "boll_mid": round(float(boll_mid.iloc[-1]), 2) if not pd.isna(boll_mid.iloc[-1]) else None,
        "boll_lower": round(float(boll_lower.iloc[-1]), 2) if not pd.isna(boll_lower.iloc[-1]) else None,
        "vwma": round(float(vwma.iloc[-1]), 2) if not pd.isna(vwma.iloc[-1]) else None,
    }

    # ── 4 维度判断 ──
    trend = _judge_trend(
        cur,
        indicators["close_10_ema"] or cur,
        indicators["close_50_sma"] or cur,
        indicators["close_200_sma"] or cur,
    )
    momentum = _judge_momentum(
        indicators["macd"] or 0,
        indicators["macd_signal"] or 0,
        indicators["rsi"] or 50,
    )
    volatility = _judge_volatility(
        indicators["atr"] or 0,
        cur,
        indicators["boll_upper"] or cur * 1.1,
        indicators["boll_mid"] or cur,
        indicators["boll_lower"] or cur * 0.9,
    )
    vol_judgment = _judge_volume(
        indicators["vwma"] or cur,
        cur,
        volume_ratio,
    )

    # ── 关键价位 ──
    key_levels = _extract_key_levels(df, indicators)

    # ── 指标汇总表 (对标 deep research) ──
    indicators_table = [
        {
            "indicator": "close_10_ema",
            "role": "短期趋势/入场节奏",
            "reason": "捕捉短线动量节奏",
            "value": indicators["close_10_ema"],
            "interpretation": f"{indicators['close_10_ema']}，{'上行' if trend['short'] == '偏多' else '下行'}，价在其{'上' if trend['short'] == '偏多' else '下'} → 短期{trend['short']}",
        },
        {
            "indicator": "close_50_sma",
            "role": "中期趋势",
            "reason": "判断修复/主升行情是否成立",
            "value": indicators["close_50_sma"],
            "interpretation": f"{indicators['close_50_sma']}，{trend['mid']}趋势",
        },
        {
            "indicator": "close_200_sma",
            "role": "长期趋势基准",
            "reason": "界定大级别多空",
            "value": indicators["close_200_sma"],
            "interpretation": f"{indicators['close_200_sma']}，{'长期多头' if trend['long'] == '多头' else '长期空头未翻转'}",
        },
        {
            "indicator": "macd",
            "role": "动量交叉/强弱",
            "reason": "度量动能持续性",
            "value": indicators["macd"],
            "interpretation": momentum["summary"],
        },
        {
            "indicator": "rsi",
            "role": "超买超卖/动量位置",
            "reason": "与MACD互补，防追高",
            "value": indicators["rsi"],
            "interpretation": f"RSI {indicators['rsi']}，{momentum['rsi_zone']}，{'无超买' if indicators['rsi'] and indicators['rsi'] < 70 else '注意超买'}",
        },
        {
            "indicator": "atr",
            "role": "波动/止损尺度",
            "reason": "定止损/仓位的真实波幅",
            "value": indicators["atr"],
            "interpretation": f"ATR {indicators['atr']}(≈{volatility['atr_pct']}%/日) · 波动{volatility['vol_level']}",
        },
        {
            "indicator": "boll",
            "role": "波动带/突破",
            "reason": "动态区间与触轨极端",
            "value": f"中{indicators['boll_mid']}/上{indicators['boll_upper']}/下{indicators['boll_lower']}",
            "interpretation": f"布林位置 {volatility['boll_position']:.0%}，{'未触轨' if 0.1 < volatility['boll_position'] < 0.9 else '接近极端'}",
        },
        {
            "indicator": "vwma",
            "role": "量价趋势确认",
            "reason": "用成交量确认趋势真伪",
            "value": indicators["vwma"],
            "interpretation": vol_judgment["summary"],
        },
    ]

    # ── 综合判断 ──
    bull_signals = sum([
        1 if trend["long"] == "多头" else 0,
        1 if trend["mid"] == "多头" else 0,
        1 if momentum["macd_positive"] else 0,
        1 if momentum["rsi"] and momentum["rsi"] > 50 else 0,
        1 if vol_judgment["vol_confirm"] else 0,
    ])
    bear_signals = 5 - bull_signals

    if bull_signals >= 4:
        overall = "偏多"
        strength = "强" if bull_signals == 5 else "中"
    elif bear_signals >= 4:
        overall = "偏空"
        strength = "强" if bear_signals == 5 else "中"
    else:
        overall = "中性"
        strength = "弱"

    # MACD 降速检测
    macd_slowing = False
    if len(macd_line) >= 5 and not pd.isna(macd_line.iloc[-5]):
        macd_peak = float(macd_line.iloc[-5:].max())
        macd_cur = float(macd_line.iloc[-1])
        if macd_peak > 0 and macd_cur < macd_peak * 0.85:
            macd_slowing = True

    # RSI 超买回落检测
    rsi_cooling = False
    if len(rsi) >= 10:
        rsi_recent_max = float(rsi.iloc[-10:].max())
        rsi_cur = float(rsi.iloc[-1])
        if rsi_recent_max > 75 and rsi_cur < 65:
            rsi_cooling = True

    return {
        "indicators": indicators,
        "trend": trend,
        "momentum": momentum,
        "volatility": volatility,
        "volume": vol_judgment,
        "key_levels": key_levels,
        "indicators_table": indicators_table,
        "overall_verdict": {
            "direction": overall,
            "strength": strength,
            "bull_signals": bull_signals,
            "bear_signals": bear_signals,
            "macd_slowing": macd_slowing,
            "rsi_cooling": rsi_cooling,
            "summary": f"技术面{overall}({strength}) — {trend['alignment']}，{momentum['rsi_zone']}，{volatility['vol_level']}波动，{vol_judgment['summary'].split('·')[-1].strip() if '·' in vol_judgment['summary'] else ''}",
        },
    }


def compute_from_futu_kline(symbol: str, num_bars: int = 250) -> Optional[Dict]:
    """便捷入口: 从 Futu 获取 K-line 并计算指标。

    用于 Pipeline 中自动获取数据。若 Futu 不可用返回 None。
    """
    try:
        # 2026-07-25: 改单例 + 不 disconnect (handoff 07-24 解锁spam同款反模式收敛)
        from app.web_dashboard.manual_order_service import get_manual_order_service
        from futu import RET_OK, SubType

        svc = get_manual_order_service()
        if not svc.connect():
            return None

        svc._quote_ctx.subscribe([symbol], [SubType.K_DAY])
        import time
        time.sleep(0.3)

        ret, data = svc._quote_ctx.get_cur_kline(symbol, num_bars, ktype="K_DAY")

        if ret != RET_OK or data.empty:
            return None

        # 转换为标准 DataFrame
        df = pd.DataFrame({
            "open": data["open"].astype(float),
            "high": data["high"].astype(float),
            "low": data["low"].astype(float),
            "close": data["close"].astype(float),
            "volume": data["volume"].astype(float),
        })

        # 计算量比
        if len(df) >= 20:
            avg_vol_20 = float(df["volume"].tail(20).mean())
            cur_vol = float(df["volume"].iloc[-1])
            volume_ratio = cur_vol / avg_vol_20 if avg_vol_20 > 0 else None
        else:
            volume_ratio = None

        result = compute_indicators_full(df, volume_ratio)
        result["symbol"] = symbol
        return result

    except Exception as e:
        logger.warning(f"[compute_indicators_full] {symbol} Futu error: {e}")
        return None


__all__ = [
    "compute_indicators_full",
    "compute_from_futu_kline",
]
