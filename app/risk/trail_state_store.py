"""
Trail State Store — 统一 trail_state 持久化模块

单一写入接口，确保 trail_state 在内存与 SQLite 之间保持一致。
消除 position_exit_monitor / position_sync / reconciler 多写入者竞争。

数据库: data/trail_state.db
表: trail_state (symbol TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT)

用法:
    from app.risk.trail_state_store import save_trail_state, load_trail_state, get_trail_state_store

一致性保证:
    - save_trail_state: 原子写入（INSERT OR REPLACE）
    - load_trail_state: 返回最新持久化状态（如不存在返回 None）
    - validate_consistency: 内存 vs DB 一致性校验
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek_us.trail_state_store")

# DB 路径: data/trail_state.db (独立于 ftseek_us.db，避免表锁竞争)
_DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'trail_state.db')
)


class TrailStateStore:
    """Trail State 持久化存储 (线程安全单例)"""

    def __init__(self, db_path: str = _DB_PATH):
        self._db_path = db_path
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self):
        """确保表结构存在"""
        try:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trail_state (
                    symbol      TEXT PRIMARY KEY,
                    state_json  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[TrailStateStore] 建表失败: {e}")

    def save(self, symbol: str, state: Dict[str, Any]) -> bool:
        """原子持久化 trail_state"""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO trail_state (symbol, state_json, updated_at) VALUES (?, ?, ?)",
                (symbol, json.dumps(state, default=str), datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"[TrailStateStore] save({symbol}) 失败: {e}")
            return False

    def load(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 DB 加载 trail_state"""
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT state_json FROM trail_state WHERE symbol = ?", (symbol,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return json.loads(row[0])
            return None
        except Exception as e:
            logger.error(f"[TrailStateStore] load({symbol}) 失败: {e}")
            return None

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """加载所有 trail_state"""
        result = {}
        try:
            conn = self._get_conn()
            rows = conn.execute("SELECT symbol, state_json FROM trail_state").fetchall()
            conn.close()
            for sym, state_json in rows:
                try:
                    result[sym] = json.loads(state_json)
                except json.JSONDecodeError:
                    logger.warning(f"[TrailStateStore] {sym} JSON 解析失败，跳过")
        except Exception as e:
            logger.error(f"[TrailStateStore] load_all 失败: {e}")
        return result

    def delete(self, symbol: str) -> bool:
        """删除指定 symbol 的 trail_state"""
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM trail_state WHERE symbol = ?", (symbol,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"[TrailStateStore] delete({symbol}) 失败: {e}")
            return False

    def validate_consistency(self, symbol: str, memory_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        验证内存 trail_state 与 DB 的一致性
        
        Returns:
            {
                "consistent": bool,
                "db_state": dict or None,
                "diffs": list of str  # 差异描述
            }
        """
        db_state = self.load(symbol)
        if db_state is None:
            return {"consistent": True, "db_state": None, "diffs": ["DB 无记录 (首次)"]}

        diffs = []
        # 检查关键字段一致性
        critical_keys = [
            "dt_profit_lock_activated", "dt_profit_lock_tp",
            "dt_peak_pnl", "highest_price", "lowest_price",
            "dt_triggered_tiers", "dt_breakeven_activated"
        ]
        for key in critical_keys:
            mem_val = memory_state.get(key)
            db_val = db_state.get(key)
            if mem_val != db_val:
                diffs.append(f"{key}: memory={mem_val} vs db={db_val}")

        return {
            "consistent": len(diffs) == 0,
            "db_state": db_state,
            "diffs": diffs,
        }

    def recover_from_db(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 DB 恢复 trail_state (启动时一致性恢复)
        
        Returns:
            恢复后的 state dict, 或 None (DB 无记录)
        """
        db_state = self.load(symbol)
        if db_state:
            logger.info(f"[TrailStateStore] 从 DB 恢复 {symbol} trail_state (keys={list(db_state.keys())})")
        return db_state


# ─── 全局单例 ───

_store_instance: Optional[TrailStateStore] = None


def get_trail_state_store() -> TrailStateStore:
    """获取 TrailStateStore 单例"""
    global _store_instance
    if _store_instance is None:
        _store_instance = TrailStateStore()
    return _store_instance


def save_trail_state(symbol: str, state: Dict[str, Any]) -> bool:
    """便捷函数: 持久化 trail_state"""
    return get_trail_state_store().save(symbol, state)


def load_trail_state(symbol: str) -> Optional[Dict[str, Any]]:
    """便捷函数: 加载 trail_state"""
    return get_trail_state_store().load(symbol)
