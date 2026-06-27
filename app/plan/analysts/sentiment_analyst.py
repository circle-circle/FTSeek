"""情绪分析师 — Deep Research 级别的社交情绪分析

对标 deep research 报告的情绪章节（如 MU 的 StockTwits 多空比 + Reddit 叙事 + 期权 IV）。

输入: target dict
输出: 结构化分析 (stocktwits, reddit, options_iv, crowding, conclusion)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def run_sentiment_analysis(
    target: Dict,
    premarket_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """运行情绪分析。

    Args:
        target: plan target dict
        premarket_data: 可选的盘前数据

    Returns:
        {
            "status": "complete" | "degraded" | "missing",
            "stocktwits": {...},         # StockTwits 数据
            "reddit": {...},             # Reddit 数据
            "options_iv": {...},         # 期权隐含波动率
            "crowding": "...",           # 拥挤度 (正常/拥挤/极端)
            "direction": "...",          # 情绪方向 (偏多/偏空/中性)
            "conclusion": "...",
        }
    """
    symbol = target.get("symbol", "")
    bare = symbol.replace("US.", "")
    result = {"status": "missing", "symbol": symbol}

    try:
        tasks = []

        # 0. Fear & Greed Index (free, no key, always works)
        tasks.append(_fetch_fear_greed())

        # 1. StockTwits (并行)
        tasks.append(_fetch_stocktwits(bare))

        # 2. Reddit (并行)
        tasks.append(_fetch_reddit(bare))

        # 3. 从 social_sentiment 模块获取 (并行)
        tasks.append(_fetch_social_sentiment(symbol))

        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=30,
        )

        fng_data = results[0] if not isinstance(results[0], Exception) else None
        st_data = results[1] if not isinstance(results[1], Exception) else None
        reddit_data = results[2] if not isinstance(results[2], Exception) else None
        social_data = results[3] if not isinstance(results[3], Exception) else None

        has_data = False

        # Fear & Greed (market-wide sentiment)
        if fng_data:
            result["fear_greed"] = fng_data
            has_data = True

        # StockTwits
        if st_data:
            result["stocktwits"] = st_data
            has_data = True

        # Reddit
        if reddit_data:
            result["reddit"] = reddit_data
            has_data = True

        # Social sentiment fallback
        if social_data and not has_data:
            result["social_sentiment"] = social_data
            has_data = True

        if has_data:
            result["status"] = "complete"
        else:
            result["status"] = "degraded"

        # 综合判断
        result["crowding"] = _assess_crowding(st_data, reddit_data)
        result["direction"] = _assess_direction(st_data, reddit_data, social_data)
        if fng_data:
            # Override direction based on Fear & Greed + VIX combination
            fng_val = fng_data.get("fear_greed", {}).get("value", 50)
            vix_val = fng_data.get("vix", {}).get("value", 0)
            vix_regime = fng_data.get("vix", {}).get("regime", "normal")

            if fng_val <= 25 and vix_regime == "high_vol":
                result["direction"] = "偏空"  # Extreme Fear + high VIX = bearish
            elif fng_val >= 75 and vix_regime == "calm":
                result["direction"] = "偏多"  # Extreme Greed + low VIX = bullish
            elif fng_val <= 30 or vix_regime == "high_vol":
                result["direction"] = "偏空"  # Fear or high vol
            elif fng_val >= 70 or vix_regime == "calm":
                result["direction"] = "偏多"  # Greed or calm
        result["conclusion"] = _build_conclusion(result, target)

    except asyncio.TimeoutError:
        result["status"] = "degraded"
        result["error"] = "情绪分析超时(30s)"
    except Exception as e:
        result["status"] = "missing"
        result["error"] = str(e)
        logger.debug(f"[sentiment_analyst] {symbol} error: {e}")

    return result


async def _fetch_fear_greed() -> Optional[Dict]:
    """获取 CNN Fear & Greed Index + VIX (市场整体情绪, free, no key)"""
    try:
        import requests

        # Fear & Greed Index
        fng_resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        )
        fng_data = None
        if fng_resp.status_code == 200:
            d = fng_resp.json().get("data", [{}])[0]
            fng_data = {
                "value": int(d.get("value", 50)),
                "classification": d.get("value_classification", "Neutral"),
            }

        # VIX (from Yahoo Finance, free)
        vix_data = None
        vix_resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
        )
        if vix_resp.status_code == 200:
            meta = vix_resp.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            vix_price = meta.get("regularMarketPrice", 0)
            vix_52w_high = meta.get("fiftyTwoWeekHigh", 0)
            vix_52w_low = meta.get("fiftyTwoWeekLow", 0)
            if vix_price:
                vix_data = {
                    "value": vix_price,
                    "52w_high": vix_52w_high,
                    "52w_low": vix_52w_low,
                    "regime": "high_vol" if vix_price >= 25 else "calm" if vix_price <= 15 else "normal",
                }

        if not fng_data and not vix_data:
            return None

        return {
            "fear_greed": fng_data,
            "vix": vix_data,
            "source": "alternative.me + yahoo finance",
        }
    except Exception as e:
        logger.debug(f"[sentiment] Fear&Greed/VIX error: {e}")
        return None


async def _fetch_stocktwits(symbol: str) -> Optional[Dict]:
    """获取 StockTwits 情绪数据"""
    try:
        import requests
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: requests.get(url, timeout=10, headers={"User-Agent": "FTSeek/1.0"})
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            return None

        # 统计多空
        bullish = 0
        bearish = 0
        neutral = 0
        for msg in messages[:30]:
            sentiment = msg.get("entities", {}).get("sentiment", {})
            basic = sentiment.get("basic", "") if isinstance(sentiment, dict) else ""
            if basic == "Bullish":
                bullish += 1
            elif basic == "Bearish":
                bearish += 1
            else:
                neutral += 1

        total_tagged = bullish + bearish
        ratio = bullish / max(total_tagged, 1)

        # Watchlist count
        watchlist = data.get("symbol", {}).get("watchlist_count", 0)

        return {
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "total_messages": len(messages),
            "bull_bear_ratio": round(ratio, 2),
            "watchlist_count": watchlist,
            "direction": "偏多" if ratio > 0.6 else ("偏空" if ratio < 0.4 else "中性"),
        }
    except Exception as e:
        logger.debug(f"[sentiment] StockTwits {symbol} error: {e}")
        return None


async def _fetch_reddit(symbol: str) -> Optional[Dict]:
    """获取 Reddit/社交媒体讨论数据
    
    修复 (2026-06-16): 原 scripts.reddit_rss 不存在。
    改用 social_sentiment.db (由 app/data/social_sentiment.py → last30days skill 填充)
    """
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent.parent / "data" / "social_sentiment.db"
        if not db_path.exists():
            return None
        
        bare = symbol.replace("US.", "")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        
        # 查询 Reddit 来源的数据 (source='reddit' 或含 reddit 关键词)
        rows = conn.execute(
            "SELECT * FROM sentiment WHERE symbol = ? "
            "AND (source LIKE '%reddit%' OR source LIKE '%wallstreetbets%' OR source LIKE '%options%') "
            "ORDER BY timestamp DESC LIMIT 10",
            (bare,)
        ).fetchall()
        
        # 如果 Reddit 源无数据，查全源作为降级
        if not rows:
            rows = conn.execute(
                "SELECT * FROM sentiment WHERE symbol = ? ORDER BY timestamp DESC LIMIT 10",
                (bare,)
            ).fetchall()
        
        conn.close()
        
        if rows:
            titles = []
            scores = []
            for r in rows:
                t = r.get("title", "") or r.get("text", "") or ""
                if t:
                    titles.append(t[:80])
                s = r.get("score", 0)
                if s is not None:
                    try:
                        scores.append(float(s))
                    except (ValueError, TypeError):
                        pass
            
            return {
                "post_count": len(rows),
                "top_topics": titles[:5],
                "avg_score": sum(scores) / len(scores) if scores else 0,
                "source": "social_sentiment_db",
            }
    except Exception as e:
        logger.debug(f"[sentiment] Reddit/DB {symbol} error: {e}")

    return None


async def _fetch_social_sentiment(symbol: str) -> Optional[Dict]:
    """从项目的 social_sentiment 模块获取 (last30days → Reddit/HN/Polymarket)
    
    修复 (2026-06-16): 原路径 app.analysis.social_sentiment 不存在。
    正确路径: app.data.social_sentiment
    """
    try:
        from app.data.social_sentiment import collect_sentiment
        data = await asyncio.get_event_loop().run_in_executor(
            None, lambda: collect_sentiment(symbol.replace("US.", ""))
        )
        if data:
            return data
    except ImportError as e:
        logger.debug(f"[sentiment] social_sentiment import failed: {e}")
    except Exception as e:
        logger.debug(f"[sentiment] social_sentiment error: {e}")

    # 尝试从 social_sentiment.db
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent.parent / "data" / "social_sentiment.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            bare = symbol.replace("US.", "")
            rows = conn.execute(
                "SELECT * FROM sentiment WHERE symbol = ? ORDER BY timestamp DESC LIMIT 5",
                (bare,)
            ).fetchall()
            conn.close()
            if rows:
                return {
                    "recent_count": len(rows),
                    "avg_score": sum(float(r.get("score", 0)) for r in rows) / len(rows),
                }
    except Exception:
        pass

    return None


def _assess_crowding(st_data: Optional[Dict], reddit_data: Optional[Dict]) -> str:
    """评估拥挤度"""
    if not st_data and not reddit_data:
        return "未知"

    # StockTwits watchlist
    watchlist = st_data.get("watchlist_count", 0) if st_data else 0
    msg_count = st_data.get("total_messages", 0) if st_data else 0

    if watchlist > 100000 or msg_count > 25:
        return "拥挤"
    elif watchlist > 50000 or msg_count > 15:
        return "较高"
    elif msg_count > 5:
        return "正常"
    else:
        return "低关注"


def _assess_direction(st_data: Optional[Dict], reddit_data: Optional[Dict], social_data: Optional[Dict]) -> str:
    """评估情绪方向"""
    if st_data:
        return st_data.get("direction", "中性")
    if social_data and isinstance(social_data, dict):
        avg = social_data.get("avg_score", 0)
        if avg > 0.3:
            return "偏多"
        elif avg < -0.3:
            return "偏空"
    return "中性"


def _build_conclusion(result: Dict, target: Dict) -> str:
    """构建情绪结论"""
    parts = []

    direction = result.get("direction", "中性")
    crowding = result.get("crowding", "未知")

    parts.append(f"散户情绪{direction}")
    parts.append(f"拥挤度{crowding}")

    st = result.get("stocktwits", {})
    if st:
        parts.append(f"多空比 {st.get('bullish', 0)}:{st.get('bearish', 0)}")

    return " · ".join(parts)


__all__ = ["run_sentiment_analysis"]
