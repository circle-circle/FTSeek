"""DEB 证据补测 4: ① K1 单独及其两成分的双窗稳健性 (K4 已撤, K1+K2+K3 IS反号)
② low_atr skip 8 行的**意图方向** (direction 列全为 'SKIP' ⇒ ③ 结论无证据, 需另查)
③ EXT 通道最低价硬闸缺失确认 (CTNT $0.07 实弹成交)
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path
import statistics as st

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

print("=== ② low_atr skip 行的意图方向 (direction 列='SKIP' ⇒ 查 raw_instruction_json) ===")
for r in c.execute("SELECT date, symbol, direction, atr_pct, raw_instruction_json, skip_reason "
                   "FROM playbook_instructions WHERE skip_reason LIKE '%low_atr%'"):
    ri = r["raw_instruction_json"]
    intent_dir = None
    try:
        d = json.loads(ri) if ri else {}
        intent_dir = d.get("direction") or d.get("action") or d.get("signal")
    except Exception:
        pass
    print(f"   {r['date']} {r['symbol']:9s} direction列={r['direction']!r} "
          f"atr_pct={r['atr_pct']} 意图方向={intent_dir!r} | {str(r['skip_reason'])[:46]}")
dirs = set()
for r in c.execute("SELECT raw_instruction_json FROM playbook_instructions "
                   "WHERE skip_reason LIKE '%low_atr%'"):
    try:
        d = json.loads(r["raw_instruction_json"]) if r["raw_instruction_json"] else {}
        dirs.add(str(d.get("direction") or d.get("action") or d.get("signal")))
    except Exception:
        dirs.add("(unparsable)")
print(f"  意图方向集合: {dirs}")
print(f"  ⇒ C1 (门加 LONG-only) "
      f"{'零行为变更 (历史无 SHORT 被拦)' if not (dirs & {'SHORT','SELL_SHORT','short'}) else '🔴 行为变更: 历史有 SHORT 被拦过'}")

print("\n=== ③ EXT 通道最低价硬闸: 实弹反证 ===")
for r in c.execute("SELECT symbol, entry_time, entry_price, quantity, source_type, "
                   "strategy_id, decision_id, pnl_net FROM trade_records "
                   "WHERE entry_price < 5 AND entry_time>='2026-08-01' AND entry_time<'2026-10-02 00:00:00' "
                   "AND exit_time IS NOT NULL"):
    px = r["entry_price"]
    qty = r["quantity"] or 0
    print(f"   {r['entry_time'][:16]} {r['symbol']:9s} 价={(px if px is not None else -1):6.2f} "
          f"qty={qty:5d} 名义={(px or 0)*qty:7.0f} "
          f"st={r['source_type']} sid={r['strategy_id']} 净={(r['pnl_net'] or 0):+8.1f}")
print("   ⇒ 价<$5 成交存在 ⇒ EXT 通道不受 admission.min_entry_price=5 约束")
r = c.execute("SELECT COUNT(*) n, SUM(pnl_net) s FROM trade_records WHERE entry_price<5 "
              "AND entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL").fetchone()
print(f"   全窗口 价<$5 成交: n={r['n']} 净={r['s'] or 0:+.1f}")
r2 = c.execute("SELECT COUNT(*) n FROM trade_records WHERE entry_price<5 "
               "AND entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
               "AND strategy_id='extended_hours_entry'").fetchone()
print(f"   其中走 EXT (strategy_id=extended_hours_entry): n={r2['n']}")

print("\n=== ① K1 单独 + 两成分: 双窗 / 逐月 / 剔尾 ===")
rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, metadata_json, "
    " mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()
recs = []
for r in rows:
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "day": (r["entry_time"] or "")[:10], "month": (r["entry_time"] or "")[:7],
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
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
                gb=gross / nt * 1e4 if nt else 0, fb=fees / nt * 1e4 if nt else 0)


def rob(g):
    syms = {}
    for x in g:
        syms[x["sym"]] = syms.get(x["sym"], 0) + x["net"]
    rk = sorted(syms.items(), key=lambda z: z[1])
    tot = sum(x["net"] for x in g)
    return tot - sum(v for _, v in rk[:3]), tot - sum(v for _, v in rk[-3:])


CANDS = {
    "K1 全 (价>=26 且 名义>=4200)": lambda x: x["price"] >= 26 and x["notional"] >= 4200,
    "K1a 仅价>=26": lambda x: x["price"] >= 26,
    "K1b 仅名义>=4200": lambda x: x["notional"] >= 4200,
    "K1c 价>=5 且名义>=2500 (现役)": lambda x: x["price"] >= 5 and x["notional"] >= 2500,
    "K1d 价>=26 且名义>=2500": lambda x: x["price"] >= 26 and x["notional"] >= 2500,
    "K1e 价>=5 且名义>=4200": lambda x: x["price"] >= 5 and x["notional"] >= 4200,
}
base = A(recs)
print(f"  基线 n={base['n']} 净={base['net']:+.1f} 均={base['avg']:+.2f} 死率={base['dead']:.1f}% "
      f"费bps={base['fb']:.1f}")
print(f"\n  {'候选':30s} {'n':>4s} {'净':>8s} {'均':>7s} {'死率':>5s} {'费bps':>6s} "
      f"{'ISΔ均':>7s} {'OOSΔ均':>7s} {'剔3亏':>8s} {'剔3赚':>8s} {'改善月':>7s} 判定")
for name, p in CANDS.items():
    g = [x for x in recs if p(x)]
    a = A(g)
    d3w, d3b = rob(g)
    ia = oa = None
    for lab, pp in (("IS", lambda x: x["t"] < "2026-08-20"), ("OOS", lambda x: x["t"] >= "2026-08-20")):
        sg = [x for x in g if pp(x)]
        bg = [x for x in recs if pp(x)]
        if len(sg) >= 5:
            d = A(sg)["avg"] - A(bg)["avg"]
            if lab == "IS":
                ia = d
            else:
                oa = d
    better = tot = 0
    for m in sorted({x["month"] for x in g}):
        mm = [x for x in g if x["month"] == m]
        bb = [x for x in recs if x["month"] == m]
        if len(mm) >= 3:
            tot += 1
            better += A(mm)["net"] > A(bb)["net"]
    if ia is None or oa is None:
        v = "⚠️单窗"
    elif ia > 0 and oa > 0:
        v = "✅双窗改善"
    elif (ia > 0) != (oa > 0):
        v = "🔴双窗反号"
    else:
        v = "🔴双窗皆劣"
    tail = "剔尾稳✅" if d3w > 0 and d3b > 0 else ("剔3赚翻负🔴" if d3b <= 0 else "剔3亏翻负🔴")
    print(f"  {name:30s} {a['n']:4d} {a['net']:+8.1f} {a['avg']:+7.2f} {a['dead']:4.1f}% "
          f"{a['fb']:6.1f} {(ia if ia is not None else 0):+7.2f} "
          f"{(oa if oa is not None else 0):+7.2f} {d3w:+8.1f} {d3b:+8.1f} "
          f"{better}/{tot}  {v} {tail}")

print("\n=== K1 全 的当前时代 (9/1起) 读数 ===")
cur = [x for x in recs if x["t"] >= "2026-09-01"]
for name, p in CANDS.items():
    g = [x for x in cur if p(x)]
    if not g:
        continue
    a = A(g)
    print(f"  {name:30s} n={a['n']:3d}/{len(cur)} 净={a['net']:+8.1f} 均={a['avg']:+6.2f} "
          f"费bps={a['fb']:5.1f} 死率={a['dead']:3.0f}%")

print("\n=== 被 K1 拒的人群画像 (给影子追加腿写阈值) ===")
rej = [x for x in recs if not (x["price"] >= 26 and x["notional"] >= 4200)]
a = A(rej)
print(f"  n={a['n']} 毛={a['gross']:+.1f} 费={a['fees']:.1f} 净={a['net']:+.1f} "
      f"费bps={a['fb']:.1f} 死率={a['dead']:.0f}%")
for lab, pp in (("价<$26", lambda x: x["price"] < 26),
                ("价$5-26", lambda x: 5 <= x["price"] < 26),
                ("价<$5", lambda x: x["price"] < 5),
                ("价>=$26名义<$4.2k", lambda x: x["price"] >= 26 and x["notional"] < 4200)):
    g = [x for x in rej if pp(x)]
    if g:
        ag = A(g)
        print(f"   {lab:20s} n={ag['n']:3d} 毛={ag['gross']:+8.1f} 净={ag['net']:+8.1f} "
              f"费bps={ag['fb']:5.1f} 死率={ag['dead']:3.0f}%")
c.close()
