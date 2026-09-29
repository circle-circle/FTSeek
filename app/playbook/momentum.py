from __future__ import annotations

from typing import Dict, Optional, Sequence


def classify_ma_trend(closes: Sequence[float]) -> tuple[str, Optional[Dict[str, float]]]:
    """Classify MA5/10/20 trend using the DA/UE shared caliber."""
    if len(closes) < 20:
        return "no_data", None

    def _ma(data: Sequence[float], period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return round(sum(data[-period:]) / period, 2)

    ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
    close = round(closes[-1], 2)
    detail = {
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "close": close,
        "bars_used": len(closes),
    }

    trend = "震荡"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20 and close > ma5:
            trend = "多头排列"
        elif ma5 < ma10 < ma20 and close < ma5:
            trend = "空头排列"
        elif ma5 > ma10:
            trend = "短期偏多"
        elif ma5 < ma10:
            trend = "短期偏空"
    return trend, detail
