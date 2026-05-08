"""
FTSeek 数据存储模块 - SQLite 数据库

数据存储策略:
- 信号记录：永久保存
- 交易记录：永久保存
- 新闻数据：保留 90 天
- 系统日志：保留 30 天 (由日志配置管理)

P0 修复 (2026-04-11):
- 实现连接池，防止高频并发下的 database is locked
- 连接复用减少创建/销毁开销
- 后台健康检查确保连接可用性
"""
import sqlite3
import logging
import queue
import threading
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
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    # 启用 WAL 模式（更好的并发性能）
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")  # 毫秒
    cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡性能和安全
    
    return conn


class ConnectionPool:
    """
    SQLite 连接池 (P0 修复 2026-04-11)
    
    解决高频并发场景下的 database is locked 问题
    - 预创建固定数量连接
    - 连接复用减少创建开销
    - 后台健康检查
    - 超时获取防止无限等待
    """
    
    def __init__(self, db_path: str, pool_size: int = 5, timeout: float = 30.0):
        """
        初始化连接池
        
        Args:
            db_path: 数据库文件路径
            pool_size: 连接池大小 (默认5)
            timeout: 获取连接超时时间 (秒)
        """
        self.db_path = db_path
        self.pool_size = pool_size
        self.timeout = timeout
        self._pool = queue.Queue(maxsize=pool_size)
        self._lock = threading.Lock()
        self._closed = False
        
        # 预创建连接
        self._initialize_pool()
        logger.info(f"连接池已初始化 | 路径={db_path} | 大小={pool_size}")
    
    def _initialize_pool(self):
        """预创建连接池中的所有连接"""
        for i in range(self.pool_size):
            conn = self._create_connection()
            self._pool.put(conn)
    
    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接并配置优化参数"""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        
        # 启用 WAL 模式（更好的并发性能）
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")  # 毫秒
        cursor.execute("PRAGMA synchronous=NORMAL")  # 平衡性能和安全
        cursor.execute("PRAGMA cache_size=-8192")  # 8MB 缓存 (原 2MB，适配 30 股高并发)
        cursor.execute("PRAGMA temp_store=MEMORY")  # 临时表在内存
        
        return conn
    
    def get_connection(self, timeout: float = None) -> sqlite3.Connection:
        """
        从连接池获取连接
        
        Args:
            timeout: 超时时间 (秒)，默认使用初始化时的值
        
        Returns:
            sqlite3.Connection
        
        Raises:
            queue.Empty: 超时未获取到连接
        """
        if self._closed:
            raise RuntimeError("连接池已关闭")
        
        timeout = timeout if timeout is not None else self.timeout
        
        try:
            conn = self._pool.get(timeout=timeout)
            
            # 健康检查：如果连接已关闭，创建新连接
            try:
                conn.execute("SELECT 1")
            except Exception:
                logger.warning("连接池中获取到失效连接，正在重建")
                with self._lock:
                    conn = self._create_connection()
            
            return conn
        except queue.Empty:
            logger.error(f"获取数据库连接超时 ({timeout}秒)，当前池大小={self._pool.qsize()}")
            raise
    
    def return_connection(self, conn: sqlite3.Connection):
        """
        归还连接到连接池
        
        Args:
            conn: 数据库连接
        """
        if self._closed:
            try:
                conn.close()
            except Exception as _e:
                logger.debug(f"连接关闭失败: {_e}")
            return
        
        try:
            # 检查连接是否仍然有效
            conn.execute("SELECT 1")
            
            # 如果有未提交的事务，回滚
            if conn.in_transaction:
                conn.rollback()
            
            self._pool.put_nowait(conn)
        except Exception as _e:
            # 连接已失效，创建新连接替换
            logger.warning(f"归还的连接已失效，正在重建: {_e}")
            try:
                conn.close()
            except Exception as _e2:
                logger.debug(f"失效连接关闭失败: {_e2}")
            
            try:
                new_conn = self._create_connection()
                self._pool.put_nowait(new_conn)
            except queue.Full:
                logger.debug("连接池已满，丢弃失效连接")  # 池已满，丢弃
    
    def close_all(self):
        """关闭连接池中的所有连接"""
        self._closed = True
        
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Exception as _e:
                logger.debug(f"连接池关闭时清理失败: {_e}")
        
        logger.info("连接池已关闭")
    
    @property
    def size(self) -> int:
        """当前可用连接数"""
        return self._pool.qsize()


class Database:
    """SQLite 数据库管理器 (P0 修复: 集成连接池)"""
    
    def __init__(self, db_path: str = None, pool_size: int = 5):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径，默认使用配置目录
            pool_size: 连接池大小 (默认5)
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = config.DATA_DIR / "ftseek.db"
        
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # P0 修复: 初始化连接池
        self._pool = ConnectionPool(str(self.db_path), pool_size=pool_size)
        
        self._init_tables()
        logger.info(f"数据库已初始化：{self.db_path} | 连接池大小={pool_size}")
    
    @contextmanager
    def get_connection(self):
        """
        获取数据库连接上下文管理器 (P0 修复: 使用连接池)
        
        使用连接池复用连接，减少创建/销毁开销
        防止高频并发场景下的 database is locked
        """
        # 从连接池获取连接
        conn = self._pool.get_connection()
        
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败：{e}")
            raise
        finally:
            # 归还连接到连接池
            self._pool.return_connection(conn)
    
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

            # ============================================================
            # 自适应权重快照表 (2026-04-11 新增 - 解决权重零持久化)
            # 每次权重更新时保存快照，进程重启后可恢复上次学习成果
            # ============================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS adaptive_weight_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL,
                    weight REAL NOT NULL,
                    win_rate REAL DEFAULT 0.5,
                    trades INTEGER DEFAULT 0,
                    regime_detail TEXT,
                    scenario_detail TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_aws_strategy
                ON adaptive_weight_snapshots(strategy)
            """)

            # ============================================================
            # 场景交易结果表 (2026-04-11 新增 - 替代内存 _scenario_outcomes)
            # 持久化场景级别的交易反馈，重启后可恢复场景统计
            # ============================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scenario_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario TEXT NOT NULL,
                    code TEXT NOT NULL,
                    pnl REAL NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_so_scenario
                ON scenario_outcomes(scenario)
            """)

            # ============================================================
            # Unified Trade Record (UTR) 表 - 整合 TradeChain 四层架构
            # 2026-04-12: 复盘/交易 trace/策略反馈模块整合
            # ============================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_chains (
                    -- 基础信息
                    trade_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,          -- BUY/SELL
                    quantity INTEGER DEFAULT 0,
                    account_type TEXT DEFAULT 'real',
                    strategy TEXT,
                    status TEXT DEFAULT 'open',    -- open/closed/cancelled
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

                    -- 衍生指标 (用于快速查询)
                    is_closed BOOLEAN DEFAULT FALSE,
                    is_profitable BOOLEAN DEFAULT FALSE,
                    days_ago INTEGER DEFAULT 0,

                    -- 四层数据 (JSON 格式存储)
                    signal_layer TEXT,             -- SignalLayer JSON
                    decision_layer TEXT,           -- DecisionLayer JSON
                    execution_layer TEXT,          -- ExecutionLayer JSON
                    result_layer TEXT              -- ResultLayer JSON (平仓后填充)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tc_code
                ON trade_chains(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tc_status
                ON trade_chains(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tc_created_at
                ON trade_chains(created_at)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tc_strategy
                ON trade_chains(strategy)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_tc_is_closed
                ON trade_chains(is_closed)
            """)

            logger.info("Unified Trade Record (UTR) 表初始化完成")

            # ============================================================
            # Trade Evidence Chain 表 - 证据/归因/反馈三层架构
            # 2026-04-12: 交易归因与深度复盘设计方案
            # ============================================================

            # 证据表 - 存储完整证据链
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_evidence (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    symbol TEXT NOT NULL,
                    timestamp_open DATETIME DEFAULT CURRENT_TIMESTAMP,
                    timestamp_close DATETIME,
                    strategy_mode TEXT,
                    primary_scene TEXT,
                    secondary_scenes TEXT,              -- JSON 数组
                    primary_strategy TEXT,
                    secondary_strategies TEXT,          -- JSON 数组
                    signal_sources TEXT,                -- JSON 数组
                    consensus_score REAL DEFAULT 0.0,
                    confidence REAL DEFAULT 0.0,
                    scene_confidence TEXT,              -- JSON 对象
                    risk_decision TEXT,
                    approval_status TEXT,
                    llm_reasoning TEXT,
                    llm_confidence REAL DEFAULT 0.0,
                    llm_key_factors TEXT,               -- JSON 数组
                    regime_snapshot TEXT,               -- JSON 对象
                    z_score REAL DEFAULT 0.0,
                    news_sentiment REAL DEFAULT 0.0,
                    volatility_snapshot REAL DEFAULT 0.0,
                    conflict_resolution_log TEXT,       -- JSON 数组
                    
                    -- 【优化1 2026-04-12】时段维度字段 (四维分析扩展)
                    trading_session TEXT DEFAULT '',    -- 交易时段 (pre_market/opening/morning/afternoon/closing)
                    session_start_time TEXT DEFAULT '', -- 时段开始时间
                    session_duration_minutes INTEGER DEFAULT 0,  -- 时段持续分钟数
                    
                    is_closed BOOLEAN DEFAULT FALSE,
                    status TEXT DEFAULT 'open',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_symbol
                ON trade_evidence(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_session
                ON trade_evidence(session_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_strategy
                ON trade_evidence(strategy_mode)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_scene
                ON trade_evidence(primary_scene)
            """)
            # 【优化1 2026-04-12】时段维度索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_session_time
                ON trade_evidence(trading_session)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_status
                ON trade_evidence(status)
            """)

            # 归因表 - 存储标签化归因结果
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_attribution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    result_tags TEXT,                   -- JSON 数组
                    pnl_category TEXT,
                    direction_tags TEXT,                -- JSON 数组
                    direction_correct BOOLEAN DEFAULT FALSE,
                    scene_tags TEXT,                    -- JSON 数组
                    scene_correct BOOLEAN DEFAULT FALSE,
                    strategy_tags TEXT,                 -- JSON 数组
                    strategy_fit BOOLEAN DEFAULT FALSE,
                    signal_tags TEXT,                   -- JSON 数组
                    signal_quality TEXT,
                    execution_tags TEXT,                -- JSON 数组
                    execution_quality TEXT,
                    slippage_category TEXT,
                    slippage_bps REAL DEFAULT 0,
                    risk_tags TEXT,                     -- JSON 数组
                    risk_quality TEXT,
                    position_quality TEXT,
                    timing_tags TEXT,                   -- JSON 数组
                    timing_quality TEXT,
                    entry_timing TEXT,
                    exit_timing TEXT,
                    direction_score INTEGER DEFAULT 0,
                    scene_score INTEGER DEFAULT 0,
                    strategy_score INTEGER DEFAULT 0,
                    signal_score INTEGER DEFAULT 0,
                    execution_score INTEGER DEFAULT 0,
                    timing_score INTEGER DEFAULT 0,
                    risk_score INTEGER DEFAULT 0,
                    overall_score INTEGER DEFAULT 0,
                    success_factors TEXT,               -- JSON 数组
                    failure_factors TEXT,               -- JSON 数组
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (trace_id) REFERENCES trade_evidence(trace_id)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_attribution_trace
                ON trade_attribution(trace_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_attribution_pnl
                ON trade_attribution(pnl_category)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_attribution_overall
                ON trade_attribution(overall_score)
            """)

            # 反馈表 - 存储反馈层数据 (2026-04-12 新增)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    strategy_weight_adjustment REAL DEFAULT 0.0,
                    strategy_enabled BOOLEAN DEFAULT TRUE,
                    strategy_scenes_to_avoid TEXT,      -- JSON 数组
                    strategy_scenes_to_favor TEXT,      -- JSON 数组
                    scene_confidence_adjustment TEXT,   -- JSON 对象
                    scene_threshold_adjustment TEXT,    -- JSON 对象
                    scene_gray_zones TEXT,              -- JSON 数组
                    stop_loss_adjustment REAL DEFAULT 0.0,
                    take_profit_adjustment REAL DEFAULT 0.0,
                    position_size_adjustment REAL DEFAULT 0.0,
                    risk_rules_to_add TEXT,             -- JSON 数组
                    execution_rules_to_update TEXT,     -- JSON 数组
                    order_type_suggestion TEXT,
                    slippage_tolerance_adjustment REAL DEFAULT 0.0,
                    auto_optimization_suggestions TEXT, -- JSON 数组
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (trace_id) REFERENCES trade_evidence(trace_id)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_trace
                ON trade_feedback(trace_id)
            """)

            # 策略表现表 - 策略维度统计
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    period TEXT,                        -- daily/weekly/monthly/all
                    start_date DATETIME,
                    end_date DATETIME,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    total_pnl REAL DEFAULT 0.0,
                    avg_pnl REAL DEFAULT 0.0,
                    avg_pnl_percent REAL DEFAULT 0.0,
                    sharpe_ratio REAL DEFAULT 0.0,
                    sortino_ratio REAL DEFAULT 0.0,
                    max_drawdown REAL DEFAULT 0.0,
                    avg_slippage_bps REAL DEFAULT 0.0,
                    avg_latency_ms REAL DEFAULT 0.0,
                    quality_score REAL DEFAULT 0.0,
                    scene_distribution TEXT,            -- JSON 对象
                    regime_distribution TEXT,           -- JSON 对象
                    weight_adjustment REAL DEFAULT 0.0,
                    scenes_to_avoid TEXT,               -- JSON 数组
                    scenes_to_favor TEXT,               -- JSON 数组
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(strategy_name, period)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategy_name
                ON strategy_performance(strategy_name)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategy_period
                ON strategy_performance(period)
            """)

            # 场景表现表 - 场景维度统计
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scene_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scene_name TEXT NOT NULL,
                    period TEXT,
                    start_date DATETIME,
                    end_date DATETIME,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    total_pnl REAL DEFAULT 0.0,
                    avg_pnl_percent REAL DEFAULT 0.0,
                    confidence_adjustment REAL DEFAULT 0.0,
                    threshold_adjustment REAL DEFAULT 0.0,
                    is_gray_zone BOOLEAN DEFAULT FALSE,
                    strategies_distribution TEXT,       -- JSON 对象
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(scene_name, period)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scene_name
                ON scene_performance(scene_name)
            """)

            # LLM 负反馈表 - 记录 LLM 判断错误的场景 (用于防御性更新)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS llm_negative_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    news_topic TEXT,
                    scene TEXT,
                    penalty REAL DEFAULT 0.0,
                    expires_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_feedback_trace
                ON llm_negative_feedback(trace_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_feedback_scene
                ON llm_negative_feedback(scene)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_llm_feedback_expires
                ON llm_negative_feedback(expires_at)
            """)

            # 执行质量表 - 执行维度统计
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT,
                    slippage_bps REAL DEFAULT 0.0,
                    latency_ms INTEGER DEFAULT 0,
                    fill_rate REAL DEFAULT 1.0,
                    execution_quality TEXT,
                    order_type_suggestion TEXT,
                    slippage_tolerance_adjustment REAL DEFAULT 0.0,
                    execution_rules TEXT,               -- JSON 数组
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (trace_id) REFERENCES trade_evidence(trace_id)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_execution_trace
                ON execution_quality(trace_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_execution_quality
                ON execution_quality(execution_quality)
            """)

            # 复盘摘要表 - 复盘报告摘要
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS review_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_type TEXT NOT NULL,          -- daily/weekly/monthly
                    review_date DATETIME NOT NULL,
                    period_start DATETIME,
                    period_end DATETIME,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0.0,
                    avg_pnl_percent REAL DEFAULT 0.0,
                    win_rate REAL DEFAULT 0.0,
                    sharpe_ratio REAL DEFAULT 0.0,
                    max_drawdown REAL DEFAULT 0.0,
                    by_strategy TEXT,                   -- JSON 对象
                    by_scene TEXT,                      -- JSON 对象
                    by_regime TEXT,                     -- JSON 对象
                    noise_signals TEXT,                 -- JSON 数组
                    overfit_signals TEXT,               -- JSON 数组
                    improvement_suggestions TEXT,       -- JSON 数组
                    auto_generated BOOLEAN DEFAULT FALSE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(review_type, review_date)
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_review_type
                ON review_summary(review_type)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_review_date
                ON review_summary(review_date)
            """)

            # 【优化6 2026-04-12】每周归因优化报告表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weekly_optimization_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL,          -- 如: 2026-W15
                    period_start TEXT,                  -- 如: 2026-04-05
                    period_end TEXT,                    -- 如: 2026-04-11
                    total_trades INTEGER DEFAULT 0,
                    suggestions TEXT,                   -- JSON 数组
                    llm_issues TEXT,                    -- JSON 数组
                    execution_quality TEXT,             -- JSON 对象
                    matrix_data TEXT,                   -- JSON 对象 (策略×场景矩阵)
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(report_date)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_wor_report_date
                ON weekly_optimization_reports(report_date)
            """)

            logger.info("Trade Evidence Chain 表初始化完成")
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_so_timestamp
                ON scenario_outcomes(timestamp)
            """)

            logger.info("数据库表结构初始化完成")

            # 数据库迁移: 确保 trade_attribution 表包含 pnl_category 列
            # (兼容旧数据库: 表已存在但缺少此列)
            try:
                cursor.execute("PRAGMA table_info(trade_attribution)")
                columns = {row[1] for row in cursor.fetchall()}
                if "pnl_category" not in columns:
                    cursor.execute("ALTER TABLE trade_attribution ADD COLUMN pnl_category TEXT")
                    logger.info("[迁移] trade_attribution 表已添加 pnl_category 列")
            except Exception as e:
                logger.warning(f"[迁移] pnl_category 列检查/添加异常: {e}")
    
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
                    exit_reason = ?, exit_time = ?, status = 'CLOSED'
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
                WHERE UPPER(status) = 'OPEN'
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


    # ============================================================
    #  自适应权重快照 CRUD (2026-04-11 新增)
    # ============================================================

    def save_weight_snapshot(
        self,
        strategy: str,
        weight: float,
        win_rate: float = 0.5,
        trades: int = 0,
        regime_detail: str = None,
        scenario_detail: str = None,
    ) -> bool:
        """
        保存/更新某策略的权重快照 (UPSERT)

        Args:
            strategy: 策略名称
            weight: 当前权重
            win_rate: 整体胜率
            trades: 总交易笔数
            regime_detail: JSON - 各市场状态表现 {"trending": {"wins":3,"total":5}, ...}
            scenario_detail: JSON - 各场景表现 {"TREND_STRONG": {"wins":2,"total":4}, ...}
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO adaptive_weight_snapshots
                    (strategy, weight, win_rate, trades, regime_detail, scenario_detail, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(strategy) DO UPDATE SET
                        weight = excluded.weight,
                        win_rate = excluded.win_rate,
                        trades = excluded.trades,
                        regime_detail = excluded.regime_detail,
                        scenario_detail = excluded.scenario_detail,
                        updated_at = CURRENT_TIMESTAMP
                """, (strategy, weight, win_rate, trades, regime_detail, scenario_detail))
            return True
        except Exception as e:
            logger.error(f"[权重快照] 保存失败 {strategy}: {e}")
            return False

    def load_weight_snapshots(self) -> Dict[str, Dict]:
        """
        加载所有策略的最新权重快照

        Returns:
            {策略名: {weight, win_rate, trades, regime_detail, scenario_detail, updated_at}}
        """
        result = {}
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT strategy, weight, win_rate, trades,
                           regime_detail, scenario_detail, updated_at
                    FROM adaptive_weight_snapshots
                """)
                for row in cursor.fetchall():
                    result[row[0]] = {
                        'weight': row[1],
                        'win_rate': row[2],
                        'trades': row[3],
                        'regime_detail': row[4],
                        'scenario_detail': row[5],
                        'updated_at': row[6],
                    }
        except Exception as e:
            logger.error(f"[权重快照] 加载失败: {e}")
        return result

    # ============================================================
    #  场景交易结果 CRUD (2026-04-11 新增)
    # ============================================================

    def save_scenario_outcome(self, scenario: str, code: str, pnl: float) -> bool:
        """
        保存场景级别的交易结果

        Args:
            scenario: 场景名称 (如 'TREND_STRONG')
            code: 股票代码
            pnl: 已实现盈亏
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO scenario_outcomes (scenario, code, pnl, timestamp)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """, (scenario, code, pnl))
            return True
        except Exception as e:
            logger.error(f"[场景结果] 保存失败 {scenario}/{code}: {e}")
            return False

    def load_scenario_outcomes(self, days: int = 30) -> Dict[str, List[Dict]]:
        """
        加载最近 N 天的场景交易结果

        Args:
            days: 回溯天数

        Returns:
            {场景名: [{pnl, ts, code}, ...]}
        """
        result: Dict[str, List[Dict]] = {}
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT scenario, code, pnl, timestamp
                    FROM scenario_outcomes
                    WHERE timestamp > ?
                    ORDER BY timestamp ASC
                """, (cutoff,))
                for row in cursor.fetchall():
                    scenario = row[0]
                    if scenario not in result:
                        result[scenario] = []
                    result[scenario].append({
                        'pnl': row[2],
                        'ts': row[3],
                        'code': row[1],
                    })
        except Exception as e:
            logger.error(f"[场景结果] 加载失败: {e}")
        return result

    def cleanup_scenario_outcomes(self, keep_days: int = 90) -> int:
        """清理过期的场景交易结果"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        deleted = 0
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM scenario_outcomes WHERE timestamp < ?
                """, (cutoff,))
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info(f"清理了 {deleted} 条旧场景交易结果")
        except Exception as e:
            logger.error(f"[场景结果] 清理失败: {e}")
        return deleted

    # ============================================================
    #  Unified Trade Record (UTR) CRUD operations
    # ============================================================

    def save_trade_chain(self, utr_data: Dict[str, Any]) -> bool:
        """Save or update Unified Trade Record"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO trade_chains
                    (trade_id, code, action, quantity, account_type, strategy,
                     status, is_closed, is_profitable, days_ago,
                     signal_layer, decision_layer, execution_layer, result_layer,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    utr_data.get('trade_id'), utr_data.get('code'), utr_data.get('action'),
                    utr_data.get('quantity', 0), utr_data.get('account_type', 'real'),
                    utr_data.get('strategy'), utr_data.get('status', 'open'),
                    utr_data.get('is_closed', False), utr_data.get('is_profitable', False),
                    utr_data.get('days_ago', 0), utr_data.get('signal_layer'),
                    utr_data.get('decision_layer'), utr_data.get('execution_layer'),
                    utr_data.get('result_layer'), utr_data.get('created_at'),
                    utr_data.get('updated_at')
                ))
            return True
        except Exception as e:
            logger.error(f"[UTR] 保存失败 {utr_data.get('trade_id')}: {e}")
            return False

    def get_trade_chain(self, trade_id: str) -> Optional[Dict]:
        """Get single trade chain by ID"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trade_chains WHERE trade_id = ?", (trade_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"[UTR] 获取失败 {trade_id}: {e}")
            return None

    def get_trade_chains_by_filter(self, start_date: str = None, end_date: str = None,
                                   code: str = None, strategy: str = None,
                                   status: str = None, is_closed: bool = None,
                                   limit: int = 100) -> List[Dict]:
        """Get trade chains with filters"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM trade_chains WHERE 1=1"
                params = []
                if start_date:
                    query += " AND created_at >= ?"; params.append(start_date)
                if end_date:
                    query += " AND created_at <= ?"; params.append(end_date)
                if code:
                    query += " AND code = ?"; params.append(code)
                if strategy:
                    query += " AND strategy = ?"; params.append(strategy)
                if status:
                    query += " AND status = ?"; params.append(status)
                if is_closed is not None:
                    query += " AND is_closed = ?"; params.append(is_closed)
                query += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[UTR] 查询失败：{e}")
            return []

    def update_trade_chain_result(self, trade_id: str, result_layer_json: str) -> bool:
        """Update result layer when trade closes"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE trade_chains
                    SET result_layer = ?, is_closed = TRUE, status = 'closed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE trade_id = ?
                """, (result_layer_json, trade_id))
            return True
        except Exception as e:
            logger.error(f"[UTR] 更新结果层失败 {trade_id}: {e}")
            return False

    def get_trade_chain_statistics(self, start_date: str = None, end_date: str = None,
                                   strategy: str = None) -> Dict:
        """Get trade chain statistics"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = """
                    SELECT COUNT(*) as total_trades,
                           SUM(CASE WHEN is_profitable THEN 1 ELSE 0 END) as winning_trades,
                           SUM(CASE WHEN is_closed THEN 1 ELSE 0 END) as closed_trades,
                           SUM(COALESCE(json_extract(result_layer, '\$.pnl'), 0)) as total_pnl,
                           AVG(COALESCE(json_extract(result_layer, '\$.pnl'), 0)) as avg_pnl,
                           AVG(COALESCE(json_extract(execution_layer, '\$.slippage_bps'), 0)) as avg_slippage_bps,
                           AVG(COALESCE(json_extract(execution_layer, '\$.execution_latency_ms'), 0)) as avg_latency_ms
                    FROM trade_chains WHERE is_closed = 1
                """
                params = []
                if start_date:
                    query += " AND created_at >= ?"; params.append(start_date)
                if end_date:
                    query += " AND created_at <= ?"; params.append(end_date)
                if strategy:
                    query += " AND strategy = ?"; params.append(strategy)
                cursor.execute(query, params)
                row = cursor.fetchone()
                if row:
                    stats = dict(row)
                    for key in ['total_trades', 'winning_trades', 'closed_trades']:
                        if stats.get(key) is None: stats[key] = 0
                    for key in ['total_pnl', 'avg_pnl', 'avg_slippage_bps', 'avg_latency_ms']:
                        if stats.get(key) is None: stats[key] = 0.0
                    return stats
                return {}
        except Exception as e:
            logger.error(f"[UTR] 统计查询失败：{e}")
            return {}


    # ============================================================
    #  Trade Evidence Chain CRUD operations
    # ============================================================

    def save_trade_evidence(self, evidence_data: Dict[str, Any]) -> bool:
        """
        保存交易证据层数据

        Args:
            evidence_data: EvidenceLayer 数据字典
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO trade_evidence
                    (trace_id, session_id, symbol, timestamp_open, timestamp_close,
                     strategy_mode, primary_scene, secondary_scenes, primary_strategy,
                     secondary_strategies, signal_sources, consensus_score, confidence,
                     scene_confidence, risk_decision, approval_status, llm_reasoning,
                     llm_confidence, llm_key_factors, regime_snapshot, z_score,
                     news_sentiment, volatility_snapshot, conflict_resolution_log,
                     trading_session, session_start_time, session_duration_minutes,
                     is_closed, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    evidence_data.get('trace_id'), evidence_data.get('session_id'),
                    evidence_data.get('symbol'), evidence_data.get('timestamp_open'),
                    evidence_data.get('timestamp_close'), evidence_data.get('strategy_mode'),
                    evidence_data.get('primary_scene'), evidence_data.get('secondary_scenes'),
                    evidence_data.get('primary_strategy'), evidence_data.get('secondary_strategies'),
                    evidence_data.get('signal_sources'), evidence_data.get('consensus_score'),
                    evidence_data.get('confidence'), evidence_data.get('scene_confidence'),
                    evidence_data.get('risk_decision'), evidence_data.get('approval_status'),
                    evidence_data.get('llm_reasoning'), evidence_data.get('llm_confidence'),
                    evidence_data.get('llm_key_factors'), evidence_data.get('regime_snapshot'),
                    evidence_data.get('z_score'), evidence_data.get('news_sentiment'),
                    evidence_data.get('volatility_snapshot'), evidence_data.get('conflict_resolution_log'),
                    # 【优化1 2026-04-12】时段维度字段
                    evidence_data.get('trading_session', ''),
                    evidence_data.get('session_start_time', ''),
                    evidence_data.get('session_duration_minutes', 0),
                    evidence_data.get('is_closed', False), evidence_data.get('status', 'open'),
                    evidence_data.get('created_at'), evidence_data.get('updated_at')
                ))
            return True
        except Exception as e:
            logger.error(f"[Evidence] 保存失败 {evidence_data.get('trace_id')}: {e}")
            return False

    def save_trade_attribution(self, attribution_data: Dict[str, Any]) -> bool:
        """
        保存交易归因层数据 (修复版 - 包含所有字段)
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trade_attribution
                    (trace_id, result_tags, pnl_category, direction_tags, direction_correct,
                     scene_tags, scene_correct, strategy_tags, strategy_fit, signal_tags,
                     signal_quality, execution_tags, execution_quality, slippage_category,
                     slippage_bps, risk_tags, risk_quality, position_quality, timing_tags,
                     timing_quality, entry_timing, exit_timing, direction_score, scene_score,
                     strategy_score, signal_score, execution_score, timing_score, risk_score,
                     overall_score, success_factors, failure_factors,
                     code, event_type, signal_price, actual_price, pnl, audit_data, timestamp, exit_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    attribution_data.get('trace_id'),
                    _to_json_safe(attribution_data.get('result_tags')),
                    attribution_data.get('pnl_category'),
                    _to_json_safe(attribution_data.get('direction_tags')),
                    bool(attribution_data.get('direction_correct', False)),
                    _to_json_safe(attribution_data.get('scene_tags')),
                    bool(attribution_data.get('scene_correct', False)),
                    _to_json_safe(attribution_data.get('strategy_tags')),
                    bool(attribution_data.get('strategy_fit', False)),
                    _to_json_safe(attribution_data.get('signal_tags')),
                    attribution_data.get('signal_quality'),
                    _to_json_safe(attribution_data.get('execution_tags')),
                    attribution_data.get('execution_quality'),
                    attribution_data.get('slippage_category'),
                    float(attribution_data.get('slippage_bps', 0)),
                    _to_json_safe(attribution_data.get('risk_tags')),
                    attribution_data.get('risk_quality'),
                    attribution_data.get('position_quality'),
                    _to_json_safe(attribution_data.get('timing_tags')),
                    attribution_data.get('timing_quality'),
                    attribution_data.get('entry_timing'),
                    attribution_data.get('exit_timing'),
                    int(attribution_data.get('direction_score', 0)),
                    int(attribution_data.get('scene_score', 0)),
                    int(attribution_data.get('strategy_score', 0)),
                    int(attribution_data.get('signal_score', 0)),
                    int(attribution_data.get('execution_score', 0)),
                    int(attribution_data.get('timing_score', 0)),
                    int(attribution_data.get('risk_score', 0)),
                    int(attribution_data.get('overall_score', 0)),
                    _to_json_safe(attribution_data.get('success_factors')),
                    _to_json_safe(attribution_data.get('failure_factors')),
                    attribution_data.get('code', ''),
                    attribution_data.get('event_type', ''),
                    float(attribution_data.get('signal_price', 0)),
                    float(attribution_data.get('actual_price', 0)),
                    float(attribution_data.get('pnl', 0)),
                    _to_json_safe(attribution_data.get('audit_data')),
                    attribution_data.get('timestamp', datetime.now().isoformat()),
                    attribution_data.get('exit_reason', ''),
                ))
            logger.info(f"[Attribution] 保存成功: {attribution_data.get('trace_id')}")
            return True
        except Exception as e:
            logger.error(f"[Attribution] 保存失败 {attribution_data.get('trace_id')}: {e}")
            return False


def _to_json_safe(value):
    """安全地将值转换为 JSON 字符串"""
    import json
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)

    def save_trade_feedback(self, feedback_data: Dict[str, Any]) -> bool:
        """
        保存交易反馈层数据 (2026-04-12 新增)

        Args:
            feedback_data: FeedbackLayer 数据字典
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # 首先检查是否已存在该 trace_id 的反馈记录
                cursor.execute("SELECT trace_id FROM trade_feedback WHERE trace_id = ?",
                             (feedback_data.get('trace_id'),))
                exists = cursor.fetchone() is not None

                if exists:
                    # 更新现有记录
                    cursor.execute("""
                        UPDATE trade_feedback
                        SET strategy_weight_adjustment = ?,
                            strategy_enabled = ?,
                            strategy_scenes_to_avoid = ?,
                            strategy_scenes_to_favor = ?,
                            scene_confidence_adjustment = ?,
                            scene_threshold_adjustment = ?,
                            scene_gray_zones = ?,
                            stop_loss_adjustment = ?,
                            take_profit_adjustment = ?,
                            position_size_adjustment = ?,
                            risk_rules_to_add = ?,
                            execution_rules_to_update = ?,
                            order_type_suggestion = ?,
                            slippage_tolerance_adjustment = ?,
                            auto_optimization_suggestions = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE trace_id = ?
                    """, (
                        feedback_data.get('strategy_weight_adjustment', 0.0),
                        feedback_data.get('strategy_enabled', True),
                        feedback_data.get('strategy_scenes_to_avoid'),
                        feedback_data.get('strategy_scenes_to_favor'),
                        feedback_data.get('scene_confidence_adjustment'),
                        feedback_data.get('scene_threshold_adjustment'),
                        feedback_data.get('scene_gray_zones'),
                        feedback_data.get('stop_loss_adjustment', 0.0),
                        feedback_data.get('take_profit_adjustment', 0.0),
                        feedback_data.get('position_size_adjustment', 0.0),
                        feedback_data.get('risk_rules_to_add'),
                        feedback_data.get('execution_rules_to_update'),
                        feedback_data.get('order_type_suggestion'),
                        feedback_data.get('slippage_tolerance_adjustment', 0.0),
                        feedback_data.get('auto_optimization_suggestions'),
                        feedback_data.get('trace_id')
                    ))
                else:
                    # 插入新记录
                    cursor.execute("""
                        INSERT INTO trade_feedback
                        (trace_id, strategy_weight_adjustment, strategy_enabled,
                         strategy_scenes_to_avoid, strategy_scenes_to_favor,
                         scene_confidence_adjustment, scene_threshold_adjustment,
                         scene_gray_zones, stop_loss_adjustment, take_profit_adjustment,
                         position_size_adjustment, risk_rules_to_add,
                         execution_rules_to_update, order_type_suggestion,
                         slippage_tolerance_adjustment, auto_optimization_suggestions,
                         created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (
                        feedback_data.get('trace_id'),
                        feedback_data.get('strategy_weight_adjustment', 0.0),
                        feedback_data.get('strategy_enabled', True),
                        feedback_data.get('strategy_scenes_to_avoid'),
                        feedback_data.get('strategy_scenes_to_favor'),
                        feedback_data.get('scene_confidence_adjustment'),
                        feedback_data.get('scene_threshold_adjustment'),
                        feedback_data.get('scene_gray_zones'),
                        feedback_data.get('stop_loss_adjustment', 0.0),
                        feedback_data.get('take_profit_adjustment', 0.0),
                        feedback_data.get('position_size_adjustment', 0.0),
                        feedback_data.get('risk_rules_to_add'),
                        feedback_data.get('execution_rules_to_update'),
                        feedback_data.get('order_type_suggestion'),
                        feedback_data.get('slippage_tolerance_adjustment', 0.0),
                        feedback_data.get('auto_optimization_suggestions')
                    ))
            return True
        except Exception as e:
            logger.error(f"[Feedback] 保存失败 {feedback_data.get('trace_id')}: {e}")
            return False

    def get_trade_evidence(self, trace_id: str) -> Optional[Dict]:
        """Get trade evidence by trace ID"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trade_evidence WHERE trace_id = ?", (trace_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"[Evidence] 获取失败 {trace_id}: {e}")
            return None

    def get_trade_attribution(self, trace_id: str) -> Optional[Dict]:
        """Get trade attribution by trace ID"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM trade_attribution WHERE trace_id = ?", (trace_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"[Attribution] 获取失败 {trace_id}: {e}")
            return None

    def get_trade_evidence_chain(self, trace_id: str) -> Optional[Dict]:
        """Get complete evidence chain (evidence + attribution) by trace ID"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT e.*, a.overall_score, a.pnl_category, a.success_factors, a.failure_factors
                    FROM trade_evidence e
                    LEFT JOIN trade_attribution a ON e.trace_id = a.trace_id
                    WHERE e.trace_id = ?
                """, (trace_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"[EvidenceChain] 获取失败 {trace_id}: {e}")
            return None

    def get_trade_evidence_by_filter(self, start_date: str = None, end_date: str = None,
                                     symbol: str = None, strategy: str = None,
                                     scene: str = None, status: str = None,
                                     limit: int = 100) -> List[Dict]:
        """Get trade evidence with filters"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM trade_evidence WHERE 1=1"
                params = []
                if start_date:
                    query += " AND timestamp_open >= ?"; params.append(start_date)
                if end_date:
                    query += " AND timestamp_open <= ?"; params.append(end_date)
                if symbol:
                    query += " AND symbol = ?"; params.append(symbol)
                if strategy:
                    query += " AND strategy_mode = ?"; params.append(strategy)
                if scene:
                    query += " AND primary_scene = ?"; params.append(scene)
                if status:
                    query += " AND status = ?"; params.append(status)
                query += " ORDER BY timestamp_open DESC LIMIT ?"
                params.append(limit)
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[Evidence] 查询失败：{e}")
            return []

    def update_trade_evidence_close(self, trace_id: str, timestamp_close: str,
                                    is_closed: bool = True) -> bool:
        """Update trade evidence when trade closes"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE trade_evidence
                    SET timestamp_close = ?, is_closed = ?, status = 'closed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE trace_id = ?
                """, (timestamp_close, is_closed, trace_id))
            return True
        except Exception as e:
            logger.error(f"[Evidence] 更新失败 {trace_id}: {e}")
            return False

    def get_strategy_performance(self, strategy_name: str, period: str = None) -> Optional[Dict]:
        """Get strategy performance stats"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if period:
                    cursor.execute("""
                        SELECT * FROM strategy_performance
                        WHERE strategy_name = ? AND period = ?
                    """, (strategy_name, period))
                else:
                    cursor.execute("""
                        SELECT * FROM strategy_performance
                        WHERE strategy_name = ?
                        ORDER BY updated_at DESC LIMIT 1
                    """, (strategy_name,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"[StrategyPerformance] 获取失败：{e}")
            return None

    def query_strategy_scene_performance(self, strategy: str, scene: str,
                                         window_days: int = 30) -> Optional[Dict]:
        """
        查询策略在某场景下的历史表现 (供归因引擎使用)

        Args:
            strategy: 策略名称
            scene: 场景名称
            window_days: 回溯天数

        Returns:
            统计字典 {trade_count, win_rate, avg_pnl, sharpe}
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()

                # 从 trade_attribution 和 trade_evidence 关联查询
                cursor.execute("""
                    SELECT
                        COUNT(*) as trade_count,
                        SUM(CASE WHEN a.direction_correct = 1 THEN 1 ELSE 0 END) as winning_count,
                        AVG(CASE WHEN a.direction_correct = 1 THEN 1 ELSE 0 END) as win_rate,
                        AVG(a.overall_score) as avg_score
                    FROM trade_evidence e
                    LEFT JOIN trade_attribution a ON e.trace_id = a.trace_id
                    WHERE e.strategy_mode = ?
                      AND e.primary_scene = ?
                      AND e.timestamp_open >= ?
                """, (strategy, scene, cutoff))

                row = cursor.fetchone()
                if row:
                    stats = dict(row)
                    # 计算简化版 Sharpe (假设无风险利率=0)
                    stats['sharpe'] = stats['avg_score'] / 100.0 if stats['avg_score'] else 0
                    return stats
                return None

        except Exception as e:
            logger.error(f"[QueryStrategyScene] 查询失败 {strategy}/{scene}: {e}")
            return None

    def record_llm_negative_feedback(self, trace_id: str, news_topic: str,
                                     scene: str, penalty: float,
                                     expires_at: str) -> bool:
        """
        记录 LLM 负反馈 (用于防御性更新)

        Args:
            trace_id: 交易追踪 ID
            news_topic: 新闻主题
            scene: 场景名称
            penalty: 权重惩罚值 (如 -0.15)
            expires_at: 过期时间 (ISO 格式)
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO llm_negative_feedback
                    (trace_id, news_topic, scene, penalty, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (trace_id, news_topic, scene, penalty, expires_at))
            return True
        except sqlite3.OperationalError:
            # 表不存在时跳过 (降级处理)
            logger.debug(f"[LLM 负反馈] 表不存在，跳过记录 {trace_id}")
            return True
        except Exception as e:
            logger.error(f"[LLM 负反馈] 记录失败 {trace_id}: {e}")
            return False

    def save_strategy_performance(self, perf_data: Dict[str, Any]) -> bool:
        """Save strategy performance stats"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO strategy_performance
                    (strategy_name, period, start_date, end_date, total_trades, winning_trades,
                     losing_trades, win_rate, total_pnl, avg_pnl, avg_pnl_percent, sharpe_ratio,
                     sortino_ratio, max_drawdown, avg_slippage_bps, avg_latency_ms, quality_score,
                     scene_distribution, regime_distribution, weight_adjustment, scenes_to_avoid,
                     scenes_to_favor, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(strategy_name, period) DO UPDATE SET
                        total_trades = excluded.total_trades,
                        winning_trades = excluded.winning_trades,
                        losing_trades = excluded.losing_trades,
                        win_rate = excluded.win_rate,
                        total_pnl = excluded.total_pnl,
                        avg_pnl = excluded.avg_pnl,
                        avg_pnl_percent = excluded.avg_pnl_percent,
                        sharpe_ratio = excluded.sharpe_ratio,
                        sortino_ratio = excluded.sortino_ratio,
                        max_drawdown = excluded.max_drawdown,
                        avg_slippage_bps = excluded.avg_slippage_bps,
                        avg_latency_ms = excluded.avg_latency_ms,
                        quality_score = excluded.quality_score,
                        scene_distribution = excluded.scene_distribution,
                        regime_distribution = excluded.regime_distribution,
                        weight_adjustment = excluded.weight_adjustment,
                        scenes_to_avoid = excluded.scenes_to_avoid,
                        scenes_to_favor = excluded.scenes_to_favor,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    perf_data.get('strategy_name'), perf_data.get('period'),
                    perf_data.get('start_date'), perf_data.get('end_date'),
                    perf_data.get('total_trades', 0), perf_data.get('winning_trades', 0),
                    perf_data.get('losing_trades', 0), perf_data.get('win_rate', 0.0),
                    perf_data.get('total_pnl', 0.0), perf_data.get('avg_pnl', 0.0),
                    perf_data.get('avg_pnl_percent', 0.0), perf_data.get('sharpe_ratio', 0.0),
                    perf_data.get('sortino_ratio', 0.0), perf_data.get('max_drawdown', 0.0),
                    perf_data.get('avg_slippage_bps', 0.0), perf_data.get('avg_latency_ms', 0.0),
                    perf_data.get('quality_score', 0.0), perf_data.get('scene_distribution'),
                    perf_data.get('regime_distribution'), perf_data.get('weight_adjustment'),
                    perf_data.get('scenes_to_avoid'), perf_data.get('scenes_to_favor')
                ))
            return True
        except Exception as e:
            logger.error(f"[StrategyPerformance] 保存失败：{e}")
            return False

    def save_scene_performance(self, perf_data: Dict[str, Any]) -> bool:
        """Save scene performance stats"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO scene_performance
                    (scene_name, period, start_date, end_date, total_trades, winning_trades,
                     win_rate, total_pnl, avg_pnl_percent, confidence_adjustment,
                     threshold_adjustment, is_gray_zone, strategies_distribution, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(scene_name, period) DO UPDATE SET
                        total_trades = excluded.total_trades,
                        winning_trades = excluded.winning_trades,
                        win_rate = excluded.win_rate,
                        total_pnl = excluded.total_pnl,
                        avg_pnl_percent = excluded.avg_pnl_percent,
                        confidence_adjustment = excluded.confidence_adjustment,
                        threshold_adjustment = excluded.threshold_adjustment,
                        is_gray_zone = excluded.is_gray_zone,
                        strategies_distribution = excluded.strategies_distribution,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    perf_data.get('scene_name'), perf_data.get('period'),
                    perf_data.get('start_date'), perf_data.get('end_date'),
                    perf_data.get('total_trades', 0), perf_data.get('winning_trades', 0),
                    perf_data.get('win_rate', 0.0), perf_data.get('total_pnl', 0.0),
                    perf_data.get('avg_pnl_percent', 0.0), perf_data.get('confidence_adjustment'),
                    perf_data.get('threshold_adjustment'), perf_data.get('is_gray_zone', False),
                    perf_data.get('strategies_distribution')
                ))
            return True
        except Exception as e:
            logger.error(f"[ScenePerformance] 保存失败：{e}")
            return False

    def save_execution_quality(self, exec_data: Dict[str, Any]) -> bool:
        """Save execution quality record"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO execution_quality
                    (trace_id, symbol, strategy, slippage_bps, latency_ms, fill_rate,
                     execution_quality, order_type_suggestion, slippage_tolerance_adjustment,
                     execution_rules, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    exec_data.get('trace_id'), exec_data.get('symbol'),
                    exec_data.get('strategy'), exec_data.get('slippage_bps'),
                    exec_data.get('latency_ms'), exec_data.get('fill_rate', 1.0),
                    exec_data.get('execution_quality'), exec_data.get('order_type_suggestion'),
                    exec_data.get('slippage_tolerance_adjustment'), exec_data.get('execution_rules')
                ))
            return True
        except Exception as e:
            logger.error(f"[ExecutionQuality] 保存失败：{e}")
            return False

    def save_review_summary(self, summary_data: Dict[str, Any]) -> bool:
        """Save review summary"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO review_summary
                    (review_type, review_date, period_start, period_end, total_trades,
                     winning_trades, total_pnl, avg_pnl_percent, win_rate, sharpe_ratio,
                     max_drawdown, by_strategy, by_scene, by_regime, noise_signals,
                     overfit_signals, improvement_suggestions, auto_generated, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(review_type, review_date) DO UPDATE SET
                        period_start = excluded.period_start,
                        period_end = excluded.period_end,
                        total_trades = excluded.total_trades,
                        winning_trades = excluded.winning_trades,
                        total_pnl = excluded.total_pnl,
                        avg_pnl_percent = excluded.avg_pnl_percent,
                        win_rate = excluded.win_rate,
                        sharpe_ratio = excluded.sharpe_ratio,
                        max_drawdown = excluded.max_drawdown,
                        by_strategy = excluded.by_strategy,
                        by_scene = excluded.by_scene,
                        by_regime = excluded.by_regime,
                        noise_signals = excluded.noise_signals,
                        overfit_signals = excluded.overfit_signals,
                        improvement_suggestions = excluded.improvement_suggestions,
                        auto_generated = excluded.auto_generated,
                        created_at = CURRENT_TIMESTAMP
                """, (
                    summary_data.get('review_type'), summary_data.get('review_date'),
                    summary_data.get('period_start'), summary_data.get('period_end'),
                    summary_data.get('total_trades', 0), summary_data.get('winning_trades', 0),
                    summary_data.get('total_pnl', 0.0), summary_data.get('avg_pnl_percent', 0.0),
                    summary_data.get('win_rate', 0.0), summary_data.get('sharpe_ratio', 0.0),
                    summary_data.get('max_drawdown', 0.0), summary_data.get('by_strategy'),
                    summary_data.get('by_scene'), summary_data.get('by_regime'),
                    summary_data.get('noise_signals'), summary_data.get('overfit_signals'),
                    summary_data.get('improvement_suggestions'),
                    summary_data.get('auto_generated', True)
                ))
            return True
        except Exception as e:
            logger.error(f"[ReviewSummary] 保存失败：{e}")
            return False

    def get_review_summaries(self, review_type: str = None, limit: int = 30) -> List[Dict]:
        """Get review summaries"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if review_type:
                    cursor.execute("""
                        SELECT * FROM review_summary
                        WHERE review_type = ?
                        ORDER BY review_date DESC LIMIT ?
                    """, (review_type, limit))
                else:
                    cursor.execute("""
                        SELECT * FROM review_summary
                        ORDER BY review_date DESC LIMIT ?
                    """, (limit,))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[ReviewSummary] 查询失败：{e}")
            return []

    def save_weekly_optimization_report(self, report: Dict[str, Any]) -> bool:
        """
        保存每周归因优化报告 (2026-04-12 新增)
        
        Args:
            report: 优化报告字典
            
        Returns:
            是否保存成功
        """
        try:
            import json
            
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO weekly_optimization_reports
                    (report_date, period_start, period_end, total_trades,
                     suggestions, llm_issues, execution_quality, matrix_data,
                     created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(report_date) DO UPDATE SET
                        period_start = excluded.period_start,
                        period_end = excluded.period_end,
                        total_trades = excluded.total_trades,
                        suggestions = excluded.suggestions,
                        llm_issues = excluded.llm_issues,
                        execution_quality = excluded.execution_quality,
                        matrix_data = excluded.matrix_data,
                        created_at = CURRENT_TIMESTAMP
                """, (
                    report.get('report_date'),
                    report.get('period_start'),
                    report.get('period_end'),
                    report.get('total_trades', 0),
                    json.dumps(report.get('suggestions', []), ensure_ascii=False),
                    json.dumps(report.get('llm_issues', []), ensure_ascii=False),
                    json.dumps(report.get('execution_quality', {}), ensure_ascii=False),
                    json.dumps(report.get('matrix', {}), ensure_ascii=False, default=str),
                ))
            return True
        except Exception as e:
            logger.error(f"[WeeklyOptimization] 保存失败：{e}")
            return False

    def get_weekly_optimization_reports(self, limit: int = 10) -> List[Dict]:
        """
        获取每周归因优化报告 (2026-04-12 新增)
        
        Args:
            limit: 返回报告数量限制
            
        Returns:
            报告列表
        """
        try:
            import json
            
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM weekly_optimization_reports
                    ORDER BY report_date DESC LIMIT ?
                """, (limit,))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[WeeklyOptimization] 查询失败：{e}")
            return []


    def close(self):
        """
        关闭数据库连接池 (P0 修复 2026-04-11)
        
        在系统退出时调用，确保所有连接正确关闭
        """
        if hasattr(self, '_pool'):
            self._pool.close_all()
            logger.info("数据库连接池已关闭")


# 全局单例
db: Optional[Database] = None


def init_database(db_path: str = None, pool_size: int = 5) -> Database:
    """
    初始化数据库单例
    
    Args:
        db_path: 数据库路径
        pool_size: 连接池大小 (默认5)
    """
    global db
    db = Database(db_path, pool_size=pool_size)
    return db


def get_database() -> Database:
    """获取数据库单例"""
    if db is None:
        return init_database()
    return db
