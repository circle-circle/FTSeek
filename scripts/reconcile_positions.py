#!/usr/bin/env python3
"""
持仓对账脚本 (Reconcile Positions) - v2
功能：
1. 以富途 OpenD 实时数据为唯一真理源 (SSOT)，修复本地 DB 状态漂移。
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

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    print("⚠️ 未找到 futu 模块，跳过持仓对账 (非阻塞)")
    sys.exit(0)

# 导入项目配置
try:
    from app.config import OPEND_HOST, OPEND_PORT
except ImportError:
    OPEND_HOST = '127.0.0.1'
    OPEND_PORT = 11111

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(Path(__file__).resolve().parent.parent, 'logs', 'reconcile.log'))
    ]
)
logger = logging.getLogger("reconcile")

DB_PATH = os.path.join(Path(__file__).resolve().parent.parent, 'data', 'ftseek.db')

def find_signal_id(cursor, code, price):
    """尝试匹配历史信号，以确认是否为 AI 交易"""
    try:
        # 匹配逻辑：同代码、已执行、价格偏差在 5% 以内 (HK signals 价格可能为 0 或未成交)
        # 注意：HK 信号表目前可能没有 EXECUTED 状态，所以我们放宽条件：只要有信号且价格接近
        cursor.execute("""
            SELECT id FROM signals 
            WHERE code = ? 
            AND price > 0
            AND abs(price - ?) / ? < 0.05
            ORDER BY id DESC LIMIT 1
        """, (code, price, price))
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception as e:
        logger.debug(f"匹配信号失败: {e}")
    return None

def _check_trading_time(market: str) -> bool:
    """检查当前是否在指定市场的交易时间内"""
    from datetime import datetime
    now = datetime.now()
    month = now.month
    is_dst = 3 <= month <= 10
    weekday = now.weekday()
    current_minutes = now.hour * 60 + now.minute
    
    if market == "HK":
        if weekday >= 5: return False
        # 09:00 - 16:15
        return (9 * 60 <= current_minutes < 12 * 60) or (13 * 60 <= current_minutes < 16 * 60 + 15)
    else:
        # US Market (CST): DST 21:00-05:30, Standard 22:00-06:00
        if weekday == 5: return False # Saturday
        if is_dst:
            start, end = 21 * 60, 5 * 60 + 30
        else:
            start, end = 22 * 60, 6 * 60
        # Cross midnight
        return current_minutes >= start or current_minutes < end

def run_reconciliation(market_filter='HK'):
    """执行持仓对账
    
    Args:
        market_filter: 'HK' 只同步港股, 'US' 只同步美股, None 同步全部
                       默认 'HK' 以匹配 FTSeek 港股项目定位
    """
    logger.info(f"🚀 开始持仓对账 (market_filter={market_filter})...")
    
    # 交易时间门控：非交易时间跳过查询，防止 Futu 返回空数据导致误杀持仓
    if market_filter == 'US':
        if not _check_trading_time("US"):
            logger.info("⏰ 美股非交易时间，跳过美股对账 (防止数据误判)")
            return
    elif market_filter == 'HK':
        if not _check_trading_time("HK"):
            logger.info("⏰ 港股非交易时间，跳过港股对账")
            return
    
    # 1. 获取富途实时持仓
    server_positions = {} # key: code -> info
    
    # 检查 DB
    if not os.path.exists(DB_PATH):
        logger.warning(f"DB 不存在: {DB_PATH}，跳过对账")
        return

    # 查询港股 (始终查询，market_filter 在写入时过滤)
    hk_positions = 0
    try:
        logger.info("📡 查询港股持仓...")
        hk_ctx = OpenSecTradeContext(host=OPEND_HOST, port=OPEND_PORT, filter_trdmarket=TrdMarket.HK)
        ret, data = hk_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret == 0:
            for _, row in data.iterrows():
                qty = float(row.get('qty', 0))
                if qty > 0:
                    code = str(row['code'])
                    server_positions[code] = {
                        'code': code, 
                        'qty': qty,
                        'cost': float(row.get('cost_price', 0)), 
                        'market': 'HK'
                    }
                    hk_positions += 1
        hk_ctx.close()
        logger.info(f"   富途港股持仓: {hk_positions} 只")
    except Exception as e: 
        logger.error(f"   港股查询失败: {e}")

    # 查询美股
    us_positions = 0
    try:
        logger.info("📡 查询美股持仓...")
        us_ctx = OpenSecTradeContext(host=OPEND_HOST, port=OPEND_PORT, filter_trdmarket=TrdMarket.US)
        ret, data = us_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret == 0:
            for _, row in data.iterrows():
                qty = float(row.get('qty', 0))
                if qty > 0:
                    code = str(row['code'])
                    # 确保代码格式一致 (US. 开头)
                    if not code.startswith('US.'):
                        code = f'US.{code}'
                        
                    server_positions[code] = {
                        'code': code, 
                        'qty': qty,
                        'cost': float(row.get('cost_price', 0)), 
                        'market': 'US'
                    }
                    us_positions += 1
        us_ctx.close()
        logger.info(f"   富途美股持仓: {us_positions} 只")
    except Exception as e: 
        logger.error(f"   美股查询失败: {e}")

    total_server = len(server_positions)
    logger.info(f"✅ 富途服务端总有效持仓: {total_server} 只")

    # 2. 对比并更新本地 DB
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 确保列存在
    try:
        cursor.execute("ALTER TABLE positions ADD COLUMN trader_type TEXT DEFAULT 'MANUAL';")
        logger.info("✅ 新增 trader_type 列")
    except Exception: pass  # Already exists

    # 2.1 修复现有持仓的 trader_type (如果未标注)
    # 查询所有 OPEN 且未标记类型的持仓
    cursor.execute("SELECT id, code, entry_price FROM positions WHERE status='OPEN' AND (trader_type IS NULL OR trader_type = '')")
    open_pos_to_check = cursor.fetchall()
    
    if open_pos_to_check:
        logger.info(f"🔍 检查 {len(open_pos_to_check)} 个未标注持仓...")
        for pos_id, code, entry_price in open_pos_to_check:
            sig_id = find_signal_id(cursor, code, entry_price)
            trader_type = 'AI' if sig_id else 'MANUAL'
            cursor.execute("UPDATE positions SET trader_type=?, signal_id=? WHERE id=?", 
                           (trader_type, sig_id, pos_id))
            if trader_type == 'AI':
                logger.info(f"🤖 识别 AI 持仓: {code} (Signal #{sig_id})")
            else:
                logger.warning(f"👤 识别手动持仓: {code}")

    # 根据 market_filter 过滤：只处理指定市场的持仓
    if market_filter == 'HK':
        server_positions = {k: v for k, v in server_positions.items() if k.startswith('HK.')}
        logger.info(f"📌 市场过滤: 仅处理港股 (HK.*), 过滤后 {len(server_positions)} 只")
    elif market_filter == 'US':
        server_positions = {k: v for k, v in server_positions.items() if k.startswith('US.')}
        logger.info(f"📌 市场过滤: 仅处理美股 (US.*), 过滤后 {len(server_positions)} 只")

    # 获取 DB 中状态为 OPEN 的持仓 (按市场过滤)
    if market_filter:
        market_prefix = market_filter + '.'
        cursor.execute("SELECT code FROM positions WHERE status='OPEN' AND code LIKE ?", (market_prefix,))
    else:
        cursor.execute("SELECT code FROM positions WHERE status='OPEN'")
    db_codes = set(row[0] for row in cursor.fetchall())
    server_codes = set(server_positions.keys())
    
    updated = 0
    closed = 0
    opened = 0
    
    # 2.2 处理 DB 有但 Server 没有的 -> 标记为 CLOSED (幽灵持仓)
    ghost_codes = db_codes - server_codes
    for code in ghost_codes:
        logger.warning(f"⚠️ 幽灵持仓修复: DB 有 {code} 但服务端无，标记为平仓")
        cursor.execute("UPDATE positions SET status='CLOSED' WHERE code=? AND status='OPEN'", (code,))
        closed += 1

    # 2.3 处理 Server 有但 DB 没有的 -> 插入 DB (新持仓)
    new_codes = server_codes - db_codes
    for code in new_codes:
        info = server_positions[code]
        logger.info(f"🆕 发现服务端新持仓 {code}，同步至 DB")
        
        # P0 修复 (2026-05-22): 移除 10min 冷却跳过逻辑
        # 第一性原理: Futu 持有该持仓 = 本地平仓未真实执行到 Futu
        # 旧逻辑: 10min 内有关闭记录 → 跳过创建 → 幽灵仓永远不被修复
        # 新逻辑: Futu 有持仓 → 本地必须同步，无论何时关闭过
        # 如果本地平仓确实已发到 Futu (Futu 订单成功)，Futu 不可能仍持有
        # 因此 reconcile 阶段 Futu 仍有持仓 = 本地平仓失败 → 必须重建本地记录
        # (根本修复在上游: trade_executor 止损/止盈必须先发 Futu 订单再更新 DB)

        
        # 查找信号
        sig_id = find_signal_id(cursor, code, info['cost'])
        trader_type = 'AI' if sig_id else 'MANUAL'
        
        # P1 修复 (2026-05-16): 尝试找到最近关闭的同代码同价格持仓，继承其 strategy
        # 原因: 当 exit 逻辑关闭了持仓但 reconcile 发现 Futu 仍有持仓时，
        # 不应创建新的 reconcile_sync 记录，而应继承原始策略归属
        strategy_name = 'reconcile_sync'
        try:
            cursor.execute("""
                SELECT strategy FROM positions 
                WHERE code = ? AND status = 'CLOSED' 
                AND abs(entry_price - ?) / ? < 0.01
                ORDER BY exit_time DESC LIMIT 1
            """, (code, info['cost'], info['cost']))
            row = cursor.fetchone()
            if row and row[0] and row[0] != 'reconcile_sync':
                strategy_name = row[0]
                logger.info(f"   📋 继承原始策略: {strategy_name}")
        except Exception as _strat_e:
            logger.debug(f"   策略继承失败: {_strat_e}")
        
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        action = 'SELL' if info.get('qty', 0) < 0 else 'BUY'
        
        # 插入记录
        # P0 2026-05-22: INSERT OR IGNORE 防止 UNIQUE constraint 冲突
        # 根因: 多线程竞态 (position_sync + reconcile 同时运行) 导致同 code OPEN 记录冲突
        # 修复: INSERT OR IGNORE + 检查 rowcount，未插入则 UPDATE 对齐
        cursor.execute("""
            INSERT OR IGNORE INTO positions (
                code, action, quantity, entry_price, entry_time, status, 
                market, strategy, signal_id, trader_type
            ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
        """, (
            code, action, info['qty'], info['cost'], now_str, info['market'], strategy_name, sig_id, trader_type
        ))
        if cursor.rowcount == 0:
            # 已有 OPEN 记录，更新数量/价格对齐
            cursor.execute(
                "UPDATE positions SET quantity=?, entry_price=? WHERE code=? AND status='OPEN'",
                (info['qty'], info['cost'], code)
            )
            logger.info(f"   🔄 {code} 已有 OPEN 记录，已更新数量/价格对齐")
        
        if trader_type == 'AI':
            logger.info(f"   🤖 确认为 AI 持仓")
        else:
            logger.warning(f"   👤 确认为手动持仓")
        opened += 1

    conn.commit()
    
    # 统计最终结果 (按市场过滤)
    if market_filter:
        market_prefix = market_filter + '.'
        cursor.execute("SELECT count(*) FROM positions WHERE status='OPEN' AND code LIKE ?", (market_prefix,))
    else:
        cursor.execute("SELECT count(*) FROM positions WHERE status='OPEN'")
    final_db_count = cursor.fetchone()[0]
    
    # 统计 AI vs Manual (按市场过滤)
    if market_filter:
        cursor.execute("SELECT count(*) FROM positions WHERE status='OPEN' AND trader_type='AI' AND code LIKE ?", (market_prefix,))
    else:
        cursor.execute("SELECT count(*) FROM positions WHERE status='OPEN' AND trader_type='AI'")
    ai_count = cursor.fetchone()[0]
    manual_count = final_db_count - ai_count
    
    conn.close()

    logger.info(f"🏁 对账完成总结 (market={market_filter}):")
    logger.info(f"   修复幽灵持仓 (关闭): {closed} 只")
    logger.info(f"   同步新持仓 (新增): {opened} 只")
    logger.info(f"   DB 最终有效持仓: {final_db_count} 只 (服务端: {len(server_positions)} 只)")
    logger.info(f"   🤖 AI: {ai_count} | 👤 Manual: {manual_count}")
    
    if final_db_count != len(server_positions):
        logger.warning("⚠️ 注意：DB 与服务端持仓数仍不一致，请检查日志详情！")
    else:
        logger.info("✅ DB 与服务端持仓数完全一致")

    return final_db_count

if __name__ == '__main__':
    run_reconciliation()
