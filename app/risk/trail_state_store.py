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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ftseek_us.trail_state_store")


def is_stale_for_position(updated_at: Optional[str], entry_time: Any,
                          margin_hours: float = 24.0) -> bool:
    """存储态是否**早于当前这笔仓** ⇒ 属同标的的历史仓残留, 必须整条丢弃。

    病灶 (2026-11-20 实测 US.MU): store 按 symbol 单键, 2026-06-12 的残留状态
    (`lowest_price=977.0` / `entry_session_ratio=1.5` / `entry_session='opening_drive'` /
    `scale_out_original_size=10.0`) 被恢复进 2026-11-18 入场的仓。exit_monitor 的完整性
    检查只校验"数值方向"(lowest > entry)并重置了那一个字段, 而 `entry_session_ratio` 与
    `entry_session` 因"键已存在"(:1205 只在缺失时写)而永久沿用六月值 ⇒ 该仓被当成
    刚在开盘驱动段入场、用最宽波动档 1.5 的仓, 五个月后仍是。

    判据取**单边 + 24h 余量**: 仅当 updated_at 比 entry_time 早超过 margin_hours 才判陈旧。
    ①同一笔仓的合法状态必然 updated_at >= entry_time ⇒ 单边判据不会误杀正常重启恢复
      (含隔夜持仓, 那正是本 store 存在的目的);
    ②DB 的 entry_time 既有 ET-aware('-05:00') 也有 naive 本地(CST) 两种写法, 而 store 的
      updated_at 是 naive 本地 ⇒ 归一化存在最多 ~14h 的口径不确定性, 24h 余量确保不会
      因时区误判丢掉合法 peak。
    缺任一时间或解析失败 ⇒ 无法判别, 返回 False (保持原行为, 不引入新的丢弃面)。
    """
    if not updated_at or entry_time is None:
        return False
    try:
        upd = datetime.fromisoformat(str(updated_at))
        ent = (entry_time if isinstance(entry_time, datetime)
               else datetime.fromisoformat(str(entry_time)))
    except (TypeError, ValueError):
        return False
    local_tz = datetime.now().astimezone().tzinfo
    if upd.tzinfo is None:
        upd = upd.replace(tzinfo=local_tz)
    if ent.tzinfo is None:
        ent = ent.replace(tzinfo=local_tz)
    delta_s = (ent.astimezone(timezone.utc) - upd.astimezone(timezone.utc)).total_seconds()
    return delta_s > margin_hours * 3600.0

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
            # Phase 1 (2026-05-31): 增加 position_id 粒度的 trail_state_snapshot
            # 与上表 trail_state(symbol 为主键) 互补：snapshot 以 position_id 为主键,
            # 记录 trail_high/low / 激活时间 / 触发时间 等关键字段，供启动息复。
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trail_state_snapshot (
                    position_id        TEXT PRIMARY KEY,
                    symbol             TEXT,
                    trail_status       TEXT,
                    trail_high         REAL,
                    trail_low          REAL,
                    activation_time    TEXT,
                    last_update_time   TEXT,
                    close_trigger_time TEXT
                )
            """)
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_trail_snap_symbol ON trail_state_snapshot(symbol)")
            except Exception:
                pass
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
            # Phase 1: 同步写 trail_state_snapshot (错误不影响主流程)
            try:
                self._save_snapshot(symbol, state)
            except Exception as _se:
                logger.debug(f"[TrailStateStore] snapshot 同步失败 {symbol}: {_se}")
            return True
        except Exception as e:
            logger.error(f"[TrailStateStore] save({symbol}) 失败: {e}")
            return False

    # ---- Phase 1 (2026-05-31): position_id 粒度的快照 ----

    def _save_snapshot(self, symbol: str, state: Dict[str, Any]) -> None:
        """同步写入 trail_state_snapshot (position_id 主键)"""
        position_id = (
            state.get("position_id")
            or state.get("pid")
            or symbol  # fallback: 表结构 PRIMARY KEY 要求非空
        )
        trail_status = state.get("trail_status") or (
            "ACTIVE" if state.get("dt_profit_lock_activated") or state.get("dt_breakeven_activated")
            else "PENDING"
        )
        trail_high = state.get("trail_high") or state.get("highest_price")
        trail_low = state.get("trail_low") or state.get("lowest_price")
        activation_time = state.get("activation_time") or state.get("entry_time_ts")
        last_update_time = state.get("last_update_time") or datetime.now().isoformat()
        close_trigger_time = state.get("close_trigger_time") or state.get("last_trigger_time")

        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO trail_state_snapshot (
                    position_id, symbol, trail_status, trail_high, trail_low,
                    activation_time, last_update_time, close_trigger_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(position_id), symbol, str(trail_status),
                    float(trail_high) if isinstance(trail_high, (int, float)) else None,
                    float(trail_low) if isinstance(trail_low, (int, float)) else None,
                    str(activation_time) if activation_time is not None else None,
                    str(last_update_time),
                    str(close_trigger_time) if close_trigger_time is not None else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def load_snapshot(self, position_id: str) -> Optional[Dict[str, Any]]:
        """按 position_id 读回 trail_state_snapshot (启动息复用)"""
        try:
            conn = self._get_conn()
            row = conn.execute(
                """SELECT position_id, symbol, trail_status, trail_high, trail_low,
                          activation_time, last_update_time, close_trigger_time
                   FROM trail_state_snapshot WHERE position_id = ?""",
                (position_id,),
            ).fetchone()
            conn.close()
            if not row:
                return None
            return {
                "position_id": row[0],
                "symbol": row[1],
                "trail_status": row[2],
                "trail_high": row[3],
                "trail_low": row[4],
                "activation_time": row[5],
                "last_update_time": row[6],
                "close_trigger_time": row[7],
            }
        except Exception as e:
            logger.error(f"[TrailStateStore] load_snapshot({position_id}) 失败: {e}")
            return None

    def load_all_snapshots(self) -> List[Dict[str, Any]]:
        """加载全部 snapshot (进程启动时恢复)"""
        out: List[Dict[str, Any]] = []
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT position_id, symbol, trail_status, trail_high, trail_low,
                          activation_time, last_update_time, close_trigger_time
                   FROM trail_state_snapshot"""
            ).fetchall()
            conn.close()
            for r in rows:
                out.append({
                    "position_id": r[0],
                    "symbol": r[1],
                    "trail_status": r[2],
                    "trail_high": r[3],
                    "trail_low": r[4],
                    "activation_time": r[5],
                    "last_update_time": r[6],
                    "close_trigger_time": r[7],
                })
        except Exception as e:
            logger.error(f"[TrailStateStore] load_all_snapshots 失败: {e}")
        return out

    def mark_close_triggered(self, position_id: str, trigger_time: Optional[str] = None) -> bool:
        """标记 close 触发时间 (供退出路径调用)"""
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE trail_state_snapshot SET close_trigger_time = ?, last_update_time = ? WHERE position_id = ?",
                (trigger_time or datetime.now().isoformat(), datetime.now().isoformat(), position_id),
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"[TrailStateStore] mark_close_triggered({position_id}) 失败: {e}")
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

    def load_with_ts(self, symbol: str):
        """同 `load`, 但一并返回 `updated_at` 供陈旧态判别。

        返回 `(state|None, updated_at|None)`。新增方法, `load` 语义与签名逐字不变
        (其余调用方零感知)。
        """
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT state_json, updated_at FROM trail_state WHERE symbol = ?", (symbol,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                return json.loads(row[0]), row[1]
            return None, None
        except Exception as e:
            logger.error(f"[TrailStateStore] load_with_ts({symbol}) 失败: {e}")
            return None, None

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
