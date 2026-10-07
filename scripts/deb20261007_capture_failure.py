#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑦ 决定性切片: 捕获失败 16.6pp (摸到够付费用的利润 65.2% 但只落袋 48.6%) 的构成.
   问: 这 157 笔差额是「入场定价」问题还是「退出/持有」问题?
   判据: 若差额集中在 MFE 大但落袋小 + MAE 也大 ⇒ 退出/回吐问题;
         若差额集中在 MFE≈0 ⇒ 入场即错 (定价轴)."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

rows = c.execute("""SELECT symbol, direction d, exit_reason e, entry_price ep, exit_price xp, quantity q,
                           realized_pnl g, pnl_net n, fees_est f, mfe_pct m, mae_pct a, trade_date dt,
                           holding_hours hh, entry_session es
                    FROM trade_records
                    WHERE fees_est IS NOT NULL AND fees_est>0 AND entry_price>0 AND quantity>0
                      AND mfe_pct IS NOT NULL
                    ORDER BY id""").fetchall()
print(f"样本 n={len(rows)} (费用非空 + MFE 非空)")

fee_bps, gross_bps = [], []
fail, ok = [], []
for r in rows:
    notional = float(r["ep"]) * float(r["q"])
    if notional <= 0: continue
    fb = float(r["f"]) / notional * 10000
    gb = float(r["g"] or 0) / notional * 10000
    mb = abs(float(r["m"] or 0)) * 100          # MFE bps (mfe_pct 是百分点 ⇒ ×100=bps)
    fee_bps.append(fb); gross_bps.append(gb)
    if mb > fb:                                  # 曾摸到够付费用的利润
        (ok if gb > fb else fail).append((r, fb, gb, mb))

print(f"\n摸到过够付费用的利润 = {len(ok)+len(fail)} ({100*(len(ok)+len(fail))/len(rows):.1f}%)")
print(f"  其中落袋成功 = {len(ok)}   落袋失败 = {len(fail)}  ⇒ 捕获失败率 = {100*len(fail)/max(len(ok)+len(fail),1):.1f}%")
print(f"  落袋失败这批: 净pnl=${sum(float(x[0]['n'] or 0) for x in fail):.2f}  "
      f"毛利=${sum(float(x[0]['g'] or 0) for x in fail):.2f}  费用=${sum(float(x[0]['f'] or 0) for x in fail):.2f}")

print(f"\n=== A. 捕获失败这批的出场原因分布 (定性: 退出问题还是入场问题) ===")
by = collections.defaultdict(list)
for x in fail: by[str(x[0]["e"] or "NULL")].append(x)
print(f"    {'出场原因':26s} {'n':>4s} {'MFE中位bps':>11s} {'落袋bps':>9s} {'MAE中位':>9s} {'净额':>10s} {'持有h中位':>9s}")
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    if len(v) < 3: continue
    mf = st.median([x[3] for x in v]); gb = st.median([x[2] for x in v])
    ma = st.median([abs(float(x[0]["a"] or 0)) for x in v])
    net = sum(float(x[0]["n"] or 0) for x in v)
    hh = [float(x[0]["hh"]) for x in v if x[0]["hh"] is not None]
    print(f"    {k:26s} {len(v):4d} {mf:11.1f} {gb:9.1f} {ma:8.2f}% {net:10.2f} {st.median(hh) if hh else 0:9.2f}")

print(f"\n=== B. 捕获失败 vs 落袋成功: MFE 量级对比 (判是否「摸到很多却没守住」) ===")
for nm, grp in (("落袋成功", ok), ("落袋失败", fail)):
    if not grp: continue
    mf = sorted(x[3] for x in grp); k = len(mf)
    ma = sorted(abs(float(x[0]["a"] or 0)) for x in grp)
    gb = sorted(x[2] for x in grp)
    print(f"  {nm:8s} n={k:4d}  MFE bps: 中位={st.median(mf):7.1f} p75={mf[3*k//4]:7.1f} p90={mf[int(k*.9)]:7.1f}")
    print(f"           {'':8s}  落袋 bps: 中位={st.median(gb):7.1f}  |MAE|中位={st.median(ma):.2f}%")

print(f"\n=== C. 回吐比 = (MFE − 落袋) / MFE 的分布 (真正的「让回利润」度量) ===")
give = []
for r, fb, gb, mb in fail + ok:
    if mb > 1:                                   # MFE > 1bps 才算
        give.append(((mb - gb) / mb * 100, r, mb, gb))
give.sort(key=lambda x: -x[0])
if give:
    gv = [x[0] for x in give]
    print(f"  回吐比 n={len(gv)}: 中位={st.median(gv):.1f}% p75={sorted(gv)[3*len(gv)//4]:.1f}% p90={sorted(gv)[int(len(gv)*.9)]:.1f}%")
    print(f"  --- 回吐最狠的 12 笔 (MFE 大但落袋少) ---")
    for g_, r, mb, gb in give[:12]:
        print(f"    {r['symbol']:9s} {str(r['d'])[:9]:10s} {r['dt']} MFE={mb:7.1f}bp 落袋={gb:7.1f}bp "
              f"回吐={g_:6.1f}% 净=${float(r['n'] or 0):9.2f} 出场={str(r['e'])[:22]:24s} 持{float(r['hh'] or 0):5.1f}h")

print(f"\n=== D. 按日聚类 (防单日支配 — review-system-design 铁律: 单日>50%不得作裁决依据) ===")
byd = collections.defaultdict(lambda: [0, 0.0])
for x in fail:
    d = str(x[0]["dt"])[:10]
    byd[d][0] += 1; byd[d][1] += float(x[0]["n"] or 0)
tops = sorted(byd.items(), key=lambda kv: kv[1][1])[:8]
tot_net = sum(float(x[0]["n"] or 0) for x in fail)
print(f"  捕获失败批总净=${tot_net:.2f}  独立日数={len(byd)}")
for d, (n_, s_) in tops:
    print(f"    {d}  n={n_:3d}  净=${s_:9.2f}  ({100*s_/tot_net if tot_net else 0:5.1f}% of 总)")
print(f"  ⇒ Top-3 日占比 = {100*sum(s for _,(n_,s) in tops[:3])/tot_net if tot_net else 0:.1f}% "
      f"({'⚠ 单日支配, 不得作裁决依据' if tot_net and abs(sum(s for _,(n_,s) in tops[:3])/tot_net)>0.5 else '分散, 可作方向判读'})")
