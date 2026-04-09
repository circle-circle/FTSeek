"""
交易决策引擎 SQLAlchemy 数据模型

数据表:
1. trading_modes - 交易模式表
2. paper_positions - 模拟持仓表
3. paper_trades - 交易记录表
4. performance_stats - 绩效统计表
5. graduation_history - 晋级历史表

所有表使用 WAL 模式避免锁死，包含 created_at 和 updated_at 字段
"""
from enum import Enum
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
import sqlite3
from contextlib import contextmanager
import logging
import re


def parse_datetime(dt_string: str) -> datetime:
    """
    解析 ISO 格式日期字符串（兼容 Python 3.6）
    
    Args:
        dt_string: ISO 格式日期字符串
        
    Returns:
        datetime 对象
    """
    if dt_string is None:
        return None
    
    # 处理带时区的情况
    dt_string = dt_string.replace('Z', '+00:00')
    
    # 尝试多种格式
    formats = [
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d'
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(dt_string, fmt)
        except ValueError:
            continue
    
    # 如果都失败，尝试从 ISO 格式解析
    try:
        # 处理带时区偏移的情况
        if '+' in dt_string or (dt_string.count('-') > 2):
            dt_string = re.split(r'[+-]', dt_string)[0]
        return datetime.strptime(dt_string[:19], '%Y-%m-%dT%H:%M:%S')
    except (ValueError, AttributeError) as e:
        raise ValueError(f"无法解析日期时间：{dt_string}") from e

logger = logging.getLogger("ftseek.v4.models")
logger.propagate = False  # 防止日志重复


class TradingMode(str, Enum):
    """交易模式枚举"""
    PAPER = "PAPER"       # 纯模拟模式
    HYBRID = "HYBRID"     # 混合模式（高置信度实盘，低置信度模拟）
    LIVE = "LIVE"         # 纯实盘模式
    
    @classmethod
    def from_string(cls, value: str) -> Optional['TradingMode']:
        """从字符串转换为枚举"""
        try:
            return cls(value.upper())
        except (ValueError, AttributeError):
            return None


class TradingModeModel:
    """交易模式数据模型"""
    def __init__(self, id: int, code: str, mode: TradingMode, 
                 created_at: datetime, updated_at: datetime):
        self.id = id
        self.code = code
        self.mode = mode
        self.created_at = created_at
        self.updated_at = updated_at
    
    def __repr__(self):
        return f"TradingModeModel(code={self.code}, mode={self.mode})"


class PaperPosition:
    """模拟持仓数据模型"""
    def __init__(self, id: int, code: str, quantity: int, avg_cost: float,
                 entry_time: datetime, created_at: datetime, updated_at: datetime,
                 direction: str = "long"):
        self.id = id
        self.code = code
        self.quantity = quantity
        self.avg_cost = avg_cost
        self.entry_time = entry_time
        self.created_at = created_at
        self.updated_at = updated_at
        self.direction = direction  # "long" 多头 / "short" 空头
    
    def __repr__(self):
        return f"PaperPosition(code={self.code}, quantity={self.quantity}, avg_cost={self.avg_cost}, direction={self.direction})"


class PaperTrade:
    """交易记录数据模型"""
    def __init__(self, id: int, code: str, action: str, quantity: int,
                 price: float, timestamp: datetime, pnl: Optional[float],
                 created_at: datetime, updated_at: datetime):
        self.id = id
        self.code = code
        self.action = action
        self.quantity = quantity
        self.price = price
        self.timestamp = timestamp
        self.pnl = pnl
        self.created_at = created_at
        self.updated_at = updated_at
    
    def __repr__(self):
        return f"PaperTrade(code={self.code}, action={self.action}, quantity={self.quantity}, price={self.price})"


class PerformanceStats:
    """绩效统计数据模型"""
    def __init__(self, id: int, code: str, date: datetime, daily_return: float,
                 cumulative_return: float, created_at: datetime, updated_at: datetime):
        self.id = id
        self.code = code
        self.date = date
        self.daily_return = daily_return
        self.cumulative_return = cumulative_return
        self.created_at = created_at
        self.updated_at = updated_at
    
    def __repr__(self):
        return f"PerformanceStats(code={self.code}, date={self.date}, daily_return={self.daily_return})"


class GraduationHistory:
    """晋级历史数据模型"""
    def __init__(self, id: int, code: str, from_mode: TradingMode, to_mode: TradingMode,
                 reason: str, created_at: datetime, updated_at: datetime):
        self.id = id
        self.code = code
        self.from_mode = from_mode
        self.to_mode = to_mode
        self.reason = reason
        self.created_at = created_at
        self.updated_at = updated_at
    
    def __repr__(self):
        return f"GraduationHistory(code={self.code}, from={self.from_mode}, to={self.to_mode})"


class TradingDatabase:
    """交易决策引擎数据库管理类"""
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径，默认使用配置目录
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            # 默认使用配置目录
            from app import config
            self.db_path = config.DATA_DIR / "trading_decision.db"
        
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_tables()
        logger.info(f"交易决策数据库已初始化：{self.db_path}")
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row  # 返回字典式行
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败：{e}")
            raise
        finally:
            conn.close()
    
    def _init_tables(self):
        """初始化数据库表结构（使用 WAL 模式）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 启用 WAL 模式
            cursor.execute("PRAGMA journal_mode=WAL")
            
            # 交易模式表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_modes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    mode TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trading_modes_code 
                ON trading_modes(code)
            """)
            
            # 模拟持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    entry_time DATETIME NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'long',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # 安全迁移：如旧表没有 direction 字段则补充
            try:
                cursor.execute("ALTER TABLE paper_positions ADD COLUMN direction TEXT NOT NULL DEFAULT 'long'")
                logger.info("paper_positions 表已迁移：新增 direction 字段")
            except Exception:
                pass  # 字段已存在，忽略
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_paper_positions_code 
                ON paper_positions(code)
            """)
            
            # 交易记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    timestamp DATETIME NOT NULL,
                    pnl REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_paper_trades_code 
                ON paper_trades(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_paper_trades_action 
                ON paper_trades(action)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_paper_trades_timestamp 
                ON paper_trades(timestamp)
            """)
            
            # 绩效统计表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS performance_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    date DATE NOT NULL,
                    daily_return REAL NOT NULL,
                    cumulative_return REAL NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, date)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_stats_code 
                ON performance_stats(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_stats_date 
                ON performance_stats(date)
            """)
            
            # 晋级历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS graduation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    from_mode TEXT NOT NULL,
                    to_mode TEXT NOT NULL,
                    reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_graduation_history_code 
                ON graduation_history(code)
            """)
            
            logger.info("交易决策数据库表结构初始化完成")
    
    # ==================== Trading Modes 操作 ====================
    
    def create_trading_mode(self, code: str, mode: TradingMode) -> TradingModeModel:
        """创建交易模式记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO trading_modes (code, mode, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (code, mode.value, now, now))
            
            cursor.execute("SELECT * FROM trading_modes WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            return self._row_to_trading_mode(row)
    
    def update_trading_mode(self, code: str, mode: TradingMode) -> Optional[TradingModeModel]:
        """更新交易模式"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute("""
                UPDATE trading_modes 
                SET mode = ?, updated_at = ?
                WHERE code = ?
            """, (mode.value, now, code))
            
            if cursor.rowcount > 0:
                cursor.execute("SELECT * FROM trading_modes WHERE code = ?", (code,))
                row = cursor.fetchone()
                return self._row_to_trading_mode(row)
        return None
    
    def get_trading_mode(self, code: str) -> Optional[TradingModeModel]:
        """获取单个交易模式"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trading_modes WHERE code = ?", (code,))
            row = cursor.fetchone()
            return self._row_to_trading_mode(row) if row else None
    
    def list_all_modes(self) -> List[TradingModeModel]:
        """列出所有交易模式"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trading_modes ORDER BY code")
            return [self._row_to_trading_mode(row) for row in cursor.fetchall()]
    
    def delete_trading_mode(self, code: str) -> bool:
        """删除交易模式"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trading_modes WHERE code = ?", (code,))
            return cursor.rowcount > 0
    
    def _row_to_trading_mode(self, row: sqlite3.Row) -> TradingModeModel:
        """将数据库行转换为 TradingModeModel"""
        return TradingModeModel(
            id=row['id'],
            code=row['code'],
            mode=TradingMode(row['mode']),
            created_at=parse_datetime(row['created_at']),
            updated_at=parse_datetime(row['updated_at'])
        )
    
    # ==================== Paper Positions 操作 ====================
    
    def create_paper_position(self, code: str, quantity: int, avg_cost: float,
                             entry_time: datetime, direction: str = "long") -> PaperPosition:
        """创建模拟持仓记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            entry_str = entry_time.isoformat() if entry_time else now
            cursor.execute("""
                INSERT INTO paper_positions (code, quantity, avg_cost, entry_time, direction, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (code, quantity, avg_cost, entry_str, direction, now, now))
            
            cursor.execute("SELECT * FROM paper_positions WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            return self._row_to_position(row)
    
    def update_paper_position(self, position_id: int, quantity: int = None,
                             avg_cost: float = None) -> Optional[PaperPosition]:
        """更新模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            
            updates = []
            params = []
            if quantity is not None:
                updates.append("quantity = ?")
                params.append(quantity)
            if avg_cost is not None:
                updates.append("avg_cost = ?")
                params.append(avg_cost)
            
            updates.append("updated_at = ?")
            params.append(now)
            params.append(position_id)
            
            cursor.execute(f"""
                UPDATE paper_positions 
                SET {', '.join(updates)}
                WHERE id = ?
            """, params)
            
            if cursor.rowcount > 0:
                cursor.execute("SELECT * FROM paper_positions WHERE id = ?", (position_id,))
                row = cursor.fetchone()
                return self._row_to_position(row)
        return None
    
    def get_position_by_code(self, code: str, direction: str = None) -> Optional[PaperPosition]:
        """按代码获取持仓，可指定方向（long/short）"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if direction:
                cursor.execute(
                    "SELECT * FROM paper_positions WHERE code = ? AND direction = ?",
                    (code, direction)
                )
            else:
                cursor.execute("SELECT * FROM paper_positions WHERE code = ?", (code,))
            row = cursor.fetchone()
            return self._row_to_position(row) if row else None
    
    def get_all_positions(self) -> List[PaperPosition]:
        """获取所有持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM paper_positions ORDER BY code")
            return [self._row_to_position(row) for row in cursor.fetchall()]
    
    def delete_paper_position(self, position_id: int) -> bool:
        """删除模拟持仓"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM paper_positions WHERE id = ?", (position_id,))
            return cursor.rowcount > 0
    
    def _row_to_position(self, row: sqlite3.Row) -> PaperPosition:
        """将数据库行转换为 PaperPosition"""
        return PaperPosition(
            id=row['id'],
            code=row['code'],
            quantity=row['quantity'],
            avg_cost=row['avg_cost'],
            entry_time=parse_datetime(row['entry_time']),
            created_at=parse_datetime(row['created_at']),
            updated_at=parse_datetime(row['updated_at']),
            direction=row['direction'] if 'direction' in row.keys() else 'long'
        )
    
    # ==================== Paper Trades 操作 ====================
    
    def create_paper_trade(self, code: str, action: str, quantity: int,
                          price: float, timestamp: datetime = None,
                          pnl: float = None) -> PaperTrade:
        """创建交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            timestamp_str = timestamp.isoformat() if timestamp else now
            cursor.execute("""
                INSERT INTO paper_trades (code, action, quantity, price, timestamp, pnl, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, action, quantity, price, timestamp_str, pnl, now, now))
            
            cursor.execute("SELECT * FROM paper_trades WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            return self._row_to_trade(row)
    
    def get_trades_by_code(self, code: str) -> List[PaperTrade]:
        """按代码获取交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM paper_trades WHERE code = ? ORDER BY timestamp DESC", (code,))
            return [self._row_to_trade(row) for row in cursor.fetchall()]
    
    def get_trades_by_action(self, action: str) -> List[PaperTrade]:
        """按操作类型获取交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM paper_trades WHERE action = ? ORDER BY timestamp DESC", (action,))
            return [self._row_to_trade(row) for row in cursor.fetchall()]
    
    def get_recent_trades(self, limit: int = 50) -> List[PaperTrade]:
        """获取最近交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM paper_trades 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
            return [self._row_to_trade(row) for row in cursor.fetchall()]
    
    def _row_to_trade(self, row: sqlite3.Row) -> PaperTrade:
        """将数据库行转换为 PaperTrade"""
        return PaperTrade(
            id=row['id'],
            code=row['code'],
            action=row['action'],
            quantity=row['quantity'],
            price=row['price'],
            timestamp=parse_datetime(row['timestamp']),
            pnl=row['pnl'],
            created_at=parse_datetime(row['created_at']),
            updated_at=parse_datetime(row['updated_at'])
        )
    
    # ==================== Performance Stats 操作 ====================
    
    def create_performance_stats(self, code: str, date: datetime,
                                daily_return: float, cumulative_return: float) -> PerformanceStats:
        """创建绩效统计记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            date_str = date.isoformat() if isinstance(date, datetime) else str(date)
            cursor.execute("""
                INSERT INTO performance_stats (code, date, daily_return, cumulative_return, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, date_str, daily_return, cumulative_return, now, now))
            
            cursor.execute("SELECT * FROM performance_stats WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            return self._row_to_stats(row)
    
    def update_performance_stats(self, stats_id: int, daily_return: float = None,
                                cumulative_return: float = None) -> Optional[PerformanceStats]:
        """更新绩效统计"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            
            updates = []
            params = []
            if daily_return is not None:
                updates.append("daily_return = ?")
                params.append(daily_return)
            if cumulative_return is not None:
                updates.append("cumulative_return = ?")
                params.append(cumulative_return)
            
            updates.append("updated_at = ?")
            params.append(now)
            params.append(stats_id)
            
            cursor.execute(f"""
                UPDATE performance_stats 
                SET {', '.join(updates)}
                WHERE id = ?
            """, params)
            
            if cursor.rowcount > 0:
                cursor.execute("SELECT * FROM performance_stats WHERE id = ?", (stats_id,))
                row = cursor.fetchone()
                return self._row_to_stats(row)
        return None
    
    def get_stats_by_code_and_date(self, code: str, date: datetime) -> Optional[PerformanceStats]:
        """按代码和日期获取绩效统计"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            date_str = date.isoformat() if isinstance(date, datetime) else str(date)
            cursor.execute("""
                SELECT * FROM performance_stats 
                WHERE code = ? AND date = ?
            """, (code, date_str))
            row = cursor.fetchone()
            return self._row_to_stats(row) if row else None
    
    def get_stats_history(self, code: str, days: int = 30) -> List[PerformanceStats]:
        """获取绩效历史"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("""
                SELECT * FROM performance_stats 
                WHERE code = ? AND date > ?
                ORDER BY date DESC
            """, (code, cutoff))
            return [self._row_to_stats(row) for row in cursor.fetchall()]
    
    def _row_to_stats(self, row: sqlite3.Row) -> PerformanceStats:
        """将数据库行转换为 PerformanceStats"""
        return PerformanceStats(
            id=row['id'],
            code=row['code'],
            date=parse_datetime(row['date']),
            daily_return=row['daily_return'],
            cumulative_return=row['cumulative_return'],
            created_at=parse_datetime(row['created_at']),
            updated_at=parse_datetime(row['updated_at'])
        )
    
    # ==================== Graduation History 操作 ====================
    
    def create_graduation_history(self, code: str, from_mode: TradingMode,
                                 to_mode: TradingMode, reason: str) -> GraduationHistory:
        """创建晋级历史记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute("""
                INSERT INTO graduation_history (code, from_mode, to_mode, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, from_mode.value, to_mode.value, reason, now, now))
            
            cursor.execute("SELECT * FROM graduation_history WHERE id = ?", (cursor.lastrowid,))
            row = cursor.fetchone()
            return self._row_to_graduation(row)
    
    def get_graduation_history_by_code(self, code: str) -> List[GraduationHistory]:
        """按代码获取晋级历史"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM graduation_history 
                WHERE code = ? 
                ORDER BY created_at DESC
            """, (code,))
            return [self._row_to_graduation(row) for row in cursor.fetchall()]
    
    def get_all_graduation_records(self) -> List[GraduationHistory]:
        """获取所有晋级记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM graduation_history ORDER BY created_at DESC")
            return [self._row_to_graduation(row) for row in cursor.fetchall()]
    
    def _row_to_graduation(self, row: sqlite3.Row) -> GraduationHistory:
        """将数据库行转换为 GraduationHistory"""
        return GraduationHistory(
            id=row['id'],
            code=row['code'],
            from_mode=TradingMode(row['from_mode']),
            to_mode=TradingMode(row['to_mode']),
            reason=row['reason'],
            created_at=parse_datetime(row['created_at']),
            updated_at=parse_datetime(row['updated_at'])
        )


# 全局单例
_trading_db: Optional[TradingDatabase] = None


def init_trading_db(db_path: str = None) -> TradingDatabase:
    """初始化交易决策数据库单例"""
    global _trading_db
    _trading_db = TradingDatabase(db_path)
    return _trading_db


def get_trading_db() -> TradingDatabase:
    """获取交易决策数据库单例"""
    if _trading_db is None:
        return init_trading_db()
    return _trading_db
