"""
持仓快照记录器

定期记录持仓状态，支持收益计算和风险监控
"""
import sqlite3
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
import threading

logger = logging.getLogger("ftseek_us.storage.position_snapshot")
logger.propagate = False


class PositionSnapshotter:
    """持仓快照记录器"""
    
    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app.core.paths import get_trade_records_db
            self.db_path = get_trade_records_db()
        
        self._lock = threading.Lock()
        self._init_db()
        logger.info(f"持仓快照记录器已初始化：{self.db_path}")
    
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # P0 修复: 启用 WAL 模式
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    
    def _init_db(self):
        """初始化快照表"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS position_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        snapshot_time TEXT NOT NULL,
                        snapshot_type TEXT NOT NULL,  -- hourly/daily/pre_close/post_close
                        symbol TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        avg_cost REAL,
                        current_price REAL,
                        market_value REAL,
                        unrealized_pnl REAL,
                        pnl_pct REAL,
                        stop_loss REAL,
                        take_profit REAL,
                        signal_strength INTEGER,
                        signal_confidence REAL,
                        holding_hours REAL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_time ON position_snapshots(snapshot_time)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_symbol ON position_snapshots(symbol)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshot_type ON position_snapshots(snapshot_type)")
                conn.commit()
            finally:
                conn.close()
    
    def take_snapshot(self, positions: List[Dict], snapshot_type: str = "hourly") -> int:
        """
        记录持仓快照
        
        Args:
            positions: 持仓列表，每个持仓包含 symbol/quantity/avg_cost/current_price 等
            snapshot_type: 快照类型 (hourly/daily/pre_close/post_close)
        
        Returns:
            记录数量
        """
        if not positions:
            return 0
        
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now()
                snapshot_time = now.isoformat()
                count = 0
                
                for pos in positions:
                    conn.execute("""
                        INSERT INTO position_snapshots (
                            snapshot_time, snapshot_type, symbol, direction,
                            quantity, avg_cost, current_price, market_value,
                            unrealized_pnl, pnl_pct, stop_loss, take_profit,
                            signal_strength, signal_confidence, holding_hours, created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        snapshot_time,
                        snapshot_type,
                        pos.get("symbol", ""),
                        pos.get("direction", "LONG"),
                        pos.get("quantity", 0),
                        pos.get("avg_cost", 0),
                        pos.get("current_price", 0),
                        pos.get("market_value", 0),
                        pos.get("unrealized_pnl", 0),
                        pos.get("pnl_pct", 0),
                        pos.get("stop_loss", 0),
                        pos.get("take_profit", 0),
                        pos.get("signal_strength", 0),
                        pos.get("signal_confidence", 0),
                        pos.get("holding_hours", 0),
                        snapshot_time,
                    ))
                    count += 1
                
                conn.commit()
                logger.info(f"[快照] 记录 {count} 个持仓 ({snapshot_type})")
                return count
            finally:
                conn.close()
    
    def get_snapshots_by_period(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
    ) -> List[Dict]:
        """
        获取指定时间段的快照
        
        Args:
            symbol: 股票代码
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            快照列表
        """
        with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute("""
                    SELECT * FROM position_snapshots
                    WHERE symbol = ? AND snapshot_time >= ? AND snapshot_time <= ?
                    ORDER BY snapshot_time ASC
                """, (symbol, start_time, end_time)).fetchall()
                
                columns = [
                    "id", "snapshot_time", "snapshot_type", "symbol", "direction",
                    "quantity", "avg_cost", "current_price", "market_value",
                    "unrealized_pnl", "pnl_pct", "stop_loss", "take_profit",
                    "signal_strength", "signal_confidence", "holding_hours", "created_at"
                ]
                
                return [dict(zip(columns, row)) for row in rows]
            finally:
                conn.close()
    
    def calculate_max_drawdown(self, symbol: str, entry_time: str, exit_time: str) -> float:
        """
        计算持仓期间的最大回撤
        
        Args:
            symbol: 股票代码
            entry_time: 入场时间
            exit_time: 出场时间
        
        Returns:
            最大回撤比例 (正数)
        """
        snapshots = self.get_snapshots_by_period(symbol, entry_time, exit_time)
        
        if not snapshots:
            return 0.0
        
        # 计算峰值和最大回撤
        peak = float('-inf')
        max_drawdown = 0.0
        
        for snap in snapshots:
            pnl_pct = snap.get("pnl_pct", 0) or 0
            if pnl_pct > peak:
                peak = pnl_pct
            
            drawdown = peak - pnl_pct
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        return max_drawdown
    
    def cleanup_old_snapshots(self, days: int = 30):
        """清理旧快照数据"""
        with self._lock:
            conn = self._get_conn()
            try:
                cutoff = datetime.now().strftime('%Y-%m-%d')
                cursor = conn.execute("""
                    DELETE FROM position_snapshots
                    WHERE date(snapshot_time) < date(?, ?)
                """, (cutoff, f"-{days} days"))
                deleted = cursor.rowcount
                conn.commit()
                logger.info(f"[快照] 清理 {deleted} 条旧记录")
                return deleted
            finally:
                conn.close()


# 全局实例
_snapshotter: Optional[PositionSnapshotter] = None


def get_position_snapshotter() -> PositionSnapshotter:
    """获取持仓快照记录器实例"""
    global _snapshotter
    if _snapshotter is None:
        _snapshotter = PositionSnapshotter()
    return _snapshotter