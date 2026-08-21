#!/usr/bin/env python3
"""UE self_tier 影子结算评估器 (record-only, 2026-08-21 立项).

背景: UE 通道无双模型共识, 置信度锚点=self_tier 证据层级
(HARD_EVENT > MARKET_CORROBORATED > THESIS_ONLY). 本评估器按
backtest-rule-replay 纪律验证各 tier 是否有真实 edge:
  同日截面市场调整 (当日全部已结算非SKIP指令 fwd_t1 均值=市场代理)
  + IS/OOS 时间对半同号 + |t|>=2.0 + n>=30 门槛.

输出: data/ue_tier_shadow_eval_latest.json (verdict, 固定键序确定性)
      + stdout 报告 (cron 捕获). 不改任何执行行为.

数据管道复用现有: self_tier 已在 inject 落库 (raw_instruction_json.origin.self_tier),
fwd_return_t1 由 ic_tracker/backfill_forward_returns 覆盖 UE 行. 本脚本纯读.
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "data" / "ftseek_us.db"
OUT_PATH = REPO / "data" / "ue_tier_shadow_eval_latest.json"
MIN_N = 30
MIN_T = 2.0


def tstat(values):
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0
    m = sum(values) / n
    if n < 2:
        return m, 0.0, n
    var = sum((x - m) ** 2 for x in values) / (n - 1)
    s = math.sqrt(var) if var > 0 else 0.0
    t = m / (s / math.sqrt(n)) if s > 0 else 0.0
    return m, t, n


def main():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    # 市场代理: 当日全部已结算非SKIP指令 fwd_t1 均值 (同日截面基准)
    allrows = db.execute(
        "SELECT date, fwd_return_t1 FROM playbook_instructions "
        "WHERE direction != 'SKIP' AND fwd_return_t1 IS NOT NULL"
    ).fetchall()
    by_date = defaultdict(list)
    for r in allrows:
        by_date[r["date"]].append(r["fwd_return_t1"])
    proxy = {d: sum(v) / len(v) for d, v in by_date.items()}

    rows = db.execute(
        "SELECT date, direction, fwd_return_t1, raw_instruction_json "
        "FROM playbook_instructions "
        "WHERE decision_id LIKE 'universe_expansion%' AND fwd_return_t1 IS NOT NULL"
    ).fetchall()

    dated = defaultdict(list)  # tier -> [(date, excess)]
    for r in rows:
        try:
            rj = json.loads(r["raw_instruction_json"] or "{}")
        except (TypeError, ValueError):
            rj = {}
        tier = (rj.get("origin") or {}).get("self_tier") or "UNKNOWN"
        dated[tier].append((r["date"], r["fwd_return_t1"] - proxy[r["date"]]))

    tiers = {}
    for tier, v in sorted(dated.items()):
        v.sort()  # chronological for IS/OOS
        excess = [x[1] for x in v]
        m, t, n = tstat(excess)
        mid = n // 2
        m_is, t_is, n_is = tstat(excess[:mid])
        m_oos, t_oos, n_oos = tstat(excess[mid:])
        same_sign = (m_is * m_oos > 0) if (n_is and n_oos) else None
        verdict = "PASS" if (
            n >= MIN_N and abs(t) >= MIN_T and same_sign and abs(m) >= 0.003
        ) else "NOT_SIGNIFICANT"
        tiers[tier] = {
            "n": n,
            "excess_mean_pct": round(m * 100, 3),
            "t": round(t, 2),
            "is_pct": round(m_is * 100, 3),
            "oos_pct": round(m_oos * 100, 3),
            "is_oos_same_sign": same_sign,
            "gates": {
                "n_gate": n >= MIN_N,
                "t_gate": abs(t) >= MIN_T,
                "oos_gate": bool(same_sign),
                "effect_floor": abs(m) >= 0.003,
            },
            "verdict": verdict,
        }

    out = {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "method": "同日截面市场调整 + IS/OOS对半 + |t|>=2 + n>=30 + 效应量地板0.3%",
        "prereg": "tier PASS (四关门全过) → 提案升级UE该tier优先级/权重; "
                  "THESIS_ONLY 持续负 → 提案 inject 端过滤",
        "settled_total": len(rows),
        "tiers": tiers,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # stdout report
    print(f"UE self_tier 影子结算 ({out['generated']}, 已结算 {len(rows)} 条)")
    for tier, d in tiers.items():
        g = d["gates"]
        flags = "".join(
            ("✓" if g[k] else "✗") for k in ("n_gate", "t_gate", "oos_gate", "effect_floor")
        )
        print(
            f"  {tier:22s} n={d['n']:3d} 超额={d['excess_mean_pct']:+.2f}% "
            f"t={d['t']:+.2f} IS/OOS={d['is_pct']:+.2f}/{d['oos_pct']:+.2f} "
            f"[{flags}] {d['verdict']}"
        )
    passed = [t for t, d in tiers.items() if d["verdict"] == "PASS"]
    if passed:
        print(f"⚠️ 有 tier 过四关门: {passed} — 提案人工裁决升级")
    else:
        print("无 tier 过四关门 — 继续攒样, 不改执行行为")
    return 0


if __name__ == "__main__":
    sys.exit(main())
