"""
Social Sentiment Collector for FTSeek-US
Calls last30days-skill as subprocess to avoid Python env conflicts.
Caches results in SQLite for dashboard integration.
"""
import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek_us.social_sentiment")

# Paths — env-overridable. last30days-skill is an external subproject that may
# live elsewhere or be absent on this host; _run_last30days skips gracefully
# when the entry script is missing (set LAST30DAYS_SCRIPT / LAST30DAYS_ROOT to override).
LAST30DAYS_SCRIPT = os.environ.get(
    "LAST30DAYS_SCRIPT",
    "/root/last30days-skill/skills/last30days/scripts/last30days.py",
)
LAST30DAYS_ROOT = os.environ.get(
    "LAST30DAYS_ROOT",
    "/root/last30days-skill",
)
FTSEEK_ROOT = Path("/opt/ftseek/FTSeek-US")
CACHE_DB = FTSEEK_ROOT / "data" / "social_sentiment.db"
UV_BIN = "uv"  # uv manages the last30days venv

# Cache 4 hours
CACHE_TTL = 3600 * 4

# Company name mapping for better search
NAME_MAP = {
    "BABA": "Alibaba", "NVDA": "Nvidia", "AAPL": "Apple",
    "TSLA": "Tesla", "MU": "Micron", "META": "Meta Platforms",
    "AMD": "AMD", "MSFT": "Microsoft", "GOOG": "Google",
    "GOOGL": "Google", "AMZN": "Amazon", "SNDK": "Western Digital",
}


def _init_db():
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_cache (
            symbol TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _get_cached(symbol: str) -> Optional[dict]:
    try:
        conn = sqlite3.connect(str(CACHE_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT data_json, fetched_at FROM sentiment_cache WHERE symbol = ?",
            (symbol,)
        ).fetchone()
        conn.close()
        if row and (time.time() - row["fetched_at"]) < CACHE_TTL:
            return json.loads(row["data_json"])
    except Exception:
        pass
    return None


def _save_cache(symbol: str, data: dict):
    try:
        conn = sqlite3.connect(str(CACHE_DB))
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_cache (symbol, data_json, fetched_at) VALUES (?, ?, ?)",
            (symbol, json.dumps(data), time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to cache sentiment for {symbol}: {e}")


def _run_last30days(query: str, sources: str = "reddit,hackernews,polymarket", days: int = 7) -> dict:
    """Run last30days as subprocess, return parsed JSON."""
    # Graceful skip when the external skill isn't deployed on this host.
    # Avoids repeated subprocess failures + ERROR noise; returns {} so callers
    # fall back to other sentiment sources. Set LAST30DAYS_SCRIPT to enable.
    if not LAST30DAYS_SCRIPT or not os.path.exists(LAST30DAYS_SCRIPT):
        logger.warning(
            "last30days skill not found at %s (set LAST30DAYS_SCRIPT env to enable); skipping",
            LAST30DAYS_SCRIPT or "<empty>",
        )
        return {}
    cmd = [
        UV_BIN, "run", "python3",
        LAST30DAYS_SCRIPT,
        query,
        f"--search={sources}",
        f"--days={days}",
        "--emit=json",
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=LAST30DAYS_ROOT,
            env={
                **{k: v for k, v in __import__('os').environ.items() 
                   if k not in ('VIRTUAL_ENV', 'PYTHONPATH')},
            }
        )
        
        if result.returncode != 0:
            logger.warning(f"last30days stderr: {result.stderr[:200]}")
        
        # Parse JSON from stdout (skip warnings/non-JSON lines)
        stdout = result.stdout.strip()
        # Find the JSON object
        brace_start = stdout.find('{')
        if brace_start >= 0:
            json_str = stdout[brace_start:]
            return json.loads(json_str)
        
    except subprocess.TimeoutExpired:
        logger.warning(f"last30days timed out for: {query}")
    except json.JSONDecodeError:
        logger.warning(f"last30days JSON parse failed for: {query}")
    except Exception as e:
        logger.error(f"last30days failed: {e}")
    
    return {}


def _extract_items(result: dict) -> dict:
    """Extract structured items from last30days JSON output."""
    items_by_source = result.get("items_by_source", {})
    
    reddit_items = []
    for item in items_by_source.get("reddit", []):
        reddit_items.append({
            "title": item.get("title", ""),
            "subreddit": item.get("subreddit", ""),
            "url": item.get("url", ""),
            "date": item.get("date", ""),
            "snippet": item.get("evidence", item.get("snippet", ""))[:200],
            "score": item.get("score", 0),
        })
    
    hn_items = []
    for item in items_by_source.get("hackernews", []):
        hn_items.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "date": item.get("date", ""),
            "score": item.get("score", 0),
            "comments": item.get("comments", item.get("num_comments", 0)),
            "snippet": item.get("evidence", "")[:200],
        })
    
    pm_items = []
    for item in items_by_source.get("polymarket", []):
        pm_items.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "odds": item.get("odds", item.get("probability", 0)),
            "volume": item.get("volume", 0),
            "change_pct": item.get("change_pct", item.get("one_month_change", 0)),
        })
    
    return {
        "reddit": reddit_items,
        "hackernews": hn_items,
        "polymarket": pm_items,
    }


def collect_sentiment(symbol: str, force_refresh: bool = False) -> dict:
    """Collect social sentiment for a stock symbol."""
    short = symbol.replace("US.", "")
    
    if not force_refresh:
        cached = _get_cached(short)
        if cached:
            return cached
    
    name = NAME_MAP.get(short, short)
    # Use "Company SYMBOL" format - best HN/Reddit hit rate
    query = f"{name} {short}"
    
    # Run last30days with 14-day window for better coverage
    result = _run_last30days(query, days=14)
    
    if not result:
        data = {
            "symbol": short,
            "reddit": [],
            "hackernews": [],
            "polymarket": [],
            "summary": {
                "reddit_count": 0,
                "hn_count": 0,
                "polymarket_count": 0,
                "total_signals": 0,
                "hot_topics": [],
            },
            "fetched_at": time.time(),
            "error": "no data returned",
        }
        _save_cache(short, data)
        return data
    
    items = _extract_items(result)
    
    # Build summary
    hot_topics = []
    for r in items["reddit"][:2]:
        if r.get("title"):
            hot_topics.append(f"[{r.get('subreddit','')}] {r['title'][:70]}")
    for s in items["hackernews"][:1]:
        if s.get("title"):
            hot_topics.append(f"[HN ⬆{s.get('score',0)}] {s['title'][:70]}")
    for m in items["polymarket"][:1]:
        if m.get("title"):
            hot_topics.append(f"[🎲] {m['title'][:70]}")
    
    data = {
        "symbol": short,
        "reddit": items["reddit"],
        "hackernews": items["hackernews"],
        "polymarket": items["polymarket"],
        "summary": {
            "reddit_count": len(items["reddit"]),
            "hn_count": len(items["hackernews"]),
            "polymarket_count": len(items["polymarket"]),
            "total_signals": len(items["reddit"]) + len(items["hackernews"]) + len(items["polymarket"]),
            "hot_topics": hot_topics[:3],
        },
        "fetched_at": time.time(),
    }
    
    _save_cache(short, data)
    return data


def collect_batch(symbols: List[str], force_refresh: bool = False) -> Dict[str, dict]:
    """Collect sentiment for multiple symbols."""
    _init_db()
    results = {}
    for sym in symbols:
        short = sym.replace("US.", "")
        try:
            results[short] = collect_sentiment(short, force_refresh)
        except Exception as e:
            logger.error(f"Sentiment collection failed for {short}: {e}")
            results[short] = {"symbol": short, "error": str(e)}
    return results


if __name__ == "__main__":
    """CLI test."""
    import sys
    logging.basicConfig(level=logging.INFO)
    
    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["BABA", "NVDA"]
    results = collect_batch(symbols)
    
    for sym, data in results.items():
        summary = data.get("summary", {})
        print(f"\n{'='*60}")
        print(f"📊 {sym}")
        print(f"  Reddit: {summary.get('reddit_count', 0)} posts")
        print(f"  HN: {summary.get('hn_count', 0)} stories")
        print(f"  Polymarket: {summary.get('polymarket_count', 0)} markets")
        print(f"  Hot topics: {summary.get('hot_topics', [])}")
        
        for r in data.get("reddit", [])[:3]:
            print(f"  📌 [{r.get('subreddit','')}] {r.get('title','')[:70]}")
        for s in data.get("hackernews", [])[:3]:
            print(f"  🔶 [HN ⬆{s.get('score',0)}] {s.get('title','')[:70]}")
        for m in data.get("polymarket", [])[:3]:
            print(f"  🎲 [PM] {m.get('title','')[:70]}")
