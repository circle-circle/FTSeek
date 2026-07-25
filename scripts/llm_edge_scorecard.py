#!/usr/bin/env python3
"""
llm_edge_scorecard.py — LLM 判断力 edge 判分器 (只读)

回答一个可检验的问题: LLM 的 conviction / 共识 / 是否辩论 / 环境判断,
到底和实测期望值相不相关?

数据源 (真实、已核实):
  - data/decisions.db  : decisions(2257) + model_votes(4203)  — LLM 投票明细
  - data/ftseek_us.db  : trade_records                        — 实测 realized_pnl

方法 (2026-07-12 定): 因 decision_id 不传播到 trade_records (0 条含 DEC-),
只能按 (symbol, date) 模糊 join → n≈211。这是相关性分析, 非因果。

⚠️ 已知置信边界 (必须与结果一起读):
  1. 模糊 join: 决策方向未必=实际成交方向; 一天多笔被平均。
  2. 时间混淆: 早期行 market_regime=None 且更赚钱 → "越自信越差" 可能部分是时间效应。
  3. 混合 source_type (MAIN_PIPELINE/SCALPING/PLAYBOOK 合并)。
  → 结论级别: "强线索", 不足以定因果。用于佐证 "LLM 信心无正向 edge", 不用于精调。

落地: data/llm_edge_scorecard.json (供 handoff / 后续对比)
"""
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent.parent
DECISIONS_DB = ROOT / "data" / "decisions.db"
TRADES_DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "llm_edge_scorecard.json"


def _corr(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return num / den if den else 0.0


def _bucket(rows, key, edges_labels):
    """edges_labels: list of (lo, hi, label). 返回每桶 {n, expectancy, win_rate}."""
    out = []
    for lo, hi, label in edges_labels:
        sub = [r for r in rows if lo <= r[key] < hi]
        if sub:
            pnls = [r["pnl"] for r in sub]
            wins = sum(1 for p in pnls if p > 0)
            out.append({
                "bucket": label, "n": len(sub),
                "expectancy": round(mean(pnls), 2),
                "win_rate": round(100 * wins / len(sub), 1),
                "noisy": len(sub) < 15,
            })
        else:
            out.append({"bucket": label, "n": 0, "expectancy": None, "win_rate": None, "noisy": True})
    return out


def main():
    if not DECISIONS_DB.exists() or not TRADES_DB.exists():
        print(f"缺少 DB: {DECISIONS_DB} 或 {TRADES_DB}", file=sys.stderr)
        return 1

    # trade_records: (symbol, date) -> 平均 realized_pnl
    t = sqlite3.connect(str(TRADES_DB)); t.row_factory = sqlite3.Row
    tr = {}
    for r in t.execute("SELECT symbol, trade_date, realized_pnl FROM trade_records "
                       "WHERE exit_price>0 AND realized_pnl IS NOT NULL"):
        k = (r["symbol"].replace("US.", ""), r["trade_date"])
        tr.setdefault(k, []).append(float(r["realized_pnl"]))

    # decisions -> join
    d = sqlite3.connect(str(DECISIONS_DB)); d.row_factory = sqlite3.Row
    rows = []
    for r in d.execute("SELECT symbol, substr(created_at,1,10) dt, final_strength, "
                       "consensus_level, auto_debate, market_regime FROM decisions"):
        k = (r["symbol"].replace("US.", ""), r["dt"])
        if k not in tr:
            continue
        try:
            fs = float(r["final_strength"])
        except (TypeError, ValueError):
            continue
        rows.append({
            "fs": fs,
            "cons": float(r["consensus_level"] or 0),
            "deb": int(r["auto_debate"] or 0),
            "reg": r["market_regime"] or "None",
            "pnl": mean(tr[k]),
        })

    if not rows:
        print("join 后无样本", file=sys.stderr)
        return 1

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "fuzzy (symbol,date) join decisions↔trade_records; correlational not causal",
        "n": len(rows),
        "caveats": [
            "模糊 join: 决策方向未必=实际成交方向; 一天多笔被平均",
            "时间混淆: 早期行 regime=None 且更赚钱, 结论可能部分是时间效应",
            "混合 source_type; 结论级别=强线索, 非因果",
        ],
        "conviction_buckets": _bucket(rows, "fs",
            [(0, 45, "弱 [<45]"), (45, 55, "中 [45-55)"), (55, 101, "强 [>=55]")]),
        "consensus_buckets": _bucket(rows, "cons",
            [(-1, 0.01, "无共识[~0]"), (0.01, 0.5, "低[0-0.5)"), (0.5, 1.01, "高[>=0.5]")]),
        "debate": {
            "debated": _bucket([r for r in rows if r["deb"] == 1], "fs", [(-1, 999, "已辩论")])[0],
            "not_debated": _bucket([r for r in rows if r["deb"] == 0], "fs", [(-1, 999, "未辩论")])[0],
        },
        "corr_final_strength_pnl": round(_corr([r["fs"] for r in rows], [r["pnl"] for r in rows]), 3),
        "corr_consensus_pnl": round(_corr([r["cons"] for r in rows], [r["pnl"] for r in rows]), 3),
    }

    # regime buckets
    regs = {}
    for r in rows:
        regs.setdefault(r["reg"], []).append(r)
    result["regime_buckets"] = []
    for reg, sub in sorted(regs.items(), key=lambda kv: -len(kv[1])):
        pnls = [x["pnl"] for x in sub]
        wins = sum(1 for p in pnls if p > 0)
        result["regime_buckets"].append({
            "regime": reg, "n": len(sub),
            "expectancy": round(mean(pnls), 2),
            "win_rate": round(100 * wins / len(sub), 1),
            "noisy": len(sub) < 15,
        })

    # verdict 一句话
    r_fs = result["corr_final_strength_pnl"]
    if r_fs <= -0.1:
        result["verdict"] = ("LLM conviction 与结果负相关 (越自信越差) → 别让 LLM 信心驱动方向/加仓。"
                             "佐证 conf 拍平 + '不会预测' 论点。")
    elif abs(r_fs) < 0.1:
        result["verdict"] = "LLM conviction 与结果基本无关 (|r|<0.1) → 无预测 edge。"
    else:
        result["verdict"] = "LLM conviction 与结果正相关 → 值得复核 (与既往发现相悖)。"

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"✅ 已落 {OUT}")
    print(f"   n={result['n']}  corr(conviction,pnl)={r_fs}  corr(consensus,pnl)={result['corr_consensus_pnl']}")
    print(f"   verdict: {result['verdict']}")
    for b in result["conviction_buckets"]:
        print(f"   conviction {b['bucket']:12s} n={b['n']:3d} 期望=${b['expectancy']} 胜率={b['win_rate']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
