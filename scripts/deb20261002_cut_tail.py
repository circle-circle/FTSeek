"""DEB 证据补测 5: 剔尾判据修正 — 对"被砍人群"算, 不是对保留侧算

自查发现的方法错误:
  第十八轮 rob() 用在**保留侧** ⇒ "剔最赚3票翻负" 是保留侧的正常现象 (留下的票当然靠头部赚),
  不构成否决。对一把刀的正确检验 = **被砍人群**剔掉最亏3票后是否仍为负
  (若翻正 ⇒ 该刀只被少数极端亏损票支撑 = 尾部主导, 不该砍)。
本轮对 K1/K1a/K1d/K2/K3 及最终组合逐刀按正确口径重算。
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path

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
        "month": (r["entry_time"] or "")[:7], "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")), "atr": md.get("atr"),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })

atr_ok = lambda x: x["atr"] not in (None, 0)


def cut_tail(g):
    """被砍人群按 symbol 聚合, 剔最亏3票 / 最赚3票"""
    syms = {}
    for x in g:
        syms[x["sym"]] = syms.get(x["sym"], 0) + x["net"]
    rk = sorted(syms.items(), key=lambda z: z[1])
    tot = sum(x["net"] for x in g)
    return {
        "n": len(g), "net": tot,
        "drop3worst": tot - sum(v for _, v in rk[:3]),
        "drop3best": tot - sum(v for _, v in rk[-3:]),
        "worst3": [(s, round(v, 1)) for s, v in rk[:3]],
        "nsym": len(rk),
        "win": sum(1 for x in g if x["net"] > 0),
        "killrate": sum(1 for x in g if x["net"] > 0) / len(g) * 100 if g else 0,
    }


CUTS = {
    "K1 全拒 (价<26 或 名义<4200)": lambda x: not (x["price"] >= 26 and x["notional"] >= 4200),
    "K1a 拒 (价<26)": lambda x: x["price"] < 26,
    "K1d 拒 (价<26, 名义闸保持2500)": lambda x: x["price"] < 26 and x["notional"] >= 2500,
    "K1e 拒 (名义<4200, 价闸保持5)": lambda x: x["notional"] < 4200 and x["price"] >= 5,
    "K2 拒 (SHORT |gap|>=8%)": lambda x: x["dir"] == "SHORT" and x["gap"] is not None and abs(x["gap"]) >= 8,
    "K3 拒 (SHORT 原始ATR>=8%)": lambda x: x["dir"] == "SHORT" and atr_ok(x) and x["atr"] >= 8,
    "K4 拒 (LONG |gap|3-8%) [已撤]": lambda x: x["dir"] == "LONG" and x["gap"] is not None and 3 <= abs(x["gap"]) < 8,
}
print("=== 各刀「被砍人群」的尾部主导检验 (正确口径) ===")
print(f"  {'刀':34s} {'n':>4s} {'标的':>4s} {'净':>9s} {'剔最亏3票':>10s} {'剔最赚3票':>10s} "
      f"{'误杀率':>6s} 判定")
for name, p in CUTS.items():
    g = [x for x in recs if p(x)]
    if not g:
        continue
    t = cut_tail(g)
    if t["drop3worst"] < 0:
        v = "✅剔尾仍负(该砍)"
    else:
        v = "🔴剔3亏翻正(尾部主导)"
    kill = "🔴>40%" if t["killrate"] > 40 else "✅<=40%"
    print(f"  {name:34s} {t['n']:4d} {t['nsym']:4d} {t['net']:+9.1f} "
          f"{t['drop3worst']:+10.1f} {t['drop3best']:+10.1f} "
          f"{t['killrate']:5.0f}%{kill} {v}")
    print(f"      最亏3票: {t['worst3']}")

print("\n=== 最终组合 K1+K2+K3 的被砍人群 ===")
FINAL3 = lambda x: (x["price"] >= 26 and x["notional"] >= 4200
                    and not (x["dir"] == "SHORT" and x["gap"] is not None and abs(x["gap"]) >= 8)
                    and not (x["dir"] == "SHORT" and atr_ok(x) and x["atr"] >= 8))
cut = [x for x in recs if not FINAL3(x)]
t = cut_tail(cut)
print(f"  n={t['n']} 标的={t['nsym']} 净={t['net']:+.1f} 剔最亏3票={t['drop3worst']:+.1f} "
      f"剔最赚3票={t['drop3best']:+.1f} 误杀率={t['killrate']:.0f}% "
      f"{'🔴>40%' if t['killrate'] > 40 else '✅<=40%'}")
print(f"  最亏3票: {t['worst3']}")
print(f"  ⇒ 误杀率 {t['killrate']:.0f}% {'超过' if t['killrate'] > 40 else '未超'} 40% 门 "
      f"(extreme_gap_ban_shadow 同口径判据: 被拦笔盈利占比 ≤40%)")

print("\n=== 双窗同号的唯一候选: K1a / K1c / K1d 的被砍人群 ===")
for name, p in (("K1a 拒 (价<26)", lambda x: x["price"] < 26),
                ("K1c 拒 (价<5 或 名义<2500)", lambda x: not (x["price"] >= 5 and x["notional"] >= 2500)),
                ("K1d 拒 (价<26 或 名义<2500)", lambda x: not (x["price"] >= 26 and x["notional"] >= 2500))):
    g = [x for x in recs if p(x)]
    if not g:
        continue
    t = cut_tail(g)
    print(f"  {name:32s} n={t['n']:4d} 净={t['net']:+9.1f} 剔最亏3票={t['drop3worst']:+9.1f} "
          f"误杀率={t['killrate']:3.0f}% {'✅' if t['drop3worst']<0 else '🔴'}")
    print(f"      最亏3票: {t['worst3']}")
    for lab, pp in (("IS ", lambda x: x["t"] < "2026-08-20"), ("OOS", lambda x: x["t"] >= "2026-08-20")):
        sg = [x for x in g if pp(x)]
        if sg:
            ts = cut_tail(sg)
            print(f"      {lab}: n={ts['n']:3d} 净={ts['net']:+8.1f} 剔3亏={ts['drop3worst']:+8.1f}")

print("\n=== K1a (价<$26) 被砍人群的费bps 与死率 — 机制诊断 ===")
g = [x for x in recs if x["price"] < 26]
nt = sum(x["notional"] for x in g)
print(f"  n={len(g)} 毛={sum(x['gross'] for x in g):+.1f} 费={sum(x['fees'] for x in g):.1f} "
      f"净={sum(x['net'] for x in g):+.1f} 费bps={sum(x['fees'] for x in g)/nt*1e4:.1f} "
      f"死率={sum(x['dead'] for x in g)/len(g)*100:.0f}%")
print(f"  ⇒ 毛额 {sum(x['gross'] for x in g):+.1f} "
      f"({'正=费用问题' if sum(x['gross'] for x in g)>0 else '负=入场问题'}), "
      f"费 {sum(x['fees'] for x in g):.1f} 是净亏的主因")
for lab, pp in (("<$5", lambda x: x["price"] < 5), ("$5-10", lambda x: 5 <= x["price"] < 10),
                ("$10-26", lambda x: 10 <= x["price"] < 26)):
    s2 = [x for x in g if pp(x)]
    if not s2:
        continue
    nt2 = sum(x["notional"] for x in s2)
    print(f"   {lab:8s} n={len(s2):3d} 毛={sum(x['gross'] for x in s2):+8.1f} "
          f"费={sum(x['fees'] for x in s2):6.1f} 净={sum(x['net'] for x in s2):+8.1f} "
          f"费bps={sum(x['fees'] for x in s2)/nt2*1e4 if nt2 else 0:6.1f}")
c.close()
