-- ============================================
-- Migration 001: 持仓表扩展
-- 描述：创建/扩展 paper_positions 表，支持持仓跟踪和平仓记录
-- 日期：2026-03-13
-- ============================================

-- 1. paper_positions 表 - 持仓跟踪表
-- 支持开仓→持仓→平仓的完整流程
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,              -- 证券代码
    action TEXT NOT NULL,            -- 买卖方向 (BUY/SELL)
    quantity INTEGER NOT NULL,       -- 持仓数量
    entry_price REAL NOT NULL,       -- 建仓价格
    entry_time DATETIME NOT NULL,    -- 建仓时间
    current_price REAL,              -- 当前价格
    market_value REAL,               -- 市值
    unrealized_pnl REAL,             -- 浮动盈亏
    unrealized_pnl_rate REAL,        -- 浮动盈亏率
    status TEXT DEFAULT 'OPEN',      -- 状态 (OPEN/CLOSED)
    exit_price REAL,                 -- 平仓价格
    exit_time DATETIME,              -- 平仓时间
    realized_pnl REAL,               -- 实现盈亏
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 索引 - 优化查询性能
CREATE INDEX IF NOT EXISTS idx_positions_code ON paper_positions(code);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(status);

-- 3. paper_trades 表扩展 - 添加订单关联和费用字段
-- 注意：以下列如已存在则跳过 (SQLite 不支持 IF NOT EXISTS)
-- ALTER TABLE paper_trades ADD COLUMN order_id TEXT;
-- ALTER TABLE paper_trades ADD COLUMN position_id INTEGER;
-- ALTER TABLE paper_trades ADD COLUMN commission REAL DEFAULT 0;
-- ALTER TABLE paper_trades ADD COLUMN trade_type TEXT DEFAULT 'OPEN';
