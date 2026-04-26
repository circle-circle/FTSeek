"""
模拟盘专用数据库表结构

方案 A: 物理隔离 - 新增专用表
- simulation_trades: 模拟交易记录
- simulation_positions: 模拟持仓记录
- simulation_account: 模拟账户资金

优势:
- 与实盘数据完全隔离
- 查询性能更优
- 数据清理更方便
- 避免误操作风险
"""
from app.storage.repository import get_db
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

from app import config

logger = logging.getLogger("ftseek.storage.simulation_tables")
logger.propagate = False  # 防止日志重复


class SimulationDatabase:
    """模拟盘数据库管理器"""
    
    def __init__(self, db_path: str = None):
        """
        初始化模拟盘数据库
        
        Args:
            db_path: 数据库文件路径，默认使用配置目录
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = config.DATA_DIR / "simulation.db"
        
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_tables()
        logger.info(f"模拟盘数据库已初始化：{self.db_path}")
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接上下文管理器"""
        with get_db(str(self.db_path)) as conn:
            pass
        conn.row_factory = sqlite3.Row  # 返回字典式行
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"模拟盘数据库操作失败：{e}")
            raise
    
    def _init_tables(self):
        """初始化模拟盘专用表结构"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 模拟交易记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS simulation_trades (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT,
                    action TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity INTEGER NOT NULL,
                    pnl REAL,
                    pnl_percent REAL,
                    fees REAL DEFAULT 0,
                    strategy TEXT,
                    signal_id TEXT,
                    entry_time DATETIME NOT NULL,
                    exit_time DATETIME,
                    exit_reason TEXT,
                    order_id TEXT,
                    status TEXT DEFAULT 'open',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_trades_code 
                ON simulation_trades(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_trades_status 
                ON simulation_trades(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_trades_entry_time 
                ON simulation_trades(entry_time)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_trades_signal 
                ON simulation_trades(signal_id)
            """)
            
            # 模拟持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS simulation_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    current_price REAL DEFAULT 0,
                    direction TEXT DEFAULT 'long',
                    entry_time DATETIME NOT NULL,
                    unrealized_pnl REAL DEFAULT 0,
                    unrealized_pnl_percent REAL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_positions_code 
                ON simulation_positions(code)
            """)
            
            # 模拟账户资金表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS simulation_account (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT DEFAULT 'SIMULATION_MAIN',
                    initial_capital REAL NOT NULL,
                    current_capital REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    frozen_cash REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    total_pnl_percent REAL DEFAULT 0,
                    total_fees REAL DEFAULT 0,
                    trading_day TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_account_trading_day 
                ON simulation_account(trading_day)
            """)
            
            # 模拟交易日志表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS simulation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_type TEXT NOT NULL,
                    code TEXT,
                    action TEXT,
                    price REAL,
                    quantity INTEGER,
                    order_id TEXT,
                    signal_id TEXT,
                    message TEXT,
                    details TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_logs_type 
                ON simulation_logs(log_type)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_logs_code 
                ON simulation_logs(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sim_logs_created_at 
                ON simulation_logs(created_at)
            """)
            
            # 初始化默认账户记录
            cursor.execute("""
                INSERT OR IGNORE INTO simulation_account 
                (account_id, initial_capital, current_capital, available_cash, trading_day)
                VALUES ('SIMULATION_MAIN', 1000000.0, 1000000.0, 1000000.0, date('now'))
            """)
            
            logger.info("模拟盘表结构初始化完成")
    
    # ==================== 交易记录操作 ====================
    
    def save_simulation_trade(self, trade_data: dict) -> bool:
        """保存模拟交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO simulation_trades 
                (id, code, name, action, entry_price, exit_price,
                 quantity, pnl, pnl_percent, fees, strategy, signal_id,
                 entry_time, exit_time, exit_reason, order_id, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data.get('id'),
                trade_data.get('code'),
                trade_data.get('name', ''),
                trade_data.get('action'),
                trade_data.get('entry_price'),
                trade_data.get('exit_price'),
                trade_data.get('quantity'),
                trade_data.get('pnl'),
                trade_data.get('pnl_percent'),
                trade_data.get('fees', 0),
                trade_data.get('strategy', ''),
                trade_data.get('signal_id'),
                trade_data.get('entry_time', datetime.now().isoformat()),
                trade_data.get('exit_time'),
                trade_data.get('exit_reason', ''),
                trade_data.get('order_id', ''),
                trade_data.get('status', 'open'),
                datetime.now().isoformat()
            ))
        return True
    
    def update_simulation_trade_exit(self, trade_id: str, exit_price: float,
                                     pnl: float, pnl_percent: float,
                                     exit_reason: str = '') -> bool:
        """更新模拟交易退出信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE simulation_trades 
                SET exit_price = ?, pnl = ?, pnl_percent = ?, 
                    exit_reason = ?, exit_time = ?, status = 'closed',
                    updated_at = ?
                WHERE id = ?
            """, (exit_price, pnl, pnl_percent, exit_reason,
                  datetime.now().isoformat(), datetime.now().isoformat(), trade_id))
        return True
    
    def get_simulation_trades(self, status: str = None, 
                              code: str = None,
                              days: int = None) -> list:
        """获取模拟交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM simulation_trades WHERE 1=1"
            params = []
            
            if status:
                query += " AND status = ?"
                params.append(status)
            
            if code:
                query += " AND code = ?"
                params.append(code)
            
            if days:
                query += " AND entry_time >= datetime('now', ?)"
                params.append(f'-{days} days')
            
            query += " ORDER BY entry_time DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_simulation_trade(self, trade_id: str) -> Optional[dict]:
        """获取单个模拟交易"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM simulation_trades WHERE id = ?", 
                (trade_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    # ==================== 持仓操作 ====================
    
    def save_simulation_position(self, position_data: dict) -> bool:
        """保存模拟持仓记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO simulation_positions 
                (code, quantity, avg_cost, current_price, direction,
                 entry_time, unrealized_pnl, unrealized_pnl_percent, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_data.get('code'),
                position_data.get('quantity'),
                position_data.get('avg_cost'),
                position_data.get('current_price', 0),
                position_data.get('direction', 'long'),
                position_data.get('entry_time', datetime.now().isoformat()),
                position_data.get('unrealized_pnl', 0),
                position_data.get('unrealized_pnl_percent', 0),
                datetime.now().isoformat()
            ))
        return True
    
    def update_simulation_position(self, code: str, quantity: int = None,
                                   avg_cost: float = None,
                                   current_price: float = None) -> bool:
        """更新模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            updates = []
            params = []
            
            if quantity is not None:
                updates.append("quantity = ?")
                params.append(quantity)
            
            if avg_cost is not None:
                updates.append("avg_cost = ?")
                params.append(avg_cost)
            
            if current_price is not None:
                updates.append("current_price = ?")
                params.append(current_price)
            
            if not updates:
                return True
            
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(code)
            
            cursor.execute(f"""
                UPDATE simulation_positions 
                SET {', '.join(updates)}
                WHERE code = ?
            """, params)
        return True
    
    def delete_simulation_position(self, code: str) -> bool:
        """删除模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM simulation_positions WHERE code = ?",
                (code,)
            )
        return True
    
    def get_simulation_positions(self) -> list:
        """获取所有模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM simulation_positions 
                WHERE quantity > 0
                ORDER BY code
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_simulation_position(self, code: str) -> Optional[dict]:
        """获取单个模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM simulation_positions WHERE code = ?",
                (code,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    # ==================== 账户资金操作 ====================
    
    def get_simulation_account(self, account_id: str = 'SIMULATION_MAIN') -> Optional[dict]:
        """获取模拟账户信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM simulation_account WHERE account_id = ?",
                (account_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_simulation_account(self, account_id: str = 'SIMULATION_MAIN',
                                  current_capital: float = None,
                                  available_cash: float = None,
                                  total_pnl: float = None,
                                  total_pnl_percent: float = None) -> bool:
        """更新模拟账户资金"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            updates = []
            params = []
            
            if current_capital is not None:
                updates.append("current_capital = ?")
                params.append(current_capital)
            
            if available_cash is not None:
                updates.append("available_cash = ?")
                params.append(available_cash)
            
            if total_pnl is not None:
                updates.append("total_pnl = ?")
                params.append(total_pnl)
            
            if total_pnl_percent is not None:
                updates.append("total_pnl_percent = ?")
                params.append(total_pnl_percent)
            
            updates.append("trading_day = ?")
            params.append(datetime.now().date().isoformat())
            
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(account_id)
            
            cursor.execute(f"""
                UPDATE simulation_account 
                SET {', '.join(updates)}
                WHERE account_id = ?
            """, params)
        return True
    
    def initialize_simulation_account(self, initial_capital: float = 1000000.0,
                                      account_id: str = 'SIMULATION_MAIN') -> bool:
        """初始化模拟账户"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO simulation_account 
                (account_id, initial_capital, current_capital, available_cash,
                 total_pnl, total_pnl_percent, total_fees, trading_day)
                VALUES (?, ?, ?, ?, 0, 0, 0, date('now'))
            """, (account_id, initial_capital, initial_capital, initial_capital))
        return True
    
    # ==================== 日志操作 ====================
    
    def log_simulation_event(self, log_type: str, code: str = None,
                             action: str = None, price: float = None,
                             quantity: int = None, order_id: str = None,
                             signal_id: str = None, message: str = None,
                             details: str = None) -> bool:
        """记录模拟盘事件日志"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO simulation_logs 
                (log_type, code, action, price, quantity, order_id,
                 signal_id, message, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_type, code, action, price, quantity, order_id,
                  signal_id, message, details))
        return True
    
    def get_simulation_logs(self, log_type: str = None, code: str = None,
                           days: int = 7) -> list:
        """获取模拟盘日志"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = """
                SELECT * FROM simulation_logs 
                WHERE created_at >= datetime('now', ?)
            """
            params = [f'-{days} days']
            
            if log_type:
                query += " AND log_type = ?"
                params.append(log_type)
            
            if code:
                query += " AND code = ?"
                params.append(code)
            
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 统计查询 ====================
    
    def get_simulation_statistics(self, days: int = 30) -> dict:
        """获取模拟盘统计数据"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 交易统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    AVG(pnl_percent) as avg_pnl_percent
                FROM simulation_trades 
                WHERE status = 'closed' 
                AND entry_time >= datetime('now', ?)
            """, (f'-{days} days',))
            trade_stats = dict(cursor.fetchone())
            
            # 持仓统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as position_count,
                    COALESCE(SUM(unrealized_pnl), 0) as unrealized_pnl
                FROM simulation_positions 
                WHERE quantity > 0
            """)
            position_stats = dict(cursor.fetchone())
            
            # 账户统计
            account = self.get_simulation_account()
            
            return {
                'period_days': days,
                'trades': trade_stats,
                'positions': position_stats,
                'account': account
            }


# 全局单例
_sim_db: Optional[SimulationDatabase] = None


def get_simulation_database(db_path: str = None) -> SimulationDatabase:
    """获取模拟盘数据库单例"""
    global _sim_db
    if _sim_db is None:
        _sim_db = SimulationDatabase(db_path)
    return _sim_db


def reset_simulation_database():
    """重置模拟盘数据库单例 (用于测试)"""
    global _sim_db
    _sim_db = None
