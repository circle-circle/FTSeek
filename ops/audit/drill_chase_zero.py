# -*- coding: utf-8 -*-
"""Q4 深挖: 追价≈0 桶 (落库entry≈DA planned entry) 为何最差 — 组成分解.
假设: (a) 真'D案式入场'差 → 冲击D案; (b) 组成伪影 (方向/日期/单笔支配/无确认位直入单).
"""
import argparse
import json
import sqlite3
import statistics as st

ap = argparse.ArgumentParser(description="DEB-20260916 §七 追价≈0 桶下钻 (record-only)")
ap.add_argument("--since", default="2026-08-01", help="指令窗口起点 (默认 2026-08-01)")
args = ap.parse_args()
SINCE = args.since

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

rows = db.execute(
    """SELECT i.date, i.symbol, i.direction, i.entry_price, i.stop_loss,
              i.raw_instruction_json, t.pnl_net, t.exit_reason, t.mfe_pct, t.entry_time
       FROM playbook_instructions i
       JOIN trade_records t ON t.decision_id = i.decision_id AND t.source_type='PLAYBOOK_SIM'
       WHERE i.date >= ? AND i.direction IN ('BUY','SELL_SHORT')
         AND i.entry_price > 0 AND t.pnl_net IS NOT NULL""", (SINCE,)).fetchall()

zero, nonzero = [], []
for r in rows:
    try:
        raw = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    pe = raw.get("planned_entry")
    if not (isinstance(pe, (int, float)) and pe > 0):
        continue
    chase = abs(r["entry_price"] / pe - 1) * 100
    rec = {"date": r["date"], "sym": r["symbol"].split(".")[-1], "dir": r["direction"][:4],
           "pnl": r["pnl_net"], "exit": r["exit_reason"],
           "mfe0": (r["mfe_pct"] is not None and abs(r["mfe_pct"]) < 1e-9),
           "arch": raw.get("entry_archetype"),
           "conf": bool((raw.get("confirmation") or {}).get("price_level")),
           "chase": chase, "pe": pe, "landed": r["entry_price"],
           "etime": str(r["entry_time"] or "")[:16]}
    (zero if chase < 0.2 else nonzero).append(rec)

print(f"追价≈0 桶 n={len(zero)} 净{sum(x['pnl'] for x in zero):+.0f}")
print("\n明细 (按pnl排序):")
for x in sorted(zero, key=lambda y: y["pnl"]):
    print(f"  {x['date'][5:]} {x['sym']:6s} {x['dir']} arch={str(x['arch'])[:14]:14s} conf位={x['conf']} "
          f"plan={x['pe']} 落={x['landed']} 入场时刻={x['etime'][11:]} pnl={x['pnl']:+8.1f} {x['exit']:18s} mfe0={x['mfe0']}")

print("\n分解:")
print(f"  LONG/SHORT: L={sum(1 for x in zero if x['dir']=='BUY')} S={sum(1 for x in zero if x['dir']=='SELL')}")
print(f"  带确认位(可路由): {sum(1 for x in zero if x['conf'])} | 无确认位(市价直入): {sum(1 for x in zero if not x['conf'])}")
no_conf = [x for x in zero if not x["conf"]]
with_conf = [x for x in zero if x["conf"]]
if no_conf:
    print(f"  无确认位子组: n={len(no_conf)} 净{sum(x['pnl'] for x in no_conf):+.0f} "
          f"mfe0率={sum(1 for x in no_conf if x['mfe0'])/len(no_conf)*100:.0f}%")
if with_conf:
    print(f"  带确认位子组: n={len(with_conf)} 净{sum(x['pnl'] for x in with_conf):+.0f} "
          f"mfe0率={sum(1 for x in with_conf if x['mfe0'])/len(with_conf)*100:.0f}%")
worst = sorted(zero, key=lambda y: y["pnl"])[:3]
rest = [x["pnl"] for x in sorted(zero, key=lambda y: y["pnl"])[3:]]
print(f"  最差3笔 {[round(x['pnl']) for x in worst]} 合计{sum(x['pnl'] for x in worst):+.0f} | "
      f"其余{len(rest)}笔合计{sum(rest):+.0f}")
by_day = {}
for x in zero:
    by_day.setdefault(x["date"], []).append(x["pnl"])
print(f"  按日: {[(d[5:], len(v), round(sum(v))) for d, v in sorted(by_day.items())]}")
# 入场时刻分布 (盘前/开盘即入 = 无耐心等确认?)
early = [x for x in zero if x["etime"][11:16] and x["etime"][11:16] < "09:45"]
print(f"  09:45前入场: {len(early)}/{len(zero)} 净{sum(x['pnl'] for x in early):+.0f}")
