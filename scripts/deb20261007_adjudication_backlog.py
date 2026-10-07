#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""㉑ 复算子代理声称的「已远超门槛但未裁决」影子 (禁采信自报, 逐条实读).
   若属实 ⇒ 瓶颈不是定价轴, 而是退出轴影子攒够了没人裁 = 裁决欠账."""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import json, os, sqlite3
from pathlib import Path
import sys
sys.path.insert(0, _ROOT)   # 复查者无需设 PYTHONPATH
from app.playbook.shadow_registry import REGISTRY, ShadowStatus, _count_samples

BASE = Path("/opt/ftseek/FTSeek-US")
TERMINAL = ("KILLED", "SUPERSEDED", "GRADUATED")

print("=" * 100)
print("㉑-1  全部 OBSERVING 影子: 口径化样本数 vs min_n (实读, 不采信自报)")
print("=" * 100)
rows = []
for s in REGISTRY:
    if s.status.name in TERMINAL:
        continue
    try:
        n = _count_samples(s, BASE)
    except Exception as e:
        n = f"ERR:{type(e).__name__}"
    rows.append((s.id, s.min_n, n, s.status.name, s.data_path, bool(s.eval_script), bool(s.eval_on_demand)))

ok = [r for r in rows if isinstance(r[2], int)]
over = [r for r in ok if r[1] and r[2] >= r[1]]
under = [r for r in ok if r[1] and r[2] < r[1]]
zero_min = [r for r in ok if not r[1]]
err = [r for r in rows if not isinstance(r[2], int)]

print(f"  OBSERVING 总条数 = {len(rows)}")
print(f"  ✅ 样本已达/超 min_n = {len(over)}   ⏳ 未达 = {len(under)}   ➖ min_n=0(永久测量) = {len(zero_min)}   ⚠ 读数失败 = {len(err)}")

print(f"\n  --- 🔴 已达门槛但仍在 OBSERVING (裁决欠账候选) {len(over)} 条 ---")
print(f"  {'shadow_id':36s} {'min_n':>6s} {'实测n':>8s} {'倍数':>7s} {'eval_script':>12s} {'on_demand':>10s}")
for sid, mn, n, st_, dp, has_eval, on_d in sorted(over, key=lambda x: -(x[2] / max(x[1], 1))):
    print(f"  {sid:36s} {mn:6d} {n:8d} {n/max(mn,1):6.1f}x {('有' if has_eval else '❌无'):>12s} {('是' if on_d else '否'):>10s}")

print(f"\n  --- ⏳ 未达门槛 {len(under)} 条 ---")
for sid, mn, n, st_, dp, has_eval, on_d in sorted(under, key=lambda x: x[2] / max(x[1], 1)):
    print(f"  {sid:36s} {mn:6d} {n:8d}  还差 {mn-n:6d}  ({100*n/max(mn,1):5.1f}%)")

print(f"\n  --- ➖ min_n=0 (永久测量项, 无毕业门) {len(zero_min)} 条 ---")
for sid, mn, n, st_, dp, has_eval, on_d in sorted(zero_min):
    print(f"  {sid:36s} 实测n={n}")

if err:
    print(f"\n  --- ⚠ 读数失败 {len(err)} 条 (需人工核) ---")
    for sid, mn, n, st_, dp, has_eval, on_d in err:
        print(f"  {sid:36s} min_n={mn} {n}  path={dp}")

print()
print("=" * 100)
print("㉑-2  本主题相关的三条影子 (定价轴 vs 退出轴) 精确读数")
print("=" * 100)
FOCUS = ("entry_band_widen", "exit_param_shadow", "exit_disable_cf", "early_dead_signal_shadow",
         "entry_delay_confirm", "shadow_portfolio", "ext_entry_exec_price_shadow", "eod_depth_gate")
for sid, mn, n, st_, dp, has_eval, on_d in rows:
    if sid not in FOCUS:
        continue
    ax = "定价/入场轴" if sid in ("entry_band_widen", "entry_delay_confirm", "ext_entry_exec_price_shadow") else \
         ("退出轴" if sid in ("exit_param_shadow", "exit_disable_cf", "early_dead_signal_shadow", "eod_depth_gate") else "反事实台账")
    full = BASE / str(dp).split(" ")[0] if dp else None
    exists = full.exists() if full else False
    mt = ""
    if exists:
        import datetime
        mt = datetime.datetime.fromtimestamp(full.stat().st_mtime).strftime("%m-%d %H:%M")
    print(f"  [{ax:12s}] {sid:32s} min_n={mn:<5} 实测n={str(n):<7} "
          f"{'✅达标' if isinstance(n,int) and mn and n>=mn else '⏳未达' if isinstance(n,int) and mn else '➖永久'}"
          f"  产物{'在' if exists else '❌不在'}({mt})  eval_script={'有' if has_eval else '❌无'}")

print()
print("=" * 100)
print("㉑-3  判据注册表 (adjudication) 侧的 READY 欠账")
print("=" * 100)
try:
    from app.playbook.criteria_registry import REGISTRY as CR
    print(f"  判据注册表条数 = {len(CR)}")
except Exception as e:
    print(f"  criteria_registry 导入失败: {type(e).__name__}: {e}")
    CR = None
if CR:
    import dataclasses
    cnt = {}
    ready = []
    for x in CR:
        d = dataclasses.asdict(x) if dataclasses.is_dataclass(x) else vars(x)
        s = str(d.get("status") or d.get("state") or "?")
        cnt[s] = cnt.get(s, 0) + 1
        if "READY" in s.upper():
            ready.append(d)
    print(f"  状态分布: {cnt}")
    print(f"  READY (样本够, 待裁) = {len(ready)}")
    for d in ready[:20]:
        cid = d.get("id") or d.get("criterion_id") or "?"
        cur = d.get("current_n"); mn = d.get("min_n")
        print(f"    {str(cid):40s} n={cur}/{mn}  {(d.get('name') or '')[:44]}")
