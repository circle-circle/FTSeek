#!/usr/bin/env python3
"""
从富途同步成交记录到数据库
P0 紧急修复脚本
"""

import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')

import sqlite3
import logging
from datetime import datetime, timedelta
from futu import OpenSecTradeContext, TrdMarket, TrdEnv

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/sync_trades.log')
    ]
)
logger = logging.getLogger('sync_trades')

# 配置
DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))
FUTU_ACC_ID = int(os.getenv("SIMULATE_ACCOUNT_ID", "8246815"))

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def insert_trade(conn, order):
    """插入成交记录到数据库"""
    cursor = conn.cursor()
    
    # 生成唯一 ID
    trade_id = f"trade_{order['update_time'].replace(' ', '_').replace(':', '').replace('-', '')}_{order['code'].replace('.', '')}"
    
    # 检查是否已存在
    cursor.execute("SELECT id FROM trades WHERE id = ?", (trade_id,))
    if cursor.fetchone():
        logger.info(f"⏭️  跳过：{trade_id} (已存在)")
        return False
    
    # 计算盈亏 (简化)
    pnl = 0
    pnl_percent = 0
    
    # 插入数据库
    cursor.execute("""
        INSERT INTO trades (
            id, signal_id, code, name, action,
            entry_price, exit_price, quantity,
            pnl, pnl_percent, fees,
            strategy, entry_time, exit_time,
            exit_reason, order_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_id,
        f"auto_{trade_id}",
        order['code'],
        order['stock_name'],
        'BUY' if order['trd_side'] == 1 else 'SELL',
        order['price'],
        None,
        order['qty'],
        pnl,
        pnl_percent,
        order.get('commission', 0),
        'auto_sync',
        order['update_time'],
        None,
        None,
        order['order_id'],
        'OPEN' if order['trd_side'] == 1 else 'CLOSED'
    ))
    
    conn.commit()
    logger.info(f"✅ 导入：{order['code']} {'BUY' if order['trd_side'] == 1 else 'SELL'} {order['qty']}股 @ {order['price']} ({order['update_time']})")
    return True

def sync_trades():
    """同步成交记录"""
    logger.info("="*60)
    logger.info("开始同步富途成交记录...")
    logger.info("="*60)
    
    conn = None
    ctx = None
    
    try:
        # 连接数据库
        conn = get_db_connection()
        logger.info("✅ 数据库连接成功")
        
        # 连接富途
        ctx = OpenSecTradeContext(
            host=OPEND_HOST,
            port=OPEND_PORT,
            filter_trdmarket=TrdMarket.HK
        )
        logger.info(f"✅ 富途连接成功 ({OPEND_HOST}:{OPEND_PORT})")
        
        # 获取今日成交
        today = datetime.now().strftime('%Y-%m-%d')
        logger.info(f"获取今日 ({today}) 成交记录...")
        
        # 使用 history_order_list_query (富途 API 参数名不同)
        ret, data = ctx.history_order_list_query(
            acc_id=FUTU_ACC_ID,
        )
        
        if ret != 0:
            logger.error(f"❌ 获取成交记录失败：{data}")
            return 0
        
        if data is None or len(data) == 0:
            logger.info("⚠️  今日无成交记录")
            return 0
        
        # 筛选已成交订单
        filled_orders = data[data['status'] == 1]  # 1 = FILLED
        
        logger.info(f"📊 找到 {len(filled_orders)} 条已成交订单")
        
        # 导入数据库
        imported = 0
        for _, order in filled_orders.iterrows():
            if insert_trade(conn, order):
                imported += 1
        
        # 统计
        cursor = conn.cursor()
        cursor.execute("""
            SELECT code, name, COUNT(*) as cnt, SUM(quantity) as vol
            FROM trades 
            WHERE date(entry_time) = date('now') AND strategy = 'auto_sync'
            GROUP BY code, name
        """)
        stats = cursor.fetchall()
        
        cursor.execute("""
            SELECT COUNT(*) FROM trades 
            WHERE date(entry_time) = date('now') AND strategy = 'auto_sync'
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
        
        return imported
        
    except Exception as e:
        logger.error(f"❌ 同步失败：{e}", exc_info=True)
        return 0
        
    finally:
        if conn:
            conn.close()
        if ctx:
            ctx.close()

if __name__ == '__main__':
    imported = sync_trades()
    sys.exit(0 if imported > 0 else 1)
