"""
FTSeek 数据存储模块 - SQLite 数据库

数据存储策略:
- 信号记录：永久保存
- 交易记录：永久保存
- 新闻数据：保留 90 天
- 系统日志：保留 30 天 (由日志配置管理)
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from app import config

logger = logging.getLogger("ftseek.storage.database")
logger.propagate = False  # 防止日志重复


def get_sqlite_connection(db_path: str, timeout: float = 30.0):
    """
    获取 SQLite 连接（统一配置）
    
    2026-04-08: 解决 database is locked 问题
    
    Args:
        db_path: 数据库文件路径
        timeout: 连接超时时间（秒）
    
    Returns:
        sqlite3.Connection
    """
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    
    # 启用 WAL 模式（更好的并发性能）
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")  # 毫秒
    cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡性能和安全
    
    return conn


class Database:
    """SQLite 数据库管理器"""
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径，默认使用配置目录
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = config.DATA_DIR / "ftseek.db"
        
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_tables()
        logger.info(f"数据库已初始化：{self.db_path}")
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接上下文管理器"""
        # 2026-04-08 修复: 添加 timeout 和 WAL 模式，解决 database is locked
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row  # 返回字典式行
        
        # 启用 WAL 模式 (更好的并发性能)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # 30 秒等待
        cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡性能和安全
        
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
        """初始化数据库表结构"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 信号记录表 (永久)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    strategy TEXT,
                    technical_score REAL,
                    sentiment_score REAL,
                    confidence REAL,
                    reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expire_at DATETIME,
                    confirmed_at DATETIME,
                    executed_at DATETIME,
                    status TEXT DEFAULT 'pending'
                )
            """)
            
            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_code 
                ON signals(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_status 
                ON signals(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signals_created_at 
                ON signals(created_at)
            """)
            
            # 交易记录表 (永久)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    signal_id TEXT,
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
                    entry_time DATETIME NOT NULL,
                    exit_time DATETIME,
                    exit_reason TEXT,
                    order_id TEXT,
                    status TEXT DEFAULT 'open',
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_code 
                ON trades(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_status 
                ON trades(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_entry_time 
                ON trades(entry_time)
            """)
            
            # 新闻数据表 (90 天)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS news (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT,
                    sentiment_score REAL,
                    sentiment_confidence REAL,
                    keywords TEXT,
                    source TEXT,
                    url TEXT,
                    published_at DATETIME,
                    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_code 
                ON news(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_published_at 
                ON news(published_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_news_fetched_at 
                ON news(fetched_at)
            """)
            
            # 绩效快照表 (永久)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    id TEXT PRIMARY KEY,
                    period TEXT NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL,
                    profit_factor REAL,
                    max_drawdown REAL,
                    sharpe_ratio REAL,
                    sortino_ratio REAL,
                    total_return REAL,
                    avg_win REAL,
                    avg_loss REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_period 
                ON performance_snapshots(period)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_dates 
                ON performance_snapshots(start_date, end_date)
            """)
            
            # 频率限制记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    action TEXT,
                    date DATE NOT NULL,
                    count INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(code, action, date)
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_rate_limits_date 
                ON rate_limits(date)
            """)
            
            # 交易配对记录表 (绩效追踪)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_pairs (
                    id TEXT PRIMARY KEY,
                    buy_signal_id TEXT,
                    sell_signal_id TEXT,
                    code TEXT,
                    buy_price REAL,
                    sell_price REAL,
                    quantity INTEGER,
                    return_rate REAL,
                    profit REAL,
                    open_time DATETIME,
                    close_time DATETIME,
                    status TEXT DEFAULT 'closed',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (buy_signal_id) REFERENCES signals(id),
                    FOREIGN KEY (sell_signal_id) REFERENCES signals(id)
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_pairs_code 
                ON trade_pairs(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_pairs_status 
                ON trade_pairs(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_pairs_close_time 
                ON trade_pairs(close_time)
            """)
            
            # 绩效汇总表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS performance_summary (
                    id TEXT PRIMARY KEY,
                    period TEXT,
                    start_date DATETIME,
                    end_date DATETIME,
                    total_trades INTEGER,
                    winning_trades INTEGER,
                    losing_trades INTEGER,
                    win_rate REAL,
                    total_return REAL,
                    avg_return REAL,
                    total_profit REAL,
                    total_loss REAL,
                    max_drawdown REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_summary_period 
                ON performance_summary(period)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_performance_summary_created_at 
                ON performance_summary(created_at)
            """)
            
            # 信号质量记录表 (P1-1 新增 - 永久保存)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    holding_periods INTEGER DEFAULT 0,
                    market_regime TEXT NOT NULL,
                    scenario TEXT,                    -- Phase 1: 场景标签
                    adx_value REAL DEFAULT 0.0,
                    mtf_alignment REAL DEFAULT 0.0,
                    sentiment_score REAL DEFAULT 0.0,
                    consensus_score REAL DEFAULT 0.0,
                    quality_score REAL DEFAULT 1.0,
                    max_drawdown REAL DEFAULT 0.0,
                    max_profit REAL DEFAULT 0.0,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_code 
                ON signal_outcomes(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_strategy 
                ON signal_outcomes(strategy)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_timestamp 
                ON signal_outcomes(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_market_regime
                ON signal_outcomes(market_regime)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_scenario
                ON signal_outcomes(scenario)
            """)

            # Phase 1 迁移：为现有 signal_outcomes 表添加 scenario 列
            try:
                cursor.execute("""
                    ALTER TABLE signal_outcomes
                    ADD COLUMN scenario TEXT
                """)
                logger.info("数据库迁移：signal_outcomes 表已添加 scenario 列")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    logger.debug("数据库迁移：scenario 列已存在，跳过")
                else:
                    logger.warning(f"数据库迁移失败：{e}")

            # Phase 3 迁移：添加 confidence 和 mtf_alignment 列
            try:
                cursor.execute("""
                    ALTER TABLE signal_outcomes
                    ADD COLUMN confidence REAL DEFAULT 0.5
                """)
                logger.info("数据库迁移：signal_outcomes 表已添加 confidence 列")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    logger.debug("数据库迁移：confidence 列已存在，跳过")
                else:
                    logger.warning(f"数据库迁移失败：{e}")

            logger.info("数据库表结构初始化完成")
    
    # ==================== 信号操作 ====================
    
    def save_signal(self, signal_data: Dict[str, Any]) -> bool:
        """保存信号记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO signals 
                (id, code, name, action, price, quantity, strategy, 
                 technical_score, sentiment_score, confidence, reason,
                 created_at, expire_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_data.get('id'),
                signal_data.get('code'),
                signal_data.get('name', ''),
                signal_data.get('action'),
                signal_data.get('price'),
                signal_data.get('quantity', 1),
                signal_data.get('strategy', ''),
                signal_data.get('technical_score'),
                signal_data.get('sentiment_score'),
                signal_data.get('confidence'),
                signal_data.get('reason', ''),
                signal_data.get('created_at', datetime.now().isoformat()),
                signal_data.get('expire_at'),
                signal_data.get('status', 'pending')
            ))
        return True
    
    def update_signal_status(self, signal_id: str, status: str, 
                            confirmed_at: str = None, 
                            executed_at: str = None) -> bool:
        """更新信号状态"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status == 'confirmed':
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?, confirmed_at = ?
                    WHERE id = ?
                """, (status, confirmed_at or datetime.now().isoformat(), signal_id))
            elif status == 'executed':
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?, executed_at = ?
                    WHERE id = ?
                """, (status, executed_at or datetime.now().isoformat(), signal_id))
            elif status == 'rejected':
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?
                    WHERE id = ?
                """, (status, signal_id))
            elif status == 'expired':
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?
                    WHERE id = ?
                """, (status, signal_id))
            elif status == 'ignored':
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?
                    WHERE id = ?
                """, (status, signal_id))
            else:
                # 通用更新
                cursor.execute("""
                    UPDATE signals 
                    SET status = ?
                    WHERE id = ?
                """, (status, signal_id))
        return True
    
    def get_signal(self, signal_id: str) -> Optional[Dict]:
        """获取单个信号"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_pending_signals(self) -> List[Dict]:
        """获取待处理信号"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM signals 
                WHERE status = 'pending' 
                AND (expire_at IS NULL OR expire_at > datetime('now'))
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_signals_by_code(self, code: str, days: int = 30) -> List[Dict]:
        """获取某股票的历史信号"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("""
                SELECT * FROM signals 
                WHERE code = ? AND created_at > ?
                ORDER BY created_at DESC
            """, (code, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 交易操作 ====================
    
    def save_trade(self, trade_data: Dict[str, Any]) -> bool:
        """保存交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trades 
                (id, signal_id, code, name, action, entry_price, exit_price,
                 quantity, pnl, pnl_percent, fees, strategy, 
                 entry_time, exit_time, exit_reason, order_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data.get('id'),
                trade_data.get('signal_id'),
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
                trade_data.get('entry_time', datetime.now().isoformat()),
                trade_data.get('exit_time'),
                trade_data.get('exit_reason', ''),
                trade_data.get('order_id', ''),
                trade_data.get('status', 'open')
            ))
        return True
    
    def update_trade_exit(self, trade_id: str, exit_price: float, 
                         pnl: float, pnl_percent: float,
                         exit_reason: str = '') -> bool:
        """更新交易退出信息"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades 
                SET exit_price = ?, pnl = ?, pnl_percent = ?, 
                    exit_reason = ?, exit_time = ?, status = 'closed'
                WHERE id = ?
            """, (exit_price, pnl, pnl_percent, exit_reason, 
                  datetime.now().isoformat(), trade_id))
        return True
    
    def get_open_trades(self) -> List[Dict]:
        """获取未平仓交易"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE status = 'open'
                ORDER BY entry_time DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trades_by_period(self, start_date: str, end_date: str) -> List[Dict]:
        """获取指定时间段的交易记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE entry_time BETWEEN ? AND ?
                ORDER BY entry_time DESC
            """, (start_date, end_date))
            return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 新闻操作 ====================
    
    def save_news(self, news_data: Dict[str, Any]) -> bool:
        """保存新闻数据"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO news 
                (id, code, title, content, sentiment_score, 
                 sentiment_confidence, keywords, source, url, 
                 published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                news_data.get('id'),
                news_data.get('code'),
                news_data.get('title'),
                news_data.get('content', ''),
                news_data.get('sentiment_score'),
                news_data.get('sentiment_confidence'),
                news_data.get('keywords', '[]'),
                news_data.get('source'),
                news_data.get('url'),
                news_data.get('published_at'),
                news_data.get('fetched_at', datetime.now().isoformat())
            ))
        return True
    
    def get_news_by_code(self, code: str, days: int = 7) -> List[Dict]:
        """获取某股票的最新新闻"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("""
                SELECT * FROM news 
                WHERE code = ? AND fetched_at > ?
                ORDER BY published_at DESC
                LIMIT 50
            """, (code, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    
    def cleanup_old_news(self, keep_days: int = 90) -> int:
        """清理旧新闻数据"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM news WHERE fetched_at < ?
            """, (cutoff,))
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"清理了 {deleted} 条旧新闻数据")
        return deleted
    
    # ==================== 绩效快照操作 ====================
    
    def save_performance_snapshot(self, snapshot_data: Dict[str, Any]) -> bool:
        """保存绩效快照"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO performance_snapshots 
                (id, period, start_date, end_date, total_trades, 
                 winning_trades, losing_trades, win_rate, profit_factor,
                 max_drawdown, sharpe_ratio, sortino_ratio, total_return,
                 avg_win, avg_loss)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_data.get('id'),
                snapshot_data.get('period'),
                snapshot_data.get('start_date'),
                snapshot_data.get('end_date'),
                snapshot_data.get('total_trades', 0),
                snapshot_data.get('winning_trades', 0),
                snapshot_data.get('losing_trades', 0),
                snapshot_data.get('win_rate'),
                snapshot_data.get('profit_factor'),
                snapshot_data.get('max_drawdown'),
                snapshot_data.get('sharpe_ratio'),
                snapshot_data.get('sortino_ratio'),
                snapshot_data.get('total_return'),
                snapshot_data.get('avg_win'),
                snapshot_data.get('avg_loss')
            ))
        return True
    
    def get_performance_snapshots(self, period: str = None, 
                                  limit: int = 12) -> List[Dict]:
        """获取绩效快照"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if period:
                cursor.execute("""
                    SELECT * FROM performance_snapshots 
                    WHERE period = ?
                    ORDER BY end_date DESC
                    LIMIT ?
                """, (period, limit))
            else:
                cursor.execute("""
                    SELECT * FROM performance_snapshots 
                    ORDER BY end_date DESC
                    LIMIT ?
                """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    # ==================== 频率限制操作 ====================
    
    def increment_rate_limit(self, code: str, action: str = None) -> int:
        """增加频率限制计数，返回当前计数"""
        today = datetime.now().date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO rate_limits (code, action, date, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(code, action, date) 
                DO UPDATE SET count = count + 1
            """, (code, action, today))
            
            cursor.execute("""
                SELECT count FROM rate_limits 
                WHERE code = ? AND action = ? AND date = ?
            """, (code, action, today))
            row = cursor.fetchone()
            return row['count'] if row else 1
    
    def get_rate_limit_count(self, code: str, action: str = None) -> int:
        """获取当前频率限制计数"""
        today = datetime.now().date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT count FROM rate_limits 
                WHERE code = ? AND action = ? AND date = ?
            """, (code, action, today))
            row = cursor.fetchone()
            return row['count'] if row else 0
    
    def get_global_daily_count(self) -> int:
        """获取全局今日总信号数"""
        today = datetime.now().date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SUM(count) as total FROM rate_limits 
                WHERE date = ?
            """, (today,))
            row = cursor.fetchone()
            return row['total'] if row and row['total'] else 0
    
    def cleanup_old_rate_limits(self, keep_days: int = 7) -> int:
        """清理旧频率限制记录"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).date().isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM rate_limits WHERE date < ?
            """, (cutoff,))
            return cursor.rowcount
    
    # ==================== 交易配对操作 ====================
    
    def save_trade_pair(self, trade_data: Dict[str, Any]) -> bool:
        """保存交易配对记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trade_pairs 
                (id, buy_signal_id, sell_signal_id, code, buy_price, 
                 sell_price, quantity, return_rate, profit, 
                 open_time, close_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data.get('id'),
                trade_data.get('buy_signal_id'),
                trade_data.get('sell_signal_id'),
                trade_data.get('code'),
                trade_data.get('buy_price'),
                trade_data.get('sell_price'),
                trade_data.get('quantity'),
                trade_data.get('return_rate'),
                trade_data.get('profit'),
                trade_data.get('open_time', datetime.now().isoformat()),
                trade_data.get('close_time'),
                trade_data.get('status', 'closed')
            ))
        return True
    
    def get_trade_pairs_by_code(self, code: str, days: int = 90) -> List[Dict]:
        """获取某股票的交易配对记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            cursor.execute("""
                SELECT * FROM trade_pairs 
                WHERE code = ? AND close_time > ?
                ORDER BY close_time DESC
            """, (code, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_trade_pairs_by_status(self, status: str) -> List[Dict]:
        """获取指定状态的交易配对"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trade_pairs 
                WHERE status = ?
                ORDER BY close_time DESC
            """, (status,))
            return [dict(row) for row in cursor.fetchall()]
    
    def update_trade_pair_status(self, trade_id: str, status: str) -> bool:
        """更新交易配对状态"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trade_pairs 
                SET status = ?
                WHERE id = ?
            """, (status, trade_id))
        return True
    
    def get_open_trade_pairs(self) -> List[Dict]:
        """获取未平仓的交易配对"""
        return self.get_trade_pairs_by_status('open')
    
    # ==================== 绩效汇总操作 ====================
    
    def save_performance_summary(self, summary_data: Dict[str, Any]) -> bool:
        """保存绩效汇总记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO performance_summary 
                (id, period, start_date, end_date, total_trades, 
                 winning_trades, losing_trades, win_rate, total_return, 
                 avg_return, total_profit, total_loss, max_drawdown)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary_data.get('id'),
                summary_data.get('period'),
                summary_data.get('start_date'),
                summary_data.get('end_date'),
                summary_data.get('total_trades', 0),
                summary_data.get('winning_trades', 0),
                summary_data.get('losing_trades', 0),
                summary_data.get('win_rate'),
                summary_data.get('total_return'),
                summary_data.get('avg_return'),
                summary_data.get('total_profit'),
                summary_data.get('total_loss'),
                summary_data.get('max_drawdown')
            ))
        return True
    
    def get_performance_summaries(self, period: str = None, 
                                  limit: int = 12) -> List[Dict]:
        """获取绩效汇总记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if period:
                cursor.execute("""
                    SELECT * FROM performance_summary 
                    WHERE period = ?
                    ORDER BY end_date DESC
                    LIMIT ?
                """, (period, limit))
            else:
                cursor.execute("""
                    SELECT * FROM performance_summary 
                    ORDER BY end_date DESC
                    LIMIT ?
                """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_latest_performance_summary(self, period: str) -> Optional[Dict]:
        """获取最新一期的绩效汇总"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM performance_summary 
                WHERE period = ?
                ORDER BY end_date DESC
                LIMIT 1
            """, (period,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    # ==================== 统计查询 ====================
    
    def get_statistics(self, start_date: str, end_date: str) -> Dict:
        """获取指定时间段的统计数据"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 信号统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_signals,
                    SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) as executed_signals,
                    AVG(confidence) as avg_confidence
                FROM signals 
                WHERE created_at BETWEEN ? AND ?
            """, (start_date, end_date))
            signal_stats = dict(cursor.fetchone())
            
            # 交易统计
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    AVG(pnl_percent) as avg_pnl_percent,
                    MAX(pnl) as max_win,
                    MIN(pnl) as max_loss
                FROM trades 
                WHERE entry_time BETWEEN ? AND ? AND status = 'closed'
            """, (start_date, end_date))
            trade_stats = dict(cursor.fetchone())
            
            # 处理 None 值
            winning_trades = trade_stats.get('winning_trades', 0) or 0
            losing_trades = trade_stats.get('losing_trades', 0) or 0
            
            # 计算胜率
            total = winning_trades + losing_trades
            win_rate = winning_trades / total if total > 0 else 0
            
            # 计算盈亏比
            winning = winning_trades
            losing = losing_trades
            profit_factor = 0
            if losing and losing > 0:
                cursor.execute("""
                    SELECT 
                        COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as total_profit,
                        ABS(COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0)) as total_loss
                    FROM trades 
                    WHERE entry_time BETWEEN ? AND ? AND status = 'closed'
                """, (start_date, end_date))
                pf_row = dict(cursor.fetchone())
                total_loss = pf_row.get('total_loss', 0) or 0
                if total_loss > 0:
                    profit_factor = (pf_row.get('total_profit', 0) or 0) / total_loss
            
            return {
                'period': {'start': start_date, 'end': end_date},
                'signals': signal_stats,
                'trades': {
                    **trade_stats,
                    'win_rate': win_rate,
                    'profit_factor': profit_factor
                }
            }
    
    # ==================== 信号质量记录操作 (P1-1 新增) ====================
    
    def save_signal_outcome(self, outcome_data: Dict[str, Any]) -> bool:
        """
        保存信号质量记录 (P1-1 新增)

        Args:
            outcome_data: SignalOutcome 数据字典，包含以下字段:
                - code: 股票代码
                - action: 'BUY' / 'SELL'
                - strategy: 策略名称
                - entry_price: 入场价
                - exit_price: 出场价
                - pnl_pct: 盈亏百分比
                - holding_periods: 持仓周期数
                - market_regime: 市场状态
                - scenario: 场景标签 (Phase 1 新增)
                - adx_value: ADX 值
                - mtf_alignment: 多周期对齐度
                - sentiment_score: 情绪得分
                - consensus_score: 共识得分
                - quality_score: 质量分
                - max_drawdown: 最大回撤
                - max_profit: 最大浮盈
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO signal_outcomes
                (code, action, strategy, entry_price, exit_price, pnl_pct,
                 holding_periods, market_regime, scenario, adx_value, mtf_alignment,
                 sentiment_score, consensus_score, quality_score,
                 max_drawdown, max_profit, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                outcome_data.get('code'),
                outcome_data.get('action'),
                outcome_data.get('strategy'),
                outcome_data.get('entry_price', 0.0),
                outcome_data.get('exit_price', 0.0),
                outcome_data.get('pnl_pct', 0.0),
                outcome_data.get('holding_periods', 0),
                outcome_data.get('market_regime', 'neutral'),
                outcome_data.get('scenario'),  # Phase 1: 场景标签
                outcome_data.get('adx_value', 0.0),
                outcome_data.get('mtf_alignment', 0.0),
                outcome_data.get('sentiment_score', 0.0),
                outcome_data.get('consensus_score', 0.0),
                outcome_data.get('quality_score', 1.0),
                outcome_data.get('max_drawdown', 0.0),
                outcome_data.get('max_profit', 0.0),
                outcome_data.get('timestamp', datetime.now().isoformat())
            ))
        return True
    
    def get_signal_outcomes_by_code(self, code: str, days: int = 90, 
                                    limit: int = None) -> List[Dict]:
        """
        获取某股票的信号质量记录
        
        Args:
            code: 股票代码
            days: 查询天数范围
            limit: 限制返回数量
        
        Returns:
            SignalOutcome 记录列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            
            query = """
                SELECT * FROM signal_outcomes 
                WHERE code = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """
            params = [code, cutoff]
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_signal_outcomes_by_strategy(self, strategy: str, days: int = 90,
                                        limit: int = None) -> List[Dict]:
        """
        获取某策略的信号质量记录
        
        Args:
            strategy: 策略名称
            days: 查询天数范围
            limit: 限制返回数量
        
        Returns:
            SignalOutcome 记录列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            
            query = """
                SELECT * FROM signal_outcomes 
                WHERE strategy = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """
            params = [strategy, cutoff]
            
            if limit:
                query += " LIMIT ?"
                params.append(limit)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_signal_outcomes_by_regime(self, market_regime: str, days: int = 90,
                                      limit: int = None) -> List[Dict]:
        """
        获取某市场状态下的信号质量记录

        Args:
            market_regime: 市场状态 (trending/ranging/neutral)
            days: 查询天数范围
            limit: 限制返回数量

        Returns:
            SignalOutcome 记录列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()

            query = """
                SELECT * FROM signal_outcomes
                WHERE market_regime = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """
            params = [market_regime, cutoff]

            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_signal_outcomes_by_scenario(self, scenario: str, days: int = 90,
                                        limit: int = None) -> List[Dict]:
        """
        获取某场景下的信号质量记录 (Phase 1 新增)

        Args:
            scenario: 场景标签 (trend_strong, range_bound, etc.)
            days: 查询天数范围
            limit: 限制返回数量

        Returns:
            SignalOutcome 记录列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()

            query = """
                SELECT * FROM signal_outcomes
                WHERE scenario = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """
            params = [scenario, cutoff]

            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_signal_outcomes_summary(self, days: int = 90, 
                                    group_by: str = None) -> Dict:
        """
        获取信号质量统计摘要
        
        Args:
            days: 查询天数范围
            group_by: 分组字段 ('strategy', 'market_regime', 'code', None)
        
        Returns:
            统计摘要字典
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            
            if group_by:
                # 分组统计
                cursor.execute(f"""
                    SELECT 
                        {group_by},
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END) as losing_trades,
                        AVG(pnl_pct) as avg_pnl_pct,
                        SUM(pnl_pct) as total_pnl_pct,
                        AVG(adx_value) as avg_adx,
                        AVG(mtf_alignment) as avg_mtf_alignment,
                        AVG(sentiment_score) as avg_sentiment,
                        AVG(consensus_score) as avg_consensus,
                        AVG(max_drawdown) as avg_max_drawdown,
                        AVG(max_profit) as avg_max_profit
                    FROM signal_outcomes 
                    WHERE timestamp > ?
                    GROUP BY {group_by}
                    ORDER BY total_trades DESC
                """, (cutoff,))
            else:
                # 总体统计
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as winning_trades,
                        SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END) as losing_trades,
                        AVG(pnl_pct) as avg_pnl_pct,
                        SUM(pnl_pct) as total_pnl_pct,
                        AVG(adx_value) as avg_adx,
                        AVG(mtf_alignment) as avg_mtf_alignment,
                        AVG(sentiment_score) as avg_sentiment,
                        AVG(consensus_score) as avg_consensus,
                        AVG(max_drawdown) as avg_max_drawdown,
                        AVG(max_profit) as avg_max_profit
                    FROM signal_outcomes 
                    WHERE timestamp > ?
                """, (cutoff,))
            
            rows = cursor.fetchall()
            
            if group_by:
                results = []
                for row in rows:
                    d = dict(row)
                    total = d.get('winning_trades', 0) + d.get('losing_trades', 0)
                    d['win_rate'] = d.get('winning_trades', 0) / total if total > 0 else 0
                    results.append(d)
                return {'grouped_by': group_by, 'data': results}
            else:
                result = dict(rows[0]) if rows else {}
                total = result.get('winning_trades', 0) + result.get('losing_trades', 0)
                result['win_rate'] = result.get('winning_trades', 0) / total if total > 0 else 0
                return result
    
    def cleanup_old_signal_outcomes(self, keep_days: int = 365) -> int:
        """
        清理旧的信号质量记录
        
        Args:
            keep_days: 保留天数
        
        Returns:
            删除的记录数
        """
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM signal_outcomes WHERE timestamp < ?
            """, (cutoff,))
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"清理了 {deleted} 条旧信号质量记录")
        return deleted


# 全局单例
db: Optional[Database] = None


def init_database(db_path: str = None) -> Database:
    """初始化数据库单例"""
    global db
    db = Database(db_path)
    return db


def get_database() -> Database:
    """获取数据库单例"""
    if db is None:
        return init_database()
    return db
