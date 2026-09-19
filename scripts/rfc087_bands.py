"""RFC-087 分档表 (修复后样本): 按 09:25 收口时读数分档, 逐臂均值/笔数."""
import json

d = json.load(open("data/reports/replay_ext_entry_sources_20260919_premarket_fixed.json"))
BANDS = (("盈利 >0", 0.0, 1e9), ("浅亏 -3~0", -3.0, 0.0), ("中亏 -6~-3", -6.0, -3.0), ("深亏 <=-6", -1e9, -6.0))
ARMS = ("pre_close", "a_cond", "b_band", "intraday_sl", "rth_close", "rule")
rows = {}
for r in d["trades"]:
    pc = (r.get("pre_close") or {}).get("ret_pct")
    if pc is None:
        continue
    for name, lo, hi in BANDS:
        if lo < pc <= hi or (name == "盈利 >0" and pc > 0) or (name == "深亏 <=-6" and pc <= -6.0):
            rows.setdefault(name, []).append(r)
            break
print(f"{'档':<12}{'n':>4} " + " ".join(f"{a:>11}" for a in ARMS))
for name, lo, hi in BANDS:
    rs = rows.get(name, [])
    if not rs:
        continue
    cells = []
    for a in ARMS:
        v = [r[a]["net_pct"] for r in rs if a in r and "net_pct" in r[a]]
        cells.append(f"{(sum(v)/len(v)):>+11.3f}" if v else f"{'—':>11}")
    print(f"{name:<12}{len(rs):>4} " + " ".join(cells))
print("\n极值 (修复后):")
for a in ARMS:
    v = sorted(r[a]["net_pct"] for r in d["trades"] if a in r and "net_pct" in r[a])
    if v:
        print(f"  {a:<12} n={len(v)} 最差 {v[0]:+.3f} 次差 {v[1]:+.3f} 最好 {v[-1]:+.3f}")
print("\n0920 读数 vs 0925 读数 分档一致性 (a_cond 触发条件 = 0920>0 或已武装):")
arm = sum(1 for r in d["trades"] if r.get("flatten_armed"))
prof = sum(1 for r in d["trades"] if (r.get("flatten_read_pnl_pct") or 0) > 0)
print(f"  0920 有浮盈 {prof}/{len(d['trades'])}; 已武装 {arm}/{len(d['trades'])}; "
      f"两者皆无 (=会被豁免) {sum(1 for r in d['trades'] if (r.get('flatten_read_pnl_pct') or 0) <= 0 and not r.get('flatten_armed'))}")