#!/usr/bin/env python
"""DEB-20261007 C6 口径探针 — time_to_mfe 的可信度三查 (方案 C 的唯一依据).

为什么必须入库: 本脚本答的是 §9 复查挑战点 #1 的三个子问题, 而 E9-E11 全部依赖
`time_to_mfe` 字段。若该字段口径不成立, 方案 C 失去依据 ⇒ 这是本 DEB 最脆弱处。

三查:
  ① 单位是否真是分钟 (max 是否远超单日交易分钟数 390)
  ② 缺失的 46.7% 是否**非随机缺失** (若集中在某类出场原因 ⇒ E9/E10 对比有选择偏倚)
  ③ 顶层 `time_to_mfe` 与 `exit_telemetry.time_to_mfe` 是否同值
     (E9-E11 用了 `or` 兜底, 两者不一致 = 混合口径)

用法: cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/deb20261007_time_to_mfe_probe.py
只读 (mode=ro), 零写入。
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])

import collections
import json
import sqlite3
import statistics as st

RTH_MINUTES = 390  # 09:30-16:00 ET 常规时段分钟数 (单位哨兵)

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT id, exit_reason, metadata_json FROM trade_records "
    "WHERE exit_time IS NOT NULL"
).fetchall()
print(f"已结算样本 (exit_time 非空) n={len(rows)}")


def pick(d: dict, key: str):
    """顶层优先, exit_telemetry 兜底 — 与 E9-E11 同一取法."""
    v = d.get(key)
    if v is None:
        v = (d.get("exit_telemetry") or {}).get(key)
    return v


top, tel, both, mismatch = [], [], [], []
have, miss_reason = [], collections.Counter()
units = []

for r in rows:
    try:
        d = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        d = {}
    t_top = d.get("time_to_mfe")
    t_tel = (d.get("exit_telemetry") or {}).get("time_to_mfe")
    v = t_top if t_top is not None else t_tel
    if v is None:
        # ② 非随机缺失检验: 缺失行的出场原因分布
        miss_reason[str(r["exit_reason"] or "NULL")] += 1
        continue
    try:
        fv = float(v)
    except Exception:
        miss_reason["unparseable"] += 1
        continue
    have.append((fv, str(r["exit_reason"] or "NULL")))
    units.append(fv)
    if t_top is not None:
        top.append(float(t_top))
    if t_tel is not None:
        tel.append(float(t_tel))
    if t_top is not None and t_tel is not None:
        both.append((float(t_top), float(t_tel)))
        if abs(float(t_top) - float(t_tel)) > 1e-9:
            mismatch.append((r["id"], float(t_top), float(t_tel)))

print(f"有 time_to_mfe n={len(have)}  缺失 n={len(rows) - len(have)} "
      f"(覆盖率 {100 * len(have) / max(len(rows), 1):.1f}%)")

print("\n=== ① 单位检验 (若是分钟, 绝大多数应 ≤ 390 = RTH 单日分钟数) ===")
if units:
    over = [u for u in units if u > RTH_MINUTES]
    print(f"  中位={st.median(units):.2f}  p90={sorted(units)[int(len(units) * 0.9)]:.2f}  "
          f"max={max(units):.2f}")
    print(f"  >{RTH_MINUTES}min 的行数 = {len(over)} / {len(units)} "
          f"({100 * len(over) / len(units):.1f}%)")
    print(f"  ⇒ 判读: 占比低 = 隔夜/多日持仓所致, 单位仍是分钟 (合理); "
          f"占比高或 max 达数万 ⇒ 单位可疑 (秒/毫秒), E9-E11 作废")

print("\n=== ② 缺失是否非随机 (按出场原因分布) ===")
have_reason = collections.Counter(r for _, r in have)
print(f"  {'出场原因':28s} {'有值':>6s} {'缺失':>6s}  缺失率")
keys = sorted(set(have_reason) | set(miss_reason),
              key=lambda k: -(have_reason[k] + miss_reason[k]))
skew = []
for k in keys[:12]:
    h, m = have_reason[k], miss_reason[k]
    tot = h + m
    rate = 100 * m / tot if tot else 0.0
    print(f"  {k[:28]:28s} {h:6d} {m:6d}  {rate:5.1f}%")
    if tot >= 20:
        skew.append((k, rate, tot))
if skew:
    lo = min(r for _, r, _ in skew)
    hi = max(r for _, r, _ in skew)
    print(f"  ⇒ n≥20 的出场原因缺失率区间 = {lo:.1f}% ~ {hi:.1f}%  (跨度 {hi - lo:.1f}pp)")
    print(f"     判读: 跨度大 ⇒ 非随机缺失, E9/E10 的成功 vs 失败对比含选择偏倚 (须在文档披露);")
    print(f"           跨度小 ⇒ 近似随机缺失, 对比可用")

print("\n=== ③ 顶层 vs exit_telemetry 是否同值 ===")
print(f"  两字段都有 n={len(both)}  不一致 n={len(mismatch)}")
if mismatch[:5]:
    for tid, a, b in mismatch[:5]:
        print(f"    id={tid} 顶层={a} telemetry={b}")
print(f"  ⇒ 判读: 不一致=0 ⇒ E9-E11 的 `or` 兜底不是混合口径 (安全);")
print(f"          不一致>0 ⇒ 混合口径, 须指定单一来源重算")

print("\n=== 结论行 (供 E/C 表引用) ===")
print(f"  覆盖率={100 * len(have) / max(len(rows), 1):.1f}%  "
      f"中位={st.median(units):.2f}min  max={max(units):.2f}min  "
      f"telemetry不一致={len(mismatch)}")
