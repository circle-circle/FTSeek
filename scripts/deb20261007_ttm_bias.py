#!/usr/bin/env python
"""DEB-20261007 自曝#10 量化 — time_to_mfe 结构性缺失对 E9/E10 的偏倚影响.

探针 ① 已证: 缺失是**结构性**的 (sl_hit/TRAILING_STOP/STRUCTURE_EXIT/EOD_FORCE_CLOSE/
DAY_TRADE_TP 缺失率 100%, SL_INITIAL 85.7%, TIME_EXIT 76.0%) ⇒ E9/E10 的
「落袋成功 vs 失败」对比人群被系统性筛过。

本脚本量化偏倚方向与幅度:
  A. E9/E10 实际用的人群 (有 time_to_mfe) 的出场原因构成 vs 全量已结算人群
  B. 关键: 死池出血口 (SL_INITIAL/OVERNIGHT_GAP_STOP) 在 E9/E10 人群里剩几笔?
  C. 若把缺失行按「有值行的同出场原因分布」外推, 落袋失败率会变多少?
     (粗外推, 只为判偏倚方向, 不作裁决依据)

只读 (mode=ro), 零写入。
用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261007_ttm_bias.py
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])

import collections
import json
import sqlite3

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, symbol, exit_reason, pnl_net, pnl_pct, mfe_pct, fees_est, "
    "       holding_hours, metadata_json "
    "FROM trade_records WHERE exit_time IS NOT NULL"
).fetchall()


def ttm(d):
    v = d.get("time_to_mfe")
    if v is None:
        v = (d.get("exit_telemetry") or {}).get("time_to_mfe")
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


recs = []
for r in rows:
    try:
        d = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        d = {}
    recs.append({
        "id": r["id"], "sym": r["symbol"], "er": str(r["exit_reason"] or "NULL"),
        "net": r["pnl_net"], "mfe": r["mfe_pct"],
        "fees": r["fees_est"], "ttm": ttm(d),
    })

print(f"全量已结算 n={len(recs)}")
have = [x for x in recs if x["ttm"] is not None]
miss = [x for x in recs if x["ttm"] is None]
print(f"有 time_to_mfe n={len(have)} ({100*len(have)/len(recs):.1f}%)  缺失 n={len(miss)}")

print("\n=== A. E9/E10 实际人群 vs 全量: 出场原因构成 ===")
ch = collections.Counter(x["er"] for x in have)
cm = collections.Counter(x["er"] for x in miss)
ca = collections.Counter(x["er"] for x in recs)
print(f"  {'出场原因':24s} {'全量':>5s} {'有值':>5s} {'有值占比':>8s}  偏倚")
for k, n in ca.most_common(14):
    h = ch.get(k, 0)
    share = 100 * h / n if n else 0
    flag = "🔴 系统性排除" if share < 20 and n >= 20 else ("⚠ 部分排除" if share < 60 and n >= 20 else "")
    print(f"  {k[:24]:24s} {n:5d} {h:5d} {share:7.1f}%  {flag}")

print("\n=== B. 死池出血口在 E9/E10 人群里剩几笔 (E8 的两条) ===")
for k in ("SL_INITIAL", "OVERNIGHT_GAP_STOP"):
    tot = ca.get(k, 0)
    h = ch.get(k, 0)
    net_all = sum(float(x["net"] or 0) for x in recs if x["er"] == k)
    net_have = sum(float(x["net"] or 0) for x in have if x["er"] == k)
    print(f"  {k:22s} 全量 n={tot:4d} 净=${net_all:10.2f} | 有值 n={h:3d} 净=${net_have:9.2f}"
          f"  ⇒ 覆盖 {100*h/max(tot,1):.1f}% 的人, {100*abs(net_have)/max(abs(net_all),1e-9):.1f}% 的钱")

print("\n=== C. 偏倚方向判定 ===")
# 有值人群 vs 缺失人群的净额对比: 若有值人群明显更「好」⇒ E9/E10 是在优选人群上做的对比
net_h = sum(float(x["net"] or 0) for x in have)
net_m = sum(float(x["net"] or 0) for x in miss)
med_h = sorted(float(x["net"] or 0) for x in have)
med_m = sorted(float(x["net"] or 0) for x in miss)
def med(v): return v[len(v)//2] if v else 0.0
print(f"  有值人群 n={len(have)} 净=${net_h:10.2f} 中位单笔=${med(med_h):8.2f}")
print(f"  缺失人群 n={len(miss)} 净=${net_m:10.2f} 中位单笔=${med(med_m):8.2f}")
print(f"  ⇒ 若有值人群净额/中位显著优于缺失人群 ⇒ E9/E10 在**优选人群**上做对比,")
print(f"     其「失败组」也不含最坏出口 ⇒ 方案 C 的收益上限须按此再下修")

print("\n=== D. 结论行 (供文档引用) ===")
print(f"  覆盖率={100*len(have)/len(recs):.1f}%  "
      f"SL_INITIAL覆盖={100*ch.get('SL_INITIAL',0)/max(ca.get('SL_INITIAL',1),1):.1f}%  "
      f"OVERNIGHT覆盖={100*ch.get('OVERNIGHT_GAP_STOP',0)/max(ca.get('OVERNIGHT_GAP_STOP',1),1):.1f}%")
