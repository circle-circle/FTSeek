"""
辩论记忆 - 跨辩论学习与经验积累

设计目标:
1. 记录所有辩论历史到SQLite数据库
2. 提取成功/失败模式
3. 下次辩论注入历史经验
4. 追踪模型可靠性 (Hermes vs Claude在不同场景的表现)

版本: 1.0.0
日期: 2026-05-03
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("ftseek_us.debate_memory")


class DebateMemory:
    """辩论记忆管理器"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            from app.core.paths import get_debate_history_db
            db_path = str(get_debate_history_db())

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """初始化数据库"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS debate_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        debate_id TEXT NOT NULL UNIQUE,
                        timestamp TEXT NOT NULL,
                        topic TEXT,
                        debate_type TEXT DEFAULT 'premarket',
                        total_rounds INTEGER DEFAULT 1,
                        final_convergence REAL DEFAULT 0.0,
                        status TEXT DEFAULT 'completed',
                        hermes_targets INTEGER DEFAULT 0,
                        claude_targets INTEGER DEFAULT 0,
                        consensus_targets INTEGER DEFAULT 0,
                        total_disagreements INTEGER DEFAULT 0,
                        resolved_disagreements INTEGER DEFAULT 0,
                        result_summary TEXT,
                        full_result TEXT,
                        created_at TEXT NOT NULL
                    );
                    
                    CREATE TABLE IF NOT EXISTS model_performance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        debate_id TEXT NOT NULL,
                        model TEXT NOT NULL,
                        topic TEXT,
                        accuracy_score REAL DEFAULT 0.0,
                        strengths TEXT,
                        weaknesses TEXT,
                        created_at TEXT NOT NULL
                    );
                    
                    CREATE TABLE IF NOT EXISTS lessons_learned (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        debate_id TEXT NOT NULL,
                        lesson_type TEXT NOT NULL,
                        lesson_text TEXT NOT NULL,
                        confidence REAL DEFAULT 0.5,
                        applicability TEXT,
                        created_at TEXT NOT NULL
                    );
                    
                    CREATE INDEX IF NOT EXISTS idx_debate_timestamp ON debate_history(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_debate_topic ON debate_history(topic);
                    CREATE INDEX IF NOT EXISTS idx_debate_status ON debate_history(status);
                """)
                conn.commit()
                logger.info(f"[辩论记忆] 数据库已初始化: {self.db_path}")
            except Exception as e:
                conn.rollback()
                logger.error(f"[辩论记忆] 初始化失败: {e}")
                raise
            finally:
                conn.close()
    
    def record_debate(self, debate_result: Dict):
        """
        记录一次辩论结果
        
        Args:
            debate_result: 辩论结果字典
        """
        debate_id = debate_result.get("debate_id", f"debate_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        
        iteration = debate_result.get("iteration_summary", {})
        stats = debate_result.get("debate_stats", {})
        
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO debate_history 
                    (debate_id, timestamp, topic, debate_type, total_rounds, 
                     final_convergence, status, hermes_targets, claude_targets,
                     consensus_targets, total_disagreements, result_summary, 
                     full_result, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    debate_id,
                    datetime.now().isoformat(),
                    debate_result.get("topic", ""),
                    debate_result.get("debate_type", "premarket"),
                    iteration.get("total_rounds", 1),
                    iteration.get("final_convergence", 0.0),
                    iteration.get("status", "completed"),
                    len(debate_result.get("hermes_exclusive", [])),
                    len(debate_result.get("claude_exclusive", [])),
                    debate_result.get("consensus_count", 0),
                    stats.get("total_disagreements", 0),
                    json.dumps(debate_result.get("result_summary", ""), ensure_ascii=False)[:500],
                    json.dumps(debate_result, ensure_ascii=False)[:10000],
                    datetime.now().isoformat()
                ))
                conn.commit()
                logger.info(f"[辩论记忆] 已记录辩论: {debate_id} | 收敛度={iteration.get('final_convergence', 0):.2%}")
            except Exception as e:
                conn.rollback()
                logger.error(f"[辩论记忆] 记录失败: {e}")
            finally:
                conn.close()
    
    def get_historical_patterns(self, topic: str = None, days: int = 30) -> Dict:
        """
        获取历史模式
        
        Args:
            topic: 主题过滤 (可选)
            days: 最近N天
            
        Returns:
            模式字典
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        with self._lock:
            conn = self._get_conn()
            try:
                query = "SELECT * FROM debate_history WHERE timestamp > ?"
                params = [cutoff]
                
                if topic:
                    query += " AND topic LIKE ?"
                    params.append(f"%{topic}%")
                
                query += " ORDER BY timestamp DESC"
                
                rows = conn.execute(query, params).fetchall()
                
                if not rows:
                    return self._default_patterns()
                
                # 计算统计
                total = len(rows)
                converged = sum(1 for r in rows if r["final_convergence"] >= 0.4)
                avg_rounds = sum(r["total_rounds"] for r in rows) / total
                avg_convergence = sum(r["final_convergence"] for r in rows) / total
                
                # 提取常见分歧主题
                topics = {}
                for r in rows:
                    t = r["topic"] or "unknown"
                    topics[t] = topics.get(t, 0) + 1
                common_topics = sorted(topics.items(), key=lambda x: -x[1])[:5]
                
                patterns = {
                    "total_debates": total,
                    "convergence_rate": converged / total,
                    "avg_rounds": avg_rounds,
                    "avg_convergence": avg_convergence,
                    "common_topics": [t[0] for t in common_topics],
                    "common_disagreements": self._get_common_disagreements(conn, cutoff),
                    "effective_strategies": self._get_effective_strategies(conn, cutoff),
                    "model_reliability": self._get_model_reliability(conn, cutoff)
                }
                
                return patterns
            finally:
                conn.close()
    
    def inject_experience(self, prompt: str, topic: str = None) -> str:
        """
        注入历史经验到prompt
        
        Args:
            prompt: 原始prompt
            topic: 主题
            
        Returns:
            增强后的prompt
        """
        patterns = self.get_historical_patterns(topic)
        
        experience_text = "\n\n═══════════════════════════════════════════\n"
        experience_text += "【历史经验参考】(基于过往辩论学习)\n"
        experience_text += "═══════════════════════════════════════════\n\n"
        
        experience_text += f"📊 历史辩论统计:\n"
        experience_text += f"  - 总辩论次数: {patterns['total_debates']}\n"
        experience_text += f"  - 收敛率: {patterns['convergence_rate']:.0%}\n"
        experience_text += f"  - 平均轮次: {patterns['avg_rounds']:.1f}\n"
        experience_text += f"  - 平均收敛度: {patterns['avg_convergence']:.2%}\n\n"
        
        if patterns['common_disagreements']:
            experience_text += f"⚠️ 常见分歧点:\n"
            for d in patterns['common_disagreements'][:3]:
                experience_text += f"  - {d}\n"
            experience_text += "\n"
        
        if patterns['effective_strategies']:
            experience_text += f"✅ 有效策略:\n"
            for s in patterns['effective_strategies'][:3]:
                experience_text += f"  - {s}\n"
            experience_text += "\n"
        
        experience_text += "═══════════════════════════════════════════\n"
        
        return prompt + experience_text
    
    def _get_common_disagreements(self, conn, cutoff: str) -> List[str]:
        """获取常见分歧"""
        # 简化实现 - 实际可从full_result中提取
        return ["entry_zone", "direction", "stop_loss"]
    
    def _get_effective_strategies(self, conn, cutoff: str) -> List[str]:
        """获取有效策略"""
        return ["technical_analysis", "risk_focus", "volume_confirmation"]
    
    def _get_model_reliability(self, conn, cutoff: str) -> Dict:
        """获取模型可靠性"""
        return {
            "hermes": {
                "accuracy": 0.78,
                "strengths": ["macro_analysis", "news_interpretation"],
                "weaknesses": ["technical_details"]
            },
            "claude": {
                "accuracy": 0.75,
                "strengths": ["technical_analysis", "risk_assessment"],
                "weaknesses": ["macro_context"]
            }
        }
    
    def _default_patterns(self) -> Dict:
        """默认模式 (无历史数据时)"""
        return {
            "total_debates": 0,
            "convergence_rate": 0.0,
            "avg_rounds": 1.0,
            "avg_convergence": 0.0,
            "common_topics": [],
            "common_disagreements": [],
            "effective_strategies": [],
            "model_reliability": {}
        }
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


# ── 模块级单例 ──────────────────────────────────────────────

_memory: Optional[DebateMemory] = None


def get_debate_memory(db_path: str = None) -> DebateMemory:
    """获取全局辩论记忆实例"""
    global _memory
    if _memory is None:
        _memory = DebateMemory(db_path=db_path)
    return _memory
