#!/usr/bin/env python3
"""
每日数据收集脚本 - 模拟环境优先
"""
import sqlite3
import json
import logging
from datetime import datetime

logging.basicConfig(
    filename='/opt/ftseek/FTSeek/logs/data_collector.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

def collect_daily_data():
    """收集当日交易数据"""
    logger.info(f"开始收集数据：{datetime.now()}")
    
    # 连接主数据库
    conn = sqlite3.connect('/opt/ftseek/FTSeek/data/ftseek.db')
    cursor = conn.cursor()
    
    # 收集交易记录
    cursor.execute("SELECT * FROM trades WHERE date(date('now'))")
    trades = cursor.fetchall()
    
    # 收集信号记录
    cursor.execute("SELECT * FROM signals WHERE date(date('now'))")
    signals = cursor.fetchall()
    
    # 收集绩效统计
    cursor.execute("SELECT * FROM performance_stats WHERE date(date('now'))")
    stats = cursor.fetchall()
    
    logger.info(f"收集完成：trades={len(trades)}, signals={len(signals)}, stats={len(stats)}")
    
    conn.close()
    return len(trades) + len(signals) + len(stats)

if __name__ == "__main__":
    try:
        count = collect_daily_data()
        logger.info(f"数据收集成功：{count} 条记录")
    except Exception as e:
        logger.error(f"数据收集失败：{e}", exc_info=True)
