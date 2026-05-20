#!/usr/bin/env python3
"""
复盘分析系统 - 数据库初始化脚本 (美股版)

功能:
- 创建 8 张复盘表
- 检查表是否已存在
- 提供回滚/重置功能

执行:
    python scripts/init_review_tables.py
"""
import sys
import os
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.review_tables import ReviewDatabase
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("init_review_tables")


def init_database():
    """初始化复盘数据库"""
    logger.info("=" * 60)
    logger.info("复盘分析系统 - 数据库初始化 (美股版)")
    logger.info("=" * 60)
    
    try:
        db = ReviewDatabase()
        
        logger.info("✓ 数据库初始化成功")
        logger.info(f"✓ 数据库路径：{db.db_path}")
        
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name LIKE 'trades_review%'
                OR name LIKE 'trade_reviews%'
                OR name LIKE 'strategy_performance%'
                OR name LIKE 'error_patterns%'
                OR name LIKE 'psychology_stats%'
                OR name LIKE 'improvement_actions%'
                OR name LIKE 'best_worst_trades%'
                OR name LIKE 'sim_vs_real_comparison%'
            """)
            tables = [row[0] for row in cursor.fetchall()]
        
        logger.info(f"✓ 已创建 {len(tables)} 张表:")
        for table in tables:
            logger.info(f"  - {table}")
        
        logger.info("=" * 60)
        logger.info("初始化完成!")
        logger.info("=" * 60)
        
        return True
        
    except Exception as e:
        logger.error(f"初始化失败：{e}")
        return False


def reset_database():
    """重置数据库"""
    logger.warning("警告：此操作将删除所有复盘数据!")
    response = input("确认重置？(yes/no): ")
    
    if response.lower() != 'yes':
        logger.info("操作已取消")
        return False
    
    try:
        db = ReviewDatabase()
        
        with db.get_connection() as conn:
            cursor = conn.cursor()
            
            tables = [
                'trades_review', 'trade_reviews', 'strategy_performance',
                'error_patterns', 'psychology_stats', 'improvement_actions',
                'best_worst_trades', 'sim_vs_real_comparison'
            ]
            
            for table in tables:
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
                logger.info(f"✓ 删除表：{table}")
            
            conn.commit()
        
        logger.info("数据库已重置")
        return init_database()
        
    except Exception as e:
        logger.error(f"重置失败：{e}")
        return False


def show_status():
    """显示数据库状态"""
    try:
        db = ReviewDatabase()
        
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            
            logger.info("数据库状态:")
            logger.info(f"路径：{db.db_path}")
            logger.info(f"表数量：{len(tables)}")
            
            review_tables = [
                'trades_review', 'trade_reviews', 'strategy_performance',
                'error_patterns', 'psychology_stats', 'improvement_actions',
                'best_worst_trades', 'sim_vs_real_comparison'
            ]
            
            for table in review_tables:
                if table in tables:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    logger.info(f"  {table}: {count} 条记录")
                else:
                    logger.info(f"  {table}: 不存在")
        
        return True
        
    except Exception as e:
        logger.error(f"状态查询失败：{e}")
        return False


def main():
    """主函数"""
    if len(sys.argv) > 1:
        command = sys.argv[1]
        
        if command == 'init':
            success = init_database()
        elif command == 'reset':
            success = reset_database()
        elif command == 'status':
            success = show_status()
        else:
            logger.error(f"未知命令：{command}")
            logger.info("可用命令：init, reset, status")
            success = False
    else:
        success = init_database()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
