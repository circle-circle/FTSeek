#!/usr/bin/env python3
"""P5 (dead_signal_exit_minutes 60->30) 精确反事实 (DEB-20260813 §十二)

用 early_dead_signal_shadow.jsonl 的 30min 检查点价做忠实反事实:
30min 时点 mfe_pct<0.2 且 cur_pct<0 → 判死, 按该时点价出场; 与实际已平仓结局求差。
费用两臂相同 (均为一趟往返), 故比较毛口径; MISS 的 delta 即误杀机会成本。
确定性零参数: python3 scripts/p5_early_cut_counterfactual.py
"""
import json
import sqlite3
from collections import defaultdict

DB = "data/ftseek_us.db"
SHADOW = "data/early_dead_signal_shadow.jsonl"
DEAD_MFE = 0.2


def load_predictions():
    by_trade = defaultdict(dict)
    n_test = 0
    for line in open(SHADOW):
        r = json.loads(line)
        if r.get("symbol") == "TEST":
            n_test += 1
            continue
        key = (r["symbol"], r.get("entry_time") or "")
        by_trade[key][r["checkpoint"]] = r
    return by_trade, n_test


def main():
    preds, n_test = load_predictions()
    db = sqlite3.connect(DB)
    trades = {}
    for row in db.execute(
        "SELECT symbol, entry_time, direction, entry_price, quantity,"
        " realized_pnl, pnl_net, mfe_pct, exit_reason FROM trade_records"
        " WHERE exit_time IS NOT NULL"
    ):
        trades[(row[0], row[1])] = row[2:]

    print("P5 精确反事实: 30min 检查点判死 (mfe<%.1f%% 且 cur<0) -> 按检查点价出场" % DEAD_MFE)
    print("TEST 行已排除: %d" % n_test)
    hdr = ("symbol", "入场日", "cur30%", "实际毛$", "反事实毛$", "delta$", "结局")
    print("%-8s %-6s %7s %9s %10s %8s  %s" % hdr)
    hit_delta = miss_delta = 0.0
    n_hit = n_miss = 0
    dates = set()
    for (sym, etime), cps in sorted(preds.items(), key=lambda kv: kv[0][1]):
        cp30 = cps.get(30)
        if cp30 is None:
            continue
        if not (cp30["mfe_pct"] < DEAD_MFE and (cp30.get("cur_pct") or 0) < 0):
            continue
        tr = trades.get((sym, etime))
        if tr is None:
            print("%-8s %-6s  !! 未匹配 trade_records" % (sym, etime[:10]))
            continue
        direction, entry, qty, gross, net, mfe_final, exit_reason = tr
        cf_gross = cp30["cur_pct"] / 100 * entry * qty
        delta = cf_gross - gross
        dead = mfe_final < DEAD_MFE
        dates.add(etime[:10])
        if dead:
            hit_delta += delta
            n_hit += 1
        else:
            miss_delta += delta
            n_miss += 1
        print("%-8s %-6s %7.2f %9.2f %10.2f %+8.2f  %s(%s)" % (
            sym, etime[5:10], cp30["cur_pct"], gross, cf_gross, delta,
            "HIT" if dead else "MISS", exit_reason))
    print("-" * 64)
    print("HIT(真死单) %d 笔, 反事实 delta 合计 %+.2f (≈%+.2f/笔)" % (
        n_hit, hit_delta, hit_delta / max(n_hit, 1)))
    print("MISS(误杀) %d 笔, 机会成本合计 %+.2f" % (n_miss, miss_delta))
    print("P5 净 delta: %+.2f / %d 交易日 ≈ %+.2f/日" % (
        hit_delta + miss_delta, len(dates), (hit_delta + miss_delta) / max(len(dates), 1)))


if __name__ == "__main__":
    main()
