#!/usr/bin/env python3
"""个股画像卡 (只读) — 统一聚合单股的历史规律, 消除黑盒。

数据源 (全部只读, 不改任何决策逻辑):
  1. strategy_memory.db / symbol_memory  — 历史胜率×方向 (已修复为 trade_records 真源投影)
  2. ftseek_us.db / playbook_instructions — 近期指令: 方向/结果/退出/捕获率/vol_bucket
  3. strategy_memory.db / pattern_memory  — 该方向的退出模式表现

用法:
    ./venv/bin/python scripts/symbol_profile.py NVDA          # 单股画像
    ./venv/bin/python scripts/symbol_profile.py US.NVDA SHORT # 指定方向
    ./venv/bin/python scripts/symbol_profile.py --all         # 全部有历史的股 (概览表)
    ./venv/bin/python scripts/symbol_profile.py --all --min 5 # 仅 >=5 笔的股
"""
import argparse
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FTSEEK_DB = os.path.join(_ROOT, "data", "ftseek_us.db")
MEM_DB = os.path.join(_ROOT, "data", "strategy_memory.db")


def _norm(sym: str) -> str:
    s = sym.strip().upper()
    return s if s.startswith("US.") else f"US.{s}"


def _symbol_memory(symbol: str):
    if not os.path.exists(MEM_DB):
        return []
    c = sqlite3.connect(MEM_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM symbol_memory WHERE symbol=? ORDER BY total_trades DESC", (symbol,)
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _recent_instructions(symbol: str, limit: int = 12):
    c = sqlite3.connect(FTSEEK_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT date, direction, outcome, pnl, pnl_pct, exit_reason, confidence,
               vol_bucket, atr_pct, max_favorable_excursion, max_adverse_excursion,
               execution_price, position_size
        FROM playbook_instructions
        WHERE symbol=? AND direction != 'SKIP' AND executed=1
          AND outcome IN ('win','loss')
        ORDER BY date DESC, id DESC LIMIT ?
    """, (symbol, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _pattern_for_direction(direction: str):
    if not os.path.exists(MEM_DB):
        return []
    c = sqlite3.connect(MEM_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT exit_reason, strength_bucket, total_trades, wins, win_rate, avg_pnl, total_pnl
        FROM pattern_memory WHERE direction=? AND total_trades>=2
        ORDER BY total_pnl DESC
    """, (direction,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _conf_level(n: int) -> str:
    if n < 5:
        return "very_low(样本不足)"
    if n < 20:
        return "low"
    if n < 50:
        return "medium"
    return "high"


def print_card(symbol: str, direction_filter: str = ""):
    symbol = _norm(symbol)
    mem = _symbol_memory(symbol)
    if direction_filter:
        mem = [m for m in mem if m["direction"] == direction_filter.upper()]

    print("=" * 60)
    print(f"  个股画像卡  {symbol}")
    print("=" * 60)

    if not mem:
        print("  无历史成交记录 (symbol_memory 空)")
    for m in mem:
        d = m["direction"]
        n = m["total_trades"]
        wr = m["win_rate"] or 0
        print(f"\n【{d}】历史 {n} 笔 | 胜率 {wr:.0%} | 置信度 {_conf_level(n)}")
        print(f"   净盈亏 ${m['total_pnl']:+.0f} | 均笔 ${m['avg_pnl']:+.1f} | "
              f"最佳 ${m['best_win'] or 0:+.0f} | 最差 ${m['worst_loss'] or 0:+.0f}")
        print(f"   最后交易日: {m['last_trade_date']}")

    # 近期指令明细 (含捕获率)
    recent = _recent_instructions(symbol)
    if recent:
        print(f"\n── 近期指令 (最多12条, 含捕获率) ──")
        for r in recent:
            mfe = r["max_favorable_excursion"] or 0
            pnl = r["pnl"] or 0
            cap = (pnl / mfe * 100) if mfe > 0 else 0
            vb = r["vol_bucket"] or "?"
            atr = f"{r['atr_pct']*100:.1f}%" if r["atr_pct"] else "?"
            print(f"   {r['date']} {r['direction']:10} {r['outcome']:4} "
                  f"${pnl:+6.0f} 捕获{cap:4.0f}% | {r['exit_reason']:15} "
                  f"conf={r['confidence'] or 0:>3} vol={vb}/{atr}")
    else:
        print("\n── 近期无已平仓指令 ──")

    # 该方向的退出模式 (跨股, 供参考)
    if mem:
        for d in sorted({m["direction"] for m in mem}):
            pats = _pattern_for_direction(d)[:3]
            if pats:
                print(f"\n── {d} 方向最佳退出模式 (全样本参考) ──")
                for p in pats:
                    print(f"   {p['exit_reason']:16} {p['strength_bucket']:6} "
                          f"{p['total_trades']:2}笔 胜率{p['win_rate']:.0%} "
                          f"净${p['total_pnl']:+.0f}")
    print()


def print_all(min_trades: int):
    if not os.path.exists(MEM_DB):
        print("strategy_memory.db 不存在")
        return
    c = sqlite3.connect(MEM_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT symbol, direction, total_trades, wins, win_rate, total_pnl, avg_pnl, last_trade_date
        FROM symbol_memory WHERE total_trades>=?
        ORDER BY total_pnl DESC
    """, (min_trades,)).fetchall()
    c.close()
    print("=" * 78)
    print(f"  全部个股画像概览 (>={min_trades}笔, 按净盈亏排序)   共 {len(rows)} 条")
    print("=" * 78)
    print(f"  {'股票':10}{'方向':6}{'笔数':>4}{'胜率':>6}{'净盈亏':>9}{'均笔':>8}  最后交易")
    print("  " + "-" * 74)
    for r in rows:
        print(f"  {r['symbol']:10}{r['direction']:6}{r['total_trades']:>4}"
              f"{r['win_rate']:>6.0%}{r['total_pnl']:>+9.0f}{r['avg_pnl']:>+8.1f}  "
              f"{r['last_trade_date']}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?", help="股票代码 (NVDA 或 US.NVDA)")
    ap.add_argument("direction", nargs="?", default="", help="方向 BUY/SELL_SHORT (可选)")
    ap.add_argument("--all", action="store_true", help="输出全部个股概览表")
    ap.add_argument("--min", type=int, default=2, help="--all 时最小笔数阈值")
    args = ap.parse_args()

    if args.all:
        print_all(args.min)
        return 0
    if not args.symbol:
        ap.print_help()
        return 1
    print_card(args.symbol, args.direction)
    return 0


if __name__ == "__main__":
    sys.exit(main())
