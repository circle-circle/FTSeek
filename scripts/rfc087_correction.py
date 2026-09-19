"""RFC-087 更正取证: 旧(截断污染)产物 vs 新(修复后)逐笔对照."""
import json

OLD = "data/reports/replay_ext_entry_sources_latest_premarket_fixed.json"
NEW = "data/reports/replay_ext_entry_sources_20260919_premarket_fixed.json"
TRUNC = {"2026-06-30", "2026-07-13", "2026-07-27", "2026-08-10", "2026-08-21", "2026-09-03", "2026-09-16"}

o = json.load(open(OLD))
n = json.load(open(NEW))
oi = {(r["day"], r["symbol"]): r for r in o["trades"]}
ni = {(r["day"], r["symbol"]): r for r in n["trades"]}
print("old n", len(oi), "new n", len(ni), "同一(日,标的)集:", set(oi) == set(ni))

print("\n[臂均值] 旧 → 新 (net_pct, 同一批笔)")
for tag in ("rule", "pre_close", "rth_close", "intraday_sl", "sl_only"):
    ov = [r[tag]["net_pct"] for r in o["trades"] if tag in r and "net_pct" in r[tag]]
    nv = [r[tag]["net_pct"] for r in n["trades"] if tag in r and "net_pct" in r[tag]]
    print(f"  {tag:<12} 旧 n={len(ov)} 均{sum(ov)/len(ov):+.3f} 最差{min(ov):+.3f}"
          f"   新 n={len(nv)} 均{sum(nv)/len(nv):+.3f} 最差{min(nv):+.3f}")

print("\n[截断日 9 笔: 旧值全臂同数 → 新值分叉]")
for k in sorted(oi):
    if k[0] in TRUNC:
        r0, r1 = oi[k], ni.get(k)
        print(f"  {k[0]} {k[1]:<9} 旧: pre={r0.get('pre_close',{}).get('net_pct'):>7} "
              f"rth={r0.get('rth_close',{}).get('net_pct'):>7} isl={r0.get('intraday_sl',{}).get('net_pct'):>7}"
              f"  ||  新: pre={r1.get('pre_close',{}).get('net_pct'):>7} "
              f"rth={(r1.get('rth_close') or {}).get('net_pct'):>7} isl={(r1.get('intraday_sl') or {}).get('net_pct'):>7}"
              f" a_cond={(r1.get('a_cond') or {}).get('net_pct'):>7}")

print("\n['被割在负而当日收盘转正' 笔数] (pre_close 净<0 且 rth_close 毛>0)")
for lbl, d in (("旧", o), ("新", n)):
    cnt = tot = 0
    for r in d["trades"]:
        pc, rc = (r.get("pre_close") or {}), (r.get("rth_close") or {})
        if "net_pct" in pc:
            tot += 1
            if pc["net_pct"] < 0 and (rc.get("ret_pct") or 0) > 0:
                cnt += 1
    print(f"  {lbl}: {cnt}/{tot}")

print("\n[9 笔被割反转的合计差 (旧口径)]")
for lbl, d in (("旧", o), ("新", n)):
    s = 0.0
    for r in d["trades"]:
        pc, rc = (r.get("pre_close") or {}), (r.get("rth_close") or {})
        if pc.get("net_pct", 0) < 0 and (rc.get("ret_pct") or 0) > 0:
            s += (rc["ret_pct"] - pc["ret_pct"])
    print(f"  {lbl}: 合计 +{s:.2f}pp")

print("\n[A 条件收口 vs 现役收口: 分歧笔 (a_cond 与 pre_close 净额不同的笔)]")
g = []
for k in sorted(ni):
    r = ni[k]
    a, p = (r.get("a_cond") or {}), (r.get("pre_close") or {})
    if a and p and abs(a["net_pct"] - p["net_pct"]) > 0.001:
        g.append((k[0], k[1], r.get("flatten_read_pnl_pct"), r.get("flatten_armed"),
                  p["net_pct"], a["net_pct"], (r.get("rth_close") or {}).get("net_pct"),
                  (r.get("intraday_sl") or {}).get("net_pct")))
print(f"  分歧 {len(g)}/{len(ni)} 笔 (其余=同判):")
for row in g:
    print(f"    {row[0]} {row[1]:<9} 0920读数{row[2]:>7}% 武装={row[3]} : 收口{row[4]:>7} → A{row[5]:>7} "
          f"(收盘{row[6]:>7} 盘中SL{row[7]:>7})")