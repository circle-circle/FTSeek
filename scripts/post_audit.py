#!/usr/bin/env python3
"""FTSeek-US 盘后审计脚本 v2 (Post-Market Audit)

运行时间: 交易日 16:15-18:00 ET (收盘后)
功能:
1. 交易汇总 (收益, 胜率, 持仓, 资金占比)
2. TradeGuard 审计 (反手胜率, PDT 预警, 拦截后回撤)
3. 趋势对比 (今日 vs 近5日均值)
4. 归因分析 (策略贡献, LLM 投票表现)
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, time
from typing import Dict, List, Any

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import yaml
except ImportError:
    yaml = None

# ================== 防御性逻辑 ==================
def is_in_window():
    """判断当前美东时间是否在审计窗口 (16:15 - 18:00)"""
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
        return time(16, 15) <= et_now.time() <= time(18, 0)
    except Exception as e:
        return True

def is_trading_day():
    """判断今天是否为美股交易日"""
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
        if et_now.weekday() >= 5: return False
        month, day = et_now.month, et_now.day
        us_holidays = [
            (1, 1), (1, 20), (2, 17), (5, 26), (6, 19), 
            (7, 4), (9, 1), (11, 27), (12, 25)
        ]
        if (month, day) in us_holidays: return False
        return True
    except:
        return True
# ================================================

def format_pct(val: float) -> str:
    return f"{val:+.2%}" if val != 0 else "0.00%"
def format_usd(val: float) -> str:
    return f"${val:,.2f}"

def get_db_connection():
    db_path = PROJECT_ROOT / 'data' / 'ftseek_us.db'
    if not db_path.exists(): return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def load_config() -> Dict:
    config_path = PROJECT_ROOT / 'config' / 'unified_config.yaml'
    if not config_path.exists() or not yaml: return {}
    with open(config_path) as f: return yaml.safe_load(f)

def audit_daily_performance(conn) -> Dict[str, Any]:
    today = (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d')  # ET date
    cursor = conn.execute("SELECT * FROM performance_stats WHERE trade_date = ?", (today,))
    row = cursor.fetchone()
    if not row:
        cursor = conn.execute("SELECT * FROM trade_records WHERE date(entry_time) = ?", (today,))
        trades = cursor.fetchall()
        if not trades: return {'status': 'no_trades_today', 'message': '今日无交易记录'}
        total = len(trades)
        wins = sum(1 for t in trades if t['realized_pnl'] > 0)
        total_pnl = sum(t['realized_pnl'] for t in trades)
        return {
            'status': 'calculated', 'date': today,
            'total_trades': total, 'wins': wins, 'losses': total - wins,
            'win_rate': wins / total, 'total_pnl': total_pnl,
            'avg_pnl': total_pnl / total,
            'best_trade': max((t['realized_pnl'] for t in trades), default=0),
            'worst_trade': min((t['realized_pnl'] for t in trades), default=0),
        }
    return {
        'status': 'from_stats', 'date': row['trade_date'],
        'total_trades': row['total_trades'], 'wins': row['winning_trades'],
        'losses': row['losing_trades'], 'win_rate': row['win_rate'],
        'total_pnl': row['total_pnl'], 'avg_pnl': row['avg_pnl'],
        'max_drawdown': row['max_drawdown'], 'sharpe_ratio': row['sharpe_ratio'],
        'best_trade': row['best_trade_pnl'], 'worst_trade': row['worst_trade_pnl'],
    }

def get_5day_trend(conn, metric: str) -> float:
    """计算近 5 日均值"""
    cursor = conn.execute(f"SELECT {metric} FROM performance_stats ORDER BY trade_date DESC LIMIT 5")
    vals = [r[0] for r in cursor.fetchall() if r[0] is not None]
    return sum(vals) / len(vals) if vals else None

def audit_positions(conn) -> Dict[str, Any]:
    cursor = conn.execute("SELECT * FROM open_positions WHERE size > 0")
    positions = cursor.fetchall()
    if not positions: return {'status': 'empty', 'positions': []}
    cap = conn.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1").fetchone()
    total_equity = cap['current_equity'] if cap else 0
    total_mv = sum(p['size'] * p['avg_entry_price'] for p in positions)
    return {
        'status': 'active', 'positions': positions,
        'total_market_value': total_mv, 'total_equity': total_equity,
        'cash_ratio': (total_equity - total_mv) / total_equity if total_equity > 0 else 0,
    }

def audit_tradeguard(conn, config: Dict) -> Dict[str, Any]:
    today = (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d')  # ET date
    # 1. 反手胜率
    cursor = conn.execute("""SELECT COUNT(*) as cnt, SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins FROM trade_records WHERE date(entry_time) <= ? AND decision_reason LIKE '%REVERSE%'""", (today,))
    row = cursor.fetchone()
    reversal_total = row['cnt'] if row else 0
    reversal_wins = row['wins'] if row else 0
    reversal_win_rate = reversal_wins / reversal_total if reversal_total > 0 else 0
    # 2. PDT 拦截
    cursor = conn.execute("SELECT COUNT(*) FROM trade_records WHERE exit_reason LIKE '%PDT%'")
    pdt_blocks = cursor.fetchone()[0]
    # 3. 拦截后回撤
    cursor = conn.execute("SELECT AVG(max_drawdown) as avg_dd FROM trade_records WHERE exit_reason IN ('STOP_LOSS', 'TIME_STOP', 'CIRCUIT_BREAKER')")
    row = cursor.fetchone()
    avg_interception_dd = row['avg_dd'] if row and row['avg_dd'] else 0
    
    # 4. 从 guard_decisions.jsonl 读取精确指标 (P0)
    guard_log_path = PROJECT_ROOT / 'data' / 'guard_decisions.jsonl'
    guard_stats = {'total_decisions': 0, 'blocks': 0, 'allows': 0, 'false_positives': 0}
    if guard_log_path.exists():
        try:
            with open(guard_log_path, 'r') as f:
                for line in f:
                    if not line.strip(): continue
                    entry = json.loads(line)
                    if entry.get('datetime', '').startswith(today):
                        guard_stats['total_decisions'] += 1
                        if entry.get('decision') in ('BLOCK_OPEN_LONG', 'BLOCK_OPEN_SHORT', 'BLOCK_REVERSE'):
                            guard_stats['blocks'] += 1
                        else:
                            guard_stats['allows'] += 1
        except: pass
    
    risk = config.get('risk', {})
    cb = risk.get('circuit_breaker', {})
    return {
        'reversal_total': reversal_total, 'reversal_win_rate': reversal_win_rate,
        'pdt_blocks': pdt_blocks, 'avg_interception_drawdown': avg_interception_dd,
        'guard_stats': guard_stats,
        'config_thresholds': {
            'consecutive_losses_limit': cb.get('strategy', {}).get('consecutive_losses', 5),
            'hourly_loss_threshold': cb.get('strategy', {}).get('hourly_loss_threshold', -0.02),
        }
    }

def generate_report():
    # 防御性检查
    if not is_trading_day():
        print("📅 当前非交易日，跳过审计")
        sys.exit(0)
    if not is_in_window():
        print("⏰ 当前不在审计窗口 (16:15-18:00 ET)，跳过审计")
        sys.exit(0)

    print(f"\n{'='*60}")
    try:
        from zoneinfo import ZoneInfo
        print(f" FTSeek-US 盘后审计报告")
        print(f" 生成时间 (ET): {datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')}")
    except:
        print(f" FTSeek-US 盘后审计报告")
        print(f" 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    
    conn = get_db_connection()
    if not conn:
        print("[ERROR] 无法连接数据库")
        return
    config = load_config()
    
    try:
        # 1. Daily Performance
        perf = audit_daily_performance(conn)
        print("📊 当日交易表现 [DAILY PERFORMANCE]")
        print(f"  状态: {perf.get('status', 'unknown')}")
        if perf.get('total_trades', 0) > 0:
            print(f"  交易次数: {perf['total_trades']} (近5日均值: {get_5day_trend(conn, 'total_trades') or 'N/A'})")
            print(f"  胜率: {format_pct(perf['win_rate'])} (近5日均值: {format_pct(get_5day_trend(conn, 'win_rate') or 0)})")
            print(f"  总收益: {format_usd(perf['total_pnl'])} (近5日均值: {format_usd(get_5day_trend(conn, 'total_pnl') or 0)})")
            print(f"  最大回撤: {format_pct(perf.get('max_drawdown', 0))}")
        else:
            print(f"  {perf.get('message', '无数据')}")
        
        # 2. Positions
        print("\n📦 当前持仓 [POSITIONS]")
        pos_audit = audit_positions(conn)
        if pos_audit['status'] == 'active':
            print(f"  持仓数量: {len(pos_audit['positions'])}")
            print(f"  总市值: {format_usd(pos_audit['total_market_value'])}")
            print(f"  现金占比: {format_pct(pos_audit['cash_ratio'])}")
            for p in pos_audit['positions']:
                print(f"  - {p['symbol']}: {p['size']} shares @ {p['avg_entry_price']:.2f} ({p['direction']})")
        else:
            print("  当前无持仓")
        
        # 3. TradeGuard Audit
        print("\n🛡️ TradeGuard 审计 [GUARD METRICS]")
        guard = audit_tradeguard(conn, config)
        print(f"  反手交易: {guard['reversal_total']} 笔 (胜率: {format_pct(guard['reversal_win_rate'])})")
        print(f"  PDT 拦截: {guard['pdt_blocks']} 次")
        gs = guard.get('guard_stats', {})
        print(f"  决策日志 (今日): {gs.get('total_decisions', 0)} 次 | 拦截: {gs.get('blocks', 0)} | 放行: {gs.get('allows', 0)}")
        print(f"  配置阈值:")
        print(f"    - 连续亏损上限: {guard['config_thresholds']['consecutive_losses_limit']}")
        print(f"    - 小时亏损阈值: {format_pct(guard['config_thresholds']['hourly_loss_threshold'])}")
            
    except Exception as e:
        print(f"\n[ERROR] 审计过程异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 5. 追加历史趋势 (P2) - 必须在 conn.close() 之前
        try:
            append_history_csv(conn, perf)
        except Exception as e:
            print(f"  [WARN] 历史趋势记录失败: {e}")
        conn.close()
    
    print(f"\n{'='*60}")
    print(" 审计完成")
    print(f"{'='*60}\n")

def append_history_csv(conn, perf: Dict):
    """将当日关键指标追加到 history_stats.csv (P2)"""
    import csv
    csv_path = PROJECT_ROOT / 'data' / 'history_stats.csv'
    is_new = not csv_path.exists()
    
    today = (datetime.now() - timedelta(hours=12)).strftime('%Y-%m-%d')  # ET date
    cap = conn.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1").fetchone()
    
    row = {
        'date': today,
        'trades': perf.get('total_trades', 0),
        'win_rate': perf.get('win_rate', 0),
        'total_pnl': perf.get('total_pnl', 0),
        'max_drawdown': perf.get('max_drawdown', 0),
        'equity': cap['current_equity'] if cap else 0,
        'positions': conn.execute("SELECT COUNT(*) FROM open_positions WHERE size > 0").fetchone()[0],
    }
    
    with open(csv_path, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if is_new: writer.writeheader()
        writer.writerow(row)
    
    print(f"📈 历史趋势已追加: {csv_path}")

if __name__ == '__main__':
    generate_report()
