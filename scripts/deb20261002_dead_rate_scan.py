"""第十五轮 (最终定位): LONG 侧"入场即错"由什么驱动? (占 84% 流量)

第十四轮负面结果: 推荐范围死单率 26.8% → 26.0% (仅 0.7pp), 但净额 −1494 → +1794
  ⇒ 那些刀降的是**费用**, 不是**入场即错**
  ⇒ SHORT 侧 gap<1% 死率 28%→10% (真降), LONG 侧几乎不动
本轮: 把 LONG 死单率作为目标变量, 扫所有入场时可知的轴, 找真驱动
      (死单率 = COALESCE(mfe_pct,0)<0.3 的占比)
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path
import statistics as st

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " strategy_id, metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()

recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    gap = md.get("entry_gap") or {}
    did = r["decision_id"] or ""
    src = ("DA" if did.startswith("DA-") else "UE" if did.startswith("universe_expansion")
           else "reentry" if did.startswith("intraday_reentry")
           else "EXT" if did.startswith("EXT") or (r["strategy_id"] or "") == "extended_hours_entry"
           else "other")
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "day": (r["entry_time"] or "")[:10], "month": (r["entry_time"] or "")[:7],
        "hour": int((r["entry_time"] or "")[11:13]) if len(r["entry_time"] or "") > 12 else None,
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0), "src": src,
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")), "atr": md.get("atr"),
        "rvol": md.get("rvol"), "conf": md.get("conf"), "mfe": r["mfe_pct"] or 0,
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })

L = [x for x in recs if x["dir"] == "LONG"]
S = [x for x in recs if x["dir"] == "SHORT"]
print(f"=== 基线死单率 ===")
print(f"  LONG: n={len(L)} 死单={sum(x['dead'] for x in L)} ({sum(x['dead'] for x in L)/len(L)*100:.1f}%)")
print(f"  SHORT: n={len(S)} 死单={sum(x['dead'] for x in S)} ({sum(x['dead'] for x in S)/len(S)*100:.1f}%)")
print(f"  合计: n={len(recs)} 死单率={sum(x['dead'] for x in recs)/len(recs)*100:.1f}%")


def scan(g, keyfn, lab, min_n=12):
    """返回各桶死单率, 标注最优/最差"""
    buckets = {}
    for x in g:
        k = keyfn(x)
        if k is None:
            continue
        buckets.setdefault(k, []).append(x)
    out = []
    for k, v in sorted(buckets.items(), key=lambda z: str(z[0])):
        if len(v) < min_n:
            continue
        dr = sum(x["dead"] for x in v) / len(v) * 100
        out.append((k, len(v), dr, sum(x["net"] for x in v)))
    return out


print("\n=== LONG 侧死单率扫描 (目标变量 = 死单率) ===")
print(f"  {'轴/桶':30s} {'n':>4s} {'死率':>6s} {'净':>9s}")
AXES = [
    ("入场时段", lambda x: ("09:30-10:00" if x["hour"] in (9, 10) and x["t"][11:16] < "10:00"
                            else "10:00-12:00" if x["hour"] and x["hour"] < 12
                            else "12:00-15:00" if x["hour"] and x["hour"] < 15
                            else "15:00-16:00" if x["hour"] == 15
                            else "盘前/盘后" if x["hour"] is not None else None)),
    ("来源", lambda x: x["src"]),
    ("|gap|", lambda x: ("<1%" if abs(x["gap"]) < 1 else "1-3%" if abs(x["gap"]) < 3
                         else "3-8%" if abs(x["gap"]) < 8 else ">=8%") if x["gap"] is not None else None),
    ("gap方向", lambda x: ("跳空高开>1%" if x["gap"] and x["gap"] > 1
                          else "跳空低开<-1%" if x["gap"] and x["gap"] < -1
                          else "平开|gap|<=1%") if x["gap"] is not None else None),
    ("atr(百分数)", lambda x: ("<3%" if x["atr"] < 3 else "3-8%" if x["atr"] < 8
                              else "8-15%" if x["atr"] < 15 else ">=15%")
     if x["atr"] not in (None, 0) else None),
    ("rvol", lambda x: ("<1" if x["rvol"] < 1 else "1-2" if x["rvol"] < 2
                        else "2-4" if x["rvol"] < 4 else ">=4") if x["rvol"] else None),
    ("conf", lambda x: ("<55" if x["conf"] < 55 else "55-65" if x["conf"] < 65
                        else "65-75" if x["conf"] < 75 else ">=75") if x["conf"] else None),
    ("价格档", lambda x: ("<$10" if x["price"] < 10 else "$10-26" if x["price"] < 26
                          else "$26-60" if x["price"] < 60 else "$60-150" if x["price"] < 150
                          else "$150-400" if x["price"] < 400 else ">=$400")),
    ("名义档", lambda x: ("<$2.5k" if x["notional"] < 2500 else "$2.5-4.2k" if x["notional"] < 4200
                          else "$4.2-6k" if x["notional"] < 6000 else ">=$6k")),
]
best_ax = None
for name, fn in AXES:
    res = scan(L, fn, name)
    if not res:
        print(f"  {name:30s}  (无足量桶)")
        continue
    drs = [r[2] for r in res]
    spread = max(drs) - min(drs)
    print(f"  ── {name} (死率极差 {spread:.0f}pp) {'🔥强轴' if spread >= 15 else ''}")
    for k, n, dr, net in res:
        flag = "✅低" if dr == min(drs) else "🔴高" if dr == max(drs) else "  "
        print(f"     {str(k):26s} {n:4d} {dr:5.1f}% {flag} {net:+9.1f}")
    if spread >= 15 and (best_ax is None or spread > best_ax[1]):
        best_ax = (name, spread)

print("\n=== SHORT 侧同轴扫描 (对比: 哪条轴在 SHORT 侧降死率) ===")
for name, fn in (("​|gap|", lambda x: ("<1%" if abs(x["gap"]) < 1 else "1-3%" if abs(x["gap"]) < 3
                                        else ">=3%") if x["gap"] is not None else None),
                 ("atr(百分数)", lambda x: ("<3%" if x["atr"] < 3 else "3-8%" if x["atr"] < 8 else ">=8%")
                  if x["atr"] not in (None, 0) else None),
                 ("来源", lambda x: x["src"])):
    res = scan(S, fn, name, min_n=6)
    if not res:
        continue
    print(f"  ── {name.strip()}")
    for k, n, dr, net in res:
        print(f"     {str(k):26s} {n:4d} {dr:5.1f}% {net:+9.1f}")

print("\n=== 决定性: 死单率 vs 净额 是否是两件事 ===")
print("  第十四轮: 推荐范围 死率 26.8%→26.0% (−0.7pp) 但净额 −1494→+1794")
fee_ok = lambda x: x["price"] >= 26 and x["notional"] >= 4200
FINAL = lambda x: (fee_ok(x) and not (x["src"] == "UE" and x["dir"] == "LONG")
                   and not (x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] >= 8)
                   and not (x["dir"] == "SHORT" and x["gap"] is not None and abs(x["gap"]) >= 8)
                   and not (x["dir"] == "LONG" and x["gap"] is not None and abs(x["gap"]) >= 3)
                   and not (x["dir"] == "LONG" and x["atr"] not in (None, 0) and x["atr"] >= 20))
for lab, g in (("基线全量", recs), ("推荐范围", [x for x in recs if FINAL(x)]),
               ("仅费用线", [x for x in recs if fee_ok(x)]),
               ("费用线+砍UE·LONG", [x for x in recs if fee_ok(x)
                                     and not (x["src"] == "UE" and x["dir"] == "LONG")]),
               ("费用线+SHORT低波(全)", [x for x in recs if fee_ok(x)
                                        and (x["dir"] == "LONG"
                                             or (x["atr"] not in (None, 0) and x["atr"] < 8))])):
    if not g:
        continue
    n = len(g)
    dr = sum(x["dead"] for x in g) / n * 100
    net = sum(x["net"] for x in g)
    gross = sum(x["gross"] for x in g)
    fees = sum(x["fees"] for x in g)
    nt = sum(x["notional"] for x in g)
    dead_net = sum(x["net"] for x in g if x["dead"])
    print(f"  {lab:22s} n={n:3d} 死率={dr:5.1f}% 毛={gross:+8.1f} 费={fees:7.1f} "
          f"净={net:+8.1f} 费bps={fees/nt*1e4 if nt else 0:5.1f} | 死单净贡献={dead_net:+8.1f}")
    print(f"       ⇒ 死单占净额比 {abs(dead_net)/abs(net)*100 if net else 0:5.0f}% "
          f"| 活单净贡献={net-dead_net:+8.1f}")

print("\n=== 死单本身亏多少 (决定'降死率'值不值) ===")
dead = [x for x in recs if x["dead"]]
live = [x for x in recs if not x["dead"]]
print(f"  死单 n={len(dead)} 净={sum(x['net'] for x in dead):+.1f} "
      f"均={sum(x['net'] for x in dead)/len(dead):+.2f} "
      f"毛={sum(x['gross'] for x in dead):+.1f} 费={sum(x['fees'] for x in dead):.1f}")
print(f"  活单 n={len(live)} 净={sum(x['net'] for x in live):+.1f} "
      f"均={sum(x['net'] for x in live)/len(live):+.2f} "
      f"毛={sum(x['gross'] for x in live):+.1f} 费={sum(x['fees'] for x in live):.1f}")
print(f"  ⇒ 死单毛额={sum(x['gross'] for x in dead):+.1f} "
      f"({'毛也为负=真入场错' if sum(x['gross'] for x in dead)<0 else '毛为正=费用问题'})")
print(f"  ⇒ 若能把死单全消除: 净改善 {abs(sum(x['net'] for x in dead)):+.1f} "
      f"(但会连带砍掉其中 {sum(1 for x in dead if x['net']>0)} 笔正净单 "
      f"{sum(x['net'] for x in dead if x['net']>0):+.1f})")
c.close()
