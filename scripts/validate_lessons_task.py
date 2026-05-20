#!/usr/bin/env python3
"""
盘后教训验证任务 - Phase 1 Day 3

集成 LessonInjector.validate_lessons() 到盘后流程

用法:
1. 作为 cron job 运行: 0 17 * * 1-5 cd /opt/ftseek/FTSeek-US && python3 scripts/validate_lessons_task.py
2. 或手动运行: python3 scripts/validate_lessons_task.py

功能:
1. 获取今日交易数据
2. 调用 validate_lessons() 验证教训有效性
3. 更新教训置信度和有效期
4. 记录验证结果到日志
"""

import os
import sys
import logging
import json
from datetime import datetime

# 添加项目路径
sys.path.insert(0, "/opt/ftseek/FTSeek-US")
sys.path.insert(0, "/opt/ftseek/FTSeek-US/app")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/ftseek/FTSeek-US/logs/lesson_validation.log")
    ]
)
logger = logging.getLogger("lesson_validation")


def get_today_trades(db_path: str = "/opt/ftseek/FTSeek-US/data/ftseek_us.db") -> list:
    """获取今日交易数据"""
    import sqlite3
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # 查询今日交易 (移除不存在的entry_session列)
        rows = conn.execute("""
            SELECT symbol, direction, entry_time, exit_time, 
                   realized_pnl, signal_strength
            FROM trade_records 
            WHERE trade_date = ?
        """, (today,)).fetchall()
        
        trades = [dict(r) for r in rows]
        conn.close()
        
        logger.info(f"[验证] 获取今日交易: {len(trades)} 笔")
        return trades
        
    except Exception as e:
        logger.error(f"[验证] 获取交易数据失败: {e}")
        return []


def run_validation():
    """执行教训验证"""
    logger.info("=" * 60)
    logger.info("[验证] === 开始盘后教训验证 ===")
    logger.info(f"[验证] 时间: {datetime.now().isoformat()}")
    
    try:
        # 1. 获取今日交易
        today_trades = get_today_trades()
        
        # 2. 初始化 LessonInjector
        from app.review.lesson_injector import LessonInjector
        injector = LessonInjector()
        
        # 3. 执行验证
        result = injector.validate_lessons(today_trades)
        
        # 4. 记录结果
        logger.info(f"[验证] 验证完成:")
        logger.info(f"  - 验证通过: {result.get('validated', 0)} 条")
        logger.info(f"  - 验证失败: {result.get('invalidated', 0)} 条")
        logger.info(f"  - 已更新: {result.get('updated', 0)} 条")
        
        # 5. 获取统计信息
        stats = injector.get_stats()
        logger.info(f"[验证] 教训统计:")
        logger.info(f"  - 活跃教训: {stats.get('active_lessons', 0)} 条")
        logger.info(f"  - 平均置信度: {stats.get('avg_confidence', 0):.2f}")
        logger.info(f"  - 即将过期: {stats.get('expiring_soon', 0)} 条")
        
        logger.info("[验证] === 教训验证完成 ===")
        return result
        
    except Exception as e:
        logger.error(f"[验证] 教训验证异常: {e}", exc_info=True)
        return None


if __name__ == "__main__":
    result = run_validation()
    
    # 退出码
    if result is not None:
        sys.exit(0)
    else:
        sys.exit(1)
