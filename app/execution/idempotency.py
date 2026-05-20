"""
幂等机制 - P0 修复 2026-04-26: 引入 SQLite 持久化，防止重启后重复下单
"""
import sqlite3
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

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
        """初始化数据库表"""
        try:
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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
            with sqlite3.connect(self.db_path) as conn:
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
            bypass: P1-2 新增 2026-04-26 — 风控/退出订单可设为 True 跳过幂等拦截
                    (仍然会在 record_order 写入记录供审计)
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
        """获取已记录的订单 ID"""
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
