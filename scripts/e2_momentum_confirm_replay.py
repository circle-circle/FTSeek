#!/usr/bin/env python3
"""
E2: 入场动能确认 counterfactual 回放 (2026-08-07)
==================================================
背景 (E1 深化): DA 交易 strength=70 段 107笔 WR37% 净亏 -791; 时段解剖
显示亏损集中在 09:36-09:40 集中入场段 (61笔 -422)。全程 mfe 保守下界
已验证: θ=0.5% 时 "mfe<0.5%" 必拦 53笔 -1567, 通过 79笔 +698 WR66%。

本脚本: 用分钟级 bar (data/replay_bars/US_<SYMBOL>_<DATE>.json) 精确回放
"入场后 N 分钟窗口内 mfe" — 这是真实动能确认的判据 (区别于全程 mfe,
后者包含入场后任意时刻高点, 随时间推移必然增大, 高估动能)。

规则: LONG mfe = (window_max_high - entry_price)/entry_price*100;
      SHORT mfe = (entry_price - window_min_low)/entry_price*100;
动能确认 = 入场后 N 分钟内 mfe >= θ 才放行, 否则拦截 (SKIP)。

输出: 拦截 vs 放行 的 n/net/WR + 每单明细 (record-only, 零行为变化)。

用法: python3 scripts/e2_momentum_confirm_replay.py [--window 15] [--theta 0.5]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
DB_TRADES = PROJECT_ROOT / "data" / "ftseek_us.db"
DB_SNAPS = PROJECT_ROOT / "data" / "signal_snapshots.db"
REPLAY_DIR = PROJECT_ROOT / "data" / "replay_bars"


def parse_entry_et(raw: str):
    """entry_time → ET naive datetime (与 bar 时间戳对齐)."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        import pytz
        return dt.astimezone(pytz.timezone("America/New_York")).replace(tzinfo=None)
    return dt


def load_bars(symbol: str, trade_date: str):
    """加载标的当日分钟 bar. 返回 [(ts_str, open, high, low, close)], 时间升序."""
    # symbol 格式: US.GS → 文件名 US_GS (点号转下划线)
    fname = f"{symbol.replace('.', '_')}_{trade_date}.json"
    path = REPLAY_DIR / fname
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=15, help="入场后动能确认窗口 (分钟)")
    ap.add_argument("--theta", type=float, default=0.5, help="动能阈值 (%)")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_TRADES))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT decision_id, symbol, direction, entry_time, exit_reason,
               mfe_pct, mae_pct, pnl_net, trade_date, entry_price
        FROM trade_records
        WHERE decision_id LIKE 'DA-%' AND trade_date >= date('now', ?)
          AND mfe_pct IS NOT NULL AND entry_time IS NOT NULL AND entry_price > 0
    """, (f"-{args.days} days",)).fetchall()
    conn.close()

    snap = sqlite3.connect(str(DB_SNAPS))
    snap.row_factory = sqlite3.Row
    strength_map = {r["decision_id"]: r["strength"]
                    for r in snap.execute("SELECT decision_id, strength FROM signal_snapshots WHERE strength IS NOT NULL")}
    snap.close()

    from datetime import datetime, timedelta

    results = []
    no_bar = 0
    for r in rows:
        entry_dt = parse_entry_et(r["entry_time"])
        if entry_dt is None:
            continue
        bars = load_bars(r["symbol"], r["trade_date"])
        if not bars:
            no_bar += 1
            continue
        # 找入场时刻最近的 bar (入场时刻所在分钟或之后第一条)
        window_end = entry_dt + timedelta(minutes=args.window)
        win_high = None
        win_low = None
        for b in bars:
            try:
                ts = datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts < entry_dt:
                continue
            if ts > window_end:
                break
            hi, lo = float(b[2]), float(b[3])
            win_high = hi if win_high is None else max(win_high, hi)
            win_low = lo if win_low is None else min(win_low, lo)
        if win_high is None or win_low is None:
            no_bar += 1
            continue
        entry = float(r["entry_price"])
        if r["direction"] == "LONG":
            win_mfe = (win_high - entry) / entry * 100
        else:
            win_mfe = (entry - win_low) / entry * 100
        results.append({
            "decision_id": r["decision_id"],
            "symbol": r["symbol"],
            "trade_date": r["trade_date"],
            "strength": strength_map.get(r["decision_id"]),
            "window_mfe": round(win_mfe, 3),
            "full_mfe": r["mfe_pct"],
            "mae_pct": r["mae_pct"],
            "pnl_net": r["pnl_net"],
            "exit_reason": r["exit_reason"],
        })

    print(f"可回放: {len(results)}/{len(rows)} (无bar: {no_bar}) | 窗口={args.window}min θ={args.theta}%")

    blocked = [x for x in results if x["window_mfe"] < args.theta]
    passed = [x for x in results if x["window_mfe"] >= args.theta]

    def _stats(grp):
        if not grp:
            return "n=0"
        net = sum(x["pnl_net"] or 0 for x in grp)
        wr = sum(1 for x in grp if x["pnl_net"] and x["pnl_net"] > 0) / len(grp)
        avg_wmfe = sum(x["window_mfe"] for x in grp) / len(grp)
        return f"n={len(grp)} net={net:+.1f} WR={wr:.0%} avgWinMFE={avg_wmfe:.2f}%"

    print(f"\n=== 动能确认 ({args.window}min mfe>={args.theta}%) counterfactual ===")
    print(f"  拦截 (无动能): {_stats(blocked)}")
    print(f"  放行 (有动能): {_stats(passed)}")

    # strength 分层
    for st in [70, 60, None]:
        if st == 70:
            print(f"\n=== strength=70 子集 ===")
            b2 = [x for x in blocked if x["strength"] == 70]
            p2 = [x for x in passed if x["strength"] == 70]
            print(f"  拦截: {_stats(b2)}")
            print(f"  放行: {_stats(p2)}")
        elif st == 60:
            print(f"\n=== strength=60 子集 ===")
            b2 = [x for x in blocked if x["strength"] == 60]
            p2 = [x for x in passed if x["strength"] == 60]
            print(f"  拦截: {_stats(b2)}")
            print(f"  放行: {_stats(p2)}")

    # 拦截单明细 (亏损为主, 看被拦的价值)
    print(f"\n=== 拦截单明细 (window_mfe < {args.theta}%) ===")
    for x in sorted(blocked, key=lambda k: (k["pnl_net"] or 0)):
        print(f"  {x['trade_date']} {x['symbol']:<8} st={str(x['strength']):<4} "
              f"winMFE={x['window_mfe']:+.2f}% fullMFE={x['full_mfe']:+.2f}% "
              f"mae={x['mae_pct']:+.2f}% net={x['pnl_net']:+.1f} {x['exit_reason']}")

    # 放行单明细 (确认动能规则不误杀赢家)
    print(f"\n=== 放行单明细 (window_mfe >= {args.theta}%) ===")
    for x in sorted(passed, key=lambda k: -(k["pnl_net"] or 0)):
        print(f"  {x['trade_date']} {x['symbol']:<8} st={str(x['strength']):<4} "
              f"winMFE={x['window_mfe']:+.2f}% fullMFE={x['full_mfe']:+.2f}% "
              f"mae={x['mae_pct']:+.2f}% net={x['pnl_net']:+.1f} {x['exit_reason']}")

    # 保存
    out = {
        "_meta": {
            "generated_at": datetime.utcnow().isoformat(),
            "window_minutes": args.window,
            "theta_pct": args.theta,
            "days": args.days,
            "note": "E2 动能确认 counterfactual, record-only",
        },
        "stats": {
            "blocked": {"n": len(blocked),
                        "net": round(sum(x["pnl_net"] or 0 for x in blocked), 2),
                        "wr": round(sum(1 for x in blocked if x["pnl_net"] and x["pnl_net"] > 0) / len(blocked), 4) if blocked else None},
            "passed": {"n": len(passed),
                       "net": round(sum(x["pnl_net"] or 0 for x in passed), 2),
                       "wr": round(sum(1 for x in passed if x["pnl_net"] and x["pnl_net"] > 0) / len(passed), 4) if passed else None},
        },
        "trades": results,
    }
    out_path = PROJECT_ROOT / "data" / "e2_momentum_replay_latest.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()