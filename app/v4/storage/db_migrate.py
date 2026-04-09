#!/usr/bin/env python3
"""
交易决策引擎数据库迁移脚本

功能:
- 创建所有必要的数据库表
- 启用 WAL 模式避免锁死
- 幂等执行（可重复运行）
- 与现有数据库隔离（不修改现有表）

使用方法:
    python app/v4/storage/db_migrate.py

验证:
    sqlite3 data/trading_decision.db ".schema"
"""
import sqlite3
import logging
from pathlib import Path
from datetime import datetime
import sys
import os

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.v4.db_migrate")
logger.propagate = False  # 防止日志重复


def run_migration(db_path: str = None) -> bool:
    """
    执行数据库迁移
    
    Args:
        db_path: 数据库文件路径，默认使用配置目录
        
    Returns:
        bool: 迁移是否成功
    """
    if db_path is None:
        db_path = str(config.DATA_DIR / "trading_decision.db")
    
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"开始迁移数据库：{db_path}")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # 启用 WAL 模式
        cursor.execute("PRAGMA journal_mode=WAL")
        wal_result = cursor.fetchone()[0]
        logger.info(f"日志模式：{wal_result}")
        
        # 创建交易模式表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trading_modes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                mode TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("✓ trading_modes 表已创建/存在")
        
        # 创建索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trading_modes_code 
            ON trading_modes(code)
        """)
        
        # 创建模拟持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                entry_time DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("✓ paper_positions 表已创建/存在")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_paper_positions_code 
            ON paper_positions(code)
        """)
        
        # 创建交易记录表
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
        logger.info("✓ paper_trades 表已创建/存在")
        
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
        
        # 创建绩效统计表
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
        logger.info("✓ performance_stats 表已创建/存在")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_performance_stats_code 
            ON performance_stats(code)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_performance_stats_date 
            ON performance_stats(date)
        """)
        
        # 创建晋级历史表
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
        logger.info("✓ graduation_history 表已创建/存在")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_graduation_history_code 
            ON graduation_history(code)
        """)
        
        conn.commit()
        conn.close()
        
        logger.info("=" * 50)
        logger.info("数据库迁移完成！")
        logger.info(f"数据库位置：{db_path}")
        logger.info("已创建的表:")
        logger.info("  - trading_modes (交易模式)")
        logger.info("  - paper_positions (模拟持仓)")
        logger.info("  - paper_trades (交易记录)")
        logger.info("  - performance_stats (绩效统计)")
        logger.info("  - graduation_history (晋级历史)")
        logger.info("=" * 50)
        
        return True
        
    except Exception as e:
        logger.error(f"迁移失败：{e}")
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False


def verify_migration(db_path: str = None) -> bool:
    """
    验证迁移结果
    
    Args:
        db_path: 数据库文件路径
        
    Returns:
        bool: 验证是否通过
    """
    if db_path is None:
        db_path = str(config.DATA_DIR / "trading_decision.db")
    
    logger.info(f"验证数据库：{db_path}")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # 检查表是否存在
        expected_tables = [
            'trading_modes',
            'paper_positions',
            'paper_trades',
            'performance_stats',
            'graduation_history'
        ]
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        all_present = True
        for table in expected_tables:
            if table in tables:
                logger.info(f"✓ 表 {table} 存在")
            else:
                logger.error(f"✗ 表 {table} 不存在")
                all_present = False
        
        # 检查 WAL 模式
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        if mode == "wal":
            logger.info("✓ WAL 模式已启用")
        else:
            logger.warning(f"⚠ WAL 模式未启用，当前模式：{mode}")
        
        conn.close()
        
        if all_present:
            logger.info("验证通过！")
            return True
        else:
            logger.error("验证失败！")
            return False
            
    except Exception as e:
        logger.error(f"验证失败：{e}")
        return False


def show_schema(db_path: str = None):
    """
    显示数据库表结构
    
    Args:
        db_path: 数据库文件路径
    """
    if db_path is None:
        db_path = str(config.DATA_DIR / "trading_decision.db")
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        
        print("\n" + "=" * 60)
        print("数据库表结构")
        print("=" * 60)
        
        for table in tables:
            if not table.startswith('sqlite_'):
                print(f"\n表：{table}")
                print("-" * 60)
                cursor.execute(f"PRAGMA table_info({table})")
                columns = cursor.fetchall()
                for col in columns:
                    print(f"  {col[1]:20} {col[2]:10} {'NOT NULL' if col[3] else 'NULL':8} DEFAULT: {col[4]}")
        
        conn.close()
        print("=" * 60)
        
    except Exception as e:
        logger.error(f"显示表结构失败：{e}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="交易决策引擎数据库迁移工具")
    parser.add_argument("--verify", action="store_true", help="验证迁移结果")
    parser.add_argument("--schema", action="store_true", help="显示表结构")
    parser.add_argument("--db-path", type=str, help="数据库文件路径")
    
    args = parser.parse_args()
    
    if args.verify:
        success = verify_migration(args.db_path)
        sys.exit(0 if success else 1)
    elif args.schema:
        show_schema(args.db_path)
    else:
        success = run_migration(args.db_path)
        sys.exit(0 if success else 1)
