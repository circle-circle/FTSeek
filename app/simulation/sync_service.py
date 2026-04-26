"""
模拟盘数据同步服务
从富途 OpenD API 同步真实交易数据到本地数据库
"""
import logging
import sqlite3
from app.storage.repository import get_db

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from futu import OpenSecTradeContext, TrdEnv, TrdMarket, RET_OK

logger = logging.getLogger("ftseek.simulation.sync")
logger.propagate = False

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "simulation.db"

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn
def sync_today_trades():
    """从富途同步今日交易数据"""
    logger.info("开始同步今日富途交易数据...")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    trd_ctx = None
    
    try:
        # 创建富途交易上下文
        trd_ctx = OpenSecTradeContext(host='127.0.0.1', port=11111)
        logger.info("已连接富途 OpenD")
        
        # 获取今日订单
        today = datetime.now().strftime('%Y-%m-%d')
        logger.info(f"同步日期：{today}")
        
        ret, data = trd_ctx.history_order_list_query(
            trd_env=TrdEnv.SIMULATE,
            start=today,
            end=today
        )
        
        if ret != RET_OK:
            logger.error(f"获取订单失败：{data}")
            return {'success': False, 'error': str(data)}
        
        if data is None or len(data) == 0:
            logger.info("今日无订单")
            return {'success': True, 'synced': 0, 'message': '今日无订单'}
        
        logger.info(f"获取到 {len(data)} 条订单")
        
        # 插入数据库
        inserted = 0
        for i, row in data.iterrows():
            order_id = str(row.get('order_id', ''))
            code = row.get('code', '')
            stock_name = row.get('stock_name', '')
            trd_side = row.get('trd_side', '')
            qty = row.get('qty', 0)
            price = row.get('price', 0)
            dealt_qty = row.get('dealt_qty', 0)
            dealt_avg_price = row.get('dealt_avg_price', 0)
            order_status = row.get('order_status', 0)
            create_time = row.get('create_time', '')
            updated_time = row.get('updated_time', '')
            
            # 判断状态
            status = 'closed' if order_status == 10 else 'open'
            
            # 计算盈亏
            if dealt_qty > 0 and price > 0:
                if trd_side == 'BUY':
                    pnl = (dealt_avg_price - price) * dealt_qty
                else:
                    pnl = (price - dealt_avg_price) * dealt_qty
                pnl_percent = pnl / (price * dealt_qty) * 100
            else:
                pnl = 0
                pnl_percent = 0
            
            exit_reason = '止盈' if pnl > 0 else '止损' if pnl < 0 else ''
            
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO simulation_trades 
                    (id, code, name, action, entry_price, exit_price, quantity, pnl, pnl_percent, 
                     fees, strategy, signal_id, entry_time, exit_time, exit_reason, order_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    f'FUTU-{order_id}', code, stock_name, trd_side,
                    price, dealt_avg_price, dealt_qty, pnl, pnl_percent,
                    5.0, 'confluence_v2', None, create_time, updated_time,
                    exit_reason, order_id, status
                ))
                inserted += 1
            except Exception as e:
                logger.error(f"插入失败：{e}")
        
        conn.commit()
        
        # 统计结果
        cursor.execute("SELECT COUNT(*) FROM simulation_trades WHERE date(entry_time) = ?", (today,))
        today_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM simulation_trades WHERE date(entry_time) = ? AND status='closed'", (today,))
        closed_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT SUM(pnl) FROM simulation_trades WHERE date(entry_time) = ? AND status='closed'", (today,))
        today_pnl = cursor.fetchone()[0] or 0
        
        logger.info(f"同步完成：今日订单 {today_count} 笔，已平仓 {closed_count} 笔，盈亏 {today_pnl:.2f} HKD")
        
        return {
            'success': True,
            'synced': inserted,
            'today_orders': today_count,
            'closed': closed_count,
            'pnl': today_pnl
        }
        
    except Exception as e:
        logger.error(f"同步失败：{e}")
        return {'success': False, 'error': str(e)}
    
    finally:
        if trd_ctx:
            trd_ctx.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    result = sync_today_trades()
    print(f"同步结果：{result}")

def is_trading_day():
    """判断是否为交易日"""
    from datetime import datetime
    today = datetime.now()

    # 周末不是交易日
    if today.weekday() >= 5:
        return False

    # P1 修复 (2026-04-12): 添加港股节假日判断
    # 注：完整节假日列表需要每年更新，当前使用简化判断
    hk_holidays_2026 = [
        "2026-01-01",  # 元旦
        "2026-02-18",  # 农历年初一
        "2026-02-19",  # 农历年初二
        "2026-02-20",  # 农历年初三
        "2026-04-03",  # 清明节
        "2026-04-06",  # 复活节后周一
        "2026-05-01",  # 劳动节
        "2026-05-25",  # 端午节
        "2026-07-01",  # 香港特区成立日
        "2026-09-07",  # 中秋节
        "2026-10-01",  # 国庆节
        "2026-10-26",  # 重阳节
        "2026-12-25",  # 圣诞节
        "2026-12-26",  # 节礼日
    ]
    if today.strftime("%Y-%m-%d") in hk_holidays_2026:
        return False

    return True

def sync_if_trading_day():
    """如果是交易日且时间在 17:30 后，执行同步"""
    from datetime import datetime
    now = datetime.now()
    
    # 检查是否为交易日
    if not is_trading_day():
        logger.info("今日不是交易日，跳过同步")
        return {'success': True, 'message': '非交易日'}
    
    # 检查时间是否在 17:30 后
    if now.hour < 17 or (now.hour == 17 and now.minute < 30):
        logger.info(f"当前时间 {now.strftime('%H:%M')} 未到 17:30，跳过同步")
        return {'success': True, 'message': '未到同步时间'}
    
    # 执行同步
    logger.info(f"交易日 {now.strftime('%Y-%m-%d')} 17:30 后，开始同步")
    return sync_today_trades()

if __name__ == "__main__":
    result = sync_if_trading_day()
    print(f"同步结果：{result}")
