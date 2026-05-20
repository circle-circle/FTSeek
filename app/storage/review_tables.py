"""
深度复盘分析系统 - 数据库表定义 (美股版)

适配美股多 Agent 架构，8 张核心复盘表：
1. trades_review (扩展现有交易表，新增复盘字段)
2. trade_reviews (复盘总结表)
3. strategy_performance (策略表现表)
4. error_patterns (错误模式表)
5. psychology_stats (心理状态表)
6. improvement_actions (改进行动表)
7. best_worst_trades (最佳/最差交易表)
8. sim_vs_real_comparison (模拟 vs 实盘对比表)
"""
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import uuid

logger = logging.getLogger("ftseek_us.storage.review_tables")
logger.propagate = False


class ReviewDatabase:
    """复盘分析数据库管理器 (美股版)"""
    
    def __init__(self, db_path: str = None):
        """
        初始化复盘数据库
        
        Args:
            db_path: 数据库文件路径
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app.core.paths import get_review_db
            self.db_path = get_review_db()
        
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_tables()
        logger.info(f"复盘数据库已初始化：{self.db_path}")
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
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
        """初始化 8 张复盘表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # ============================================
            # 表 1: trades_review (扩展现有交易表，新增复盘字段)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades_review (
                    -- 基础信息
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
                    account_type TEXT DEFAULT 'real',
                    
                    -- 多 Agent 协作字段
                    analyst_score REAL DEFAULT 0,
                    strategist_recommendation TEXT,
                    risk_officer_approval BOOLEAN DEFAULT FALSE,
                    executor_notes TEXT,
                    
                    -- 决策复盘字段 (6 个)
                    decision_reason TEXT,
                    decision_confidence INTEGER,
                    strategy_type TEXT,
                    decision_time_spent INTEGER,
                    decision_quality_score INTEGER,
                    decision_notes TEXT,
                    
                    -- 时机复盘字段 (5 个)
                    entry_timing_score INTEGER,
                    exit_timing_score INTEGER,
                    entry_vs_signal_delay INTEGER,
                    market_phase TEXT,
                    timing_notes TEXT,
                    
                    -- 持仓复盘字段 (4 个)
                    position_size_actual REAL,
                    position_size_planned REAL,
                    holding_period_days INTEGER,
                    position_adjustments INTEGER,
                    position_notes TEXT,
                    
                    -- 风控复盘字段 (5 个)
                    stop_loss_planned REAL,
                    stop_loss_executed BOOLEAN,
                    stop_loss_adjusted BOOLEAN,
                    max_drawdown_during_trade REAL,
                    risk_reward_actual REAL,
                    risk_notes TEXT,
                    
                    -- 心理复盘字段 (5 个)
                    emotion_state TEXT,
                    emotion_intensity INTEGER,
                    confidence_entry INTEGER,
                    confidence_exit INTEGER,
                    psychological_notes TEXT,
                    
                    -- 系统字段
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    FOREIGN KEY (signal_id) REFERENCES signals(id)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_review_code ON trades_review(code)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_review_account_type ON trades_review(account_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_review_entry_time ON trades_review(entry_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_review_strategy ON trades_review(strategy)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_review_emotion ON trades_review(emotion_state)")
            
            # ============================================
            # 表 2: trade_reviews (复盘总结表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_reviews (
                    id TEXT PRIMARY KEY,
                    trade_id TEXT NOT NULL,
                    trader_id TEXT DEFAULT 'user_001',
                    review_date DATE NOT NULL,
                    
                    overall_score INTEGER,
                    decision_score INTEGER,
                    timing_score INTEGER,
                    position_score INTEGER,
                    risk_score INTEGER,
                    psychology_score INTEGER,
                    
                    what_went_well TEXT,
                    what_went_wrong TEXT,
                    key_learnings TEXT,
                    improvement_actions TEXT,
                    
                    tags TEXT,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    FOREIGN KEY (trade_id) REFERENCES trades_review(id)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_reviews_trade_id ON trade_reviews(trade_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_reviews_review_date ON trade_reviews(review_date)")
            
            # ============================================
            # 表 3: strategy_performance (策略表现表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id TEXT PRIMARY KEY,
                    strategy_name TEXT NOT NULL,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    account_type TEXT DEFAULT 'real',
                    
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    breakeven_trades INTEGER DEFAULT 0,
                    
                    total_pnl REAL DEFAULT 0,
                    avg_pnl REAL DEFAULT 0,
                    max_win REAL DEFAULT 0,
                    max_loss REAL DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    profit_factor REAL DEFAULT 0,
                    
                    avg_holding_period REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0,
                    sharpe_ratio REAL DEFAULT 0,
                    sortino_ratio REAL DEFAULT 0,
                    
                    avg_position_size REAL DEFAULT 0,
                    avg_risk_reward REAL DEFAULT 0,
                    strategy_type TEXT,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    UNIQUE(strategy_name, period_start, period_end, account_type)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_strategy_performance_name ON strategy_performance(strategy_name)")
            
            # ============================================
            # 表 4: error_patterns (错误模式表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS error_patterns (
                    id TEXT PRIMARY KEY,
                    pattern_name TEXT NOT NULL,
                    pattern_category TEXT,
                    
                    description TEXT,
                    example_trades TEXT,
                    
                    occurrence_count INTEGER DEFAULT 0,
                    first_occurrence DATE,
                    last_occurrence DATE,
                    avg_pnl_impact REAL DEFAULT 0,
                    
                    severity_level TEXT,
                    frequency_trend TEXT,
                    
                    suggested_action TEXT,
                    is_resolved BOOLEAN DEFAULT FALSE,
                    resolved_date DATE,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_patterns_category ON error_patterns(pattern_category)")
            
            # ============================================
            # 表 5: psychology_stats (心理状态表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS psychology_stats (
                    id TEXT PRIMARY KEY,
                    trader_id TEXT DEFAULT 'user_001',
                    stat_date DATE NOT NULL,
                    
                    calm_count INTEGER DEFAULT 0,
                    anxious_count INTEGER DEFAULT 0,
                    excited_count INTEGER DEFAULT 0,
                    fearful_count INTEGER DEFAULT 0,
                    greedy_count INTEGER DEFAULT 0,
                    frustrated_count INTEGER DEFAULT 0,
                    
                    total_trades INTEGER DEFAULT 0,
                    avg_emotion_intensity REAL DEFAULT 0,
                    impulse_trade_ratio REAL DEFAULT 0,
                    confidence_avg REAL DEFAULT 0,
                    
                    psychology_impact_score REAL DEFAULT 0,
                    best_emotion_state TEXT,
                    worst_emotion_state TEXT,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    UNIQUE(trader_id, stat_date)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_psychology_stats_date ON psychology_stats(stat_date)")
            
            # ============================================
            # 表 6: improvement_actions (改进行动表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS improvement_actions (
                    id TEXT PRIMARY KEY,
                    trader_id TEXT DEFAULT 'user_001',
                    
                    action_title TEXT NOT NULL,
                    action_description TEXT,
                    action_category TEXT,
                    
                    priority TEXT,
                    source TEXT,
                    
                    status TEXT DEFAULT 'pending',
                    created_date DATE NOT NULL,
                    target_date DATE,
                    completed_date DATE,
                    
                    effectiveness_score INTEGER,
                    effectiveness_notes TEXT,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_improvement_actions_status ON improvement_actions(status)")
            
            # ============================================
            # 表 7: best_worst_trades (最佳/最差交易表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS best_worst_trades (
                    id TEXT PRIMARY KEY,
                    trade_id TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    period TEXT NOT NULL,
                    
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    pnl REAL NOT NULL,
                    pnl_percent REAL NOT NULL,
                    
                    highlight TEXT,
                    key_factor TEXT,
                    
                    record_date DATE NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    UNIQUE(trade_id, record_type, period)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_best_worst_type ON best_worst_trades(record_type)")
            
            # ============================================
            # 表 8: sim_vs_real_comparison (模拟 vs 实盘对比表)
            # ============================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sim_vs_real_comparison (
                    id TEXT PRIMARY KEY,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    
                    sim_total_trades INTEGER DEFAULT 0,
                    sim_winning_trades INTEGER DEFAULT 0,
                    sim_total_pnl REAL DEFAULT 0,
                    sim_win_rate REAL DEFAULT 0,
                    sim_avg_pnl REAL DEFAULT 0,
                    sim_max_drawdown REAL DEFAULT 0,
                    sim_sharpe_ratio REAL DEFAULT 0,
                    
                    real_total_trades INTEGER DEFAULT 0,
                    real_winning_trades INTEGER DEFAULT 0,
                    real_total_pnl REAL DEFAULT 0,
                    real_win_rate REAL DEFAULT 0,
                    real_avg_pnl REAL DEFAULT 0,
                    real_max_drawdown REAL DEFAULT 0,
                    real_sharpe_ratio REAL DEFAULT 0,
                    
                    win_rate_diff REAL DEFAULT 0,
                    pnl_diff REAL DEFAULT 0,
                    psychology_gap_score REAL DEFAULT 0,
                    
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    
                    UNIQUE(period_start, period_end)
                )
            """)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sim_real_period ON sim_vs_real_comparison(period_start, period_end)")
            
            logger.info("8 张复盘表初始化完成 (美股版)")
    
    # ============================================
    # 通用 CRUD 操作
    # ============================================
    
    def add_trade_review(self, trade_data: Dict[str, Any]) -> str:
        """添加交易复盘记录"""
        trade_id = trade_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in trade_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [trade_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO trades_review 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [trade_id] + values)
            
            logger.info(f"添加交易复盘记录：{trade_id}")
            return trade_id
    
    def get_trade_review(self, trade_id: str) -> Optional[Dict]:
        """获取交易复盘记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades_review WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_trade_review(self, trade_id: str, updates: Dict[str, Any]) -> bool:
        """更新交易复盘记录"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            set_clause = ', '.join([f"{key} = ?" for key in updates.keys()])
            values = list(updates.values()) + [trade_id]
            
            cursor.execute(f"""
                UPDATE trades_review 
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, values)
            
            return cursor.rowcount > 0
    
    def add_review_summary(self, review_data: Dict[str, Any]) -> str:
        """添加复盘总结"""
        review_id = review_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in review_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [review_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO trade_reviews 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [review_id] + values)
            
            return review_id
    
    def get_review_summary(self, trade_id: str) -> Optional[Dict]:
        """获取复盘总结"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_reviews WHERE trade_id = ?", (trade_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def save_strategy_performance(self, perf_data: Dict[str, Any]) -> str:
        """保存策略表现"""
        perf_id = perf_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in perf_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [perf_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO strategy_performance 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [perf_id] + values)
            
            return perf_id
    
    def add_error_pattern(self, pattern_data: Dict[str, Any]) -> str:
        """添加错误模式"""
        pattern_id = pattern_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in pattern_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [pattern_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO error_patterns 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [pattern_id] + values)
            
            return pattern_id
    
    def get_error_patterns(self, category: str = None, severity: str = None) -> List[Dict]:
        """获取错误模式列表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM error_patterns WHERE is_resolved = FALSE"
            params = []
            
            if category:
                query += " AND pattern_category = ?"
                params.append(category)
            if severity:
                query += " AND severity_level = ?"
                params.append(severity)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def save_psychology_stats(self, stats_data: Dict[str, Any]) -> str:
        """保存心理统计"""
        stats_id = stats_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in stats_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [stats_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO psychology_stats 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [stats_id] + values)
            
            return stats_id
    
    def get_psychology_stats(self, trader_id: str, start_date: str, end_date: str) -> List[Dict]:
        """获取心理统计"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM psychology_stats 
                WHERE trader_id = ? AND stat_date BETWEEN ? AND ?
                ORDER BY stat_date DESC
            """, (trader_id, start_date, end_date))
            return [dict(row) for row in cursor.fetchall()]
    
    def add_improvement_action(self, action_data: Dict[str, Any]) -> str:
        """添加改进行动"""
        action_id = action_data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in action_data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [action_data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO improvement_actions 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [action_id] + values)
            
            return action_id
    
    def update_action_status(self, action_id: str, status: str) -> bool:
        """更新改进行动状态"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE improvement_actions 
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, action_id))
            return cursor.rowcount > 0
    
    def get_pending_actions(self, trader_id: str) -> List[Dict]:
        """获取待处理改进行动"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM improvement_actions 
                WHERE trader_id = ? AND status = 'pending'
                ORDER BY priority DESC, created_date DESC
            """, (trader_id,))
            return [dict(row) for row in cursor.fetchall()]
    
    def record_best_worst_trade(self, data: Dict[str, Any]) -> str:
        """记录最佳/最差交易"""
        record_id = data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO best_worst_trades 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [record_id] + values)
            
            return record_id
    
    def get_best_worst_trades(self, period: str, record_type: str = None) -> List[Dict]:
        """获取最佳/最差交易"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM best_worst_trades WHERE period = ?"
            params = [period]
            
            if record_type:
                query += " AND record_type = ?"
                params.append(record_type)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def save_sim_real_comparison(self, data: Dict[str, Any]) -> str:
        """保存模拟 vs 实盘对比"""
        record_id = data.get('id', str(uuid.uuid4())[:8])
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fields = [k for k in data.keys() if k != 'id']
            placeholders = ', '.join(['?' for _ in fields])
            field_names = ', '.join(fields)
            values = [data[k] for k in fields]
            
            cursor.execute(f"""
                INSERT OR REPLACE INTO sim_vs_real_comparison 
                (id, {field_names}) 
                VALUES (?, {placeholders})
            """, [record_id] + values)
            
            return record_id
    
    def get_sim_real_comparison(self, period_start: str, period_end: str) -> Optional[Dict]:
        """获取模拟 vs 实盘对比"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM sim_vs_real_comparison 
                WHERE period_start = ? AND period_end = ?
            """, (period_start, period_end))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_review_statistics(self, start_date: str, end_date: str, account_type: str = None) -> Dict:
        """获取复盘统计数据"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades_review WHERE entry_time BETWEEN ? AND ?"
            params = [start_date, end_date]
            
            if account_type:
                query += " AND account_type = ?"
                params.append(account_type)
            
            cursor.execute(query, params)
            trades = [dict(row) for row in cursor.fetchall()]
            
            if not trades:
                return {'total_trades': 0}
            
            total_trades = len(trades)
            winning_trades = len([t for t in trades if t.get('pnl', 0) > 0])
            losing_trades = len([t for t in trades if t.get('pnl', 0) < 0])
            total_pnl = sum(t.get('pnl', 0) for t in trades)
            win_rate = winning_trades / total_trades if total_trades > 0 else 0
            
            emotion_counts = {}
            for t in trades:
                emotion = t.get('emotion_state', 'unknown')
                emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
            
            # 处理 None 值
            confidence_values = [t.get('decision_confidence') for t in trades if t.get('decision_confidence') is not None]
            timing_values = [t.get('entry_timing_score') for t in trades if t.get('entry_timing_score') is not None]
            stop_loss_count = len([t for t in trades if t.get('stop_loss_executed') is True])
            
            return {
                'total_trades': total_trades,
                'winning_trades': winning_trades,
                'losing_trades': losing_trades,
                'total_pnl': total_pnl,
                'win_rate': round(win_rate, 4),
                'emotion_distribution': emotion_counts,
                'avg_decision_confidence': sum(confidence_values) / len(confidence_values) if confidence_values else 0,
                'avg_entry_timing_score': sum(timing_values) / len(timing_values) if timing_values else 0,
                'stop_loss_execution_rate': stop_loss_count / total_trades if total_trades > 0 else 0
            }


# 全局单例
_review_db = None

def get_review_database() -> ReviewDatabase:
    """获取复盘数据库单例"""
    global _review_db
    if _review_db is None:
        _review_db = ReviewDatabase()
    return _review_db
