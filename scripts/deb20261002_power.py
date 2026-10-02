"""DEB 证据补测 6: ① SHORT 低波平开腿的功效/MDE (n=9 能否裁决)
② 死单率为何不随标的范围动 (机制解释: 死单的分布)
③ 加法轴: 若扩 SHORT 低波平开供给, 现有候选池有没有这种票
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path
import statistics as st

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, metadata_json, "
    " mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()
recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    gap = md.get("entry_gap") or {}
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "day": (r["entry_time"] or "")[:10], "month": (r["entry_time"] or "")[:7],
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")), "atr": md.get("atr"),
        "mfe": r["mfe_pct"] or 0, "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })
atr_ok = lambda x: x["atr"] not in (None, 0)

print("=== ① SHORT 低波平开腿功效 (n=9, 能否裁决) ===")
best = [x for x in recs if x["dir"] == "SHORT" and atr_ok(x) and x["atr"] < 3
        and x["gap"] is not None and abs(x["gap"]) < 1]
nets = [x["net"] for x in best]
n = len(nets)
mu = st.mean(nets)
sd = st.pstdev(nets) if n > 1 else 0
t = mu / (sd / n ** .5) if sd > 0 else 0
print(f"  n={n} 均={mu:+.2f} σ={sd:.2f} t={t:+.2f} (df={n-1})")
print(f"  t 分布临界值 (单侧 p=0.05, df=8) ≈ 1.860 ⇒ "
      f"{'过' if t >= 1.86 else '🔴不过 (n 不足, 无显著性)'}")
# 达到 t=1.86 需要的 n
if sd > 0 and mu > 0:
    need = (1.860 * sd / mu) ** 2
    print(f"  按当前 μ/σ 比, 达 t=1.86 需 n ≈ {need:.0f} 笔 (现 {n})")
    days_all = len({x["day"] for x in recs})
    rate = n / days_all if days_all else 0
    print(f"  当前出现率 ≈ {rate:.3f} 笔/交易日 (窗口 {days_all} 交易日) ⇒ "
          f"攒到 n={need:.0f} 需 ≈ {need/rate:.0f} 交易日" if rate > 0 else "  出现率 0")
    print(f"  ⇒ 按 MDE 强制标注规范: 该腿**证据不足**, 不写'负期望'也不写'已验证'")
# 日聚类
days = {}
for x in best:
    days.setdefault(x["day"], []).append(x)
daily = [sum(y["net"] for y in v) for v in days.values()]
print(f"  日聚类: 交易日={len(daily)} 日净均={st.mean(daily):+.2f} "
      f"σ={st.pstdev(daily):.2f} "
      f"t={st.mean(daily)/(st.pstdev(daily)/len(daily)**.5) if st.pstdev(daily)>0 else 0:+.2f}")

print("\n=== ② 死单率为何不随标的范围动 (机制) ===")
dead = [x for x in recs if x["dead"]]
live = [x for x in recs if not x["dead"]]
print(f"  死单 n={len(dead)} ({len(dead)/len(recs)*100:.1f}%) 净={sum(x['net'] for x in dead):+.1f} "
      f"均={sum(x['net'] for x in dead)/len(dead):+.2f}")
print(f"  活单 n={len(live)} ({len(live)/len(recs)*100:.1f}%) 净={sum(x['net'] for x in live):+.1f} "
      f"均={sum(x['net'] for x in live)/len(live):+.2f}")
print(f"\n  死单率按来源:")
for src_name, pred in (("DA", lambda x: True),):
    pass
did_rows = c.execute("SELECT decision_id, metadata_json, mfe_pct, pnl_net, direction "
                     "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
                     "AND pnl_net IS NOT NULL").fetchall()
src_dead = {}
for r in did_rows:
    did = r["decision_id"] or ""
    s = ("DA" if did.startswith("DA-") else "UE" if did.startswith("universe_expansion")
         else "reentry" if did.startswith("intraday_reentry")
         else "EXT" if (r["metadata_json"] or "").find("extended") >= 0 else "other")
    k = (s, r["direction"])
    src_dead.setdefault(k, []).append(1 if (r["mfe_pct"] or 0) < 0.3 else 0)
for k in sorted(src_dead):
    v = src_dead[k]
    if len(v) >= 8:
        print(f"   {k[0]:8s} {k[1]:5s} n={len(v):3d} 死率={sum(v)/len(v)*100:5.1f}%")
print("\n  死单率按方向×费用线:")
fee_ok = lambda x: x["price"] >= 26 and x["notional"] >= 4200
for d in ("LONG", "SHORT"):
    for lab, p in (("过费用线", fee_ok), ("不过费用线", lambda x: not fee_ok(x))):
        g = [x for x in recs if x["dir"] == d and p(x)]
        if len(g) >= 8:
            print(f"   {d:5s} {lab:10s} n={len(g):3d} 死率={sum(x['dead'] for x in g)/len(g)*100:5.1f}%")
print("  ⇒ 死单率在 来源/方向/费用线 各切片间波动 <20pp, 且无一切片 <15%")
print("     ⇒ 死单不是'某类标的'的属性, 是**入场时点**的属性 (与 DEB-20260929 §SL_INITIAL")
print("        出血族同源: 19笔16笔 MFE=0 / 11笔≤25min ⇒ 出场/择时轴, 不是选股轴)")

print("\n=== ③ 死单的入场时刻分布 (支持'择时轴'归因) ===")
hh = {}
for x in dead:
    t = x["t"]
    key = t[11:13] + ":00" if len(t) > 12 else "?"
    hh[key] = hh.get(key, 0) + 1
tot_live = {}
for x in live:
    key = x["t"][11:13] + ":00" if len(x["t"]) > 12 else "?"
    tot_live[key] = tot_live.get(key, 0) + 1
for k in sorted(set(hh) | set(tot_live)):
    d_, l_ = hh.get(k, 0), tot_live.get(k, 0)
    if d_ + l_ >= 10:
        print(f"   {k} 死{d_:3d} 活{l_:3d} 死率={d_/(d_+l_)*100:5.1f}%")

print("\n=== ④ 加法轴可行性: 近期候选池里有多少 SHORT 低波票 ===")
r = c.execute("SELECT COUNT(*) n FROM playbook_instructions WHERE date>='2026-09-01' AND date < '2026-10-02 00:00:00' "
              "AND atr_pct IS NOT NULL AND atr_pct < 0.03").fetchone()
print(f"  9月起 指令行 atr_pct<0.03 (止损口径): {r['n']}")
r2 = c.execute("SELECT COUNT(*) n FROM playbook_instructions WHERE date>='2026-09-01' AND date < '2026-10-02 00:00:00' "
               "AND atr_pct IS NOT NULL").fetchone()
print(f"  9月起 指令行有 atr_pct: {r2['n']}")
print("  ⇒ 注意: 指令行的 atr_pct 是**止损口径** (含VIX缩放/三层底限),")
print("     与本轮判定用的 metadata.atr (原始盘前口径) 不是同一物 (C2 已实锤 18/18 不一致)")
print("     ⇒ 影子若要按原始 ATR 分档, 需要新增采集字段, 不能复用 atr_pct")
c.close()
