#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⑳ time_to_mfe 决定性分析: 捕获失败是「时机」还是「幅度」问题?
   若 MFE 在入场后很快达到而持有很久 ⇒ 退出时机问题 (与盘前定价/reprice 无关)
   若 MFE 迟迟不来 ⇒ 入场即错 (定价轴也救不了, C12 已裁)
   数据源: trade_records.metadata_json (子代理实测 time_to_mfe 441行 / exit_telemetry.time_to_mfe 548行)"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, collections, statistics as st
c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True); c.row_factory = sqlite3.Row

rows = c.execute("""SELECT symbol, direction d, exit_reason e, trade_date dt, pnl_net n, realized_pnl g,
                           fees_est f, mfe_pct m, mae_pct a, holding_hours hh, metadata_json mj,
                           entry_price ep, exit_price xp, quantity q
                    FROM trade_records WHERE mfe_pct IS NOT NULL AND metadata_json IS NOT NULL
                    ORDER BY id""").fetchall()
print(f"候选 n={len(rows)}")

def get_ttm(r):
    try:
        m = json.loads(r["mj"]) if isinstance(r["mj"], str) else (r["mj"] or {})
    except Exception:
        return None
    if not isinstance(m, dict): return None
    for path in (("exit_telemetry", "time_to_mfe"),):
        cur = m
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur: cur = cur[k]
            else: ok = False; break
        if ok and isinstance(cur, (int, float)): return float(cur)
    v = m.get("time_to_mfe")
    return float(v) if isinstance(v, (int, float)) else None

def get_ttm_ae(r):
    try:
        m = json.loads(r["mj"]) if isinstance(r["mj"], str) else (r["mj"] or {})
    except Exception:
        return None
    if not isinstance(m, dict): return None
    et = m.get("exit_telemetry")
    if isinstance(et, dict):
        v = et.get("time_to_mae")
        if isinstance(v, (int, float)): return float(v)
    v = m.get("time_to_mae")
    return float(v) if isinstance(v, (int, float)) else None

have = [(r, get_ttm(r), get_ttm_ae(r)) for r in rows]
witht = [(r, t, ta) for r, t, ta in have if t is not None]
print(f"有 time_to_mfe 的 n={len(witht)} ({100*len(witht)/len(rows):.1f}%)")
if witht:
    tt = sorted(x[1] for x in witht)
    k = len(tt)
    print(f"time_to_mfe 分布(分钟?): 中位={st.median(tt):.2f} p25={tt[k//4]:.2f} p75={tt[3*k//4]:.2f} p90={tt[int(k*.9)]:.2f} max={tt[-1]:.2f}")
    hh = [float(r["hh"]) for r, t, ta in witht if r["hh"] is not None]
    if hh:
        print(f"同批 holding_hours: 中位={st.median(hh):.2f}h = {st.median(hh)*60:.0f}min")
        print(f"⇒ time_to_mfe/holding 比值中位 = {st.median(tt)/(st.median(hh)*60)*100 if st.median(hh)>0 else 0:.1f}%")

print()
print("=" * 92)
print("⑳-1  捕获失败批 vs 成功批: MFE 何时达到 (判时机 vs 幅度)")
print("=" * 92)
grp = collections.defaultdict(list)
for r, t, ta in witht:
    try:
        notional = float(r["ep"]) * float(r["q"])
        if notional <= 0: continue
        fb = float(r["f"] or 0) / notional * 10000
        gb = float(r["g"] or 0) / notional * 10000
        mb = abs(float(r["m"] or 0)) * 100
    except Exception:
        continue
    if mb <= fb: continue                       # 没摸到够付费用的利润, 不属捕获问题
    grp["落袋成功" if gb > fb else "落袋失败"].append((r, t, ta, mb, gb, fb))

for nm in ("落袋成功", "落袋失败"):
    v = grp[nm]
    if not v: continue
    ts = sorted(x[1] for x in v)
    mb = sorted(x[3] for x in v)
    gb = sorted(x[4] for x in v)
    hhs = sorted(float(x[0]["hh"]) for x in v if x[0]["hh"] is not None)
    k = len(ts)
    print(f"  {nm:8s} n={k:4d}")
    print(f"    time_to_mfe: 中位={st.median(ts):7.2f}min p25={ts[k//4]:7.2f} p75={ts[3*k//4]:7.2f} p90={ts[int(k*.9)]:7.2f}")
    print(f"    MFE bps    : 中位={st.median(mb):7.1f}   落袋 bps 中位={st.median(gb):7.1f}")
    if hhs:
        print(f"    holding_h  : 中位={st.median(hhs):7.2f}h ({st.median(hhs)*60:.0f}min)")
        print(f"    ⇒ MFE 达峰时刻占持有时长 = {100*st.median(ts)/(st.median(hhs)*60) if st.median(hhs)>0 else 0:.1f}%")
    print()

print("=" * 92)
print("⑳-2  按 time_to_mfe 分桶: 早达峰的单是否更容易落袋 (若是 ⇒ 退出时机是靶点)")
print("=" * 92)
allv = grp["落袋成功"] + grp["落袋失败"]
bk = collections.defaultdict(lambda: [0, 0, []])
for x in allv:
    t = x[1]
    b = "<5min" if t < 5 else "5-15" if t < 15 else "15-30" if t < 30 else "30-60" if t < 60 else "60-120" if t < 120 else ">120min"
    bk[b][0] += 1
    is_ok = x in grp["落袋成功"]
    bk[b][1] += 1 if is_ok else 0
    bk[b][2].append(float(x[0]["n"] or 0))
print(f"    {'达峰时刻':10s} {'n':>5s} {'落袋成功率':>10s} {'净额':>10s} {'中位净':>8s}")
for b in ("<5min", "5-15", "15-30", "30-60", "60-120", ">120min"):
    if b not in bk: continue
    n_, ok_, nets = bk[b]
    print(f"    {b:10s} {n_:5d} {100*ok_/n_:9.1f}% {sum(nets):10.2f} {st.median(nets):8.2f}")
print(f"  ⇒ 若落袋成功率随达峰变晚而下降 ⇒ 「早锁」有依据 (退出轴, 非定价轴)")
print(f"     若平坦 ⇒ 达峰时刻不是判据, 捕获失败另有原因")

print()
print("=" * 92)
print("⑳-3  e_shadow_45/60/90m 快照: 捕获失败批在各时点的 MFE (判是否早该走)")
print("=" * 92)
snap = collections.defaultdict(list)
for r, t, ta in witht:
    try:
        m = json.loads(r["mj"]) if isinstance(r["mj"], str) else {}
    except Exception:
        continue
    if not isinstance(m, dict): continue
    try:
        notional = float(r["ep"]) * float(r["q"])
        fb = float(r["f"] or 0) / notional * 10000
        gb = float(r["g"] or 0) / notional * 10000
        mb = abs(float(r["m"] or 0)) * 100
    except Exception:
        continue
    if mb <= fb: continue
    lab = "成功" if gb > fb else "失败"
    for win in ("45", "60", "90"):
        s = m.get(f"e_shadow_{win}m")
        if isinstance(s, dict):
            v = s.get("mfe_pct")
            if isinstance(v, (int, float)): snap[(lab, win)].append(float(v))
print(f"    {'组':6s} {'时点':6s} {'n':>5s} {'MFE中位':>9s} {'MFE均值':>9s} {'>费用线占比':>11s}")
for lab in ("成功", "失败"):
    for win in ("45", "60", "90"):
        v = snap.get((lab, win))
        if not v or len(v) < 5: continue
        fee_line = 7.7   # 9/1起费用中位 bps ⇒ 0.077%
        over = sum(1 for x in v if abs(x) * 100 > fee_line) / len(v) * 100
        print(f"    {lab:6s} {win+'m':6s} {len(v):5d} {st.median(v):8.3f}% {st.mean(v):8.3f}% {over:10.1f}%")
print(f"  ⇒ 判读: 若失败组在 45m 时 MFE 已过费用线而最终落袋为负 ⇒ 「持有过久」是可治靶点")
print(f"     注意: 这是退出轴 (C1 冻结期), 不是盘前定价/reprice 轴")
