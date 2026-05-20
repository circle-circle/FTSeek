"""
事件流存储

统一事件流，支持回放和审计
"""
import json
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path
from .models import TradeEvent, FailureRecord, Alert


class EventStore:
    """
    事件流存储
    
    所有交易相关动作都写入事件日志，支持回放和审计
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = None
        self._init_db()
    
    def _get_conn(self):
        """获取数据库连接"""
        if self._conn is None:
            # 对于内存数据库，需要 check_same_thread=False 以允许多线程访问
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._conn
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        # 事件流表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                signal_id TEXT,
                order_id TEXT,
                position_id TEXT,
                data TEXT,
                source TEXT,
                severity TEXT
            )
        """)
        
        # 失败记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failures (
                failure_id TEXT PRIMARY KEY,
                failure_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                symbol TEXT,
                signal_id TEXT,
                order_id TEXT,
                position_id TEXT,
                reason TEXT,
                details TEXT,
                recovered INTEGER DEFAULT 0,
                recovery_time TEXT,
                recovery_action TEXT,
                affects_funds INTEGER DEFAULT 0,
                triggers_circuit_breaker INTEGER DEFAULT 0
            )
        """)
        
        # 告警表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                level TEXT NOT NULL,
                title TEXT,
                message TEXT,
                timestamp TEXT NOT NULL,
                signal_id TEXT,
                order_id TEXT,
                position_id TEXT,
                source TEXT,
                category TEXT,
                acknowledged INTEGER DEFAULT 0,
                acknowledged_time TEXT,
                acknowledged_by TEXT
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_signal ON events(signal_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_position ON events(position_id)")
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failures_timestamp ON failures(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_failures_type ON failures(failure_type)")
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level)")
        
        conn.commit()
        # 不要关闭连接，保持内存数据库活跃
    
    def append(self, event: TradeEvent):
        """添加事件到事件流"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO events (event_id, event_type, timestamp, signal_id, order_id, position_id, data, source, severity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_id,
            event.event_type,
            event.timestamp.isoformat(),
            event.signal_id,
            event.order_id,
            event.position_id,
            json.dumps(event.data),
            event.source,
            event.severity.value,
        ))
        
        conn.commit()
    
    def replay(self, start_time: datetime, end_time: Optional[datetime] = None) -> List[TradeEvent]:
        """按时间范围回放事件"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        end_time = end_time or datetime.now()
        
        cursor.execute("""
            SELECT event_id, event_type, timestamp, signal_id, order_id, position_id, data, source, severity
            FROM events
            WHERE timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (start_time.isoformat(), end_time.isoformat()))
        
        events = []
        for row in cursor.fetchall():
            event = TradeEvent(
                event_id=row[0],
                event_type=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                signal_id=row[3],
                order_id=row[4],
                position_id=row[5],
                data=json.loads(row[6]) if row[6] else {},
                source=row[7],
                severity=__import__('app.core.models', fromlist=['AlertLevel']).AlertLevel(row[8]),
            )
            events.append(event)
        
        return events
    
    def get_position_lifecycle(self, position_id: str) -> List[TradeEvent]:
        """获取持仓完整生命周期事件"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT event_id, event_type, timestamp, signal_id, order_id, position_id, data, source, severity
            FROM events
            WHERE position_id = ?
            ORDER BY timestamp ASC
        """, (position_id,))
        
        events = []
        for row in cursor.fetchall():
            event = TradeEvent(
                event_id=row[0],
                event_type=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                signal_id=row[3],
                order_id=row[4],
                position_id=row[5],
                data=json.loads(row[6]) if row[6] else {},
                source=row[7],
                severity=__import__('app.core.models', fromlist=['AlertLevel']).AlertLevel(row[8]),
            )
            events.append(event)
        
        return events
    
    def record_failure(self, failure: FailureRecord):
        """记录失败"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO failures (failure_id, failure_type, timestamp, symbol, signal_id, order_id, position_id, reason, details, recovered, recovery_time, recovery_action, affects_funds, triggers_circuit_breaker)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            failure.failure_id,
            failure.failure_type.value,
            failure.timestamp.isoformat(),
            failure.symbol,
            failure.signal_id,
            failure.order_id,
            failure.position_id,
            failure.reason,
            json.dumps(failure.details),
            1 if failure.recovered else 0,
            failure.recovery_time.isoformat() if failure.recovery_time else None,
            failure.recovery_action,
            1 if failure.affects_funds else 0,
            1 if failure.triggers_circuit_breaker else 0,
        ))
        
        conn.commit()
    
    def record_alert(self, alert: Alert):
        """记录告警"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO alerts (alert_id, level, title, message, timestamp, signal_id, order_id, position_id, source, category, acknowledged, acknowledged_time, acknowledged_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alert.alert_id,
            alert.level.value,
            alert.title,
            alert.message,
            alert.timestamp.isoformat(),
            alert.signal_id,
            alert.order_id,
            alert.position_id,
            alert.source,
            alert.category,
            1 if alert.acknowledged else 0,
            alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
            alert.acknowledged_by,
        ))
        
        conn.commit()
    
    def get_statistics(self, start_time: datetime, end_time: Optional[datetime] = None) -> Dict[str, Any]:
        """获取统计信息"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        end_time = end_time or datetime.now()
        
        stats = {}
        
        # 事件总数
        cursor.execute("""
            SELECT COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ?
        """, (start_time.isoformat(), end_time.isoformat()))
        stats["total_events"] = cursor.fetchone()[0]
        
        # 按类型统计
        cursor.execute("""
            SELECT event_type, COUNT(*) FROM events 
            WHERE timestamp BETWEEN ? AND ? 
            GROUP BY event_type
        """, (start_time.isoformat(), end_time.isoformat()))
        stats["events_by_type"] = dict(cursor.fetchall())
        
        # 失败统计
        cursor.execute("""
            SELECT failure_type, COUNT(*) FROM failures 
            WHERE timestamp BETWEEN ? AND ? 
            GROUP BY failure_type
        """, (start_time.isoformat(), end_time.isoformat()))
        stats["failures_by_type"] = dict(cursor.fetchall())
        
        # 告警统计
        cursor.execute("""
            SELECT level, COUNT(*) FROM alerts 
            WHERE timestamp BETWEEN ? AND ? 
            GROUP BY level
        """, (start_time.isoformat(), end_time.isoformat()))
        stats["alerts_by_level"] = dict(cursor.fetchall())
        
        return stats
    
    def query_by_type(self, event_type: str, limit: int = 100) -> List[TradeEvent]:
        """按事件类型查询"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT event_id, event_type, timestamp, signal_id, order_id, position_id, data, source, severity
            FROM events
            WHERE event_type = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (event_type, limit))
        
        events = []
        for row in cursor.fetchall():
            event = TradeEvent(
                event_id=row[0],
                event_type=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                signal_id=row[3],
                order_id=row[4],
                position_id=row[5],
                data=json.loads(row[6]) if row[6] else {},
                source=row[7],
                severity=__import__('app.core.models', fromlist=['AlertLevel']).AlertLevel(row[8]),
            )
            events.append(event)
        
        return events
    
    def query_events(self, limit: int = 100) -> List[TradeEvent]:
        """查询所有事件"""
        conn = self._get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT event_id, event_type, timestamp, signal_id, order_id, position_id, data, source, severity
            FROM events
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        
        events = []
        for row in cursor.fetchall():
            event = TradeEvent(
                event_id=row[0],
                event_type=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                signal_id=row[3],
                order_id=row[4],
                position_id=row[5],
                data=json.loads(row[6]) if row[6] else {},
                source=row[7],
                severity=__import__('app.core.models', fromlist=['AlertLevel']).AlertLevel(row[8]),
            )
            events.append(event)
        
        return events
    
    def close(self):
        """关闭数据库连接，释放文件描述符"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass  # 关闭路径中忽略异常
            finally:
                self._conn = None
    
    def __del__(self):
        """析构时自动关闭连接"""
        self.close()
