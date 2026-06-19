#!/usr/bin/env python3
"""
持仓同步脚本 (FTSeek-US 专用) - v2
功能：
1. 以富途服务端持仓为准，同步至本地 open_positions 表。
2. 自动识别交易员类型：
   - AI: 能匹配到历史 EXECUTED 信号的持仓。
   - Manual: 无法匹配信号的持仓 (可能是人工操作)。
"""

import os
import sys
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    print("❌ futu module not found")
    sys.exit(1)

try:
    from app.core.config_container import get_config
    config = get_config()
    FUTU_OPEND_HOST = os.environ.get('FUTU_OPEND_HOST', config.opend.host)
    FUTU_OPEND_PORT = os.environ.get('FUTU_OPEND_PORT', config.opend.port)
except Exception as e:
    print(f"⚠️ Config load failed: {e}, using defaults")
    FUTU_OPEND_HOST = '127.0.0.1'
    FUTU_OPEND_PORT = 11111

DB_PATH = os.path.join(Path(__file__).resolve().parent.parent, 'data', 'ftseek_us.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("ftseek_us_sync")

def find_ai_signal_id(cursor, symbol, cost_price):
    """
    尝试在 signals 表中寻找匹配的 AI 信号。
    匹配条件：
    1. 股票相同
    2. 状态为 EXECUTED
    3. 信号价格与成本价偏差 < 3%
    """
    try:
        cursor.execute("""
            SELECT id FROM signals 
            WHERE symbol = ? AND status = 'EXECUTED' 
            AND entry_price > 0
            AND abs(entry_price - ?) / ? < 0.03
            ORDER BY id DESC LIMIT 1
        """, (symbol, cost_price, cost_price))
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
    return None

def sync_us_positions():
    logger.info("🇺🇸 开始同步美股持仓 (FTSeek-US)...")
    
    # 1. 获取富途美股实时持仓
    server_positions = []
    
    try:
        logger.info(f"📡 连接富途 {FUTU_OPEND_HOST}:{FUTU_OPEND_PORT}...")
        trade_env = TrdEnv.SIMULATE if os.environ.get('TRADE_ENV', '1') == '1' else TrdEnv.REAL
        ctx = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host=FUTU_OPEND_HOST,
            port=int(FUTU_OPEND_PORT),
            security_firm=SecurityFirm.FUTUINC,
        )
        ret, data = ctx.position_list_query(trd_env=trade_env)
        
        if ret == 0:
            for _, row in data.iterrows():
                qty = float(row.get('qty', 0))
                
                # 过滤掉已清仓的 (Qty=0)
                if qty == 0:
                    continue
                
                code = str(row['code'])
                if not code.startswith('US.'):
                    code = f'US.{code}'
                
                # 区分多空
                side = str(row.get('position_side', 'LONG')).upper()
                is_short = (qty < 0) or (side == 'SHORT')
                direction = 'SHORT' if is_short else 'LONG'
                size = abs(qty)
                
                server_positions.append({
                    'symbol': code,
                    'direction': direction,
                    'size': size,
                    'avg_entry_price': float(row.get('cost_price', 0)),
                    'current_price': float(row.get('nominal_price', 0)),
                    'market_val': float(row.get('market_val', 0)),
                    'pl_ratio': float(row.get('pl_ratio', 0)),
                    'update_time': datetime.now().isoformat()
                })
                
            logger.info(f"✅ 富途服务端美股持仓: {len(server_positions)} 只")
            for p in server_positions:
                logger.info(f"   {p['symbol']} {p['direction']} {p['size']} 股 | 成本: {p['avg_entry_price']}")
        else:
            logger.error(f"❌ 查询失败: {data}")
        ctx.close()
    except Exception as e:
        logger.error(f"❌ 连接异常: {e}")
        return

    # 2. 更新本地 DB
    if not os.path.exists(DB_PATH):
        logger.warning(f"DB 不存在: {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 确保列存在
        try:
            cursor.execute("ALTER TABLE open_positions ADD COLUMN trader_type TEXT DEFAULT 'MANUAL';")
            logger.info("✅ 新增 trader_type 列")
        except: pass # Already exists

        # 获取当前 DB 持仓 (用于对比)
        cursor.execute("SELECT symbol FROM open_positions")
        db_symbols = set(row[0] for row in cursor.fetchall())
        server_symbols = set(p['symbol'] for p in server_positions)
        
        # 1. 删除旧数据 (全量覆盖策略)
        cursor.execute("DELETE FROM open_positions")
        
        # 2. 插入新数据 (含 AI 识别)
        new_count = 0
        ai_count = 0
        manual_count = 0
        
        for p in server_positions:
            # 尝试匹配信号
            sig_id = find_ai_signal_id(cursor, p['symbol'], p['avg_entry_price'])
            trader_type = 'AI' if sig_id else 'MANUAL'
            
            # 计算默认止损止盈
            # 多头止损 8%，空头止损 8% (价格高于成本)
            sl_pct = 0.08
            tp_pct = 0.15
            
            if p['direction'] == 'LONG':
                initial_sl = p['avg_entry_price'] * (1 - sl_pct)
                initial_tp = p['avg_entry_price'] * (1 + tp_pct)
            else:
                initial_sl = p['avg_entry_price'] * (1 + sl_pct)
                initial_tp = p['avg_entry_price'] * (1 - tp_pct)
            
            # 插入
            cursor.execute("""
                INSERT INTO open_positions (
                    symbol, direction, size, avg_entry_price, 
                    initial_sl, current_sl, initial_tp, 
                    entry_time, updated_at, strategy_id,
                    signal_id, trader_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p['symbol'], 
                p['direction'], 
                p['size'], 
                p['avg_entry_price'],
                initial_sl,
                initial_sl, # current_sl starts same as initial
                initial_tp,
                p['update_time'],
                p['update_time'],
                'reconcile_sync', # Strategy ID
                sig_id,
                trader_type
            ))
            new_count += 1
            if trader_type == 'AI':
                ai_count += 1
            else:
                manual_count += 1

        conn.commit()
        logger.info(f"✅ 同步完成. 总计: {new_count} | 🤖 AI: {ai_count} | 👤 Manual: {manual_count}")
        conn.close()
        
    except Exception as e:
        logger.error(f"❌ DB 更新失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    sync_us_positions()
