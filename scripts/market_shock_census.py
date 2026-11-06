#!/usr/bin/env python3
"""市场冲击日 × 方向 × 通道 的净额普查 (DEB-20261106 证据脚本; 只读).

回答三件事, 全部按 A10 口径 (2026-07-01 起 + data_quality∈(clean,real) + 剔 POSITION_SYNC/MANUAL):
  1. 按 SPY **当日收盘**涨跌分桶, LONG/SHORT 的笔数与净额 —— 即 DEB-20260924 已关闭的
     「盘前/日级普跌」轴的 A10 期复核 (只看形状, 不做判决)。
  2. 冲击日 (SPY 日跌 ≤ -1.5%) 逐笔, 含出场原因 —— 用于拆「入场时点」与「隔夜缺口」的贡献。
  3. 通道 × 是否冲击日 —— 用于检查通道间风险覆盖是否对称。

零副作用: 只读 DB + 只读日K缓存; 不写库/不改配置/无 cron。
用法: cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python3.11 scripts/market_shock_census.py
"""
import json
import os
import sqlite3
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ftseek_us.db")
KLINE = os.path.join(ROOT, "data", "cache", "daily_kline_v1.json")
ERA = ("AND trade_date >= '2026-07-01' AND COALESCE(data_quality,'') IN ('clean','real') "
       "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')")
SHOCK = -1.5


def spy_changes():
    bars = json.load(open(KLINE))["symbols"]["US.SPY"]["bars"]
    out = {}
    for i in range(1, len(bars)):
        out[bars[i]["time"][:10]] = (bars[i]["close"] / bars[i - 1]["close"] - 1) * 100
    return out


def bucket(x):
    if x <= -3.0:
        return "A <=-3%"
    if x <= -1.5:
        return "B -3~-1.5%"
    if x <= -0.5:
        return "C -1.5~-0.5%"
    if x < 0.5:
        return "D -0.5~+0.5%"
    if x < 1.5:
        return "E +0.5~+1.5%"
    return "F >+1.5%"


def main():
    chg = spy_changes()
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT trade_date, symbol, direction, owner_strategy, pnl_net, entry_vix, "
        "exit_reason FROM trade_records WHERE exit_price>0 " + ERA)]
    print("样本 n=%d  %s..%s  (A10 口径: 2026-07-01 起, clean/real, 剔 POSITION_SYNC/MANUAL)" %
          (len(rows), min(r["trade_date"] for r in rows), max(r["trade_date"] for r in rows)))

    agg = defaultdict(lambda: [0, 0.0])
    for r in rows:
        s = chg.get(r["trade_date"][:10])
        if s is None:
            continue
        k = (bucket(s), r["direction"])
        agg[k][0] += 1
        agg[k][1] += r["pnl_net"] or 0
    print("\n== 按 SPY 当日涨跌 × 方向 (净=pnl_net)")
    for k in sorted(agg):
        n, net = agg[k]
        print("  %-14s %-11s n=%3d net=%9.2f avg=%8.2f" % (k[0], k[1], n, net, net / n))

    shock = [r for r in rows if (chg.get(r["trade_date"][:10]) or 0) <= SHOCK]
    print("\n== 冲击日 (SPY 日跌 <= %.1f%%) 逐笔 n=%d" % (SHOCK, len(shock)))
    for r in sorted(shock, key=lambda x: x["trade_date"]):
        print("  %s %-9s %-6s %-22s net=%9.2f exit=%s" % (
            r["trade_date"][:10], r["symbol"], r["direction"], r["owner_strategy"],
            r["pnl_net"] or 0, r["exit_reason"]))
    gap = sum(r["pnl_net"] or 0 for r in shock if (r["exit_reason"] or "") == "OVERNIGHT_GAP_STOP")
    tot = sum(r["pnl_net"] or 0 for r in shock)
    print("  小计 net=%.2f, 其中 OVERNIGHT_GAP_STOP=%.2f (%.0f%%)" % (tot, gap, 100 * gap / tot if tot else 0))

    ch = defaultdict(lambda: [0, 0.0, 0, 0.0])
    for r in rows:
        s = chg.get(r["trade_date"][:10])
        if s is None:
            continue
        k = r["owner_strategy"] or "?"
        if s <= SHOCK:
            ch[k][2] += 1
            ch[k][3] += r["pnl_net"] or 0
        else:
            ch[k][0] += 1
            ch[k][1] += r["pnl_net"] or 0
    print("\n== 通道 × 是否冲击日")
    for k, v in sorted(ch.items(), key=lambda kv: kv[1][3]):
        print("  %-24s 平常 n=%3d net=%9.2f | 冲击日 n=%2d net=%9.2f" % (k, v[0], v[1], v[2], v[3]))


if __name__ == "__main__":
    main()