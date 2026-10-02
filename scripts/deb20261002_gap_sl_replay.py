"""隔夜跳空仓「扩大SL / 补仓」— 精确模拟 (不用持有近似) + 破帽算术

情景B 精确化: 新 SL = entry×(1−sl_pct), 用日K low 逐日判触及 (触及即按 SL 价成交,
             gap 穿破则按当日 open 成交 = gap-aware 保守口径, skill 要求)。
情景C 破帽: 现役 tier 由 equity 决定 ⇒ 算补仓后单名名义 vs 帽。
"""
import json
import sqlite3
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
FEE_RT = 0.0015
c = sqlite3.connect(f"file:{BASE}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

eq = c.execute("SELECT current_equity FROM capital_state ORDER BY rowid DESC LIMIT 1").fetchone()["current_equity"]
# tier 判定: medium = min_equity 50000 / max_equity 200000
TIER = "medium" if 50000 <= eq <= 200000 else ("large" if eq > 200000 else "inst")
CAP_PCT = {"medium": 0.08, "large": 0.20, "inst": 0.15}[TIER]
CAP = eq * CAP_PCT
print(f"═══ 破帽算术 ═══")
print(f"  净值 equity = {eq:,.2f} ⇒ tier={TIER} ⇒ max_position_pct={CAP_PCT} ⇒ 单名帽 = ${CAP:,.0f}")

rows = list(c.execute("""
    SELECT symbol, direction, entry_time, exit_time, entry_price, exit_price, quantity,
           COALESCE(pnl_net, realized_pnl) AS net, mfe_pct, mae_pct, metadata_json
    FROM trade_records WHERE exit_reason='OVERNIGHT_GAP_STOP' ORDER BY entry_time
"""))


def load_bars(sym, start):
    out = []
    for p in sorted((BASE / "data/replay_bars").glob(f"postexit_US_{sym.split('.')[-1]}_*.json")):
        try:
            for b in json.load(open(p)):
                if b[0] >= start[:10]:
                    out.append(b)   # [date, o, h, l, c]
        except Exception:
            pass
    seen, uniq = set(), []
    for b in sorted(out, key=lambda x: x[0]):
        if b[0] not in seen:
            seen.add(b[0])
            uniq.append(b)
    return uniq


print(f"\n═══ 补仓后单名名义 vs 帽 ({len(rows)} 笔) ═══")
n_break = 0
for r in rows:
    notional = r["entry_price"] * r["quantity"]
    after = notional * 2
    brk = after > CAP
    n_break += brk
    md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    oa = md.get("overnight_arm") or {}
    print(f"  {r['symbol']:<9} 原名义=${notional:>7,.0f} ({100*notional/eq:>4.2f}%净值) → "
          f"补仓后=${after:>8,.0f} ({100*after/eq:>5.2f}%) vs 帽${CAP:,.0f} ⇒ "
          f"{'🔴 破帽' if brk else '✅ 帽内'}   豁免臂={oa.get('source')} 夜{oa.get('night_n')} "
          f"水深{oa.get('underwater_pct_net')}%")
print(f"  ⇒ 破帽 {n_break}/{len(rows)} = {100*n_break/len(rows):.0f}%")

print(f"\n═══ 情景B 精确模拟: SL 放宽到 X% (gap-aware: 开盘已穿破则按 open 成交) ═══")
SL_GRID = (5.0, 7.0, 8.0, 10.0, 15.0)
recs = []
for r in rows:
    bars = load_bars(r["symbol"], r["exit_time"])
    recs.append(dict(sym=r["symbol"], ep=r["entry_price"], q=r["quantity"],
                     notional=r["entry_price"] * r["quantity"], net=r["net"],
                     bars=bars, exit_date=r["exit_time"][:10], mae=r["mae_pct"] or 0))

print(f"  实际基线 (现役SL): Σ净 = {sum(d['net'] for d in recs):+.1f}  (n={len(recs)})")
for sl in SL_GRID:
    tot, hit, cens = 0.0, 0, 0
    for d in recs:
        line = d["ep"] * (1 - sl / 100)
        if not d["bars"]:
            cens += 1
            continue
        out_px, done = None, False
        for b in d["bars"]:
            o, lo = b[1], b[3]
            if o <= line:            # 开盘已穿破 ⇒ gap-aware 按 open
                out_px = o
                break
            if lo <= line:           # 盘中触及 ⇒ 按 SL 线
                out_px = line
                break
        if out_px is None:           # 全程未触 ⇒ 持到窗末收盘 (右删失, 标 CENS)
            out_px = d["bars"][-1][4]
            cens += 1
        else:
            hit += 1
        tot += (out_px - d["ep"]) * d["q"] - d["notional"] * FEE_RT
    print(f"  SL={sl:>4.1f}%: Σ净={tot:>+9.1f}  Δvs实际={tot - sum(x['net'] for x in recs):>+9.1f}  "
          f"被扫={hit} 持到窗末={cens}")

print("\n═══ 逐笔: 现役SL 与放宽SL 的实际出场对比 ═══")
print(f"{'sym':<9} {'入场':>9} {'实际出场':>9} {'实际净':>8} {'MAE%':>6} {'SL7%线':>9} {'SL10%线':>9} "
      f"{'窗内最低':>9} 是否被放宽SL扫")
for d in recs:
    if not d["bars"]:
        print(f"{d['sym']:<9} 无 bars (9/28 出场, 缓存右端)")
        continue
    lo_min = min(b[3] for b in d["bars"])
    l7, l10 = d["ep"] * 0.93, d["ep"] * 0.90
    print(f"{d['sym']:<9} {d['ep']:>9.2f} {d['net']:>8.1f} {d['mae']:>6.2f} "
          f"{'':>9}{l7:>9.2f} {l10:>9.2f} {lo_min:>9.2f}  "
          f"{'7%被扫' if lo_min <= l7 else '7%未被扫'} / {'10%被扫' if lo_min <= l10 else '10%未被扫'}")
