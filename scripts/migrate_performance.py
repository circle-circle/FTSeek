#!/usr/bin/env python3
"""
绩效追踪数据库迁移脚本

用于在现有数据库中创建绩效追踪相关的表结构
支持 dry-run 模式和实际执行模式
"""
import argparse
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.migrate")


def check_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """检查表是否已存在"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone() is not None


def check_index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    """检查索引是否已存在"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='index' AND name=?
    """, (index_name,))
    return cursor.fetchone() is not None


def migrate_trade_pairs(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """迁移 trade_pairs 表"""
    stats = {
        'table_created': False,
        'indexes_created': 0,
        'errors': []
    }
    
    cursor = conn.cursor()
    
    # 创建表
    if not check_table_exists(conn, 'trade_pairs'):
        if dry_run:
            logger.info("[DRY-RUN] 将创建表：trade_pairs")
            stats['table_created'] = True
        else:
            try:
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
                logger.info("✓ 创建表：trade_pairs")
                stats['table_created'] = True
            except Exception as e:
                stats['errors'].append(f"创建 trade_pairs 表失败：{e}")
                logger.error(f"✗ 创建 trade_pairs 表失败：{e}")
    else:
        logger.info("○ 表已存在：trade_pairs")
    
    # 创建索引
    indexes = [
        ('idx_trade_pairs_code', 'CREATE INDEX idx_trade_pairs_code ON trade_pairs(code)'),
        ('idx_trade_pairs_status', 'CREATE INDEX idx_trade_pairs_status ON trade_pairs(status)'),
        ('idx_trade_pairs_close_time', 'CREATE INDEX idx_trade_pairs_close_time ON trade_pairs(close_time)')
    ]
    
    for index_name, create_sql in indexes:
        if not check_index_exists(conn, index_name):
            if dry_run:
                logger.info(f"[DRY-RUN] 将创建索引：{index_name}")
            else:
                try:
                    cursor.execute(create_sql)
                    logger.info(f"✓ 创建索引：{index_name}")
                    stats['indexes_created'] += 1
                except Exception as e:
                    stats['errors'].append(f"创建索引 {index_name} 失败：{e}")
                    logger.error(f"✗ 创建索引 {index_name} 失败：{e}")
        else:
            if not dry_run:
                logger.info(f"○ 索引已存在：{index_name}")
    
    return stats


def migrate_performance_summary(conn: sqlite3.Connection, dry_run: bool = False) -> dict:
    """迁移 performance_summary 表"""
    stats = {
        'table_created': False,
        'indexes_created': 0,
        'errors': []
    }
    
    cursor = conn.cursor()
    
    # 创建表
    if not check_table_exists(conn, 'performance_summary'):
        if dry_run:
            logger.info("[DRY-RUN] 将创建表：performance_summary")
            stats['table_created'] = True
        else:
            try:
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
                logger.info("✓ 创建表：performance_summary")
                stats['table_created'] = True
            except Exception as e:
                stats['errors'].append(f"创建 performance_summary 表失败：{e}")
                logger.error(f"✗ 创建 performance_summary 表失败：{e}")
    else:
        logger.info("○ 表已存在：performance_summary")
    
    # 创建索引
    indexes = [
        ('idx_performance_summary_period', 'CREATE INDEX idx_performance_summary_period ON performance_summary(period)'),
        ('idx_performance_summary_created_at', 'CREATE INDEX idx_performance_summary_created_at ON performance_summary(created_at)')
    ]
    
    for index_name, create_sql in indexes:
        if not check_index_exists(conn, index_name):
            if dry_run:
                logger.info(f"[DRY-RUN] 将创建索引：{index_name}")
            else:
                try:
                    cursor.execute(create_sql)
                    logger.info(f"✓ 创建索引：{index_name}")
                    stats['indexes_created'] += 1
                except Exception as e:
                    stats['errors'].append(f"创建索引 {index_name} 失败：{e}")
                    logger.error(f"✗ 创建索引 {index_name} 失败：{e}")
        else:
            if not dry_run:
                logger.info(f"○ 索引已存在：{index_name}")
    
    return stats


def verify_existing_tables(conn: sqlite3.Connection) -> bool:
    """验证现有表结构未被修改"""
    required_tables = ['signals', 'trades', 'news', 'performance_snapshots', 'rate_limits']
    
    for table_name in required_tables:
        if not check_table_exists(conn, table_name):
            logger.warning(f"⚠ 警告：现有表 {table_name} 不存在")
            return False
    
    logger.info("✓ 现有表结构验证通过")
    return True


def main():
    parser = argparse.ArgumentParser(description='FTSeek 绩效追踪数据库迁移工具')
    parser.add_argument('--db', type=str, default='/opt/ftseek/FTSeek/data/ftseek.db',
                       help='数据库文件路径 (默认：/opt/ftseek/FTSeek/data/ftseek.db)')
    parser.add_argument('--dry-run', action='store_true',
                       help='仅预览变更，不实际执行')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='显示详细输出')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    db_path = Path(args.db)
    
    if not db_path.exists():
        logger.error(f"数据库文件不存在：{db_path}")
        return 1
    
    logger.info(f"连接数据库：{db_path}")
    logger.info(f"模式：{'DRY-RUN (预览)' if args.dry_run else '实际执行'}")
    logger.info("=" * 60)
    
    try:
        conn = sqlite3.connect(str(db_path))
        
        # 验证现有表结构
        if not verify_existing_tables(conn):
            logger.error("现有表结构验证失败，中止迁移")
            return 1
        
        logger.info("=" * 60)
        logger.info("开始迁移...")
        logger.info("")
        
        # 迁移 trade_pairs 表
        logger.info("【1/2】迁移 trade_pairs 表")
        trade_pairs_stats = migrate_trade_pairs(conn, args.dry_run)
        logger.info("")
        
        # 迁移 performance_summary 表
        logger.info("【2/2】迁移 performance_summary 表")
        performance_stats = migrate_performance_summary(conn, args.dry_run)
        logger.info("")
        
        # 提交事务
        if not args.dry_run:
            conn.commit()
        
        # 汇总统计
        logger.info("=" * 60)
        logger.info("迁移完成!")
        logger.info("")
        logger.info("统计信息:")
        logger.info(f"  - trade_pairs 表：{'已创建' if trade_pairs_stats['table_created'] else '已存在'}")
        logger.info(f"  - trade_pairs 索引：新建 {trade_pairs_stats['indexes_created']} 个")
        logger.info(f"  - performance_summary 表：{'已创建' if performance_stats['table_created'] else '已存在'}")
        logger.info(f"  - performance_summary 索引：新建 {performance_stats['indexes_created']} 个")
        
        if trade_pairs_stats['errors'] or performance_stats['errors']:
            logger.warning("")
            logger.warning("错误信息:")
            for error in trade_pairs_stats['errors'] + performance_stats['errors']:
                logger.warning(f"  - {error}")
            return 1
        
        logger.info("")
        logger.info("✓ 迁移成功完成")
        
        if args.dry_run:
            logger.info("")
            logger.info("提示：以上为预览结果，未实际执行任何变更")
            logger.info("      移除 --dry-run 参数以执行实际迁移")
        
        return 0
        
    except Exception as e:
        logger.error(f"迁移失败：{e}", exc_info=True)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    exit(main())
