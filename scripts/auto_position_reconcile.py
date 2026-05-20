#!/usr/bin/env python3
"""
FTSeek-US 持仓自动对账脚本
定期对比 Broker 实际持仓与 DB 记录，检测并修复不一致。
由 cron 每 30 分钟执行。
"""
import os, sys, json, sqlite3, time
from datetime import datetime, timezone

os.chdir('/opt/ftseek/FTSeek-US')
sys.path.insert(0, '.')
os.environ['TRADE_ENV'] = '1'

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("position_reconcile")

def get_atr(quote_ctx, symbol, period=14):
    """计算 14 日 ATR"""
    try:
        from futu import KLType, AuType
        ret, data, _ = quote_ctx.request_history_kline(
            symbol, ktype=KLType.K_DAY, max_count=max(period + 10, 30), autype=AuType.QFQ
        )
        if ret == 0 and data is not None and len(data) >= period + 1:
            data = data.copy()
            data['prev_close'] = data['close'].shift(1)
            data['tr1'] = data['high'] - data['low']
            data['tr2'] = abs(data['high'] - data['prev_close'])
            data['tr3'] = abs(data['low'] - data['prev_close'])
            data['tr'] = data[['tr1', 'tr2', 'tr3']].max(axis=1)
            return float(data['tr'].rolling(window=period).mean().iloc[-1])
    except Exception as e:
        logger.debug(f"ATR 计算失败 {symbol}: {e}")
    return 0.0

def main():
    from futu import OpenSecTradeContext, OpenQuoteContext, RET_OK, TrdEnv, TrdMarket, SecurityFirm
    
    # 连接 Broker
    trade_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US, host='127.0.0.1', port=11111,
        security_firm=SecurityFirm.FUTUINC,
    )
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    
    try:
        ret, broker_data = trade_ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        if ret != RET_OK:
            logger.error(f"Broker 查询失败: {broker_data}")
            return
        
        # Broker 持仓
        broker_positions = {}
        for _, row in broker_data.iterrows():
            qty = float(row.get('qty', 0))
            if abs(qty) < 0.01:
                continue
            symbol = row['code']
            direction = 'SHORT' if qty < 0 else 'LONG'
            broker_positions[symbol] = {
                'symbol': symbol, 'direction': direction, 'size': abs(qty),
                'avg_entry_price': float(row.get('cost_price', 0)),
                'position_id': str(row.get('position_id', '')),
                'pnl': float(row.get('pl_val', 0)),
                'pnl_pct': float(row.get('pl_ratio', 0)),
            }
        
        # DB 持仓
        conn = sqlite3.connect('data/ftseek_us.db', timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        db_positions = {}
        rows = conn.execute('SELECT symbol, direction, size, avg_entry_price, current_sl, initial_sl, initial_tp, trail_state_json, take_profit_json FROM open_positions').fetchall()
        for r in rows:
            db_positions[r[0]] = {
                'symbol': r[0], 'direction': r[1], 'size': r[2],
                'avg_entry_price': r[3], 'current_sl': r[4], 'initial_sl': r[5],
                'initial_tp': r[6], 'trail_state_json': r[7], 'take_profit_json': r[8],
            }
        
        broker_symbols = set(broker_positions.keys())
        db_symbols = set(db_positions.keys())
        
        missing = broker_symbols - db_symbols
        extra = db_symbols - broker_symbols
        mismatches = set()
        
        for sym in broker_symbols & db_symbols:
            bp = broker_positions[sym]
            dp = db_positions[sym]
            if abs(bp['size'] - dp['size']) > 1 or abs(bp['avg_entry_price'] - dp['avg_entry_price']) > 0.5:
                mismatches.add(sym)
        
        issues = []
        
        # 1. 新增缺失持仓
        for sym in sorted(missing):
            bp = broker_positions[sym]
            atr = get_atr(quote_ctx, sym)
            entry = bp['avg_entry_price']
            direction = bp['direction']
            sl_dist = 2.5 * atr if atr > 0 else entry * 0.08
            
            if direction == 'LONG':
                initial_sl = entry - sl_dist
                initial_tp = entry + 2 * sl_dist
            else:
                initial_sl = entry + sl_dist
                initial_tp = entry - 2 * sl_dist
            
            trail_state = {
                'active': True, 'livermore_mode': True, 'activated': True,
                'initial_atr': atr, 'atr_mult': 2.0,
                'highest_price': entry, 'lowest_price': entry, 'peak_price': entry,
                'prev_scan_price': entry, 'entry_price': entry, 'entry_atr': atr,
            }
            
            take_profit_levels = [
                {"ratio": 0.3, "price": round(initial_tp if direction == 'LONG' else initial_tp, 2)},
                {"ratio": 0.3, "price": round(entry * 1.15 if direction == 'LONG' else entry * 0.85, 2)},
                {"ratio": 0.4, "price": round(entry * 1.25 if direction == 'LONG' else entry * 0.75, 2)},
            ]
            
            now = datetime.now().isoformat()
            conn.execute("""
                INSERT INTO open_positions (
                    symbol, direction, size, avg_entry_price, initial_sl, current_sl,
                    initial_tp, take_profit_json, trail_state_json, entry_time,
                    time_stop_hours, strategy_id, signal_id, position_id, market_regime,
                    updated_at, trader_type, version, source, sync_status, last_sync_at,
                    is_filled, is_partially_reduced, is_anomalous, trace_id, action_log_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sym, direction, bp['size'], entry, initial_sl, initial_sl,
                initial_tp, json.dumps(take_profit_levels), json.dumps(trail_state),
                now, 72, 'broker_recovery', None, bp['position_id'], 'Risk-Off',
                now, 'SIMULATION', 1, 'broker_sync', 'synced', now,
                1, 0, 0, None, json.dumps([{"action": "auto_reconcile_add", "time": now, "atr": atr}])
            ))
            issues.append(f"新增: {sym} {direction} {bp['size']:.0f} @ ${entry:.2f} (ATR=${atr:.3f})")
            logger.info(f"新增持仓: {sym}")
        
        # 2. 更新不匹配持仓
        for sym in sorted(mismatches):
            bp = broker_positions[sym]
            dp = db_positions[sym]
            trail = json.loads(dp['trail_state_json'] or '{}')
            atr = trail.get('initial_atr', get_atr(quote_ctx, sym))
            
            trail['active'] = True
            trail['livermore_mode'] = True
            trail['activated'] = True
            trail['initial_atr'] = atr
            
            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE open_positions
                SET size=?, avg_entry_price=?, trail_state_json=?, sync_status='reconciled', updated_at=?
                WHERE symbol=?
            """, (bp['size'], bp['avg_entry_price'], json.dumps(trail), now, sym))
            issues.append(f"更新: {sym} 数量 {dp['size']:.0f}→{bp['size']:.0f}")
            logger.info(f"更新持仓: {sym}")
        
        # 3. 清理已平仓持仓 (方案A 2026-04-24: 同时收拢 Daemon 和主线程的软删除)
        # 延后 5 分钟确保平仓写入 trade_records 已落盘
        closed_positions = conn.execute("""
            SELECT symbol, sync_status FROM open_positions 
            WHERE sync_status IN ('closed_by_daemon', 'closed_by_scheduler',
                                   'closed_by_sync', 'closed_emergency')
              AND updated_at < datetime('now', '-5 minutes')
        """).fetchall()
        for row in closed_positions:
            sym = row[0]
            _status = row[1]
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (sym,))
            issues.append(f"清理已平仓 ({_status}): {sym}")
            logger.info(f"清理已平仓 {sym} ({_status})")
        
        # 3b. [SKIP] trail_state 已合并到 open_positions.trail_state_json，无独立表
        
        # 4. 标记多余持仓 (Broker 已不存在)
        for sym in sorted(extra):
            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE open_positions
                SET is_anomalous=1, sync_status='stale_not_in_broker', updated_at=?
                WHERE symbol=?
            """, (now, sym))
            issues.append(f"标记 stale: {sym}")
            logger.info(f"标记 stale: {sym}")
        
        # 4. 确保所有持仓都有激活的 trail_state
        for sym in broker_symbols & db_symbols:
            dp = db_positions[sym]
            trail = json.loads(dp['trail_state_json'] or '{}')
            if not trail.get('active') or not trail.get('livermore_mode'):
                atr = trail.get('initial_atr', get_atr(quote_ctx, sym))
                entry = dp['avg_entry_price']
                direction = dp['direction']
                
                trail['active'] = True
                trail['livermore_mode'] = True
                trail['activated'] = True
                trail['initial_atr'] = atr
                if not trail.get('highest_price'):
                    trail['highest_price'] = entry
                    trail['lowest_price'] = entry
                    trail['peak_price'] = entry
                
                conn.execute("""
                    UPDATE open_positions SET trail_state_json=?, sync_status='activated' WHERE symbol=?
                """, (json.dumps(trail), sym))
                issues.append(f"激活 trail_state: {sym}")
        
        conn.commit()
        conn.close()
        
        # 输出报告
        if issues:
            report = f"持仓对账报告 ({datetime.now().strftime('%Y-%m-%d %H:%M')}):\n"
            report += f"Broker: {len(broker_positions)} 持仓 | DB: {len(db_positions)} 持仓\n"
            report += f"变更: {len(issues)} 项\n"
            for issue in issues:
                report += f"  - {issue}\n"
            print(report)
        else:
            print(f"✅ 持仓对账正常 (Broker={len(broker_positions)}, DB={len(db_positions)})")
    
    finally:
        trade_ctx.close()
        quote_ctx.close()

if __name__ == "__main__":
    main()
