#!/usr/bin/env python3
"""
从富途同步持仓信息到数据库
P0 紧急修复脚本
"""

import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')

import sqlite3
import logging
from datetime import datetime
from futu import OpenSecTradeContext, TrdMarket

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/sync_positions.log')
    ]
)
logger = logging.getLogger('sync_positions')

# 配置
DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"
OPEND_HOST = os.getenv("OPEND_HOST", "127.0.0.1")
OPEND_PORT = int(os.getenv("OPEND_PORT", "11111"))

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sync_positions():
    """同步持仓信息"""
    logger.info("="*60)
    logger.info("开始同步富途持仓信息...")
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
        
        # 获取持仓
        logger.info("获取持仓信息...")
        ret, data = ctx.get_position_list()
        
        if ret != 0:
            logger.error(f"❌ 获取持仓失败：{data}")
            return 0
        
        if data is None or len(data) == 0:
            logger.info("⚠️  无持仓")
            return 0
        
        logger.info(f"📊 找到 {len(data)} 个持仓")
        
        # 显示持仓
        for _, row in data.iterrows():
            logger.info(f"  {row['code']} {row.get('stock_name', '')}: "
                       f"{row['qty']}股 @ {row.get('avg_price', 0):.2f} | "
                       f"盈亏：{row.get('unrealized_pl', 0):.2f}")
        
        logger.info("="*60)
        logger.info("✅ 持仓同步完成!")
        logger.info("="*60)
        
        return len(data)
        
    except Exception as e:
        logger.error(f"❌ 同步失败：{e}", exc_info=True)
        return 0
        
    finally:
        if conn:
            conn.close()
        if ctx:
            ctx.close()

if __name__ == '__main__':
    imported = sync_positions()
    sys.exit(0 if imported > 0 else 1)
