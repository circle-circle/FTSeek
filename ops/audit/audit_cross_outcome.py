# -*- coding: utf-8 -*-
"""关键裁决证据: "落库SL跨过DA失效线" 组 vs 未跨组 的结局差异 (已平仓子集).

这是决定要不要给 re-price 加"几何守卫"的核心实证 — 若跨线组不更差, 则守卫无 PnL 依据。
"""
import json
import sqlite3
import statistics as st

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

rows = db.execute(
    """SELECT i.date, i.symbol, i.direction, i.entry_price, i.stop_loss,
              i.raw_instruction_json, t.pnl_net, t.exit_reason, t.mfe_pct
       FROM playbook_instructions i
       JOIN trade_records t ON t.decision_id = i.decision_id
            AND t.source_type = 'PLAYBOOK_SIM'
       WHERE i.date >= '2026-08-15' AND i.direction IN ('BUY','SELL_SHORT')
         AND i.entry_price > 0 AND t.pnl_net IS NOT NULL""").fetchall()

crossed, clean = [], []
for r in rows:
    try:
        raw = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    pe, ps = raw.get("planned_entry"), raw.get("planned_stop_loss")
    db_e, db_s = r["entry_price"], r["stop_loss"]
    if not all(isinstance(x, (int, float)) and x > 0 for x in (pe, ps, db_e, db_s)):
        continue
    short = r["direction"] == "SELL_SHORT"
    price_invs = [t.get("value") for t in (raw.get("invalidation_triggers") or [])
                  if isinstance(t.get("value"), (int, float)) and t.get("value") > 0
                  and 0.3 * pe <= t.get("value") <= 5.0 * pe]
    if not price_invs:
        continue
    hit = False
    for iv in price_invs:
        if short and ps >= iv > db_s:
            hit = True; break
        if (not short) and ps <= iv < db_s:
            hit = True; break
    rec = {"sym": r["symbol"].split(".")[-1], "date": r["date"][5:],
           "pnl": r["pnl_net"], "exit": r["exit_reason"],
           "mfe0": (r["mfe_pct"] is not None and abs(r["mfe_pct"]) < 1e-9)}
    (crossed if hit else clean).append(rec)


def grp(name, g):
    if not g:
        print(f"{name}: n=0")
        return
    pnls = [x["pnl"] for x in g]
    sl_init = sum(1 for x in g if x["exit"] == "SL_INITIAL")
    mfe0 = sum(1 for x in g if x["mfe0"])
    print(f"{name}: n={len(g)} 净{sum(pnls):+.0f} 均{st.mean(pnls):+.1f} "
          f"中位{st.median(pnls):+.1f} WR={sum(1 for x in pnls if x>0)/len(pnls)*100:.0f}% | "
          f"SL_INITIAL {sl_init}({sl_init/len(g)*100:.0f}%) mfe=0 {mfe0}({mfe0/len(g)*100:.0f}%)")


print("=" * 78)
print("落库SL跨DA失效线 vs 未跨 — 已平仓结局对比 (2026-08-15 起)")
print("=" * 78)
grp("跨线组  ", crossed)
grp("未跨组  ", clean)
print()
print("跨线组明细 (看是亏还是赚):")
for x in sorted(crossed, key=lambda y: y["pnl"]):
    print(f"  {x['date']} {x['sym']:6s} pnl={x['pnl']:+8.1f} {x['exit']:20s} mfe0={x['mfe0']}")
