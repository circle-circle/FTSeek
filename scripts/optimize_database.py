"""
数据库索引优化脚本

为常用查询字段添加索引，提升查询性能
"""
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger("ftseek.storage.db_optimize")

# 数据库路径
DB_PATH = Path('/opt/ftseek/FTSeek/data/ftseek.db')
TRADE_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/trade_records')


def optimize_database():
    """优化数据库"""
    logger.info("="*60)
    logger.info("开始数据库优化")
    logger.info("="*60)
    
    # 1. 优化主数据库
    if DB_PATH.exists():
        optimize_ftseek_db(DB_PATH)
    else:
        logger.warning(f"主数据库不存在：{DB_PATH}")
    
    # 2. 优化交易记录
    if TRADE_RECORDS_DIR.exists():
        logger.info("交易记录使用 JSON 文件，无需索引优化")
    
    logger.info("="*60)
    logger.info("数据库优化完成")
    logger.info("="*60)


def optimize_ftseek_db(db_path: Path):
    """优化 FTSeek 主数据库"""
    logger.info(f"优化数据库：{db_path}")
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # 要创建的索引
    indexes = [
        # 交易相关
        ("idx_signals_code", "signals", "code"),
        ("idx_signals_created_at", "signals", "created_at"),
        ("idx_signals_strategy", "signals", "strategy"),
        
        ("idx_trade_pairs_code", "trade_pairs", "code"),
        ("idx_trade_pairs_close_time", "trade_pairs", "close_time"),
        ("idx_trade_pairs_status", "trade_pairs", "status"),
        
        # 报告相关
        ("idx_report_trades_code", "report_trades", "code"),
        ("idx_report_trades_timestamp", "report_trades", "timestamp"),
        ("idx_report_performance_code", "report_performance", "code"),
        ("idx_report_performance_date", "report_performance", "date"),
    ]
    
    created_count = 0
    
    for idx_name, table_name, column_name in indexes:
        try:
            # 检查索引是否已存在
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx_name,)
            )
            
            if cursor.fetchone():
                logger.info(f"索引已存在：{idx_name}")
                continue
            
            # 创建索引
            sql = f"CREATE INDEX {idx_name} ON {table_name}({column_name})"
            cursor.execute(sql)
            
            logger.info(f"✅ 创建索引：{idx_name} ON {table_name}({column_name})")
            created_count += 1
        
        except Exception as e:
            logger.error(f"创建索引失败 {idx_name}: {e}")
    
    # 执行 VACUUM 优化数据库文件
    logger.info("执行 VACUUM 优化...")
    cursor.execute("VACUUM")
    
    # 分析数据库
    logger.info("执行 ANALYZE...")
    cursor.execute("ANALYZE")
    
    conn.commit()
    conn.close()
    
    logger.info(f"优化完成：创建 {created_count}/{len(indexes)} 个索引")


def get_db_stats(db_path: Path) -> dict:
    """获取数据库统计信息"""
    if not db_path.exists():
        return {'error': '数据库不存在'}
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # 表大小
    cursor.execute("""
        SELECT name, 
               page_count * page_size as size
        FROM sqlite_master, pragma_page_count(), pragma_page_size()
        WHERE type='table'
        ORDER BY size DESC
    """)
    
    tables = cursor.fetchall()
    
    # 索引数量
    cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'")
    index_count = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        'tables': len(tables),
        'indexes': index_count,
        'total_size': sum(t[1] for t in tables),
        'table_sizes': tables
    }


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--stats':
        stats = get_db_stats(DB_PATH)
        print("数据库统计:")
        print(f"  表数量：{stats.get('tables', 0)}")
        print(f"  索引数量：{stats.get('indexes', 0)}")
        print(f"  总大小：{stats.get('total_size', 0) / 1024 / 1024:.2f} MB")
    else:
        optimize_database()
