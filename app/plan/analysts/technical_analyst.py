"""技术面分析师 — Deep Research 级别的技术分析

对标 deep research 报告的技术面章节（如 MU 的"指标汇总表" + 趋势/动量/波动/量价 4 维度解读）。

输入: target dict + 市场数据
输出: 结构化分析结果 (indicators_table, trend, momentum, volatility, volume, key_levels, conclusion)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def run_technical_analysis(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """运行技术面分析。

    Args:
        target: plan target dict (symbol, direction, entry_zone, etc.)
        premarket_data: 可选的盘前数据 (含技术指标)

    Returns:
        {
            "status": "complete" | "degraded" | "missing",
            "indicators_table": [...],  # 8行指标汇总表
            "trend": {...},             # 趋势判断
            "momentum": {...},          # 动量判断
            "volatility": {...},        # 波动判断
            "volume": {...},            # 量价判断
            "key_levels": {...},        # 关键价位
            "conclusion": "...",        # 技术面结论
            "overall_verdict": {...},   # 综合判断
        }
    """
    symbol = target.get("symbol", "")
    result = {"status": "missing", "symbol": symbol}

    try:
        # 尝试从 premarket_data 获取已有指标
        if premarket_data and premarket_data.get("indicators"):
            indicators = premarket_data["indicators"]
            result["status"] = "degraded"
            result["indicators_table"] = _build_table_from_premarket(indicators, target)
            result["conclusion"] = _build_conclusion_from_premarket(indicators, target)
            return result

        # 从 Futu 获取完整 K-line 并计算
        from app.plan.compute_indicators_full import compute_from_futu_kline
        _loop = asyncio.get_event_loop()
        full_result = await asyncio.wait_for(
            _loop.run_in_executor(None, compute_from_futu_kline, symbol, 250),
            timeout=30,
        )

        if full_result and "error" not in full_result:
            result["status"] = "complete"
            result["indicators"] = full_result.get("indicators", {})
            result["indicators_table"] = full_result.get("indicators_table", [])
            result["trend"] = full_result.get("trend", {})
            result["momentum"] = full_result.get("momentum", {})
            result["volatility"] = full_result.get("volatility", {})
            result["volume"] = full_result.get("volume", {})
            result["key_levels"] = full_result.get("key_levels", {})
            result["overall_verdict"] = full_result.get("overall_verdict", {})
            result["conclusion"] = full_result.get("overall_verdict", {}).get("summary", "")
        else:
            result["status"] = "missing"
            result["error"] = full_result.get("error", "计算失败") if full_result else "Futu 不可用"

    except asyncio.TimeoutError:
        result["status"] = "degraded"
        result["error"] = "技术指标计算超时(30s)"
        logger.warning(f"[tech_analyst] {symbol} timeout")
    except Exception as e:
        result["status"] = "missing"
        result["error"] = str(e)
        logger.warning(f"[tech_analyst] {symbol} error: {e}")

    return result


def _build_table_from_premarket(indicators: Dict, target: Dict) -> list:
    """从 premarket 指标数据构建简化汇总表"""
    table = []
    cur = target.get("current_price", target.get("_current_price", 0))

    for key, role, reason in [
        ("close_10_ema", "短期趋势", "捕捉短线动量"),
        ("close_50_sma", "中期趋势", "判断修复行情"),
        ("close_200_sma", "长期趋势", "界定大级别多空"),
        ("macd", "动量", "度量动能持续性"),
        ("rsi", "超买超卖", "防追高"),
        ("atr", "波动", "定止损尺度"),
        ("boll_mid", "波动带", "动态区间"),
        ("vwma", "量价确认", "验证趋势真伪"),
    ]:
        val = indicators.get(key)
        if val is not None:
            interp = f"{val}"
            if key == "rsi" and isinstance(val, (int, float)):
                if val >= 70:
                    interp += " 超买"
                elif val >= 60:
                    interp += " 中性偏多"
                elif val >= 40:
                    interp += " 中性"
                elif val >= 30:
                    interp += " 中性偏空"
                else:
                    interp += " 超卖"
            table.append({
                "indicator": key,
                "role": role,
                "reason": reason,
                "value": val,
                "interpretation": interp,
            })

    return table


def _build_conclusion_from_premarket(indicators: Dict, target: Dict) -> str:
    """从 premarket 数据构建简化结论"""
    parts = []
    rsi = indicators.get("rsi")
    if rsi is not None:
        if rsi >= 70:
            parts.append("RSI超买")
        elif rsi <= 30:
            parts.append("RSI超卖")
        else:
            parts.append(f"RSI {rsi:.0f}")

    sma50 = indicators.get("close_50_sma")
    sma200 = indicators.get("close_200_sma")
    cur = target.get("current_price", 0)
    if sma50 and sma200 and cur:
        if cur > sma50 > sma200:
            parts.append("多头排列")
        elif cur < sma50 < sma200:
            parts.append("空头排列")

    return " · ".join(parts) if parts else "技术面数据有限"


__all__ = ["run_technical_analysis"]
