"""
新闻信号归因 (L1 配套, 2026-05-14)

新闻分类 + 信号生成后, T+24h 回看:
- primary_tickers 实际涨跌
- 信号若已下单, 实际 PnL
- 命中率统计 (event_type × direction × credibility 维度)

写入 news_attribution.db, 月度报告分析"哪类新闻最赚".
"""
from __future__ import annotations
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _db_path() -> str:
    from app.core.paths import get_data_dir
    return str(get_data_dir() / "news_attribution.db")


def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_attribution (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id         TEXT NOT NULL,
            event_type      TEXT,
            ticker          TEXT,
            classified_at   REAL,
            sentiment       REAL,
            credibility     REAL,
            action_hint     TEXT,
            -- T+ 实测
            price_at_news   REAL,
            price_t_5m      REAL,
            price_t_15m     REAL,
            price_t_60m     REAL,
            price_t_24h     REAL,
            actual_return_5m   REAL,
            actual_return_15m  REAL,
            actual_return_60m  REAL,
            actual_return_24h  REAL,
            -- 信号是否成交 + PnL
            signal_executed   INTEGER DEFAULT 0,
            executed_pnl      REAL,
            executed_pnl_pct  REAL,
            -- 命中评估 (sentiment 方向是否对)
            hit_5m   INTEGER,
            hit_15m  INTEGER,
            hit_60m  INTEGER,
            hit_24h  INTEGER,
            evaluated_at REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_natt_event ON news_attribution(event_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_natt_ticker ON news_attribution(ticker)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_natt_news ON news_attribution(news_id)")


def record_baseline(
    classification,
    price_at_news: Dict[str, float],
):
    """新闻分类时立即记入 baseline (实测价格留空, 后续 T+ 填)"""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        for ticker in classification.primary_tickers:
            p0 = price_at_news.get(ticker, 0.0)
            conn.execute(
                "INSERT INTO news_attribution "
                "(news_id, event_type, ticker, classified_at, sentiment, credibility, "
                " action_hint, price_at_news) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    classification.news_id,
                    classification.event_type,
                    ticker,
                    classification.classified_at,
                    classification.sentiment,
                    classification.credibility,
                    classification.action_hint,
                    p0,
                ),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[NewsAttribution·baseline] 失败: {e}")


def evaluate_pending(get_price_fn, max_age_hours: int = 36) -> int:
    """
    回看所有未评估的 news_attribution 行, 取当前价填实测.
    必须给一个 callable get_price_fn(symbol) -> float

    Returns: 评估笔数
    """
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        now = time.time()
        rows = conn.execute(
            "SELECT id, ticker, classified_at, price_at_news, sentiment, "
            "       price_t_5m, price_t_15m, price_t_60m, price_t_24h "
            "FROM news_attribution WHERE evaluated_at IS NULL "
            "AND classified_at > ?",
            (now - max_age_hours * 3600,),
        ).fetchall()
        evaluated = 0
        for row in rows:
            rid, ticker, cls_ts, p0, sentiment, p5, p15, p60, p24 = row
            if not ticker or not p0 or p0 <= 0:
                continue
            age_min = (now - cls_ts) / 60
            updates = {}
            if age_min >= 5 and p5 is None:
                p_now = get_price_fn(ticker)
                if p_now and p_now > 0:
                    updates["price_t_5m"] = p_now
                    updates["actual_return_5m"] = (p_now - p0) / p0
                    expected_dir = 1 if sentiment > 0 else (-1 if sentiment < 0 else 0)
                    actual_dir = 1 if p_now > p0 else (-1 if p_now < p0 else 0)
                    updates["hit_5m"] = int(expected_dir == actual_dir and expected_dir != 0)
            if age_min >= 15 and p15 is None:
                p_now = get_price_fn(ticker)
                if p_now and p_now > 0:
                    updates["price_t_15m"] = p_now
                    updates["actual_return_15m"] = (p_now - p0) / p0
                    expected_dir = 1 if sentiment > 0 else (-1 if sentiment < 0 else 0)
                    actual_dir = 1 if p_now > p0 else (-1 if p_now < p0 else 0)
                    updates["hit_15m"] = int(expected_dir == actual_dir and expected_dir != 0)
            if age_min >= 60 and p60 is None:
                p_now = get_price_fn(ticker)
                if p_now and p_now > 0:
                    updates["price_t_60m"] = p_now
                    updates["actual_return_60m"] = (p_now - p0) / p0
                    expected_dir = 1 if sentiment > 0 else (-1 if sentiment < 0 else 0)
                    actual_dir = 1 if p_now > p0 else (-1 if p_now < p0 else 0)
                    updates["hit_60m"] = int(expected_dir == actual_dir and expected_dir != 0)
            if age_min >= 1440 and p24 is None:
                p_now = get_price_fn(ticker)
                if p_now and p_now > 0:
                    updates["price_t_24h"] = p_now
                    updates["actual_return_24h"] = (p_now - p0) / p0
                    expected_dir = 1 if sentiment > 0 else (-1 if sentiment < 0 else 0)
                    actual_dir = 1 if p_now > p0 else (-1 if p_now < p0 else 0)
                    updates["hit_24h"] = int(expected_dir == actual_dir and expected_dir != 0)
                # 24h 后标记 evaluated
                updates["evaluated_at"] = now
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE news_attribution SET {set_clause} WHERE id=?",
                    (*updates.values(), rid),
                )
                evaluated += 1
        conn.commit()
        conn.close()
        if evaluated > 0:
            logger.info(f"[NewsAttribution·evaluate] 更新 {evaluated} 笔")
        return evaluated
    except Exception as e:
        logger.debug(f"[NewsAttribution·evaluate] 失败: {e}")
        return 0


def get_event_type_stats(window_days: int = 30) -> List[Dict[str, Any]]:
    """按 event_type 分组的命中率统计 (供 daily_review)"""
    try:
        conn = sqlite3.connect(_db_path())
        _ensure_schema(conn)
        rows = conn.execute(f"""
            SELECT event_type,
                   COUNT(*) as n,
                   AVG(hit_60m) as hit_rate_60m,
                   AVG(hit_24h) as hit_rate_24h,
                   AVG(actual_return_60m) as avg_ret_60m,
                   AVG(actual_return_24h) as avg_ret_24h
            FROM news_attribution
            WHERE classified_at > ?
            GROUP BY event_type
            ORDER BY n DESC
        """, (time.time() - window_days * 86400,)).fetchall()
        conn.close()
        return [
            {
                "event_type": r[0],
                "n": r[1],
                "hit_rate_60m": r[2],
                "hit_rate_24h": r[3],
                "avg_ret_60m": r[4],
                "avg_ret_24h": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []
