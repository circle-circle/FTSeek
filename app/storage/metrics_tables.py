"""
监控指标数据库表定义

提供 metrics 和 metrics_hourly 表的创建和管理
"""
import sqlite3
import threading
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path

logger = logging.getLogger(__name__)


class MetricsTables:
    """
    监控指标数据库表管理
    
    Schema:
        metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,        -- 时间戳 ISO 格式
            metric_type     TEXT NOT NULL,        -- 指标类型：system/trading/agent
            metric_name     TEXT NOT NULL,        -- 指标名称
            metric_value    REAL NOT NULL,        -- 指标值
            metric_unit     TEXT,                 -- 单位
            labels          TEXT,                 -- JSON 标签
            created_at      TEXT NOT NULL
        )
        
        metrics_hourly (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            hour            TEXT NOT NULL,        -- 小时 YYYY-MM-DD HH:00
            metric_type     TEXT NOT NULL,
            metric_name     TEXT NOT NULL,
            avg_value       REAL,                 -- 平均值
            min_value       REAL,                 -- 最小值
            max_value       REAL,                 -- 最大值
            count           INTEGER,              -- 样本数
            created_at      TEXT NOT NULL
        )
    """
    
    def __init__(self, db_path: str):
        """
        初始化指标表
        
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
                # 创建实时指标表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metrics (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp       TEXT NOT NULL,
                        metric_type     TEXT NOT NULL,
                        metric_name     TEXT NOT NULL,
                        metric_value    REAL NOT NULL,
                        metric_unit     TEXT,
                        labels          TEXT,
                        created_at      TEXT NOT NULL
                    )
                """)
                
                # 创建索引
                conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_type ON metrics(metric_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(metric_name)")
                
                # 创建小时聚合表
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS metrics_hourly (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        hour            TEXT NOT NULL,
                        metric_type     TEXT NOT NULL,
                        metric_name     TEXT NOT NULL,
                        avg_value       REAL,
                        min_value       REAL,
                        max_value       REAL,
                        count           INTEGER,
                        created_at      TEXT NOT NULL
                    )
                """)
                
                # 创建索引
                conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_hour ON metrics_hourly(hour)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_type_name ON metrics_hourly(metric_type, metric_name)")
                
                conn.commit()
                logger.info(f"监控指标表初始化完成：{self.db_path}")
            except Exception as e:
                conn.rollback()
                logger.error(f"初始化指标表失败：{e}")
                raise
            finally:
                conn.close()
    
    def record_metric(
        self,
        metric_type: str,
        metric_name: str,
        metric_value: float,
        metric_unit: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """
        记录实时指标
        
        Args:
            metric_type: 指标类型 (system/trading/agent)
            metric_name: 指标名称
            metric_value: 指标值
            metric_unit: 单位
            labels: 标签字典
            timestamp: 时间戳 (ISO 格式)
        
        Returns:
            插入的记录 ID
        """
        import json
        
        with self._lock:
            conn = self._get_conn()
            try:
                if timestamp is None:
                    timestamp = datetime.now().isoformat()
                
                labels_json = json.dumps(labels) if labels else None
                
                cursor = conn.execute("""
                    INSERT INTO metrics 
                    (timestamp, metric_type, metric_name, metric_value, 
                     metric_unit, labels, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp,
                    metric_type,
                    metric_name,
                    metric_value,
                    metric_unit,
                    labels_json,
                    datetime.now().isoformat(),
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                conn.rollback()
                logger.error(f"记录指标失败：{e}")
                raise
            finally:
                conn.close()
    
    def record_metrics_batch(
        self,
        metrics: List[Dict[str, Any]],
        timestamp: Optional[str] = None,
    ) -> int:
        """
        批量记录指标
        
        Args:
            metrics: 指标列表，每个元素包含 metric_type, metric_name, metric_value 等
            timestamp: 时间戳
        
        Returns:
            插入的记录数
        """
        import json
        
        with self._lock:
            conn = self._get_conn()
            try:
                if timestamp is None:
                    timestamp = datetime.now().isoformat()
                
                now = datetime.now().isoformat()
                data = []
                for m in metrics:
                    labels_json = json.dumps(m.get('labels')) if m.get('labels') else None
                    data.append((
                        timestamp,
                        m['metric_type'],
                        m['metric_name'],
                        m['metric_value'],
                        m.get('metric_unit'),
                        labels_json,
                        now,
                    ))
                
                conn.executemany("""
                    INSERT INTO metrics 
                    (timestamp, metric_type, metric_name, metric_value, 
                     metric_unit, labels, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, data)
                
                conn.commit()
                return len(data)
            except Exception as e:
                conn.rollback()
                logger.error(f"批量记录指标失败：{e}")
                raise
            finally:
                conn.close()
    
    def aggregate_hourly_metrics(self, hour: str) -> int:
        """
        聚合小时指标
        
        Args:
            hour: 小时字符串 (YYYY-MM-DD HH:00)
        
        Returns:
            聚合的记录数
        """
        from datetime import timedelta
        
        with self._lock:
            conn = self._get_conn()
            try:
                # 计算小时结束时间 (下一小时的开始)
                hour_start = datetime.strptime(hour, "%Y-%m-%d %H:00")
                hour_end = hour_start + timedelta(hours=1)
                
                # 使用 strftime 格式化以匹配 timestamp 前缀
                # timestamp 格式：2026-03-28T20:30:00.xxxxxx
                # hour 格式：2026-03-28 20:00
                # 我们需要将 hour 转换为 ISO 格式进行比较
                hour_start_iso = hour_start.strftime("%Y-%m-%dT%H:%M:%S")
                hour_end_iso = hour_end.strftime("%Y-%m-%dT%H:%M:%S")
                
                # 计算聚合值
                cursor = conn.execute("""
                    INSERT INTO metrics_hourly 
                    (hour, metric_type, metric_name, avg_value, min_value, max_value, count, created_at)
                    SELECT 
                        ? as hour,
                        metric_type,
                        metric_name,
                        AVG(metric_value) as avg_value,
                        MIN(metric_value) as min_value,
                        MAX(metric_value) as max_value,
                        COUNT(*) as count,
                        ? as created_at
                    FROM metrics
                    WHERE timestamp >= ? AND timestamp < ?
                    GROUP BY metric_type, metric_name
                """, (
                    hour,
                    datetime.now().isoformat(),
                    hour_start_iso,
                    hour_end_iso,
                ))
                
                conn.commit()
                return cursor.rowcount
            except Exception as e:
                conn.rollback()
                logger.error(f"聚合小时指标失败：{e}")
                raise
            finally:
                conn.close()
    
    def query_metrics_by_time_range(
        self,
        start_time: str,
        end_time: str,
        metric_type: Optional[str] = None,
        metric_name: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        按时间范围查询指标
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            metric_type: 指标类型筛选
            metric_name: 指标名称筛选
            limit: 返回记录数限制
        
        Returns:
            指标记录列表
        """
        import json
        
        conn = self._get_conn()
        try:
            query = """
                SELECT id, timestamp, metric_type, metric_name, metric_value,
                       metric_unit, labels, created_at
                FROM metrics
                WHERE timestamp >= ? AND timestamp <= ?
            """
            params = [start_time, end_time]
            
            if metric_type:
                query += " AND metric_type = ?"
                params.append(metric_type)
            
            if metric_name:
                query += " AND metric_name = ?"
                params.append(metric_name)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cursor = conn.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            results = []
            
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                # 解析 labels JSON
                if row_dict.get('labels'):
                    try:
                        row_dict['labels'] = json.loads(row_dict['labels'])
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"解析 labels JSON 失败：{e}")
                        row_dict['labels'] = None
                results.append(row_dict)
            
            return results
        finally:
            conn.close()
    
    def query_hourly_metrics(
        self,
        start_hour: str,
        end_hour: str,
        metric_type: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询小时聚合指标
        
        Args:
            start_hour: 开始小时 (YYYY-MM-DD HH:00)
            end_hour: 结束小时 (YYYY-MM-DD HH:00)
            metric_type: 指标类型筛选
            metric_name: 指标名称筛选
        
        Returns:
            聚合指标列表
        """
        conn = self._get_conn()
        try:
            query = """
                SELECT id, hour, metric_type, metric_name, avg_value,
                       min_value, max_value, count, created_at
                FROM metrics_hourly
                WHERE hour >= ? AND hour <= ?
            """
            params = [start_hour, end_hour]
            
            if metric_type:
                query += " AND metric_type = ?"
                params.append(metric_type)
            
            if metric_name:
                query += " AND metric_name = ?"
                params.append(metric_name)
            
            query += " ORDER BY hour DESC"
            
            cursor = conn.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    
    def get_metric_statistics(
        self,
        start_time: str,
        end_time: str,
        metric_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取指标统计
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            metric_type: 指标类型
        
        Returns:
            统计数据字典
        """
        conn = self._get_conn()
        try:
            query = """
                SELECT 
                    metric_type,
                    metric_name,
                    COUNT(*) as count,
                    AVG(metric_value) as avg_value,
                    MIN(metric_value) as min_value,
                    MAX(metric_value) as max_value
                FROM metrics
                WHERE timestamp >= ? AND timestamp <= ?
            """
            params = [start_time, end_time]
            
            if metric_type:
                query += " AND metric_type = ?"
                params.append(metric_type)
            
            query += " GROUP BY metric_type, metric_name"
            
            cursor = conn.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            
            stats = {}
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                key = f"{row_dict['metric_type']}_{row_dict['metric_name']}"
                stats[key] = row_dict
            
            return stats
        finally:
            conn.close()
    
    def get_latest_metrics(
        self,
        metric_type: Optional[str] = None,
        limit_per_metric: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        获取最新指标
        
        Args:
            metric_type: 指标类型筛选
            limit_per_metric: 每个指标返回的记录数
        
        Returns:
            最新指标列表
        """
        import json
        
        conn = self._get_conn()
        try:
            query = """
                SELECT id, timestamp, metric_type, metric_name, metric_value,
                       metric_unit, labels, created_at
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY metric_type, metric_name 
                               ORDER BY timestamp DESC
                           ) as rn
                    FROM metrics
            """
            params = []
            
            if metric_type:
                query += " WHERE metric_type = ?"
                params.append(metric_type)
            
            query += f") WHERE rn <= {limit_per_metric} ORDER BY timestamp DESC"
            
            cursor = conn.execute(query, params)
            columns = [desc[0] for desc in cursor.description]
            results = []
            
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                if row_dict.get('labels'):
                    try:
                        row_dict['labels'] = json.loads(row_dict['labels'])
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"解析 labels JSON 失败：{e}")
                        row_dict['labels'] = None
                results.append(row_dict)
            
            return results
        finally:
            conn.close()
    
    def cleanup_old_metrics(self, older_than: str) -> int:
        """
        清理旧指标
        
        Args:
            older_than: 清理早于此时间的记录 (ISO 格式)
        
        Returns:
            删除的记录数
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM metrics WHERE timestamp < ?",
                    (older_than,)
                )
                deleted = cursor.rowcount
                conn.commit()
                logger.info(f"清理 {deleted} 条旧指标记录")
                return deleted
            except Exception as e:
                conn.rollback()
                logger.error(f"清理旧指标失败：{e}")
                raise
            finally:
                conn.close()
    
    def cleanup_old_hourly_metrics(self, older_than: str) -> int:
        """
        清理旧的小时聚合指标
        
        Args:
            older_than: 清理早于此时间的记录
        
        Returns:
            删除的记录数
        """
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM metrics_hourly WHERE hour < ?",
                    (older_than,)
                )
                deleted = cursor.rowcount
                conn.commit()
                logger.info(f"清理 {deleted} 条旧小时指标记录")
                return deleted
            except Exception as e:
                conn.rollback()
                logger.error(f"清理旧小时指标失败：{e}")
                raise
            finally:
                conn.close()
