"""基本面分析师 — Deep Research 级别的基本面分析

对标 deep research 报告的基本面章节（如 MU 的财务指标/估值/分析师目标价/收入结构）。

输入: target dict
输出: 结构化分析 (financials, valuation, analyst_cluster, revenue_structure, conclusion)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def run_fundamental_analysis(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """运行基本面分析。

    Args:
        target: plan target dict
        premarket_data: 可选的盘前数据

    Returns:
        {
            "status": "complete" | "degraded" | "missing",
            "financials": {...},         # 关键财务指标
            "valuation": {...},          # 估值水位
            "analyst_cluster": {...},    # 分析师评级分布
            "conclusion": "...",
        }
    """
    symbol = target.get("symbol", "")
    bare = symbol.replace("US.", "")
    result = {"status": "missing", "symbol": symbol}

    try:
        _loop = asyncio.get_event_loop()

        # 1. 获取 Finnhub 真实数据 (已重构为直接调 REST API)
        from app.plan.deep_analysis_engine import _fetch_real_fundamental
        real_data = await asyncio.wait_for(
            _fetch_real_fundamental(symbol, target.get("company_name", "")),
            timeout=30,
        )

        sources = real_data.get("_sources", {})
        has_data = False

        # Financials — 直接从 Finnhub stock/metric 映射
        fin = real_data.get("financials", {})
        if sources.get("financials") == "ok" and fin.get("source") != "none":
            result["financials"] = {
                "market_cap": fin.get("market_cap", "N/A"),
                "pe_ttm": fin.get("pe", "N/A"),
                "forward_pe": fin.get("forward_pe", "N/A"),
                "pb": fin.get("pb", "N/A"),
                "roe": fin.get("roe", "N/A"),
                "eps_ttm": fin.get("eps_ttm", "N/A"),
                "revenue_yoy": fin.get("revenue_yoy", "N/A"),
                "gross_margin": fin.get("gross_margin", "N/A"),
                "net_margin": fin.get("net_margin", "N/A"),
                "beta": fin.get("beta", "N/A"),
                "source": fin.get("source", ""),
                "fetched_at": fin.get("fetched_at", ""),
            }
            has_data = True

        # Valuation — 从同一 Finnhub 数据提取 (不再依赖 FMP)
        if sources.get("financials") == "ok" and fin.get("source") != "none":
            result["valuation"] = {
                "pe_ttm": fin.get("pe", "N/A"),
                "forward_pe": fin.get("forward_pe", "N/A"),
                "pb": fin.get("pb", "N/A"),
                "beta": fin.get("beta", "N/A"),
                "dividend_yield": fin.get("dividend_yield", "N/A"),
                "market_cap": fin.get("market_cap", "N/A"),
            }
            has_data = True

        # Analyst ratings — 从 Finnhub recommendation_trends
        ana = real_data.get("analyst", {})
        if sources.get("analyst") == "ok" and ana.get("source") != "none":
            result["analyst_cluster"] = {
                "buy_pct": ana.get("buy_pct", "N/A"),
                "hold_pct": ana.get("hold_pct", "N/A"),
                "sell_pct": ana.get("sell_pct", "N/A"),
                "total_analysts": ana.get("total_analysts", 0),
                "strong_buy": ana.get("strong_buy", 0),
                "buy": ana.get("buy", 0),
                "hold": ana.get("hold", 0),
                "sell": ana.get("sell", 0),
                "trend": ana.get("trend", "stable"),
                "source": ana.get("source", ""),
                "fetched_at": ana.get("fetched_at", ""),
            }
            has_data = True

        # News + Catalyst
        news = real_data.get("recent_news", [])
        cat = real_data.get("next_catalyst", {})
        if news:
            result["recent_news"] = [n.get("headline", "") for n in news[:5]]
        if cat and cat.get("source") != "none":
            result["next_catalyst"] = {
                "event": cat.get("event", "N/A"),
                "date": cat.get("date", "N/A"),
                "eps_estimate": cat.get("eps_estimate"),
                "revenue_estimate": cat.get("revenue_estimate"),
            }

        # Status
        fin_ok = sources.get("financials") == "ok"
        ana_ok = sources.get("analyst") == "ok"
        if fin_ok and ana_ok:
            result["status"] = "complete"
        elif fin_ok or ana_ok or has_data:
            result["status"] = "degraded"
        else:
            result["status"] = "missing"

        # Conclusion
        result["conclusion"] = _build_conclusion(result, target)

    except asyncio.TimeoutError:
        result["status"] = "degraded"
        result["error"] = "基本面分析超时(30s)"
        logger.warning(f"[fund_analyst] {symbol} timeout")
    except Exception as e:
        result["status"] = "missing"
        result["error"] = str(e)
        logger.warning(f"[fund_analyst] {symbol} error: {e}")

    return result


def _build_conclusion(result: Dict, target: Dict) -> str:
    """构建基本面结论"""
    parts = []

    # 估值
    val = result.get("valuation", {})
    pe = val.get("pe_ttm")
    if pe and pe != "N/A":
        try:
            pe_f = float(pe)
            if pe_f > 50:
                parts.append(f"TTM PE {pe_f:.0f}x 偏贵")
            elif pe_f > 25:
                parts.append(f"TTM PE {pe_f:.0f}x 中等")
            else:
                parts.append(f"TTM PE {pe_f:.0f}x 合理")
        except (ValueError, TypeError):
            parts.append(f"PE={pe}")

    # 分析师评级
    cluster = result.get("analyst_cluster", {})
    total = cluster.get("total_analysts", 0)
    if total > 0:
        buy_pct = cluster.get("buy_pct", "N/A")
        trend = cluster.get("trend", "")
        parts.append(f"分析师 {buy_pct}买入 ({total}家, {trend})")

    # 催化
    catalyst = result.get("next_catalyst")
    if catalyst and isinstance(catalyst, dict):
        event = catalyst.get("event", "")
        date = catalyst.get("date", "")
        if event and event != "N/A":
            parts.append(f"下一催化: {event} ({date})")

    return " · ".join(parts) if parts else "基本面数据有限"


__all__ = ["run_fundamental_analysis"]
