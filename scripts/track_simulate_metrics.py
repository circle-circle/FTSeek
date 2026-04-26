#!/usr/bin/env python3.8
"""
FTSeek 模拟盘指标追踪 & 实盘就绪检查 (v2)

从 ftseek.db 真实交易数据计算所有指标，不再依赖独立 JSON 文件。

用法:
    python3.8 scripts/track_simulate_metrics.py          # 完整报告 + 就绪检查
    python3.8 scripts/track_simulate_metrics.py check     # 仅就绪检查
    python3.8 scripts/track_simulate_metrics.py summary   # 仅每日汇总
    python3.8 scripts/track_simulate_metrics.py weights   # 仅权重收敛
"""
import os
import sys
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

# 确保可以 import app 模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 常量 ──────────────────────────────────────────────────────────────
DB_PATH = PROJECT_ROOT / "data" / "ftseek.db"
INITIAL_CAPITAL = 1_000_000.0  # 初始资金 (港元)

# 准入条件阈值
READINESS = {
    'min_trading_days': 14,
    'min_total_trades': 100,
    'min_win_rate_recent30': 0.48,
    'min_profit_factor_recent30': 1.3,
    'max_drawdown': 0.08,
    'min_weight_convergence': 0.80,   # >=80% 策略置信度>=0.8
    'min_uptime_days': 14,
}


# ── ANSI 颜色 ─────────────────────────────────────────────────────────
class C:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'


def _connect():
    """连接 SQLite (只读)"""
    if not DB_PATH.exists():
        print(f"{C.RED}数据库不存在: {DB_PATH}{C.END}")
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


# ── 数据获取 ──────────────────────────────────────────────────────────

def get_all_closed_trades(conn) -> list:
    """获取所有已平仓交易"""
    c = conn.cursor()
    # 动态检测列是否存在
    c.execute("PRAGMA table_info(positions)")
    columns = {row[1] for row in c.fetchall()}

    base_cols = ["code", "entry_price", "exit_price", "realized_pnl",
                 "quantity", "entry_time", "exit_time", "strategy"]
    optional_cols = ["exit_reason", "is_add"]
    select_cols = [col for col in base_cols if col in columns]
    for oc in optional_cols:
        if oc in columns:
            select_cols.append(oc)

    # 构建 WHERE 条件
    where = "status = 'CLOSED'"
    if 'is_add' in columns:
        where += " AND (is_add IS NULL OR is_add = 0)"

    c.execute(
        f"""SELECT {', '.join(select_cols)}
           FROM positions
           WHERE {where}
           ORDER BY exit_time ASC"""
    )
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, row)) for row in c.fetchall()]


def get_trading_days_count(conn) -> int:
    """计算模拟运行的交易天数"""
    c = conn.cursor()
    c.execute(
        """SELECT COUNT(DISTINCT date(entry_time))
           FROM positions
           WHERE entry_time IS NOT NULL"""
    )
    return c.fetchone()[0] or 0


def get_first_trade_date(conn) -> str:
    """获取第一笔交易日期"""
    c = conn.cursor()
    c.execute(
        """SELECT MIN(date(entry_time))
           FROM positions
           WHERE entry_time IS NOT NULL"""
    )
    row = c.fetchone()
    return row[0] if row and row[0] else "N/A"


def get_weight_snapshots(conn) -> dict:
    """读取权重快照 (兼容多种表名)"""
    c = conn.cursor()
    # 检查表是否存在
    for table in ('weight_snapshots', 'adaptive_weight_snapshots'):
        c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        if c.fetchone():
            try:
                c.execute(f"SELECT strategy, weight, win_rate, trades FROM {table}")
                rows = c.fetchall()
                return {
                    row[0]: {
                        'weight': row[1], 'win_rate': row[2], 'trades': row[3]
                    }
                    for row in rows
                }
            except Exception:
                pass
    return {}


# ── 指标计算 ──────────────────────────────────────────────────────────

def calc_win_rate(trades: list) -> float:
    """胜率"""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get('realized_pnl') or 0) > 0)
    return wins / len(trades)


def calc_profit_factor(trades: list) -> float:
    """盈亏比 (avg_win / avg_loss)"""
    if not trades:
        return 0.0
    winners = [t for t in trades if (t.get('realized_pnl') or 0) > 0]
    losers = [t for t in trades if (t.get('realized_pnl') or 0) < 0]
    if not winners or not losers:
        return float('inf') if winners else 0.0
    avg_win = sum(t['realized_pnl'] for t in winners) / len(winners)
    avg_loss = abs(sum(t['realized_pnl'] for t in losers) / len(losers))
    return avg_win / avg_loss if avg_loss > 0 else float('inf')


def calc_max_drawdown(trades: list) -> float:
    """最大回撤 (基于 equity curve)"""
    if not trades:
        return 0.0
    equity = INITIAL_CAPITAL
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += (t.get('realized_pnl') or 0)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_strategy_stats(trades: list) -> dict:
    """按策略分组统计"""
    stats = {}
    for t in trades:
        strat = t.get('strategy') or 'unknown'
        if strat not in stats:
            stats[strat] = {'wins': 0, 'losses': 0, 'pnl': 0.0, 'count': 0}
        stats[strat]['count'] += 1
        pnl = t.get('realized_pnl') or 0
        stats[strat]['pnl'] += pnl
        if pnl > 0:
            stats[strat]['wins'] += 1
        elif pnl < 0:
            stats[strat]['losses'] += 1
    for s in stats.values():
        total = s['wins'] + s['losses']
        s['win_rate'] = s['wins'] / total if total > 0 else 0
    return stats


def calc_exit_distribution(trades: list) -> dict:
    """退出原因分布"""
    dist = {}
    for t in trades:
        reason = t.get('exit_reason') or 'unknown'
        dist[reason] = dist.get(reason, 0) + 1
    return dist


def calc_weight_convergence(conn) -> tuple:
    """
    计算权重收敛度
    返回 (converged_count, total_strategies, convergence_ratio, details)
    """
    snapshots = get_weight_snapshots(conn)
    confidence_trades = 20  # 与 AdaptiveWeightManager.CONFIDENCE_TRADES 一致

    details = {}
    converged = 0
    for strategy, info in snapshots.items():
        trades = info.get('trades', 0)
        confidence = min(1.0, trades / confidence_trades)
        is_converged = confidence >= 0.8
        if is_converged:
            converged += 1
        details[strategy] = {
            'trades': trades,
            'confidence': confidence,
            'weight': info.get('weight', 0),
            'win_rate': info.get('win_rate', 0),
            'converged': is_converged,
        }

    total = len(snapshots) or 1
    return converged, len(snapshots), converged / total, details


def check_system_uptime() -> int:
    """检查系统最近连续运行天数 (简单检查: 近N天是否每天都有信号记录)"""
    signal_dir = PROJECT_ROOT / "data" / "signal_records"
    if not signal_dir.exists():
        return 0

    today = datetime.now().date()
    consecutive = 0
    for i in range(60):  # 最多回溯60天
        check_date = today - timedelta(days=i)
        pattern = f"signals_{check_date.strftime('%Y-%m-%d')}.json"
        if (signal_dir / pattern).exists():
            consecutive += 1
        else:
            if i > 0:  # 跳过今天(可能还未生成)
                break
    return consecutive


# ── 输出格式化 ────────────────────────────────────────────────────────

def print_header(title: str):
    print(f"\n{C.BOLD}{'='*60}{C.END}")
    print(f"{C.BOLD}{C.CYAN}  {title}{C.END}")
    print(f"{C.BOLD}{'='*60}{C.END}")


def print_section(title: str):
    print(f"\n{C.BOLD}--- {title} ---{C.END}")


def print_check(name: str, ok: bool, value, threshold: str):
    icon = f"{C.GREEN}PASS{C.END}" if ok else f"{C.RED}FAIL{C.END}"
    print(f"  [{icon}] {name}: {C.BOLD}{value}{C.END}  (要求: {threshold})")


def cmd_summary(conn):
    """每日汇总"""
    trades = get_all_closed_trades(conn)
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = [t for t in trades if str(t.get('exit_time', ''))[:10] == today]

    print_header(f"每日汇总 - {today}")
    if not today_trades:
        print(f"  {C.DIM}今日无平仓交易{C.END}")
    else:
        today_pnl = sum(t.get('realized_pnl', 0) for t in today_trades)
        today_wins = sum(1 for t in today_trades if (t.get('realized_pnl') or 0) > 0)
        wr = today_wins / len(today_trades) if today_trades else 0
        icon = C.GREEN if today_pnl >= 0 else C.RED
        print(f"  交易数: {len(today_trades)}")
        print(f"  盈/亏:  {today_wins}/{len(today_trades) - today_wins}")
        print(f"  胜率:   {wr:.1%}")
        print(f"  盈亏:   {icon}HK${today_pnl:+,.2f}{C.END}")

    # 累计
    print_section("累计统计")
    total_pnl = sum(t.get('realized_pnl', 0) for t in trades)
    print(f"  总交易: {len(trades)} 笔 | 首笔: {get_first_trade_date(conn)}")
    print(f"  总盈亏: {'%s' % C.GREEN if total_pnl >= 0 else C.RED}HK${total_pnl:+,.2f}{C.END}")
    print(f"  胜率:   {calc_win_rate(trades):.1%}")
    print(f"  盈亏比: {calc_profit_factor(trades):.2f}")
    print(f"  回撤:   {calc_max_drawdown(trades):.2%}")


def cmd_weights(conn):
    """权重收敛状态"""
    converged, total, ratio, details = calc_weight_convergence(conn)

    print_header("策略权重收敛状态")
    if not details:
        print(f"  {C.DIM}无权重快照数据 (系统可能尚未运行足够交易){C.END}")
        return

    for name, d in sorted(details.items(), key=lambda x: -x[1]['confidence']):
        conf = d['confidence'] * 100
        bar = "█" * int(conf / 10) + "░" * (10 - int(conf / 10))
        icon = f"{C.GREEN}✓{C.END}" if d['converged'] else f"{C.YELLOW}○{C.END}"
        print(
            f"  {icon} {name:25s} {bar} {conf:5.1f}% "
            f"({d['trades']:3d}/20笔) "
            f"权重={d['weight']:.3f} 胜率={d['win_rate']:.0%}"
        )

    print(f"\n  收敛度: {converged}/{total} ({ratio:.0%})")
    if ratio >= READINESS['min_weight_convergence']:
        print(f"  {C.GREEN}权重已充分收敛{C.END}")
    else:
        remaining = max(0, int(total * READINESS['min_weight_convergence']) - converged)
        print(f"  {C.YELLOW}尚需 {remaining} 个策略达到收敛 (需各累积 >=16 笔交易){C.END}")


def cmd_check(conn):
    """实盘就绪检查 (7 项准入条件)"""
    trades = get_all_closed_trades(conn)
    recent_30 = trades[-30:] if len(trades) >= 30 else trades

    trading_days = get_trading_days_count(conn)
    total_trades = len(trades)
    win_rate_30 = calc_win_rate(recent_30)
    pf_30 = calc_profit_factor(recent_30)
    max_dd = calc_max_drawdown(trades)
    conv_cnt, conv_total, conv_ratio, _ = calc_weight_convergence(conn)
    uptime = check_system_uptime()

    print_header("实盘就绪检查 (7 项准入条件)")

    checks = [
        ("模拟运行天数",
         trading_days >= READINESS['min_trading_days'],
         f"{trading_days} 天",
         f">= {READINESS['min_trading_days']} 天"),

        ("有效交易笔数",
         total_trades >= READINESS['min_total_trades'],
         f"{total_trades} 笔",
         f">= {READINESS['min_total_trades']} 笔"),

        ("综合胜率 (近30笔)",
         win_rate_30 >= READINESS['min_win_rate_recent30'],
         f"{win_rate_30:.1%}",
         f">= {READINESS['min_win_rate_recent30']:.0%}"),

        ("盈亏比 (近30笔)",
         pf_30 >= READINESS['min_profit_factor_recent30'],
         f"{pf_30:.2f}",
         f">= {READINESS['min_profit_factor_recent30']:.1f}"),

        ("最大回撤",
         max_dd <= READINESS['max_drawdown'],
         f"{max_dd:.2%}",
         f"<= {READINESS['max_drawdown']:.0%}"),

        ("权重收敛度",
         conv_ratio >= READINESS['min_weight_convergence'],
         f"{conv_cnt}/{conv_total} ({conv_ratio:.0%})",
         f">= {READINESS['min_weight_convergence']:.0%} 策略收敛"),

        ("系统连续运行",
         uptime >= READINESS['min_uptime_days'],
         f"{uptime} 天",
         f">= {READINESS['min_uptime_days']} 天"),
    ]

    passed = 0
    for name, ok, value, threshold in checks:
        print_check(name, ok, value, threshold)
        if ok:
            passed += 1

    print(f"\n  {C.BOLD}通过率: {passed}/{len(checks)}{C.END}")
    if passed == len(checks):
        print(f"\n  {C.GREEN}{C.BOLD}所有标准已达成！可以考虑切换到实盘。{C.END}")
        print(f"  运行: python3.8 scripts/switch_trading_mode.py live_conservative")
    else:
        remaining = len(checks) - passed
        print(f"\n  {C.YELLOW}尚有 {remaining} 项未达标，请继续模拟盘验证。{C.END}")

    return passed == len(checks)


def cmd_full(conn):
    """完整报告"""
    trades = get_all_closed_trades(conn)

    # 1. 汇总
    cmd_summary(conn)

    # 2. 策略分析
    print_section("策略表现")
    strat_stats = calc_strategy_stats(trades)
    if strat_stats:
        for name, s in sorted(strat_stats.items(), key=lambda x: -x[1]['pnl']):
            icon = C.GREEN if s['pnl'] >= 0 else C.RED
            star = " ★" if s['win_rate'] >= 0.6 else ""
            print(
                f"  {name:25s} | {s['count']:3d}笔 | "
                f"胜率 {s['win_rate']:.0%} | "
                f"{icon}HK${s['pnl']:+,.0f}{C.END}{star}"
            )

    # 3. 退出分布
    print_section("退出原因分布 (近30笔)")
    recent = trades[-30:] if len(trades) >= 30 else trades
    exit_dist = calc_exit_distribution(recent)
    total_exits = sum(exit_dist.values()) or 1
    label_map = {
        'stop_loss': '止损', 'take_profit': '止盈',
        'trailing_stop': '追踪止盈', 'time_exit': '时间退出',
        'risk_control': '风控退出', 'partial_take_profit': '分批止盈',
    }
    for reason, count in sorted(exit_dist.items(), key=lambda x: -x[1]):
        label = label_map.get(reason, reason)
        pct = count / total_exits
        bar = "▓" * int(pct * 20) + "░" * (20 - int(pct * 20))
        print(f"  {label:12s} {bar} {pct:5.1%} ({count}笔)")

    # 4. 权重
    cmd_weights(conn)

    # 5. 就绪检查
    cmd_check(conn)


# ── 入口 ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    conn = _connect()
    try:
        cmd_name = sys.argv[1] if len(sys.argv) > 1 else 'full'
        if cmd_name == 'check':
            cmd_check(conn)
        elif cmd_name == 'summary':
            cmd_summary(conn)
        elif cmd_name == 'weights':
            cmd_weights(conn)
        elif cmd_name == 'full':
            cmd_full(conn)
        else:
            print(f"未知命令: {cmd_name}")
            print("用法: python3.8 scripts/track_simulate_metrics.py [check|summary|weights|full]")
            sys.exit(1)
    finally:
        conn.close()
