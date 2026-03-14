-- Migration 002: Position Management Fields
-- Date: 2026-03-13
-- Description: Extend paper_positions and paper_trades tables for position management

-- paper_positions 表扩展
-- Note: Using safe approach - columns may already exist
ALTER TABLE paper_positions ADD COLUMN initial_quantity INTEGER;
ALTER TABLE paper_positions ADD COLUMN added_quantity INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN avg_cost REAL;
ALTER TABLE paper_positions ADD COLUMN last_trade_time DATETIME;
ALTER TABLE paper_positions ADD COLUMN trade_count_today INTEGER DEFAULT 0;
ALTER TABLE paper_positions ADD COLUMN stop_loss_price REAL;
ALTER TABLE paper_positions ADD COLUMN take_profit_price REAL;
ALTER TABLE paper_positions ADD COLUMN position_status TEXT DEFAULT 'OPEN';

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_positions_last_trade ON paper_positions(last_trade_time);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(position_status);

-- paper_trades 表扩展
ALTER TABLE paper_trades ADD COLUMN trade_reason TEXT;
ALTER TABLE paper_trades ADD COLUMN is_stop_loss INTEGER DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN is_take_profit INTEGER DEFAULT 0;
