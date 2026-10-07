#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v3: 量纲修正 + 用 playbook_instructions 的 band 列做定价/reprice 辩论核心取数.
   量纲钉死(程序化验证): pnl_pct=小数比例(300/300反算,反例0); mfe_pct/mae_pct=百分点.
   ⇒ 捕获率 = pnl_pct*100 / mfe_pct."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, collections, statistics as st
DB = f"{_ROOT}/data/ftseek_us.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c.row_factory = sqlite3.Row

print("=" * 82)
print("A. band 列可得性 (entry_low/entry_high/anchor_dev_pct = 定价辩论直接靶点)")
print("=" * 82)
n = c.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
for col in ("entry_low", "entry_high", "anchor_dev_pct", "execution_price", "entry_price",
            "max_favorable_excursion", "max_adverse_excursion", "atr_pct", "vol_bucket",
            "triggered", "executed", "invalidation"):
    fill = c.execute(f"SELECT COUNT({col}) FROM playbook_instructions").fetchone()[0]
    nz = c.execute(f"SELECT COUNT(*) FROM playbook_instructions WHERE {col} IS NOT NULL AND CAST({col} AS REAL)!=0").fetchone()[0]
    print(f"  {col:26s} 非NULL={fill:5d} ({100*fill/max(n,1):5.1f}%)  非零={nz:5d} ({100*nz/max(n,1):5.1f}%)")

print()
print("=" * 82)
print("B. 入场带几何实证: band 半宽是否符合 3%/5% 扁平规格 (核生产实值, 不读代码默认)")
print("=" * 82)
rows = c.execute("""SELECT direction d, entry_price e, entry_low lo, entry_high hi, atr_pct, vol_bucket, date
                    FROM playbook_instructions
                    WHERE entry_low>0 AND entry_high>0 AND entry_price>0 ORDER BY id DESC LIMIT 800""").fetchall()
print(f"  样本 n={len(rows)}")
kn = []; ch = []; bydir = collections.defaultdict(lambda: ([], []))
byvol = collections.defaultdict(lambda: ([], []))
for r in rows:
    e = float(r["e"]); lo = float(r["lo"]); hi = float(r["hi"])
    if e <= 0:
        continue
    down = (e - lo) / e * 100; up = (hi - e) / e * 100
    d = str(r["d"] or "?").upper()
    is_short = d.startswith(("S",)) or "SHORT" in d
    k, cc = (up, down) if is_short else (down, up)   # knife=逆势沿, chase=顺势沿
    kn.append(k); ch.append(cc)
    bydir["SHORT" if is_short else "LONG"][0].append(k); bydir["SHORT" if is_short else "LONG"][1].append(cc)
    vb = str(r["vol_bucket"] or "NULL")
    byvol[vb][0].append(k); byvol[vb][1].append(cc)
def stat(v):
    if not v: return "n=0"
    s = sorted(v)
    return f"中位={st.median(s):.2f}% p25={s[len(s)//4]:.2f}% p75={s[3*len(s)//4]:.2f}% max={s[-1]:.2f}%"
print(f"  knife(逆势沿) 全部: {stat(kn)}")
print(f"  chase (顺势沿) 全部: {stat(ch)}")
for k, (kk, cc) in bydir.items():
    print(f"    {k:6s} n={len(kk):4d}  knife: {stat(kk)}")
    print(f"           {'':6s}  chase: {stat(cc)}")
print(f"  --- 按 vol_bucket (查 vol_widen 影子是否已在数据里留痕) ---")
for k, (kk, cc) in sorted(byvol.items(), key=lambda kv: -len(kv[1][0]))[:8]:
    print(f"    {k:12s} n={len(kk):4d} knife中位={st.median(kk):.2f}% chase中位={st.median(cc):.2f}%")

print()
print("=" * 82)
print("C. 成交率 = band 是否被盘中价格触及 (triggered/executed 链路)")
print("=" * 82)
tot = c.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
tr = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered=1").fetchone()[0]
ex = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE executed=1").fetchone()[0]
print(f"  指令总={tot}  triggered={tr} ({100*tr/tot:.1f}%)  executed={ex} ({100*ex/tot:.1f}%)")
print(f"  ⇒ 触发→执行转化 = {100*ex/max(tr,1):.1f}%   未执行损耗 = {tr-ex} 笔 ({100*(tr-ex)/max(tr,1):.1f}% of triggered)")
print(f"\n  --- 近30日逐日成交率 (看是否系统性偏低) ---")
rr = c.execute("""SELECT date, COUNT(*) tot, SUM(CASE WHEN triggered=1 THEN 1 ELSE 0 END) tr,
                         SUM(CASE WHEN executed=1 THEN 1 ELSE 0 END) ex
                  FROM playbook_instructions GROUP BY date ORDER BY date DESC LIMIT 30""").fetchall()
print(f"    {'日期':12s} {'指令':>5s} {'触发':>5s} {'执行':>5s} {'触发率':>7s} {'执/触':>7s}")
for r in rr:
    t, a, b = r["tot"], r["tr"] or 0, r["ex"] or 0
    print(f"    {str(r['date']):12s} {t:5d} {a:5d} {b:5d} {100*a/max(t,1):6.1f}% {100*b/max(a,1):6.1f}%")

print()
print("=" * 82)
print("D. 未成交的原因分布 (定价过窄? 还是信号未触发?)")
print("=" * 82)
rr = c.execute("""SELECT skip_reason s, COUNT(*) n FROM playbook_instructions
                  WHERE (triggered IS NULL OR triggered=0) AND skip_reason IS NOT NULL AND skip_reason!=''
                  GROUP BY skip_reason ORDER BY n DESC LIMIT 18""").fetchall()
un = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered IS NULL OR triggered=0").fetchone()[0]
print(f"  未触发总={un}")
for r in rr:
    print(f"    {str(r['s'])[:56]:58s} {r['n']:5d} ({100*r['n']/max(un,1):5.1f}%)")
# 已触发但未执行 (真正的定价/成交损耗)
rr2 = c.execute("""SELECT skip_reason s, COUNT(*) n FROM playbook_instructions
                   WHERE triggered=1 AND (executed IS NULL OR executed=0)
                   GROUP BY skip_reason ORDER BY n DESC LIMIT 12""").fetchall()
tn = c.execute("SELECT COUNT(*) FROM playbook_instructions WHERE triggered=1 AND (executed IS NULL OR executed=0)").fetchone()[0]
print(f"\n  --- 已触发但未执行 n={tn} (这才是定价/reprice 的真损耗面) ---")
for r in rr2:
    print(f"    {str(r['s'])[:56]:58s} {r['n']:5d} ({100*r['n']/max(tn,1):5.1f}%)")

print()
print("=" * 82)
print("E. reprice 实证: entry_price(盘前) vs execution_price(实际成交) — 列级真值")
print("=" * 82)
rows = c.execute("""SELECT symbol, direction d, entry_price e, execution_price x, entry_low lo, entry_high hi,
                           anchor_dev_pct ad, date, pnl, pnl_pct, skip_reason, metadata_json
                    FROM playbook_instructions
                    WHERE executed=1 AND entry_price>0 AND execution_price>0 ORDER BY id DESC LIMIT 900""").fetchall()
print(f"  已成交且有双价 n={len(rows)}")
if rows:
    dv = []; byd = collections.defaultdict(list); beyond = 0; out_band = []
    for r in rows:
        e = float(r["e"]); x = float(r["x"])
        d = (x - e) / e * 100
        dv.append(d); byd[str(r["d"] or "?").upper()[:5]].append(d)
        lo = float(r["lo"] or 0); hi = float(r["hi"] or 0)
        if lo > 0 and hi > 0 and (x < lo or x > hi):
            out_band.append((r, d, lo, hi))
    a = sorted(abs(v) for v in dv); k = len(a)
    print(f"  |reprice偏移| 中位={st.median(a):.3f}% p75={a[3*k//4]:.3f}% p90={a[int(k*.9)]:.3f}% p99={a[int(k*.99)]:.3f}% max={a[-1]:.3f}%")
    print(f"  超 band 沿: >5%(chase)={sum(1 for v in dv if abs(v)>5)/k*100:.1f}%  >3%(knife)={sum(1 for v in dv if abs(v)>3)/k*100:.1f}%")
    for kk, vv in sorted(byd.items()):
        av = sorted(abs(x) for x in vv)
        print(f"    {kk:6s} n={len(vv):4d} 中位|偏移|={st.median(av):.3f}% 带符号均值={st.mean(vv):+.3f}% max={av[-1]:.3f}%")
    print(f"\n  ⚠ 成交价落在 band 之外的笔数 = {len(out_band)} / {len(rows)} ({100*len(out_band)/len(rows):.1f}%)")
    for r, d, lo, hi in out_band[:8]:
        print(f"    {r['symbol']:9s} {str(r['d'])[:5]:6s} {r['date']} entry={r['e']} exec={r['x']} band=[{lo:.2f},{hi:.2f}] 偏移={d:+.2f}% pnl={r['pnl']}")
    # anchor_dev_pct 分布 (锚偏离 = 定价质量的直接度量)
    ad = [float(r["ad"]) for r in rows if r["ad"] is not None]
    if ad:
        s = sorted(abs(x) for x in ad); m = len(s)
        print(f"\n  anchor_dev_pct n={len(ad)}: 中位|dev|={st.median(s):.3f} p90={s[int(m*.9)]:.3f} max={s[-1]:.3f}")

print()
print("=" * 82)
print("F. MFE/MAE 捕获率 (量纲修正版: pnl_pct*100 / mfe_pct) — 按阶段×方向×出场拆")
print("=" * 82)
allr = c.execute("""SELECT symbol, direction, exit_reason, realized_pnl, pnl_net, pnl_pct,
                           mfe_pct, mae_pct, fees_est, trade_date, entry_session
                    FROM trade_records WHERE mfe_pct IS NOT NULL ORDER BY id""").fetchall()
print(f"  n={len(allr)} (全量)")
def cap(r):
    try:
        m = abs(float(r["mfe_pct"])); p = float(r["pnl_pct"] or 0) * 100   # ← 量纲对齐
        return p / m if m > 1e-9 else None
    except Exception:
        return None
def blk(nm, v, indent="    "):
    if not v: return
    cc = [x for x in (cap(r) for r in v) if x is not None]
    mf = [abs(float(r["mfe_pct"])) for r in v]
    ma = [abs(float(r["mae_pct"])) for r in v if r["mae_pct"] is not None and float(r["mae_pct"]) != 0]
    net = [float(r["pnl_net"]) for r in v if r["pnl_net"] is not None]
    fee = sum(float(r["fees_est"] or 0) for r in v)
    gross = sum(float(r["realized_pnl"] or 0) for r in v)
    wr = 100 * sum(1 for x in net if x > 0) / len(net) if net else 0
    print(f"{indent}{nm:24s} n={len(v):4d} 捕获中位={st.median(cc)*100 if cc else 0:6.1f}% MFE中位={st.median(mf):5.2f}% "
          f"MAE中位={st.median(ma) if ma else 0:5.2f}%")
    print(f"{indent}{'':24s}  净额=${sum(net):9.2f} 毛利=${gross:9.2f} 费用=${fee:8.2f} WR={wr:5.1f}% 中位净=${st.median(net) if net else 0:7.2f}")
blk("【全部】", allr)
print(f"\n  --- 按入场时段 (盘前定价最相关) ---")
by = collections.defaultdict(list)
for r in allr: by[str(r["entry_session"] or "NULL")].append(r)
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])): blk(k, v)
print(f"\n  --- 按方向 ---")
by = collections.defaultdict(list)
for r in allr: by[str(r["direction"] or "NULL")].append(r)
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])): blk(k, v)
print(f"\n  --- 按出场原因 (n>=10) ---")
by = collections.defaultdict(list)
for r in allr: by[str(r["exit_reason"] or "NULL")].append(r)
for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
    if len(v) >= 10: blk(k, v)

print()
print("=" * 82)
print("G. MFE 分桶: 摸到利润 vs 落袋 (量纲修正) + 费用占比")
print("=" * 82)
print(f"    {'MFE桶':12s} {'n':>5s} {'捕获中位':>9s} {'净额':>10s} {'中位净':>9s} {'费用/|毛利|':>11s}")
for lo, hi, nm in [(0, 0.25, "MFE<0.25%"), (0.25, 0.5, "0.25-0.5%"), (0.5, 1, "0.5-1%"), (1, 2, "1-2%"),
                   (2, 3, "2-3%"), (3, 5, "3-5%"), (5, 1e9, ">5%")]:
    v = [r for r in allr if lo <= abs(float(r["mfe_pct"])) < hi]
    if not v: continue
    cc = [x for x in (cap(r) for r in v) if x is not None]
    net = [float(r["pnl_net"]) for r in v if r["pnl_net"] is not None]
    g = sum(abs(float(r["realized_pnl"] or 0)) for r in v); f = sum(float(r["fees_est"] or 0) for r in v)
    print(f"    {nm:12s} {len(v):5d} {st.median(cc)*100 if cc else 0:8.1f}% {sum(net):10.2f} "
          f"{st.median(net) if net else 0:9.2f} {f/g if g>0 else 0:10.2f}x")

print()
print("=" * 82)
print("H. 完整性 integrity")
print("=" * 82)
d1 = c.execute("SELECT MIN(trade_date) a,MAX(trade_date) b,COUNT(DISTINCT trade_date) d,COUNT(*) n FROM trade_records").fetchone()
print(f"  trade_records: {d1['a']}~{d1['b']} 交易日={d1['d']} 行={d1['n']} (全量无截断)")
d2 = c.execute("SELECT MIN(date) a,MAX(date) b,COUNT(DISTINCT date) d,COUNT(*) n FROM playbook_instructions").fetchone()
print(f"  playbook_instructions: {d2['a']}~{d2['b']} 日期={d2['d']} 行={d2['n']}")
print(f"  pnl_net 非NULL={c.execute('SELECT COUNT(pnl_net) FROM trade_records').fetchone()[0]}/{d1['n']}")
_sess_miss = c.execute("SELECT SUM(CASE WHEN entry_session IS NULL THEN 1 ELSE 0 END) FROM trade_records").fetchone()[0]
print(f"  ⚠ entry_session 缺失={_sess_miss}/{d1['n']} ⇒ 时段分片有 NULL 池")
