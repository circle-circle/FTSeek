"""第十七轮 (最终): UE·LONG 按配置期切分 — 不能拿跨期样本否定 9/29 的用户裁决

第十轮发现: da_candidate_feed_long 是 9/29 (RFC-117) 用户批准重开的,
且 RFC-117 附反事实证据 (拦 UBER/RIVN −$419, 误杀 0)。
我的 UE·LONG 样本 (n=51 净−841) 横跨三个配置期 ⇒ 必须分期,
只有 P3 (9/29 起) 的读数才能对当前配置发言。
另: P2 (8/3-9/28, LONG 腿已关) 仍有 UE·LONG 成交 ⇒ 通道口径需查清。
"""
import json
import sqlite3

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{BASE}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " strategy_id, metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL AND decision_id LIKE 'universe_expansion%'").fetchall()

recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "did": r["decision_id"] or "", "sid": r["strategy_id"] or "",
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "atr": md.get("atr"), "gap": (md.get("entry_gap") or {}).get("aligned_gap_pct"),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })


def A(g):
    n = len(g)
    if not n:
        return None
    net = sum(x["net"] for x in g)
    gross = sum(x["gross"] for x in g)
    fees = sum(x["fees"] for x in g)
    nt = sum(x["notional"] for x in g)
    return dict(n=n, net=net, gross=gross, fees=fees, avg=net / n,
                wr=sum(1 for x in g if x["net"] > 0) / n * 100,
                dead=sum(x["dead"] for x in g) / n * 100,
                fb=fees / nt * 1e4 if nt else 0)


print("=== UE 成交按配置期 × 方向 ===")
P = [("P1 <8/3 (LONG双向开·旧)", lambda t: t < "2026-08-03"),
     ("P2 8/3-9/28 (只取SHORT)", lambda t: "2026-08-03" <= t < "2026-09-29"),
     ("P3 >=9/29 (RFC-117重开LONG)", lambda t: t >= "2026-09-29")]
for plab, pp in P:
    for d in ("LONG", "SHORT"):
        sub = [x for x in recs if pp(x["t"]) and x["dir"] == d]
        if not sub:
            print(f"  {plab:30s} {d:5s} n=0")
            continue
        a = A(sub)
        print(f"  {plab:30s} {d:5s} n={a['n']:3d} 毛={a['gross']:+8.1f} 费={a['fees']:6.1f} "
              f"净={a['net']:+8.1f} 均={a['avg']:+7.2f} WR={a['wr']:3.0f}% "
              f"死率={a['dead']:3.0f}% 费bps={a['fb']:5.1f}")

print("\n=== P2 期间 UE·LONG 成交的通道真相 (LONG 腿已关却仍有成交?) ===")
p2l = [x for x in recs if "2026-08-03" <= x["t"] < "2026-09-29" and x["dir"] == "LONG"]
print(f"  n={len(p2l)}")
for x in sorted(p2l, key=lambda z: z["t"]):
    print(f"   {x['t'][:16]} {x['sym']:10s} did={x['did'][:44]:44s} "
          f"sid={x['sid'][:20]:20s} 净={x['net']:+8.1f}")
sids = {}
for x in p2l:
    sids[x["sid"]] = sids.get(x["sid"], 0) + 1
print(f"  strategy_id 分布: {sids}")
print("  ⇒ 若 sid 非 UE 专属 (如 playbook_sim/da), 说明这些 LONG 是 DA 侧消化 UE 候选后产出,")
print("     即 da_candidate_feed_long 关的是'UE直写LONG', 不是'UE候选进DA后被DA选成LONG'")

print("\n=== P3 (当前配置) UE·LONG 逐笔 — 只有这段能对现在发言 ===")
p3l = [x for x in recs if x["t"] >= "2026-09-29" and x["dir"] == "LONG"]
if p3l:
    a = A(p3l)
    print(f"  n={a['n']} 毛={a['gross']:+.1f} 净={a['net']:+.1f} 均={a['avg']:+.2f} "
          f"WR={a['wr']:.0f}% 死率={a['dead']:.0f}%")
    for x in sorted(p3l, key=lambda z: z["net"]):
        print(f"   {x['t'][:16]} {x['sym']:10s} 价={x['price']:8.2f} 名义={x['notional']:8.0f} "
              f"atr={(x['atr'] if x['atr'] is not None else -1):6.2f} 净={x['net']:+8.1f} 死={x['dead']}")
else:
    print("  n=0 ⇒ P3 期 UE·LONG 尚无已平仓成交 ⇒ **无法用数据否定 9/29 裁决**")
    print("     (9/29-10/1 只有 3 个交易日, 且 UE·LONG 需 DA 侧选中才成交)")

print("\n=== UE·LONG 全期按'是否过费用线'切 (费用线是否解释其亏损) ===")
lg = [x for x in recs if x["dir"] == "LONG"]
for lab, pred in (("过费用线(价>=26&名义>=4.2k)", lambda x: x["price"] >= 26 and x["notional"] >= 4200),
                  ("不过费用线", lambda x: not (x["price"] >= 26 and x["notional"] >= 4200))):
    sub = [x for x in lg if pred(x)]
    if not sub:
        continue
    a = A(sub)
    print(f"  {lab:28s} n={a['n']:3d} 毛={a['gross']:+8.1f} 费={a['fees']:6.1f} "
          f"净={a['net']:+8.1f} 均={a['avg']:+7.2f} 死率={a['dead']:3.0f}% 费bps={a['fb']:5.1f}")
print("  ⇒ 若 UE·LONG 亏损主要落在'不过费用线'侧, 则费用线一刀已覆盖大部分,")
print("     不必单独砍 UE·LONG (避免与 9/29 裁决正面冲突)")

print("\n=== UE·LONG 过费用线子集的双窗/逐月 (决定它是否真有独立毒性) ===")
lgo = [x for x in lg if x["price"] >= 26 and x["notional"] >= 4200]
if len(lgo) >= 6:
    for lab, p in (("IS <8/20", lambda x: x["t"] < "2026-08-20"),
                   ("OOS >=8/20", lambda x: x["t"] >= "2026-08-20")):
        s2 = [x for x in lgo if p(x)]
        if len(s2) >= 3:
            a = A(s2)
            print(f"  {lab:12s} n={a['n']:3d} 毛={a['gross']:+8.1f} 净={a['net']:+8.1f} "
                  f"均={a['avg']:+7.2f} 死率={a['dead']:3.0f}%")
    ms = []
    for m in sorted({x["t"][:7] for x in lgo}):
        mm = [x for x in lgo if x["t"][:7] == m]
        if len(mm) >= 2:
            ms.append(f"{m[5:]}:n{len(mm)}/{A(mm)['net']:+.0f}")
    print(f"  逐月: {' '.join(ms)}")

print("\n=== 对照: DA·LONG 过费用线 (同条件下 DA 是否更好) ===")
da_rows = c.execute(
    "SELECT symbol, entry_time, entry_price, quantity, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND exit_time IS NOT NULL "
    "AND pnl_net IS NOT NULL AND direction='LONG' AND decision_id LIKE 'DA-%'").fetchall()
dal = [{"price": r["entry_price"] or 0, "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0, "net": r["pnl_net"] or 0,
        "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0, "t": r["entry_time"] or ""}
       for r in da_rows]
dao = [x for x in dal if x["price"] >= 26 and x["notional"] >= 4200]
if dao:
    a = A(dao)
    print(f"  DA·LONG 过费用线: n={a['n']:3d} 毛={a['gross']:+8.1f} 净={a['net']:+8.1f} "
          f"均={a['avg']:+7.2f} 死率={a['dead']:3.0f}%")
    print(f"  UE·LONG 过费用线: n={len(lgo):3d} "
          f"毛={sum(x['gross'] for x in lgo):+8.1f} 净={sum(x['net'] for x in lgo):+8.1f} "
          f"均={sum(x['net'] for x in lgo)/len(lgo):+7.2f} "
          f"死率={sum(x['dead'] for x in lgo)/len(lgo)*100:3.0f}%")
    print("  ⇒ 同费用线条件下比较, 才能判断 UE·LONG 是否有**独立**毒性 (而非只是低名义票)")
c.close()
