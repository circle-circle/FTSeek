"""DEB-20261002 证据补测: $2500-4200 名义带来源拆解 (抬地板会杀谁)

RFC-071 把 min_notional 3000→2500 的理由 = 给"砍半单"幸存带;
但 RFC-071 变更2 自己把偏斜砍半改成了直接 SKIP ⇒ 幸存带残余用户 = 跨日翻转降仓等。
抬到 4200 前必须拆: 该带成交来自哪些路径, 各路径净贡献多少。
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " strategy_id, metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()

band = []
for r in rows:
    notional = (r["entry_price"] or 0) * (r["quantity"] or 0)
    if not (2500 <= notional < 4200):
        continue
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    did = r["decision_id"] or ""
    src = ("DA" if did.startswith("DA-") else "UE" if did.startswith("universe_expansion")
           else "reentry" if did.startswith("intraday_reentry")
           else "EXT" if did.startswith("EXT") or (r["strategy_id"] or "") == "extended_hours_entry"
           else "other")
    band.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "month": (r["entry_time"] or "")[:7], "src": src, "did": did,
        "price": r["entry_price"] or 0, "notional": notional,
        "half_flag": md.get("halved") or md.get("size_halved") or md.get("skew_halved"),
        "flip_flag": md.get("flip") or md.get("overnight_flip") or md.get("reversal"),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })

n = len(band)
print(f"=== 名义 $2500-4200 带 (抬地板 4200 会全部拒掉) ===")
print(f"  n={n} 毛={sum(x['gross'] for x in band):+.1f} 费={sum(x['fees'] for x in band):.1f} "
      f"净={sum(x['net'] for x in band):+.1f} 均={sum(x['net'] for x in band)/n:+.2f} "
      f"死率={sum(x['dead'] for x in band)/n*100:.0f}%")

print("\n=== 按来源拆 ===")
for src in ("DA", "UE", "reentry", "EXT", "other"):
    g = [x for x in band if x["src"] == src]
    if not g:
        continue
    print(f"  {src:8s} n={len(g):3d} 毛={sum(x['gross'] for x in g):+8.1f} "
          f"净={sum(x['net'] for x in g):+8.1f} 死率={sum(x['dead'] for x in g)/len(g)*100:3.0f}%")

print("\n=== 按方向拆 ===")
for d in ("LONG", "SHORT"):
    g = [x for x in band if x["dir"] == d]
    if not g:
        continue
    print(f"  {d:5s} n={len(g):3d} 毛={sum(x['gross'] for x in g):+8.1f} "
          f"净={sum(x['net'] for x in g):+8.1f} 死率={sum(x['dead'] for x in g)/len(g)*100:3.0f}%")

print("\n=== 降仓痕迹 (metadata 里 halved/flip 类标记) ===")
hf = [x for x in band if x["half_flag"] or x["flip_flag"]]
print(f"  带 halved/flip 标记: {len(hf)}/{n}")
print("  ⇒ 若为 0: 该带成交**不是**砍半/翻转降仓产物, RFC-071 幸存带理由对此带无实际用户")

print("\n=== 价格档拆 (该带内 价<$26 vs >=$26) ===")
for lab, p in (("价<$26", lambda x: x["price"] < 26), ("价>=$26", lambda x: x["price"] >= 26)):
    g = [x for x in band if p(x)]
    if not g:
        continue
    nt = sum(x["notional"] for x in g)
    print(f"  {lab:8s} n={len(g):3d} 毛={sum(x['gross'] for x in g):+8.1f} "
          f"净={sum(x['net'] for x in g):+8.1f} 费bps={sum(x['fees'] for x in g)/nt*1e4:5.1f} "
          f"死率={sum(x['dead'] for x in g)/len(g)*100:3.0f}%")
print("  ⇒ 价<$26 段费bps 更高 (费用地板支配), 抬 min_entry_price 到 26 单独就能砍掉它")

print("\n=== 逐月 (该带净额) ===")
for m in sorted({x["month"] for x in band}):
    g = [x for x in band if x["month"] == m]
    print(f"  {m}: n={len(g):3d} 净={sum(x['net'] for x in g):+8.1f}")

print("\n=== 交叉: 该带 ∩ SHORT (K3 轴关注人群) ===")
gs = [x for x in band if x["dir"] == "SHORT"]
print(f"  n={len(gs)} 净={sum(x['net'] for x in gs):+.1f}")
c.close()
