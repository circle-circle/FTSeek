#!/usr/bin/env python3
"""
从富途同步成交记录到数据库 (使用 OpenD Executor)
P0 紧急修复脚本 v2
"""

import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')

import sqlite3
import logging
from datetime import datetime
from app.v4.trader.opend_executor import get_opend_executor

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/sync_trades.log')
    ]
)
logger = logging.getLogger('sync_trades_v2')

# 配置 - 统一使用 simulation.db
DB_PATH = "/opt/ftseek/FTSeek/data/simulation.db"

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def insert_trade(conn, order):
    """插入成交记录到数据库"""
    cursor = conn.cursor()
    
    # 生成唯一 ID
    order_id = getattr(order, 'order_id', '')
    trade_id = f"trade_{order_id}"
    
    # 检查是否已存在 (使用 simulation_trades 表)
    cursor.execute("SELECT id FROM simulation_trades WHERE order_id = ?", (order_id_val,))
    if cursor.fetchone():
        logger.info(f"⏭️  跳过：{trade_id} (已存在)")
        return False
    
    # 获取字段值 (兼容不同返回格式)
    code = getattr(order, 'code', '')
    stock_name = getattr(order, 'stock_name', '')
    trd_side = getattr(order, 'trd_side', 1)  # 1=BUY, 2=SELL
    qty = getattr(order, 'qty', 0)
    price = getattr(order, 'price', 0)
    create_time = getattr(order, 'create_time', '')
    order_id_val = getattr(order, 'order_id', '')
    status = getattr(order, 'status', '')
    
    # 计算盈亏 (简化，实际应从 order.profit 获取)
    pnl = getattr(order, 'profit', 0) if hasattr(order, 'profit') else 0
    pnl_percent = 0
    
    # 插入数据库 (使用 simulation_trades 表结构)
    cursor.execute("""
        INSERT INTO simulation_trades (
            id, code, name, action, entry_price, quantity, status, 
            entry_time, order_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_id,
        code,
        stock_name,
        'BUY' if trd_side == 1 else 'SELL',
        price,
        qty,
        'closed',
        create_time,
        order_id_val,
        create_time
    ))
    
    conn.commit()
    logger.info(f"✅ 导入：{code} {'BUY' if trd_side == 1 else 'SELL'} {qty}股 @ {price} ({create_time})")
    return True

def sync_trades():
    """同步成交记录"""
    logger.info("="*60)
    logger.info("开始同步富途成交记录 (使用 OpenD Executor)...")
    logger.info("="*60)
    
    conn = None
    executor = None
    
    try:
        # 连接数据库
        conn = get_db_connection()
        logger.info("✅ 数据库连接成功")
        
        # 获取 OpenD Executor
        executor = get_opend_executor(simulate=True)
        logger.info("✅ OpenD Executor 初始化成功")
        
        # 查询已成交订单
        logger.info("获取已成交订单...")
        ret_code, ret_data = executor.client.get_history_order_list(
            filter_orders_by_status=[1, 2],  # FILLED
        )
        
        if ret_code != 0:
            logger.error(f"❌ 获取订单失败：{ret_code}")
            return 0
        
        if ret_data is None or len(ret_data) == 0:
            logger.info("⚠️  无成交记录")
            return 0
        
        logger.info(f"📊 找到 {len(ret_data)} 条订单")
        
        # 过滤今日订单
        today = datetime.now().strftime('%Y-%m-%d')
        today_orders = [
            order for order in ret_data.itertuples()
            if hasattr(order, 'create_time') and order.create_time and order.create_time.startswith(today)
        ]
        
        logger.info(f"📊 今日订单：{len(today_orders)} 条")
        
        # 导入数据库
        imported = 0
        for order in today_orders:
            if insert_trade(conn, order):
                imported += 1
        
        # 统计 (使用 simulation_trades 表)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT code, name, COUNT(*) as cnt, SUM(quantity) as vol
            FROM simulation_trades 
            WHERE date(entry_time) = date('now')
            GROUP BY code, name
        """)
        stats = cursor.fetchall()
        
        cursor.execute("""
            SELECT COUNT(*) FROM simulation_trades 
            WHERE date(entry_time) = date('now')
        """)
        total = cursor.fetchone()[0]
        
        logger.info("="*60)
        logger.info(f"✅ 同步完成!")
        logger.info(f"  本次导入：{imported} 条")
        logger.info(f"  今日总成交：{total} 条")
        logger.info(f"\n各股票统计:")
        for row in stats:
            logger.info(f"  {row[0]} {row[1]}: {row[2]}笔 / {row[3]}股")
        logger.info("="*60)
        
        # 记录同步日志 (使用 simulation_sync_logs 表)
        try:
            cursor.execute("""
                INSERT INTO simulation_sync_logs (id, sync_type, sync_time, source, records_synced, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                f"sync_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                'trade',
                datetime.now(),
                'futu_opend',
                imported,
                'success'
            ))
            conn.commit()
        except sqlite3.OperationalError:
            # 表不存在时跳过日志记录
            logger.warning("simulation_sync_logs 表不存在，跳过日志记录")
            conn.commit()
        
        return imported
        
    except Exception as e:
        logger.error(f"❌ 同步失败：{e}", exc_info=True)
        return 0
        
    finally:
        if conn:
            conn.close()
        if executor:
            executor.disconnect()

if __name__ == '__main__':
    imported = sync_trades()
    sys.exit(0 if imported > 0 else 1)
