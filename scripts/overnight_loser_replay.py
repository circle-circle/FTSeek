#!/usr/bin/env python3
"""DEB-20260815 §33 前置回放: "EOD未盈利仓继续隔夜" 历史反事实裁决.

用户提案 (问题3): 盈利落袋为安 (PRE_CLOSE盈利平仓, 现状已如此), 未盈利仓继续隔夜.
样本口径: 只测 PRE_CLOSE_EXIT 了结且 pnl_net<=0 的仓 (即收盘时水下被强平的单).
   SL/TIME_EXIT 日内了结不在本政策范围 (那是另一层机制, 用户要求保留per-symbol SL桶).
反事实: 若当日15:52不平, 继续持有隔夜, 次日三种了结:
  V1 gap     : 次日开盘平 (纯隔夜跳空效应)
  V2 fullday : 次日收盘平 (给满一天修复空间)
  V3 smart   : 次日开盘若已转正→开盘平; 否则持有到收盘
费用: 隔夜不增加往返结构, delta为纯价差 (同一round trip).
方向: LONG delta=s*(next-exit_close)*qty; SHORT s=-1.
已知局限: 日bar粒度, 无法模拟次日盘中SL触碰; V2/V3为乐观上界, V1为保守下界.
"""
import json, sqlite3
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
MIN = "2026-06-01"  # 全量攒样


def load_bars(symbol):
    pat = f"daily_{symbol.replace('.', '_')}_*.json"
    best = []
    for f in CACHE.glob(pat):
        try:
            bars = json.loads(f.read_text())
        except Exception:
            continue
        if len(bars) > len(best):
            best = bars
    return best


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT symbol, direction, entry_time, exit_time, entry_price,
               quantity, pnl_net, exit_reason
        FROM trade_records
        WHERE exit_reason = 'PRE_CLOSE_EXIT' AND pnl_net IS NOT NULL
          AND pnl_net <= 0 AND exit_time >= ?
        ORDER BY exit_time
    """, (MIN,)).fetchall()
    con.close()
    print(f"═══ 样本: EOD水下强平仓 n={len(rows)} ({MIN}起) ═══\n")

    _bars_cache = {}
    stats = {"V1": [], "V2": [], "V3": []}
    detail = []
    skipped = defaultdict(int)

    for r in rows:
        sym = r["symbol"]
        if sym not in _bars_cache:
            _bars_cache[sym] = load_bars(sym)
        bars = _bars_cache[sym]
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            skipped["exit_day_missing"] += 1
            continue
        i = days.index(ex_date)
        if i + 1 >= len(bars):
            skipped["no_next_day"] += 1
            continue
        exit_close = bars[i][4]
        next_open = bars[i + 1][1]
        next_close = bars[i + 1][4]
        s = 1.0 if r["direction"] == "LONG" else -1.0
        qty = float(r["quantity"] or 1)

        v1 = s * (next_open - exit_close) * qty
        v2 = s * (next_close - exit_close) * qty
        v3 = v1 if v1 > 0 else v2

        stats["V1"].append(v1)
        stats["V2"].append(v2)
        stats["V3"].append(v3)
        detail.append((ex_date, sym, r["direction"], r["pnl_net"], v1, v2))

    n = len(stats["V2"])
    print(f"可回放 n={n} | 跳过: {dict(skipped)}\n")
    if n == 0:
        print("样本不足, 无法裁决")
        return

    print("═══ 反事实: EOD水下仓继续隔夜 vs 当日15:52平仓 ═══")
    for k, lab in (("V1", "次日开盘平(保守)"), ("V2", "次日收盘平(乐观)"),
                   ("V3", "smart(转正即走)")):
        v = stats[k]
        wins = sum(1 for x in v if x > 0)
        print(f"  {k} {lab:<16}: Σdelta=${sum(v):+8.0f}  均笔${sum(v)/len(v):+7.2f}  "
              f"隔夜修复率={wins/len(v)*100:5.1f}%")

    # 分方向
    print("\n═══ 分方向 ═══")
    for d in ("LONG", "SHORT"):
        sub = [(a, b) for (dt, sym, dd, pnl, a, b) in detail if dd == d]
        if sub:
            print(f"  {d:<6} n={len(sub)}  V1=${sum(a for a,_ in sub):+8.0f}  "
                  f"V2=${sum(b for _,b in sub):+8.0f}")

    # 逐月
    print("\n═══ 逐月 (V2) ═══")
    by_month = defaultdict(list)
    for (dt, sym, dd, pnl, a, b) in detail:
        by_month[dt[:7]].append(b)
    for m in sorted(by_month):
        v = by_month[m]
        print(f"  {m}: n={len(v)}  ${sum(v):+8.0f}")

    # 明细
    print("\n═══ 逐笔明细 ═══")
    for (dt, sym, dd, pnl, a, b) in detail:
        print(f"  {dt} {sym:<10} {dd:<6} 当日了结${pnl:+7.1f} → 隔夜V1${a:+7.1f} V2${b:+7.1f}")

    out = REPO / "data" / "overnight_loser_replay_2026-08-15.json"
    out.write_text(json.dumps({
        "sample_n": len(rows), "replayed_n": n, "skipped": dict(skipped),
        "V1_total": sum(stats["V1"]), "V2_total": sum(stats["V2"]),
        "V3_total": sum(stats["V3"]),
        "detail": [dict(date=dt, symbol=sym, direction=dd, day_pnl=pnl,
                        v1=a, v2=b) for (dt, sym, dd, pnl, a, b) in detail],
    }, indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
