#!/usr/bin/env python3
"""C16 判据复现: 「追突破 vs 回调」这条入场形态轴到底解不解释出血。

只读承诺: sqlite `mode=ro`, 不写库、不下单、不改任何文件。

人群/口径
---------
A10 口径: `trade_date >= 2026-07-01` ∧ `data_quality ∈ (clean, real)`
          ∧ `source_type ∉ (POSITION_SYNC, MANUAL)` ∧ 已平 (`exit_price > 0` ∧ `pnl_net` 非空)
          ⇒ n=721。净额一律 `pnl_net`。

两个口径都跑, 因为第一个不可用:
1) 标签口径 `playbook_instructions.raw_instruction_json.entry_archetype`
   —— 只覆盖 A10 的 34.7% (250/721), 且 **DA 内 48.1%、UE/reentry/DEC 全 0**;
   值域 400 PULLBACK_LIMIT / 128 FADE_SHORT / **8 BREAKOUT** ⇒ 分不出"追/不追"。
2) 几何口径 `chase_bps = sign × (fill − planned) / planned × 1e4`
   (sign: LONG +1 / SHORT −1; 正 = 买贵/卖便宜 = 追), `planned` 取
   `raw_instruction_json.planned_entry` (**不是** `playbook_instructions.entry_price`,
   后者已被 reprice 覆盖成执行锚)。覆盖 498/721 = 69%
   (余 223 笔是 UE/reentry/DEC 等未落 planned_entry 的通道)。

判据见 docs/ADJUDICATED-CRITERIA.md C16。
"""
import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
A10 = """
SELECT symbol, direction, entry_price, quantity, pnl_net, realized_pnl, fees_est,
       net_pnl_pct, exit_reason, decision_id, trade_date, entry_session, mfe_pct
  FROM trade_records
 WHERE exit_price > 0 AND pnl_net IS NOT NULL
   AND trade_date >= '2026-07-01'
   AND COALESCE(data_quality, '') IN ('clean', 'real')
   AND COALESCE(source_type, '') NOT IN ('POSITION_SYNC', 'MANUAL')
"""


def bucket(bps: float) -> str:
    if bps <= 0:
        return "A 贴/优于计划(真回调·接飞刀)"
    if bps <= 50:
        return "B 追 0-50bps"
    if bps <= 150:
        return "C 追 50-150bps"
    return "D 追 >150bps"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute(A10)]

    plan = {}
    for r in conn.execute("SELECT decision_id, executed, raw_instruction_json "
                          "FROM playbook_instructions "
                          "WHERE raw_instruction_json LIKE '%planned_entry%'"):
        did = r["decision_id"]
        if not did:
            continue
        try:
            raw = json.loads(r["raw_instruction_json"] or "{}")
        except ValueError:
            continue
        p = raw.get("planned_entry")
        if not p:
            continue
        if did in plan and plan[did]["exec"] == 1:
            continue          # 同 decision_id 双行 (分析日/执行日) 优先 executed=1
        plan[did] = {"plan": float(p), "arch": raw.get("entry_archetype"),
                     "exec": int(r["executed"] or 0)}

    # 标签口径覆盖率 (说明为什么不用它)
    arch_cov = sum(1 for t in trades if plan.get(t["decision_id"], {}).get("arch"))
    arch_dist = defaultdict(int)
    for v in plan.values():
        if v["arch"]:
            arch_dist[v["arch"]] += 1

    agg = defaultdict(list)
    for t in trades:
        p = plan.get(t["decision_id"])
        if not p or not t["entry_price"]:
            continue
        sign = 1 if str(t["direction"]).upper() == "LONG" else -1
        bps = sign * (float(t["entry_price"]) - p["plan"]) / p["plan"] * 1e4
        t["chase_bps"], t["arch"], t["b"] = bps, p["arch"], bucket(bps)
        agg[t["b"]].append(t)

    print(f"A10 已平 {len(trades)} 笔; 可算追价 {sum(len(v) for v in agg.values())}"
          f" ({sum(len(v) for v in agg.values()) / len(trades) * 100:.0f}%)")
    print(f"标签口径 entry_archetype 只覆盖 {arch_cov}/{len(trades)} = "
          f"{arch_cov / len(trades) * 100:.1f}%; 指令侧值域 = {dict(arch_dist)}")

    def table(rows_by_bucket, title):
        print(f"\n{title}")
        print(f"{'桶':30s}{'n':>5s}{'WR':>7s}{'Σ净':>10s}{'均/笔':>8s}{'中位净%':>9s}")
        for k in sorted(rows_by_bucket):
            v = rows_by_bucket[k]
            net = sum(x["pnl_net"] for x in v)
            print(f"{k:30s}{len(v):5d}"
                  f"{sum(1 for x in v if x['pnl_net'] > 0) / len(v) * 100:6.1f}%"
                  f"{net:10.1f}{net / len(v):8.1f}"
                  f"{st.median(x['net_pnl_pct'] or 0 for x in v) * 100:8.2f}%")

    table(agg, "① 全样本 (含隔夜跳空)")
    ex = defaultdict(list)
    for k, v in agg.items():
        ex[k] = [x for x in v if x["exit_reason"] != "OVERNIGHT_GAP_STOP"]
    table(ex, "② 剔除全部 OVERNIGHT_GAP_STOP (跳空是否与追价有关)")
    chase = sum(x["pnl_net"] for v in ex.values() for x in v if not x["b"].startswith("A"))
    calm = sum(x["pnl_net"] for v in ex.values() for x in v if x["b"].startswith("A"))
    print(f"   剔跳空后: 追价桶(B+C+D) Σ={chase:+.1f}  vs  A 桶 Σ={calm:+.1f}")

    print("\n③ 12 笔 OVERNIGHT_GAP_STOP 落在哪个桶 (跳空挑不挑追价程度)")
    for t in sorted([x for x in trades if x["exit_reason"] == "OVERNIGHT_GAP_STOP"],
                    key=lambda x: x["trade_date"]):
        p = plan.get(t["decision_id"])
        if not p:
            print(f"   {t['symbol']:9s} {t['trade_date']} 净{t['pnl_net']:+8.1f}  无 planned_entry")
            continue
        sign = 1 if str(t["direction"]).upper() == "LONG" else -1
        bps = sign * (float(t["entry_price"]) - p["plan"]) / p["plan"] * 1e4
        print(f"   {t['symbol']:9s} {t['trade_date']} 净{t['pnl_net']:+8.1f} 追价{bps:+6.0f}bps → {bucket(bps)}")

    print("\n④ 剔跳空后 按月×桶 单笔均值 (符号是否稳定 = 能否建门)")
    for m in sorted({x["trade_date"][:7] for v in ex.values() for x in v}):
        line = f"   {m} "
        for k in sorted(ex):
            v = [x for x in ex[k] if x["trade_date"][:7] == m]
            if v:
                line += f"{k[0]}={sum(x['pnl_net'] for x in v) / len(v):+6.1f}(n{len(v):3d}) "
        print(line)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())