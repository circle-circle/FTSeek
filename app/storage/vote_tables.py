"""
投票日志数据库表定义

提供 vote_logs 和 decision_logs 表的创建和管理
"""
import sqlite3
import threading
import logging
from datetime import datetime
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class VoteTables:
    """
    投票日志数据库表管理
    
    Schema:
        vote_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        TEXT NOT NULL,        -- 交易 ID
            agent_id        TEXT NOT NULL,        -- Agent ID
            vote_direction  TEXT NOT NULL,        -- LONG / SHORT / ABSTAIN
            vote_weight     REAL NOT NULL,        -- 投票权重
            vote_strength   INTEGER,              -- 投票强度 (1-5)
            vote_confidence REAL,                 -- 置信度 (0-1)
            vote_reason     TEXT,                 -- 投票理由
            created_at      TEXT NOT NULL
        )
        
        decision_logs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id            TEXT NOT NULL,
            symbol              TEXT NOT NULL,
            final_decision      TEXT NOT NULL,    -- LONG / SHORT / NO_CONSENSUS
            consensus_level     REAL NOT NULL,    -- 共识水平 (0-1)
            total_weight        REAL NOT NULL,    -- 总权重
            long_weight         REAL NOT NULL,    -- 多头权重
            short_weight        REAL NOT NULL,    -- 空头权重
            contributing_agents INTEGER NOT NULL, -- 参与 Agent 数量
            decision_confidence REAL,             -- 决策置信度
            market_regime       TEXT,             -- 市场体制
            created_at          TEXT NOT NULL
        )
    """
    
    def __init__(self, db_path: str):
        """
        初始化投票表
        
        Args:
            db_path: SQLite 数据库路径
        """
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # P0 修复: 启用 WAL 模式
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    
    def _init_db(self):
        """初始化数据库表（幂等）"""
        with self._lock:
            conn = self._get_conn()
            try:
                # 创建投票日志表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS vote_logs (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id        TEXT NOT NULL,
                        agent_id        TEXT NOT NULL,
                        vote_direction  TEXT NOT NULL,
                        vote_weight     REAL NOT NULL,
                        vote_strength   INTEGER,
                        vote_confidence REAL,
                        vote_reason     TEXT,
                        created_at      TEXT NOT NULL
                    )
                """)
                
                # 创建索引
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vote_trade ON vote_logs(trade_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vote_agent ON vote_logs(agent_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_vote_created ON vote_logs(created_at)")
                
                # 创建决策日志表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS decision_logs (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id            TEXT NOT NULL,
                        symbol              TEXT NOT NULL,
                        final_decision      TEXT NOT NULL,
                        consensus_level     REAL NOT NULL,
                        total_weight        REAL NOT NULL,
                        long_weight         REAL NOT NULL,
                        short_weight        REAL NOT NULL,
                        contributing_agents INTEGER NOT NULL,
                        decision_confidence REAL,
                        market_regime       TEXT,
                        created_at          TEXT NOT NULL
                    )
                """)
                
                # 创建索引
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_trade ON decision_logs(trade_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_symbol ON decision_logs(symbol)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_created ON decision_logs(created_at)")
                
                conn.commit()
                logger.info(f"投票日志表初始化完成：{self.db_path}")
            except Exception as e:
                conn.rollback()
                logger.error(f"初始化投票表失败：{e}")
                raise
            finally:
                conn.close()
    
    def record_vote(
        self,
        trade_id: str,
        agent_id: str,
        vote_direction: str,
        vote_weight: float,
        vote_strength: Optional[int] = None,
        vote_confidence: Optional[float] = None,
        vote_reason: Optional[str] = None,
    ) -> int:
        """
        记录投票
        
        Args:
            trade_id: 交易 ID
            agent_id: Agent ID
            vote_direction: 投票方向 (LONG/SHORT/ABSTAIN)
            vote_weight: 投票权重
            vote_strength: 投票强度 (1-5)
            vote_confidence: 置信度 (0-1)
            vote_reason: 投票理由
        
        Returns:
            插入的记录 ID
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute("""
                    INSERT INTO vote_logs 
                    (trade_id, agent_id, vote_direction, vote_weight, 
                     vote_strength, vote_confidence, vote_reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    agent_id,
                    vote_direction,
                    vote_weight,
                    vote_strength,
                    vote_confidence,
                    vote_reason,
                    datetime.now().isoformat(),
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                conn.rollback()
                logger.error(f"记录投票失败：{e}")
                raise
            finally:
                conn.close()
    
    def record_decision(
        self,
        trade_id: str,
        symbol: str,
        final_decision: str,
        consensus_level: float,
        total_weight: float,
        long_weight: float,
        short_weight: float,
        contributing_agents: int,
        decision_confidence: Optional[float] = None,
        market_regime: Optional[str] = None,
    ) -> int:
        """
        记录决策
        
        Args:
            trade_id: 交易 ID
            symbol: 股票代码
            final_decision: 最终决策 (LONG/SHORT/NO_CONSENSUS)
            consensus_level: 共识水平 (0-1)
            total_weight: 总权重
            long_weight: 多头权重
            short_weight: 空头权重
            contributing_agents: 参与 Agent 数量
            decision_confidence: 决策置信度
            market_regime: 市场体制
        
        Returns:
            插入的记录 ID
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute("""
                    INSERT INTO decision_logs 
                    (trade_id, symbol, final_decision, consensus_level,
                     total_weight, long_weight, short_weight, 
                     contributing_agents, decision_confidence, market_regime, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    symbol,
                    final_decision,
                    consensus_level,
                    total_weight,
                    long_weight,
                    short_weight,
                    contributing_agents,
                    decision_confidence,
                    market_regime,
                    datetime.now().isoformat(),
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                conn.rollback()
                logger.error(f"记录决策失败：{e}")
                raise
            finally:
                conn.close()
    
    def query_votes_by_trade(self, trade_id: str) -> list:
        """
        按交易 ID 查询投票
        
        Args:
            trade_id: 交易 ID
        
        Returns:
            投票记录列表
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT id, trade_id, agent_id, vote_direction, vote_weight,
                       vote_strength, vote_confidence, vote_reason, created_at
                FROM vote_logs
                WHERE trade_id = ?
                ORDER BY created_at
            """, (trade_id,))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def query_votes_by_agent(self, agent_id: str, limit: int = 100) -> list:
        """
        按 Agent 查询投票
        
        Args:
            agent_id: Agent ID
            limit: 返回记录数限制
        
        Returns:
            投票记录列表
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT id, trade_id, agent_id, vote_direction, vote_weight,
                       vote_strength, vote_confidence, vote_reason, created_at
                FROM vote_logs
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (agent_id, limit))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def query_votes_by_time_range(
        self, 
        start_time: str, 
        end_time: str,
        limit: int = 1000
    ) -> list:
        """
        按时间范围查询投票
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            limit: 返回记录数限制
        
        Returns:
            投票记录列表
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT id, trade_id, agent_id, vote_direction, vote_weight,
                       vote_strength, vote_confidence, vote_reason, created_at
                FROM vote_logs
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (start_time, end_time, limit))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def query_decisions_by_trade(self, trade_id: str) -> Optional[dict]:
        """
        按交易 ID 查询决策
        
        Args:
            trade_id: 交易 ID
        
        Returns:
            决策记录或 None
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT id, trade_id, symbol, final_decision, consensus_level,
                       total_weight, long_weight, short_weight, 
                       contributing_agents, decision_confidence, market_regime, created_at
                FROM decision_logs
                WHERE trade_id = ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (trade_id,))
            
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None
        finally:
            conn.close()
    
    def query_decisions_by_time_range(
        self,
        start_time: str,
        end_time: str,
        limit: int = 1000
    ) -> list:
        """
        按时间范围查询决策
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            limit: 返回记录数限制
        
        Returns:
            决策记录列表
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute("""
                SELECT id, trade_id, symbol, final_decision, consensus_level,
                       total_weight, long_weight, short_weight, 
                       contributing_agents, decision_confidence, market_regime, created_at
                FROM decision_logs
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (start_time, end_time, limit))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_decision_statistics(self, start_time: str, end_time: str) -> dict:
        """
        获取决策统计
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            统计数据字典
        """
        conn = self._get_conn()
        try:
            # 总决策数
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN final_decision = 'LONG' THEN 1 ELSE 0 END) as long_count,
                    SUM(CASE WHEN final_decision = 'SHORT' THEN 1 ELSE 0 END) as short_count,
                    SUM(CASE WHEN final_decision = 'NO_CONSENSUS' THEN 1 ELSE 0 END) as no_consensus_count,
                    AVG(consensus_level) as avg_consensus,
                    AVG(decision_confidence) as avg_confidence
                FROM decision_logs
                WHERE created_at >= ? AND created_at <= ?
            """, (start_time, end_time))
            
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                stats = dict(zip(columns, row))
                # 处理 None 值
                for key in stats:
                    if stats[key] is None:
                        stats[key] = 0 if 'count' in key or key == 'total' else 0.0
                return stats
            return {}
        finally:
            conn.close()
