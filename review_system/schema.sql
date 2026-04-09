-- ============================================
-- 深度复盘系统 - 影子数据库 schema
-- 版本: v1.0
-- 日期: 2026-03-31
-- ============================================

-- 创建影子数据库 (在 MySQL 中运行此脚本)
-- 导入命令: mysql -u root -p rebalance_shadow < schema.sql

-- ============================================
-- 表 1: trades_review (交易复盘表)
-- ============================================
CREATE TABLE IF NOT EXISTS trades_review (
    id VARCHAR(32) PRIMARY KEY,
    signal_id VARCHAR(32),
    code VARCHAR(16) NOT NULL,
    name VARCHAR(64),
    action ENUM('BUY', 'SELL') NOT NULL,
    entry_price DECIMAL(10,2) NOT NULL,
    exit_price DECIMAL(10,2),
    quantity INT NOT NULL,
    pnl DECIMAL(12,2),
    pnl_percent DECIMAL(6,4),
    fees DECIMAL(8,2) DEFAULT 0,
    strategy VARCHAR(64),
    entry_time DATETIME NOT NULL,
    exit_time DATETIME,
    exit_reason VARCHAR(64),
    order_id VARCHAR(32),
    status VARCHAR(16) DEFAULT 'open',
    account_type VARCHAR(16) DEFAULT 'real',
    
    -- 决策复盘字段
    decision_reason VARCHAR(64),
    decision_confidence TINYINT,
    strategy_type VARCHAR(64),
    decision_time_spent INT,
    decision_quality_score TINYINT,
    decision_notes TEXT,
    
    -- 时机复盘字段
    entry_timing_score TINYINT,
    exit_timing_score TINYINT,
    entry_vs_signal_delay INT,
    market_phase VARCHAR(16),
    timing_notes TEXT,
    
    -- 持仓复盘字段
    position_size_actual DECIMAL(6,4),
    position_size_planned DECIMAL(6,4),
    holding_period_days INT,
    position_adjustments TINYINT,
    position_notes TEXT,
    
    -- 风控复盘字段
    stop_loss_planned DECIMAL(10,2),
    stop_loss_executed TINYINT,
    stop_loss_adjusted TINYINT,
    max_drawdown_during_trade DECIMAL(8,4),
    risk_reward_actual DECIMAL(6,4),
    risk_notes TEXT,
    
    -- 心理复盘字段
    emotion_state VARCHAR(16),
    emotion_intensity TINYINT,
    confidence_entry TINYINT,
    confidence_exit TINYINT,
    psychological_notes TEXT,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_code (code),
    INDEX idx_account_type (account_type),
    INDEX idx_entry_time (entry_time),
    INDEX idx_strategy (strategy),
    INDEX idx_emotion (emotion_state),
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 2: trade_reviews (复盘总结表)
-- ============================================
CREATE TABLE IF NOT EXISTS trade_reviews (
    id VARCHAR(32) PRIMARY KEY,
    trade_id VARCHAR(32) NOT NULL,
    trader_id VARCHAR(32) DEFAULT 'user_001',
    review_date DATE NOT NULL,
    
    overall_score TINYINT,
    decision_score TINYINT,
    timing_score TINYINT,
    position_score TINYINT,
    risk_score TINYINT,
    psychology_score TINYINT,
    
    what_went_well TEXT,
    what_went_wrong TEXT,
    key_learnings TEXT,
    improvement_actions TEXT,
    tags JSON,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_trade_id (trade_id),
    INDEX idx_review_date (review_date),
    INDEX idx_trader_id (trader_id),
    FOREIGN KEY (trade_id) REFERENCES trades_review(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 3: strategy_performance (策略表现表)
-- ============================================
CREATE TABLE IF NOT EXISTS strategy_performance (
    id VARCHAR(32) PRIMARY KEY,
    strategy_name VARCHAR(64) NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    account_type VARCHAR(16) DEFAULT 'real',
    
    total_trades INT DEFAULT 0,
    winning_trades INT DEFAULT 0,
    losing_trades INT DEFAULT 0,
    breakeven_trades INT DEFAULT 0,
    
    total_pnl DECIMAL(12,2) DEFAULT 0,
    avg_pnl DECIMAL(10,2) DEFAULT 0,
    max_win DECIMAL(10,2) DEFAULT 0,
    max_loss DECIMAL(10,2) DEFAULT 0,
    win_rate DECIMAL(6,4) DEFAULT 0,
    profit_factor DECIMAL(8,4) DEFAULT 0,
    
    avg_holding_period DECIMAL(4,2) DEFAULT 0,
    max_drawdown DECIMAL(8,4) DEFAULT 0,
    sharpe_ratio DECIMAL(6,4) DEFAULT 0,
    sortino_ratio DECIMAL(6,4) DEFAULT 0,
    
    avg_position_size DECIMAL(6,4) DEFAULT 0,
    avg_risk_reward DECIMAL(6,4) DEFAULT 0,
    strategy_type VARCHAR(64),
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uk_strategy_period (strategy_name, period_start, period_end, account_type),
    INDEX idx_name (strategy_name),
    INDEX idx_period (period_start, period_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 4: error_patterns (错误模式表)
-- ============================================
CREATE TABLE IF NOT EXISTS error_patterns (
    id VARCHAR(32) PRIMARY KEY,
    pattern_name VARCHAR(64) NOT NULL,
    pattern_category VARCHAR(16),
    description TEXT,
    example_trades JSON,
    occurrence_count INT DEFAULT 0,
    first_occurrence DATE,
    last_occurrence DATE,
    avg_pnl_impact DECIMAL(10,2) DEFAULT 0,
    severity_level ENUM('low', 'medium', 'high', 'critical') DEFAULT 'low',
    frequency_trend ENUM('increasing', 'stable', 'decreasing') DEFAULT 'stable',
    suggested_action TEXT,
    is_resolved BOOLEAN DEFAULT FALSE,
    resolved_date DATE,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_category (pattern_category),
    INDEX idx_severity (severity_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 5: psychology_stats (心理状态表)
-- ============================================
CREATE TABLE IF NOT EXISTS psychology_stats (
    id VARCHAR(32) PRIMARY KEY,
    trader_id VARCHAR(32) DEFAULT 'user_001',
    stat_date DATE NOT NULL,
    
    calm_count INT DEFAULT 0,
    anxious_count INT DEFAULT 0,
    excited_count INT DEFAULT 0,
    fearful_count INT DEFAULT 0,
    greedy_count INT DEFAULT 0,
    frustrated_count INT DEFAULT 0,
    
    total_trades INT DEFAULT 0,
    avg_emotion_intensity DECIMAL(4,2) DEFAULT 0,
    impulse_trade_ratio DECIMAL(6,4) DEFAULT 0,
    confidence_avg DECIMAL(4,2) DEFAULT 0,
    psychology_impact_score DECIMAL(4,2) DEFAULT 0,
    best_emotion_state VARCHAR(16),
    worst_emotion_state VARCHAR(16),
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uk_trader_date (trader_id, stat_date),
    INDEX idx_date (stat_date),
    INDEX idx_trader (trader_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 6: improvement_actions (改进行动表)
-- ============================================
CREATE TABLE IF NOT EXISTS improvement_actions (
    id VARCHAR(32) PRIMARY KEY,
    trader_id VARCHAR(32) Default 'user_001',
    action_title VARCHAR(128) NOT NULL,
    action_description TEXT,
    action_category VARCHAR(16),
    priority ENUM('low', 'medium', 'high', 'critical') DEFAULT 'medium',
    source VARCHAR(32),
    status VARCHAR(16) DEFAULT 'pending',
    created_date DATE NOT NULL,
    target_date DATE,
    completed_date DATE,
    effectiveness_score TINYINT,
    effectiveness_notes TEXT,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_status (status),
    INDEX idx_category (action_category),
    INDEX idx_trader (trader_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 7: best_worst_trades (最佳/最差交易表)
-- ============================================
CREATE TABLE IF NOT EXISTS best_worst_trades (
    id VARCHAR(32) PRIMARY KEY,
    trade_id VARCHAR(32) NOT NULL,
    record_type ENUM('best', 'worst') NOT NULL,
    period ENUM('daily', 'weekly', 'monthly', 'all_time') NOT NULL,
    
    code VARCHAR(16) NOT NULL,
    action ENUM('BUY', 'SELL') NOT NULL,
    entry_price DECIMAL(10,2) NOT NULL,
    exit_price DECIMAL(10,2) NOT NULL,
    pnl DECIMAL(10,2) NOT NULL,
    pnl_percent DECIMAL(6,4) NOT NULL,
    
    highlight TEXT,
    key_factor VARCHAR(64),
    record_date DATE NOT NULL,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_type (record_type),
    INDEX idx_period (period),
    INDEX idx_date (record_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 表 8: sim_vs_real_comparison (模拟 vs 实盘对比表)
-- ============================================
CREATE TABLE IF NOT EXISTS sim_vs_real_comparison (
    id VARCHAR(32) PRIMARY KEY,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    
    -- 模拟账户
    sim_total_trades INT DEFAULT 0,
    sim_winning_trades INT DEFAULT 0,
    sim_total_pnl DECIMAL(12,2) DEFAULT 0,
    sim_win_rate DECIMAL(6,4) DEFAULT 0,
    sim_avg_pnl DECIMAL(10,2) DEFAULT 0,
    sim_max_drawdown DECIMAL(8,4) DEFAULT 0,
    sim_sharpe_ratio DECIMAL(6,4) DEFAULT 0,
    
    -- 实盘账户
    real_total_trades INT DEFAULT 0,
    real_winning_trades INT DEFAULT 0,
    real_total_pnl DECIMAL(12,2) DEFAULT 0,
    real_win_rate DECIMAL(6,4) DEFAULT 0,
    real_avg_pnl DECIMAL(10,2) DEFAULT 0,
    real_max_drawdown DECIMAL(8,4) DEFAULT 0,
    real_sharpe_ratio DECIMAL(6,4) DEFAULT 0,
    
    -- 差异分析
    win_rate_diff DECIMAL(6,4) DEFAULT 0,
    pnl_diff DECIMAL(12,2) DEFAULT 0,
    psychology_gap_score DECIMAL(4,2) DEFAULT 0,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    UNIQUE KEY uk_period (period_start, period_end),
    INDEX idx_period (period_start, period_end)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================
-- 插入测试数据 (可选)
-- ============================================
-- INSERT INTO improvement_actions (id, trader_id, action_title, action_category, priority, source, created_date)
-- VALUES 
-- ('IMP001', 'user_001', '增加开盘前检查', 'decision', 'high', 'daily_review', '2026-03-31'),
-- ('IMP002', 'user_001', '优化止损规则', 'risk', 'medium', 'weekly_review', '2026-03-31');
