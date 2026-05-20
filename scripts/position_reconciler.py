#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek-US 持仓对账同步器 (Position Reconciler)

职责: 将 Futu Broker 实际持仓与本地 open_positions 表完全对齐。
解决之前 exit_monitor 只能监控 DB 中已有持仓、遗漏新持仓的致命缺陷。

设计原则:
1. Broker 是唯一真相源 (Source of Truth)
2. 本地 open_positions 必须与 Broker 100% 同步
3. 缺失的持仓自动补充 (含 trail_state 初始化)
4. 幽灵持仓 (本地有但 Broker 没有) 标记清理
5. 每次扫描前执行对账，保证 exit_monitor 看到的永远是最新状态

作者: FTSeek-US
日期: 2026-04-24
"""

import logging
import os
import sqlite3
import time
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("position_reconciler")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "ftseek_us.db"


class PositionReconciler:
    """持仓对账器 — 确保 open_positions 与 Futu Broker 完全一致"""
    
    def __init__(self):
        self.db_path = str(DB_PATH)
    
    def reconcile(self) -> dict:
        """
        执行完整对账流程
        
        Returns:
            dict: {
                'broker_count': int,        # Broker 实际持仓数
                'db_count': int,            # DB 对账后持仓数
                'added': [str],             # 新增同步的符号
                'updated': [str],           # 更新数量/止损的符号
                'removed': [str],           # 标记清理的幽灵持仓
                'reconciled_at': str        # 对账时间
            }
        """
        result = {
            'broker_count': 0,
            'db_count': 0,
            'added': [],
            'updated': [],
            'removed': [],
            'skipped': [],
            'reconciled_at': datetime.now().isoformat()
        }
        
        # 1. 获取 Broker 实际持仓
        broker_positions = self._get_broker_positions()
        result['broker_count'] = len(broker_positions)
        broker_symbols = set(broker_positions.keys())
        
        if not broker_positions:
            logger.warning("Broker 无持仓，跳过对账")
            return result
        
        # 2. 获取 DB 当前持仓
        db_positions = self._get_db_positions()
        db_symbols = set(db_positions.keys())
        
        # 3. 分类处理
        new_symbols = broker_symbols - db_symbols       # Broker 有，DB 没有 → 新增
        existing_symbols = broker_symbols & db_symbols  # 两边都有 → 检查是否需更新
        ghost_symbols = db_symbols - broker_symbols     # DB 有，Broker 没有 → 幽灵
        
        # 4. 新增缺失持仓
        # 方案 C 修复: stale_history 不跳过 Broker 真实存在的持仓, 防止做空股永久丢失
        stale_history = self._get_recently_stale_symbols()
        for sym in sorted(new_symbols):
            bp = broker_positions[sym]
            if sym in stale_history:
                # Broker 确认有真实持仓 → 强制新增, 不再当作幽灵跳过
                logger.warning(
                    f"  ⚠️ {sym} 在 stale_history 中, 但 Broker 确认有实际持仓 "
                    f"({bp['direction']} {bp['size']} @ {bp['avg_entry_price']:.3f}) → 强制新增"
                )
            self._upsert_position(sym, bp, source="reconcile_new")
            self._init_trail_state(sym, bp)
            result['added'].append(sym)
            logger.info(f"  ➕ 新增: {sym} ({bp['direction']} {bp['size']} @ {bp['avg_entry_price']:.3f})")
        
        # 5. 更新现有持仓 (数量对齐、止损刷新)
        for sym in sorted(existing_symbols):
            bp = broker_positions[sym]
            dp = db_positions[sym]
            
            needs_update = False
            # 数量变化 (分批减仓/加仓)
            if abs(bp['size'] - dp.get('size', 0)) > 0.5:
                needs_update = True
            # sync_status 被标记过 (如 closed_by_daemon) 但 Broker 还有
            if dp.get('sync_status') in ('closed_by_daemon', 'stale_not_in_broker', 'closed_by_scheduler'):
                needs_update = True
            
            if needs_update:
                self._upsert_position(sym, bp, source="reconcile_update")
                result['updated'].append(sym)
                logger.info(f"  🔄 更新: {sym} (DB size={dp.get('size',0)} → Broker size={bp['size']})")
        
        # 6. 标记幽灵持仓
        for sym in sorted(ghost_symbols):
            dp = db_positions[sym]
            if dp.get('sync_status') not in ('closed_by_daemon', 'stale_not_in_broker', 'closed_by_scheduler'):
                self._mark_stale(sym)
                result['removed'].append(sym)
                logger.warning(f"  🗑️ 幽灵: {sym} (本地有但 Broker 无，已标记清理)")
        
        # 7. 统计最终 DB 有效持仓
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM open_positions 
            WHERE sync_status NOT IN ('closed_by_daemon', 'stale_not_in_broker', 'closed_by_scheduler')
              AND (is_filled IS NULL OR is_filled = 1)
        """)
        result['db_count'] = cur.fetchone()[0]
        conn.close()
        
        return result
    
    def _get_broker_positions(self) -> Dict[str, dict]:
        """从 Futu Broker 查询实际持仓，返回 {symbol: position_dict}"""
        positions = {}
        try:
            from futu import OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket, SecurityFirm
            
            trade_env = TrdEnv.SIMULATE if os.getenv("TRADE_ENV") == "1" else TrdEnv.REAL
            opend_host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            opend_port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
            
            trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=opend_host,
                port=opend_port,
                security_firm=SecurityFirm.FUTUINC,
            )
            
            ret, data = trade_ctx.position_list_query(trd_env=trade_env)
            if ret == RET_OK and not data.empty:
                for _, row in data.iterrows():
                    qty = float(row.get('qty', 0))
                    if qty == 0:
                        continue
                    
                    code = row.get('code', '')
                    if not code:
                        continue
                    
                    position_side = str(row.get('position_side', 'LONG'))
                    is_short = (qty < 0) or (position_side == 'SHORT')
                    direction = "SHORT" if is_short else "LONG"
                    abs_qty = abs(qty)
                    cost_price = float(row.get('cost_price', 0))
                    
                    # 计算初始止损 (8% fallback，后续由 ATR 动态覆盖)
                    if is_short:
                        initial_sl = cost_price * 1.08
                    else:
                        initial_sl = cost_price * 0.92
                    
                    positions[code] = {
                        'symbol': code,
                        'direction': direction,
                        'size': int(abs_qty),
                        'avg_entry_price': cost_price,
                        'initial_sl': initial_sl,
                        'current_sl': initial_sl,
                        'source': 'broker_reconcile',
                        'pl_val': float(row.get('pl_val', 0)),
                    }
                
                logger.info(f"从 Broker 加载 {len(positions)} 个持仓 "
                           f"(LONG={sum(1 for p in positions.values() if p['direction']=='LONG')}, "
                           f"SHORT={sum(1 for p in positions.values() if p['direction']=='SHORT')})")
            
            trade_ctx.close()
        except Exception as e:
            logger.error(f"Broker 持仓查询失败: {e}")
        
        return positions
    
    def _get_db_positions(self) -> Dict[str, dict]:
        """从 open_positions 读取所有记录 (含已标记清理的)"""
        positions = {}
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM open_positions")
            for row in cur.fetchall():
                positions[row['symbol']] = dict(row)
            conn.close()
        except Exception as e:
            logger.error(f"DB 持仓读取失败: {e}")
        return positions
    
    def _get_recently_stale_symbols(self) -> set:
        """获取近期被标记为幽灵/陈旧的符号集合
        
        P0 修复 2026-04-25: 防止 reconciler 反复写入 Futu 模拟盘中不存在的幽灵持仓。
        检查 sync_status 为 stale 或 action_log 中包含 reconcile_new 但已被清理的记录。
        """
        stale = set()
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            # 查找被标记为 stale_not_in_broker 的记录
            cur.execute("""
                SELECT symbol FROM open_positions 
                WHERE sync_status IN ('stale_not_in_broker', 'closed_by_daemon', 'closed_by_scheduler')
            """)
            for row in cur.fetchall():
                stale.add(row[0])
            # 也查找 source='reconcile_new' 但已被清理过的记录
            cur.execute("""
                SELECT symbol FROM open_positions 
                WHERE source = 'reconcile_new' AND sync_status = 'synced'
                  AND action_log_json LIKE '%reconcile_new%'
                  AND updated_at < datetime('now', '-1 hour')
            """)
            for row in cur.fetchall():
                stale.add(row[0])
            conn.close()
        except Exception as e:
            logger.debug(f"获取近期陈旧符号失败: {e}")
        return stale
    
    def _upsert_position(self, symbol: str, bp: dict, source: str = "reconcile"):
        """插入或更新持仓记录"""
        now = datetime.now().isoformat()
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                INSERT OR REPLACE INTO open_positions (
                    symbol, direction, size, avg_entry_price, initial_sl, current_sl,
                    initial_tp, take_profit_json, trail_state_json, entry_time,
                    time_stop_hours, strategy_id, signal_id, position_id,
                    market_regime, updated_at, trader_type, source, sync_status,
                    last_sync_at, is_filled, is_partially_reduced, is_anomalous,
                    trace_id, action_log_json, version
                ) VALUES (
                    :symbol, :direction, :size, :avg_entry_price, :initial_sl, :current_sl,
                    0, '[]', '{}', :entry_time,
                    72, 'reconciler', NULL, NULL,
                    'Risk-Off', :updated_at, :trader_type, :source, 'synced',
                    :last_sync_at, 1, 0, 0,
                    NULL, :action_log, 1
                )
            """, {
                'symbol': symbol,
                'direction': bp['direction'],
                'size': bp['size'],
                'avg_entry_price': bp['avg_entry_price'],
                'initial_sl': bp['initial_sl'],
                'current_sl': bp['initial_sl'],
                'entry_time': bp.get('entry_time', now),
                'updated_at': now,
                'trader_type': os.getenv('TRADE_ENV', '0'),
                'source': source,
                'last_sync_at': now,
                'action_log': json.dumps([{'action': source, 'time': now}]),
            })
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"写入 {symbol} 持仓失败: {e}")
    
    def _init_trail_state(self, symbol: str, bp: dict):
        """为新持仓初始化 trail_state"""
        try:
            from app.risk.trail_state_store import save_trail_state
            
            entry_price = bp['avg_entry_price']
            trail = {
                'active': True,
                'livermore_mode': True,
                'activated': True,
                'initial_atr': 0.0,  # 首次扫描时会填充
                'atr_mult': 2.0,
                'highest_price': entry_price if bp['direction'] == 'LONG' else 0.0,
                'lowest_price': entry_price if bp['direction'] == 'SHORT' else 0.0,
                'peak_price': entry_price,
                'prev_scan_price': entry_price,
                'entry_price': entry_price,
                'entry_atr': 0.0,
                'last_trigger_time': 0,
            }
            save_trail_state(symbol, trail)
            logger.debug(f"trail_state 已初始化: {symbol}")
        except ImportError:
            # trail_state_store 不存在时，直接写 DB
            logger.warning(f"trail_state_store 不可用，跳过 {symbol} 初始化")
        except Exception as e:
            logger.warning(f"初始化 trail_state 失败 {symbol}: {e}")
    
    def _mark_stale(self, symbol: str):
        """标记幽灵持仓为已过期 — 并触发完整的同步清理流程"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            
            # 1. 读取持仓快照 (用于写 trade_records)
            cur = conn.cursor()
            cur.execute("SELECT * FROM open_positions WHERE symbol=?", (symbol,))
            pos = cur.fetchone()
            
            if pos and pos['sync_status'] not in ('closed_by_daemon', 'stale_not_in_broker', 'closed_by_scheduler'):
                logger.info(f"🔄 检测到外部平仓: {symbol} ({pos['direction']} {pos['size']} @ {pos['avg_entry_price']})")
                self._sync_external_close(conn, pos)
            
            # 2. 标记状态
            cur.execute("""
                UPDATE open_positions 
                SET sync_status='stale_not_in_broker', updated_at=?
                WHERE symbol=?
            """, (datetime.now().isoformat(), symbol))
            conn.commit()
            
        except Exception as e:
            logger.error(f"标记 {symbol} 过期失败: {e}")
        finally:
            try: conn.close()
            except: pass
    
    def _sync_external_close(self, conn, pos_row: sqlite3.Row):
        """
        外部平仓完整同步流程:
        1. 补录 trade_records (归因/复盘)
        2. 更新 capital_state (资金水位)
        3. 写 event 记录 (审计链路)
        4. 清理 trail_state (防止残留)
        5. 触发通知 (钉钉/飞书)
        """
        symbol = pos_row['symbol']
        direction = pos_row['direction']
        size = float(pos_row['size'])
        entry_price = float(pos_row['avg_entry_price'])
        now = datetime.now().isoformat()
        trade_date = datetime.now().strftime('%Y-%m-%d')
        
        try:
            cur = conn.cursor()
            
            # ── Step 1: 估算平仓价 (用最新市价或 entry_price * (1±pnl)) ──
            # 优先从 Futu 获取实际成交均价
            exit_price = self._get_last_price(symbol) or entry_price
            if direction == 'LONG':
                pnl_pct = (exit_price - entry_price) / entry_price
                realized_pnl = (exit_price - entry_price) * size
            else:
                pnl_pct = (entry_price - exit_price) / entry_price
                realized_pnl = (entry_price - exit_price) * size
            
            # 估算持仓时间
            holding_hours = 0.0
            try:
                if pos_row['entry_time']:
                    et = datetime.fromisoformat(pos_row['entry_time'].replace('Z', '+00:00').replace('+00:00', ''))
                    holding_hours = (datetime.now() - et).total_seconds() / 3600.0
            except:
                pass
            
            logger.info(f"  📝 补录平仓: {symbol} | exit=${exit_price:.3f} | pnl=${realized_pnl:.2f} ({pnl_pct*100:.1f}%)")
            
            # ── Step 2: 写 trade_records ──
            cur.execute("""
                INSERT INTO trade_records (
                    trade_date, symbol, direction, entry_time, exit_time,
                    entry_price, exit_price, quantity, realized_pnl, pnl_pct,
                    exit_reason, holding_hours, signal_strength, signal_confidence,
                    futu_order_id, voting_result, final_strength, consensus_level,
                    model_scores, strategy_id, decision_reason, max_drawdown,
                    trace_id, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade_date, symbol, direction, pos_row['entry_time'], now,
                entry_price, exit_price, size, realized_pnl, pnl_pct,
                'EXTERNAL_CLOSE', holding_hours, 0, 0.0,
                '', None, 0, 0.0, None,
                pos_row['strategy_id'] or 'reconciler',
                f'外部手动平仓 (Futu APP {direction})', 0.0,
                pos_row.get('trace_id', '') or '', now
            ))
            
            # ── Step 3: 更新 capital_state ──
            cur.execute("""
                SELECT current_equity, cash, market_value, buying_power, peak_equity,
                       drawdown_pct, max_drawdown_pct, water_level, initial_capital
                FROM capital_state ORDER BY id DESC LIMIT 1
            """)
            cap = cur.fetchone()
            if cap:
                old_equity = float(cap[0])
                old_cash = float(cap[1])
                old_mv = float(cap[2])
                old_bp = float(cap[3])
                old_peak = float(cap[4])
                old_dd = float(cap[5])
                old_mdd = float(cap[6])
                old_wl = cap[7]
                initial_cap = float(cap[8])
                
                new_equity = old_equity + realized_pnl
                new_cash = old_cash + realized_pnl
                new_mv = old_mv - (size * entry_price)  # 移除持仓市值
                new_bp = old_bp + realized_pnl
                new_peak = max(old_peak, new_equity)
                new_dd = (new_equity - new_peak) / new_peak * 100 if new_peak > 0 else 0
                new_mdd = min(old_mdd, new_dd)
                new_wl = 'green' if new_dd > -3 else ('yellow' if new_dd > -5 else 'red')
                
                cur.execute("""
                    INSERT INTO capital_state (
                        initial_capital, current_equity, peak_equity, cash, market_value,
                        buying_power, drawdown_pct, max_drawdown_pct, water_level,
                        is_locked, lock_reason, last_update
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    initial_cap, new_equity, new_peak, new_cash, new_mv,
                    new_bp, new_dd, new_mdd, new_wl, 0, '', now
                ))
                
                logger.info(f"  💰 资金更新: equity ${old_equity:,.0f}→${new_equity:,.0f} | pnl ${realized_pnl:+,.0f}")
            
            # ── Step 4: 写 event ──
            cur.execute("""
                INSERT INTO events (event_type, timestamp, signal_id, order_id, position_id, data, source, severity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                'EXTERNAL_POSITION_CLOSED', now, None, None, pos_row.get('position_id'),
                json.dumps({
                    'symbol': symbol, 'direction': direction, 'quantity': size,
                    'entry_price': entry_price, 'exit_price': exit_price,
                    'realized_pnl': realized_pnl, 'pnl_pct': pnl_pct,
                    'reason': f'外部手动平仓 (Futu APP {direction})',
                    'detected_by': 'position_reconciler', 'detection_time': now,
                }),
                'position_reconciler', 'INFO'
            ))
            
            # ── Step 5: 清理 trail_state ──
            try:
                from app.risk.trail_state_store import get_trail_state_store
                get_trail_state_store().delete(symbol)
            except Exception:
                cur.execute("DELETE FROM trail_state WHERE symbol=?", (symbol,))
            
            # ── Step 6: 触发通知 ──
            self._notify_external_close(symbol, direction, size, entry_price, exit_price, realized_pnl, pnl_pct, holding_hours)
            
        except Exception as e:
            logger.error(f"同步外部平仓失败 {symbol}: {e}", exc_info=True)
    
    def _get_last_price(self, symbol: str) -> float:
        """获取股票最新市价"""
        try:
            from futu import OpenQuoteContext, RET_OK
            quote_ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"), port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
            ret, data = quote_ctx.get_market_snapshot([symbol])
            quote_ctx.close()
            if ret == RET_OK and not data.empty:
                price = float(data.iloc[0].get('last_price', 0))
                if price > 0:
                    return price
        except Exception:
            pass
        return 0.0
    
    def _notify_external_close(self, symbol, direction, size, entry, exit_price, pnl, pnl_pct, holding_hours):
        """发送外部平仓通知"""
        try:
            webhook_url = os.getenv('FEISHU_WEBHOOK_URL_US') or os.getenv('DINGTALK_WEBHOOK_URL_US')
            if not webhook_url:
                return
            
            emoji = "🎉" if pnl > 0 else "📉"
            direction_cn = "做多" if direction == "LONG" else "做空"
            close_cn = "买入平仓" if direction == "LONG" else "卖出平空"
            
            card = {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": f"{emoji} 外部平仓检测 — {symbol} {direction_cn} 已平仓"},
                        "template": "green" if pnl > 0 else "red"
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "lark_md",
                                "content": f"""**外部平仓检测** (position_reconciler 自动发现)

📊 **{symbol} {direction_cn} 平仓**
| 项目 | 值 |
|------|------|
| 方向 | {direction} ({direction_cn}) |
| 开仓价 | ${entry:.3f} |
| 平仓价 | ~${exit_price:.3f} |
| 数量 | {int(size):,} 股 |
| 实现利润 | **${pnl:+,.0f} ({pnl_pct*100:+.1f}%)** |
| 持仓时间 | {holding_hours:.1f} 小时 |
| 平仓方式 | 手动 (Futu APP {close_cn}) |

✅ 系统已自动同步: trade_records / capital_state / trail_state / 风控管线"""
                            }
                        },
                        {"tag": "hr"},
                        {
                            "tag": "note",
                            "elements": [
                                {"tag": "plain_text", "content": f"FTSeek-US Reconciler | {datetime.now().strftime('%H:%M:%S')} | 自动检测"}
                            ]
                        }
                    ]
                }
            }
            
            import urllib.request, urllib.error
            data = json.dumps(card).encode('utf-8')
            req = urllib.request.Request(
                webhook_url, data=data,
                headers={'Content-Type': 'application/json'}, method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"📨 外部平仓通知已发送: {symbol}")
        except Exception as e:
            logger.warning(f"发送外部平仓通知失败: {e}")


def run_reconcile() -> dict:
    """独立运行入口"""
    reconciler = PositionReconciler()
    result = reconciler.reconcile()
    
    logger.info("=" * 60)
    logger.info("持仓对账完成")
    logger.info(f"  Broker: {result['broker_count']} 只")
    logger.info(f"  DB 有效: {result['db_count']} 只")
    if result['added']:
        logger.info(f"  新增: {', '.join(result['added'])}")
    if result['updated']:
        logger.info(f"  更新: {', '.join(result['updated'])}")
    if result['removed']:
        logger.warning(f"  清理: {', '.join(result['removed'])}")
    logger.info("=" * 60)
    
    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    run_reconcile()
