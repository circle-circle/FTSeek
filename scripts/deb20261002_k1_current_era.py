"""DEB 证据补测 2: K1 在**当前时代** (9月起) 的真实拦截面
7月占了 $2500-4200 带的 121/138 ⇒ 仓位时代已漂移, 必须按近期流量估 go-forward 影响。
"""
import json
import sqlite3

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{BASE}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-09-01' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()

recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })

n = len(recs)
print(f"=== 9/1 起已平仓 n={n} ===")
fee_fail = [x for x in recs if not (x["price"] >= 26 and x["notional"] >= 4200)]
price_fail = [x for x in recs if x["price"] < 26]
notional_fail = [x for x in recs if x["price"] >= 26 and x["notional"] < 4200]
print(f"  不过费用线: {len(fee_fail)}/{n} ({len(fee_fail)/n*100:.0f}%)")
print(f"    其中 价<$26: {len(price_fail)} | 价>=$26但名义<$4.2k: {len(notional_fail)}")
for lab, g in (("价<$26", price_fail), ("名义<$4.2k", notional_fail), ("合计不过线", fee_fail)):
    if g:
        print(f"    {lab:12s} n={len(g):3d} 毛={sum(x['gross'] for x in g):+8.1f} "
              f"费={sum(x['fees'] for x in g):6.1f} 净={sum(x['net'] for x in g):+8.1f} "
              f"死率={sum(x['dead'] for x in g)/len(g)*100:3.0f}%")
        for x in sorted(g, key=lambda z: z["net"])[:8]:
            print(f"       {x['t'][:16]} {x['sym']:10s} {x['dir']:5s} 价={x['price']:7.2f} "
                  f"名义={x['notional']:7.0f} 净={x['net']:+8.1f}")
keep = [x for x in recs if x not in fee_fail]
print(f"\n  过线保留: n={len(keep)} 毛={sum(x['gross'] for x in keep):+.1f} "
      f"净={sum(x['net'] for x in keep):+.1f}")
print(f"  全量:     毛={sum(x['gross'] for x in recs):+.1f} 净={sum(x['net'] for x in recs):+.1f}")

print("\n=== 9月起名义额分布 (仓位时代现状) ===")
nots = sorted(x["notional"] for x in recs)
print(f"  n={len(nots)} min={nots[0]:.0f} p10={nots[len(nots)//10]:.0f} "
      f"p25={nots[len(nots)//4]:.0f} 中位={nots[len(nots)//2]:.0f} p75={nots[3*len(nots)//4]:.0f} "
      f"max={nots[-1]:.0f}")
below = [x for x in nots if x < 4200]
print(f"  名义<$4200: {len(below)}/{len(nots)} ({len(below)/len(nots)*100:.0f}%)")

print("\n=== 9月起价格分布 ===")
prices = sorted(x["price"] for x in recs)
print(f"  min={prices[0]:.2f} p10={prices[len(prices)//10]:.2f} 中位={prices[len(prices)//2]:.2f} "
      f"max={prices[-1]:.2f}")
print(f"  价<$26: {sum(1 for p in prices if p < 26)}/{len(prices)} "
      f"({sum(1 for p in prices if p<26)/len(prices)*100:.0f}%)")
c.close()
