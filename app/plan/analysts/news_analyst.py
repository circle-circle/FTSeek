"""新闻分析师 — Deep Research 级别的新闻分析

对标 deep research 报告的新闻章节（如 MU 的催化事件时间线 + 宏观日历 + 影响评估）。

输入: target dict + 盘前数据
输出: 结构化分析 (news_timeline, catalysts, macro_calendar, conclusion)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def run_news_analysis(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """运行新闻分析。

    Args:
        target: plan target dict
        premarket_data: 可选的盘前数据 (含新闻/板块/宏观)

    Returns:
        {
            "status": "complete" | "degraded" | "missing",
            "news_timeline": [...],      # 按时间倒序的新闻事件
            "catalysts": [...],          # 即将到来的催化事件
            "macro_calendar": [...],     # 宏观日历
            "sector_context": "...",     # 板块环境
            "conclusion": "...",
        }
    """
    symbol = target.get("symbol", "")
    bare = symbol.replace("US.", "")
    result = {"status": "missing", "symbol": symbol}

    try:
        news_items = []
        catalysts = []
        macro_events = []

        # 1. 从盘前数据获取新闻
        if premarket_data:
            # 新闻
            premarket_news = premarket_data.get("news", [])
            if isinstance(premarket_news, list):
                for item in premarket_news[:8]:
                    if isinstance(item, dict):
                        news_items.append({
                            "date": item.get("datetime", item.get("date", "")),
                            "headline": item.get("headline", item.get("summary", "")),
                            "source": item.get("source", ""),
                            "sentiment": item.get("sentiment", "neutral"),
                            "impact": _assess_impact(item),
                        })
                    elif isinstance(item, str):
                        news_items.append({
                            "headline": item,
                            "sentiment": "neutral",
                            "impact": "neutral",
                        })

            # 板块
            sector_info = premarket_data.get("sector_info", premarket_data.get("sectors", {}))
            if sector_info:
                result["sector_context"] = _format_sector(sector_info)

            # 宏观事件
            econ_events = premarket_data.get("economic_events", premarket_data.get("macro_events", []))
            if isinstance(econ_events, list):
                for ev in econ_events[:5]:
                    if isinstance(ev, dict):
                        macro_events.append({
                            "date": ev.get("date", ""),
                            "event": ev.get("event", ev.get("title", "")),
                            "impact": ev.get("impact", "medium"),
                        })

            # 财报日历
            earnings = premarket_data.get("earnings", [])
            if isinstance(earnings, list):
                for e in earnings[:5]:
                    if isinstance(e, dict):
                        sym = e.get("symbol", "")
                        if bare.upper() in str(sym).upper():
                            catalysts.append({
                                "event": f"{bare} 财报",
                                "date": e.get("date", ""),
                                "importance": "HIGH",
                            })

        # 2. 从 Finnhub company-news 获取 (已验证可用)
        if not news_items:
            try:
                from app.plan.deep_analysis_engine import _fetch_real_fundamental
                real = await asyncio.wait_for(
                    _fetch_real_fundamental(symbol, ""),
                    timeout=15,
                )
                if real and real.get("_sources", {}).get("news") == "ok":
                    for item in real.get("recent_news", [])[:8]:
                        if isinstance(item, dict):
                            news_items.append({
                                "headline": item.get("headline", ""),
                                "date": item.get("datetime", ""),
                                "source": item.get("source", ""),
                                "sentiment": "neutral",
                                "impact": _assess_impact(item),
                            })
                    # 也从 earnings calendar 补充催化
                    cat = real.get("next_catalyst", {})
                    if cat and cat.get("source") != "none":
                        catalysts.append({
                            "event": cat.get("event", ""),
                            "date": cat.get("date", ""),
                            "importance": "HIGH",
                        })
            except Exception as e:
                logger.debug(f"[news_analyst] {symbol} finnhub news error: {e}")

        # 3. 组装结果
        if news_items or catalysts or macro_events:
            result["status"] = "complete" if news_items else "degraded"
        else:
            result["status"] = "degraded"

        result["news_timeline"] = news_items[:8]
        result["catalysts"] = catalysts
        result["macro_calendar"] = macro_events

        # 4. 结论
        result["conclusion"] = _build_conclusion(result, target)

    except asyncio.TimeoutError:
        result["status"] = "degraded"
        result["error"] = "新闻分析超时"
    except Exception as e:
        result["status"] = "missing"
        result["error"] = str(e)
        logger.warning(f"[news_analyst] {symbol} error: {e}")

    return result


def _assess_impact(item: Dict) -> str:
    """评估新闻影响"""
    headline = str(item.get("headline", "")).lower()
    # 高影响关键词
    high_keywords = ["earnings", "fda", "approval", "acquisition", "merger", "guidance",
                     "layoff", "ceo", "lawsuit", "sec", "investigation"]
    if any(kw in headline for kw in high_keywords):
        return "high"
    # 中等影响
    mid_keywords = ["upgrade", "downgrade", "target", "dividend", "buyback", "partnership"]
    if any(kw in headline for kw in mid_keywords):
        return "medium"
    return "low"


def _format_sector(sector_info) -> str:
    """格式化板块信息"""
    if isinstance(sector_info, dict):
        parts = []
        for name, change in sorted(sector_info.items(), key=lambda x: -abs(x[1]) if isinstance(x[1], (int, float)) else 0)[:5]:
            if isinstance(change, (int, float)):
                parts.append(f"{name}: {change:+.1f}%")
            else:
                parts.append(f"{name}: {change}")
        return " | ".join(parts) if parts else ""
    elif isinstance(sector_info, list):
        return "; ".join(str(s) for s in sector_info[:3])
    return str(sector_info)[:200] if sector_info else ""


def _build_conclusion(result: Dict, target: Dict) -> str:
    """构建新闻面结论"""
    parts = []

    # 新闻数量和倾向
    news = result.get("news_timeline", [])
    if news:
        positive = sum(1 for n in news if n.get("sentiment", "").lower() in ("positive", "bullish"))
        negative = sum(1 for n in news if n.get("sentiment", "").lower() in ("negative", "bearish"))
        if positive > negative:
            parts.append(f"新闻偏多({positive}利好/{negative}利空)")
        elif negative > positive:
            parts.append(f"新闻偏空({positive}利好/{negative}利空)")
        else:
            parts.append(f"新闻中性({len(news)}条)")

    # 催化事件
    catalysts = result.get("catalysts", [])
    if catalysts:
        for c in catalysts[:2]:
            parts.append(f"催化: {c['event']} ({c.get('date', '?')})")

    # 宏观
    macro = result.get("macro_calendar", [])
    if macro:
        high_impact = [m for m in macro if m.get("impact", "").lower() == "high"]
        if high_impact:
            parts.append(f"宏观: {len(high_impact)}个高影响事件")

    return " · ".join(parts) if parts else "近期无重大新闻催化"


__all__ = ["run_news_analysis"]
