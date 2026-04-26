"""
否决审计存储 (Veto Audit Store)

P1/P2 核心模块：记录所有被系统拦截（否决）的信号。
用于周复盘分析“冤死率”（False Positive Rate），调优阈值。
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger("ftseek.storage.veto_audit_store")

class VetoAuditStore:
    def __init__(self, db_path: str = "data/veto_audit.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vetoed_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        code TEXT,
                        action TEXT,
                        reason TEXT,
                        original_strength REAL,
                        news_sentiment REAL,
                        risk_level TEXT,
                        outcome_recorded INTEGER DEFAULT 0, -- 0:未记录结果, 1:盈利, 2:亏损
                        outcome_pnl REAL,
                        notes TEXT
                    )
                """)
                conn.commit()
                logger.info("VetoAuditStore 初始化完成")
            except Exception as e:
                logger.error(f"VetoAuditStore 初始化失败: {e}")
            finally:
                conn.close()

    def record_veto(self, code: str, action: str, reason: str, 
                    strength: float = 0.0, sentiment: float = 0.0, 
                    risk_level: str = "UNKNOWN"):
        """
        记录一次否决事件。
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                conn.execute("""
                    INSERT INTO vetoed_signals 
                    (timestamp, code, action, reason, original_strength, news_sentiment, risk_level)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    datetime.now().isoformat(),
                    code, action, reason, strength, sentiment, risk_level
                ))
                conn.commit()
                conn.close()
                logger.info(f"[冤死记录] {code} {action} 被否决: {reason}")
        except Exception as e:
            logger.error(f"记录否决失败: {e}")

    def record_outcome(self, signal_id: int, pnl: float):
        """
        事后诸葛：记录如果当时放行，结果会怎样。
        pnl > 0 表示如果放行会盈利 (冤杀！)
        pnl < 0 表示如果放行会亏损 (否决正确)
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                outcome = 1 if pnl > 0 else 2
                conn.execute("""
                    UPDATE vetoed_signals 
                    SET outcome_recorded = ?, outcome_pnl = ?
                    WHERE id = ?
                """, (outcome, pnl, signal_id))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"记录结果失败: {e}")

    def get_false_positive_report(self) -> Dict[str, Any]:
        """
        生成周复盘报表：统计冤死率。
        """
        try:
            with self._lock:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_vetoes,
                        SUM(CASE WHEN outcome_recorded = 1 THEN 1 ELSE 0 END) as false_positives,
                        AVG(CASE WHEN outcome_recorded = 1 THEN outcome_pnl END) as avg_missed_profit
                    FROM vetoed_signals
                """)
                row = cursor.fetchone()
                conn.close()
                
                total = row[0] or 0
                fp = row[1] or 0
                rate = (fp / total) if total > 0 else 0
                
                return {
                    "total_vetoes": total,
                    "false_positives": fp,
                    "false_positive_rate": f"{rate:.2%}",
                    "avg_missed_profit": row[2] or 0.0
                }
        except Exception as e:
            logger.error(f"生成报表失败: {e}")
            return {"error": str(e)}
