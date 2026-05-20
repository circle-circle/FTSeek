"""
知识存储系统 (Knowledge Store)

职责:
1. 统一存储 EOD 复盘、交易教训、次日预判、市场规律
2. 支持跨日期查询和聚合分析
3. 自动过期清理（默认 90 天）
4. 可进化的知识积累（教训→规律→规则）

数据存储: data/knowledge.db (SQLite)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

logger = logging.getLogger("ftseek.review.knowledge_store")

KNOWLEDGE_DB = "data/knowledge.db"


class KnowledgeStore:
    """知识存储 — 统一的知识沉淀/查询/积累系统"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            KNOWLEDGE_DB,
        )
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 1. 交易教训 (lessons)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                category TEXT NOT NULL,
                stock_code TEXT,
                title TEXT,
                content TEXT,
                pnl_impact REAL,
                confidence REAL,
                recurring_count INTEGER DEFAULT 1,
                tags TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                validated INTEGER DEFAULT 0
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_date ON lessons(date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_stock ON lessons(stock_code)")

        # 2. 市场规律 (patterns)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                evidence_count INTEGER DEFAULT 1,
                first_seen TEXT,
                last_seen TEXT,
                tags TEXT DEFAULT '[]',
                confidence REAL DEFAULT 0.5,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_patterns_category ON patterns(category)")

        # 3. 次日预判历史 (predictions)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                bias TEXT,
                key_question TEXT,
                risks TEXT DEFAULT '[]',
                watchlist TEXT DEFAULT '[]',
                actual_outcome TEXT,
                accuracy_score REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(date)")

        # 4. 复盘摘要 (eod_summaries)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS eod_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                total_trades INTEGER,
                win_rate REAL,
                total_pnl REAL,
                best_trade TEXT,
                worst_trade TEXT,
                lessons_count INTEGER,
                regime TEXT,
                summary TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_eod_date ON eod_summaries(date)")

        conn.commit()
        conn.close()

        logger.info(f"[KnowledgeStore] 初始化完成: {self.db_path}")

    # ──────────────────────────────────────────────────────
    #  教训存储与查询
    # ──────────────────────────────────────────────────────

    def save_lesson(self, date: str, category: str, title: str,
                    content: str, stock_code: str = None,
                    pnl_impact: float = None, confidence: float = None,
                    tags: List[str] = None) -> int:
        """存储一条交易教训"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 检查是否已存在相似教训 (同日期同股票同类别)
        if stock_code:
            cursor.execute(
                "SELECT id, recurring_count FROM lessons "
                "WHERE stock_code=? AND category=? AND date BETWEEN ? AND ?",
                (stock_code, category, date, date)
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    "UPDATE lessons SET recurring_count = recurring_count + 1, "
                    "content = ?, title = ? "
                    "WHERE id = ?",
                    (content, title, existing[0])
                )
                conn.commit()
                conn.close()
                logger.info(f"[KnowledgeStore] 教训已更新(重复): {stock_code} - {title}")
                return existing[0]

        cursor.execute("""
            INSERT INTO lessons (date, category, stock_code, title, content,
                                 pnl_impact, confidence, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date, category, stock_code, title, content,
            pnl_impact, confidence, json.dumps(tags or [])
        ))
        lesson_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"[KnowledgeStore] 教训已存储: {lesson_id} - {title}")
        return lesson_id

    def get_lessons(self, date_from: str = None, date_to: str = None,
                    category: str = None, stock_code: str = None,
                    limit: int = 20) -> List[Dict]:
        """查询教训 (支持多维度过滤)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM lessons WHERE 1=1"
        params = []

        if date_from:
            query += " AND date >= ?"
            params.append(date_from)
        if date_to:
            query += " AND date <= ?"
            params.append(date_to)
        if category:
            query += " AND category = ?"
            params.append(category)
        if stock_code:
            query += " AND stock_code = ?"
            params.append(stock_code)

        query += " ORDER BY date DESC, recurring_count DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]

        # 解析 JSON 字段
        for r in results:
            try:
                r['tags'] = json.loads(r.get('tags', '[]'))
            except Exception:
                r['tags'] = []

        conn.close()
        return results

    def get_recurring_patterns(self, min_count: int = 2, limit: int = 10) -> List[Dict]:
        """获取重复出现的教训模式 (recurring_count >= min_count)"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT category, stock_code, title, content,
                   MAX(pnl_impact) as max_pnl_impact,
                   SUM(recurring_count) as total_occurrences,
                   MIN(date) as first_seen,
                   MAX(date) as last_seen
            FROM lessons
            GROUP BY category, COALESCE(stock_code, ''), title
            HAVING SUM(recurring_count) >= ?
            ORDER BY total_occurrences DESC, max_pnl_impact ASC
            LIMIT ?
        """, (min_count, limit))

        results = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return results

    # ──────────────────────────────────────────────────────
    #  次日预判
    # ──────────────────────────────────────────────────────

    def save_prediction(self, date: str, bias: str, key_question: str,
                        risks: List[str] = None, watchlist: List[str] = None) -> int:
        """存储次日预判"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO predictions
                (date, bias, key_question, risks, watchlist)
            VALUES (?, ?, ?, ?, ?)
        """, (
            date, bias, key_question,
            json.dumps(risks or []),
            json.dumps(watchlist or [])
        ))
        pred_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"[KnowledgeStore] 预判已存储: {date} - {bias}")
        return pred_id

    def update_prediction_outcome(self, date: str, actual_outcome: str,
                                   accuracy_score: float):
        """更新预判的实际结果"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE predictions SET actual_outcome = ?, accuracy_score = ?
            WHERE date = ?
        """, (actual_outcome, accuracy_score, date))
        conn.commit()
        conn.close()

        logger.info(f"[KnowledgeStore] 预判结果已更新: {date} - score={accuracy_score}")

    def get_recent_predictions(self, limit: int = 5) -> List[Dict]:
        """获取最近的预判"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM predictions ORDER BY date DESC LIMIT ?",
            (limit,)
        )
        results = [dict(row) for row in cursor.fetchall()]

        for r in results:
            for field in ['risks', 'watchlist']:
                try:
                    r[field] = json.loads(r.get(field, '[]'))
                except Exception:
                    r[field] = []

        conn.close()
        return results

    # ──────────────────────────────────────────────────────
    #  复盘摘要
    # ──────────────────────────────────────────────────────

    def save_eod_summary(self, date: str, total_trades: int,
                          win_rate: float, total_pnl: float,
                          best_trade: str = None, worst_trade: str = None,
                          lessons_count: int = 0, regime: str = None,
                          summary: str = None) -> int:
        """存储 EOD 复盘摘要"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO eod_summaries
                (date, total_trades, win_rate, total_pnl,
                 best_trade, worst_trade, lessons_count, regime, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date, total_trades, win_rate, total_pnl,
            best_trade, worst_trade, lessons_count, regime, summary
        ))
        summary_id = cursor.lastrowid
        conn.commit()
        conn.close()

        logger.info(f"[KnowledgeStore] 复盘摘要已存储: {date}")
        return summary_id

    def get_stats(self, days: int = 30) -> Dict:
        """获取近 N 天统计"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        # 复盘统计
        cursor.execute("""
            SELECT COUNT(*) as total_days,
                   AVG(win_rate) as avg_win_rate,
                   SUM(total_pnl) as total_pnl,
                   AVG(total_trades) as avg_trades_per_day,
                   SUM(lessons_count) as total_lessons
            FROM eod_summaries
            WHERE date >= ?
        """, (cutoff,))
        review_stats = dict(cursor.fetchone())

        # 教训分类统计
        cursor.execute("""
            SELECT category, COUNT(*) as count, AVG(recurring_count) as avg_recurring
            FROM lessons
            WHERE date >= ?
            GROUP BY category
            ORDER BY count DESC
        """, (cutoff,))
        category_stats = [dict(row) for row in cursor.fetchall()]

        # 预判准确率
        cursor.execute("""
            SELECT AVG(accuracy_score) as avg_accuracy,
                   COUNT(*) as total_predictions
            FROM predictions
            WHERE date >= ? AND accuracy_score IS NOT NULL
        """, (cutoff,))
        pred_stats = dict(cursor.fetchone())

        conn.close()

        return {
            "period_days": days,
            "review": review_stats,
            "lesson_categories": category_stats,
            "prediction_accuracy": pred_stats,
        }

    # ──────────────────────────────────────────────────────
    #  清理
    # ──────────────────────────────────────────────────────

    def cleanup(self, days: int = 90):
        """清理过期数据 (默认 90 天)"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        for table in ['lessons', 'predictions', 'eod_summaries']:
            cursor.execute(f"DELETE FROM {table} WHERE date < ?", (cutoff,))
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"[KnowledgeStore] 清理 {table}: {deleted} 条过期记录")

        conn.commit()
        conn.close()


# 全局单例
_knowledge_store: Optional[KnowledgeStore] = None


def get_knowledge_store(db_path: str = None) -> KnowledgeStore:
    global _knowledge_store
    if _knowledge_store is None or db_path:
        _knowledge_store = KnowledgeStore(db_path)
    return _knowledge_store
