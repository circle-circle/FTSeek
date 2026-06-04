"""
幂等机制 - P0 修复 2026-04-26: 引入 SQLite 持久化，防止重启后重复下单
"""
import sqlite3
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


# Critical #3 修复 (2026-06-02): 统一 SQLite 连接配置
# - timeout=10.0: 连接级别锁等待 (避免即时抛 OperationalError)
# - journal_mode=WAL: 多进程/多线程并发读写不互锁
# - busy_timeout=5000: 语句级别锁等待 5s, 避免 "database is locked"
def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class IdempotencyManager:
    """幂等管理器 (支持内存 + SQLite 双写)"""
    
    def __init__(self, window_seconds: int = 3600, db_path: str = None):
        self.window = timedelta(seconds=window_seconds)
        self.orders: Dict[str, Tuple[str, datetime]] = {}  # client_order_id -> (order_id, timestamp)
        self._lock = threading.Lock()
        
        # SQLite 持久化
        self.db_path = db_path or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'ftseek_us.db')
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self._load_from_db()
    
    def _init_db(self):
        """初始化数据库表 (Critical #3: WAL+busy_timeout 保证并发安全)"""
        try:
            with _connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS idempotency_keys (
                        client_order_id TEXT PRIMARY KEY,
                        order_id TEXT,
                        created_at REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"[Idempotency] DB init failed: {e}")

    def _load_from_db(self):
        """启动时加载最近窗口内的记录"""
        try:
            cutoff = (datetime.now() - self.window).timestamp()
            with _connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT client_order_id, order_id, created_at FROM idempotency_keys WHERE created_at > ?",
                    (cutoff,)
                )
                count = 0
                for row in cursor:
                    cid, oid, ts = row
                    self.orders[cid] = (oid, datetime.fromtimestamp(ts))
                    count += 1
            print(f"[Idempotency] Loaded {count} records from DB")
        except Exception as e:
            print(f"[Idempotency] Load from DB failed: {e}")

    def _save_to_db(self, client_order_id: str, order_id: str):
        """保存记录到数据库"""
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys (client_order_id, order_id, created_at) VALUES (?, ?, ?)",
                    (client_order_id, order_id, datetime.now().timestamp())
                )
                conn.commit()
        except Exception as e:
            print(f"[Idempotency] Save to DB failed: {e}")
    
    def is_duplicate(self, client_order_id: str, bypass: bool = False) -> bool:
        """检查是否是重复订单

        Args:
            client_order_id: 客户端订单 ID
            bypass: 风控/退出订单可设为 True 跳过幂等拦截。

        Critical 修复 2026-06-02:
            退出订单除了应该跳过重复检测，也 不应再走 get_order_id() 返回旧订单。
            具体调用者 (见 ExecutionEngine.submit_order) 应在 is_exit_order=True 时
            完全跳过幂等分支，并给 client_order_id 加 'exit_' 前缀隔离命名空间。
            本函数仅作为防御层：bypass=True 时一定返回 False，避免调用者误用。
        """
        if bypass:
            return False

        if client_order_id not in self.orders:
            return False
        
        _, recorded_at = self.orders[client_order_id]
        
        # 检查是否在窗口期内
        if datetime.now() - recorded_at > self.window:
            # 过期，清理
            del self.orders[client_order_id]
            return False
        
        return True
    
    def record_order(self, client_order_id: str, order_id: str):
        """记录订单 (P0 修复: 持久化)"""
        with self._lock:
            self.orders[client_order_id] = (order_id, datetime.now())
            self._save_to_db(client_order_id, order_id)
    
    def get_order_id(self, client_order_id: str) -> Optional[str]:
        """获取已记录的订单 ID

        ⚠️ Critical 警告 2026-06-02:
            调用者必须确保该 client_order_id 是开仓订单的 ID。
            对于退出/平仓订单，绝对不能调用本方法获取旧 order_id，
            否则会导致退出订单被旧开仓订单覆盖。退出订单应在 ExecutionEngine 层
            使用 'exit_' 前缀隔离命名空间，从源头避免冲突。
        """
        if client_order_id in self.orders:
            order_id, recorded_at = self.orders[client_order_id]
            if datetime.now() - recorded_at <= self.window:
                return order_id
        return None
    
    def cleanup_expired(self):
        """清理过期记录"""
        now = datetime.now()
        expired = [
            cid for cid, (_, recorded_at) in self.orders.items()
            if now - recorded_at > self.window
        ]
        for cid in expired:
            del self.orders[cid]
