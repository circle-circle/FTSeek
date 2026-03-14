#!/usr/bin/env python3
"""
创建回测数据表

用于存储历史回测交易数据，供回测对比模块使用。
"""
import sqlite3
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config


def create_backtest_trades_table(db_path: str = None):
    """创建回测交易表"""
    if db_path is None:
        db_path = str(config.DATA_DIR / "ftseek.db")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 创建回测交易表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            strategy TEXT,
            buy_price REAL NOT NULL,
            sell_price REAL NOT NULL,
            quantity INTEGER DEFAULT 1,
            return_rate REAL,
            profit REAL,
            open_time DATETIME,
            close_time DATETIME,
            status TEXT DEFAULT 'closed'
        )
    """)

    # 创建索引
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_backtest_trades_code 
        ON backtest_trades(code)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_backtest_trades_strategy 
        ON backtest_trades(strategy)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_backtest_trades_close_time 
        ON backtest_trades(close_time)
    """)

    conn.commit()
    conn.close()

    print(f"✅ 回测交易表已创建：{db_path}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='创建回测数据表')
    parser.add_argument('--db', type=str, default=None, help='数据库路径')
    args = parser.parse_args()

    create_backtest_trades_table(db_path=args.db)
