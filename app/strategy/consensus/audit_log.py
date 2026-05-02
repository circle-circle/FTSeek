"""
审计日志模块 — 信号链追踪 + LLM 审计记录

从 consensus_engine.py 拆出：
- _record_signal_chain() — 信号链记录
- get_weights_snapshot() — 权重快照
- 审计日志查询
"""

import logging
import sqlite3
import json
import threading
from typing import Optional, Dict, List, Any
from datetime import datetime
from pathlib import Path

from app.core.config_loader import cfg

logger = logging.getLogger("ftseek.consensus.audit")

_DB_PATH = Path(__file__).parent.parent.parent.parent / "data" / "consensus_audit.db"


class AuditLogger:
    """
    共识审计日志
    
    职责：
    1. 记录信号链 (signal → consensus → execution → outcome)
    2. 记录 LLM 审计
    3. 权重快照
    4. 历史查询
    """
    
    def __init__(self):
        self._lock = threading.Lock()
        self._init_db()
    
    def _init_db(self):
        """初始化审计数据库"""
        try:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_DB_PATH))
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS signal_chain (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT,
                    code TEXT,
                    strategy TEXT,
                    action TEXT,
                    price REAL,
                    confidence REAL,
                    consensus_score REAL,
                    market_regime TEXT,
                    adx REAL,
                    agree_strategies TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS execution_result (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT,
                    code TEXT,
                    executed INTEGER,
                    reject_reason TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    pnl REAL,
                    exit_reason TEXT,
                    holding_hours REAL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS weight_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_type TEXT,
                    code TEXT,
                    weights TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"审计 DB 初始化失败: {e}")
    
    # ===== 信号链记录 =====
    
    def record_signal(self, trace_id: str, code: str, strategy: str,
                      action: str, price: float, confidence: float,
                      consensus_score: float, market_regime: str,
                      adx: Optional[float] = None,
                      agree_strategies: Optional[List[str]] = None) -> str:
        """
        记录信号
        
        Returns:
            signal_id (用于后续关联执行结果)
        """
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            c = conn.cursor()
            c.execute('''
                INSERT INTO signal_chain 
                (trace_id, code, strategy, action, price, confidence, 
                 consensus_score, market_regime, adx, agree_strategies)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trace_id, code, strategy, action, price, confidence,
                consensus_score, market_regime, adx,
                json.dumps(agree_strategies or []),
            ))
            signal_id = c.lastrowid
            conn.commit()
            conn.close()
            return str(signal_id)
        except Exception as e:
            logger.error(f"记录信号失败: {e}")
            return ""
    
    def record_execution(self, trace_id: str, code: str, executed: bool,
                         reject_reason: str = "", entry_price: float = 0,
                         exit_price: float = 0, pnl: float = 0,
                         exit_reason: str = "", holding_hours: float = 0):
        """记录执行结果"""
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            c = conn.cursor()
            c.execute('''
                INSERT INTO execution_result 
                (trace_id, code, executed, reject_reason, entry_price, 
                 exit_price, pnl, exit_reason, holding_hours)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trace_id, code, int(executed), reject_reason, entry_price,
                exit_price, pnl, exit_reason, holding_hours,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录执行结果失败: {e}")
    
    # ===== 权重快照 =====
    
    def record_weight_snapshot(self, snapshot_type: str, code: str,
                                weights: Dict[str, float]):
        """记录权重快照"""
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            c = conn.cursor()
            c.execute('''
                INSERT INTO weight_snapshots (snapshot_type, code, weights)
                VALUES (?, ?, ?)
            ''', (snapshot_type, code, json.dumps(weights)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录权重快照失败: {e}")
    
    def get_weights_snapshot(self, code: str = None, limit: int = 10) -> List[Dict]:
        """获取权重快照"""
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            if code:
                c.execute(
                    "SELECT * FROM weight_snapshots WHERE code = ? ORDER BY created_at DESC LIMIT ?",
                    (code, limit)
                )
            else:
                c.execute(
                    "SELECT * FROM weight_snapshots ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            
            results = []
            for row in c.fetchall():
                results.append({
                    "id": row["id"],
                    "type": row["snapshot_type"],
                    "code": row["code"],
                    "weights": json.loads(row["weights"]),
                    "created_at": row["created_at"],
                })
            conn.close()
            return results
        except Exception as e:
            logger.error(f"获取权重快照失败: {e}")
            return []
    
    # ===== 查询 =====
    
    def get_signal_history(self, code: str = None, limit: int = 50) -> List[Dict]:
        """获取信号历史"""
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            if code:
                c.execute(
                    "SELECT * FROM signal_chain WHERE code = ? ORDER BY created_at DESC LIMIT ?",
                    (code, limit)
                )
            else:
                c.execute(
                    "SELECT * FROM signal_chain ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            
            results = []
            for row in c.fetchall():
                results.append({
                    "trace_id": row["trace_id"],
                    "code": row["code"],
                    "strategy": row["strategy"],
                    "action": row["action"],
                    "price": row["price"],
                    "confidence": row["confidence"],
                    "consensus_score": row["consensus_score"],
                    "market_regime": row["market_regime"],
                    "adx": row["adx"],
                    "agree_strategies": json.loads(row["agree_strategies"]),
                    "created_at": row["created_at"],
                })
            conn.close()
            return results
        except Exception as e:
            logger.error(f"获取信号历史失败: {e}")
            return []
    
    def get_execution_history(self, code: str = None, limit: int = 50) -> List[Dict]:
        """获取执行历史"""
        try:
            conn = sqlite3.connect(str(_DB_PATH))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            if code:
                c.execute(
                    "SELECT * FROM execution_result WHERE code = ? ORDER BY created_at DESC LIMIT ?",
                    (code, limit)
                )
            else:
                c.execute(
                    "SELECT * FROM execution_result ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            
            results = []
            for row in c.fetchall():
                results.append({
                    "trace_id": row["trace_id"],
                    "code": row["code"],
                    "executed": bool(row["executed"]),
                    "reject_reason": row["reject_reason"],
                    "entry_price": row["entry_price"],
                    "exit_price": row["exit_price"],
                    "pnl": row["pnl"],
                    "exit_reason": row["exit_reason"],
                    "holding_hours": row["holding_hours"],
                    "created_at": row["created_at"],
                })
            conn.close()
            return results
        except Exception as e:
            logger.error(f"获取执行历史失败: {e}")
            return []


# ===== 全局实例 =====

_audit_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    """获取审计日志实例"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
