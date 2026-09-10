#!/usr/bin/env python3
"""UE tier 闸影子结算评估器 (2026-09-11, 用户裁「做」— 方案3: 裁决报告加方向分层读数).

消费 `data/shadow/ue_tier_gate_shadow.jsonl` (PROPOSAL-20260905 U3/P4, 写者
llm_universe_scan._log_tier_gate_shadow, 每扫描日 1 行, rows[]=当日候选逐条判定)。
此前只有写者无读者 — 15 交易日裁决点 (≈9/29) 前本器即"既有裁决报告"载体。

预注册判据 (逐字引 unified_config.yaml universe_expansion.tier_gate 注释, 2026-09-05):
  攒样≥15交易日 且 被影子化桶 fwd 回填 n≥30;
  毕业 = 被拦桶方向性前瞻均值≤0 且 放行桶均笔>0 → 用户裁决;
  撤闸认错 = 被拦桶前瞻 >+0.3% 且 n≥30。

方向分层 (2026-09-11 本批新增, record-only 读数并列不改判据):
  UE LONG 全史 n=41 WR46% 净−$780 vs SHORT n=28 WR68% +$208 (30天复核同源) —
  裁决日若 tier 门过线, LONG/SHORT 分层证据须已就位; 不过线则双证据一起否。
  分层只并列呈报, 不另设门 (防多重比较挪门柱)。

口径:
  - join playbook_instructions (date, 'US.'+symbol) 取 direction/fwd_return_t1/executed;
    fwd_return_t1 为方向调整后收益 (ic_tracker sign=±1, 可直接跨方向聚合)。
  - 被拦桶 = would_block=true 行; 放行桶 = would_block=false 行。
  - 影子零成交 (enforce=false) → 全部行都是"放行后实际发生", 被拦桶的 fwd 是反事实口径。

挂载: crontab 52 5 * * 6 (周六, fwd 回填 05:30 之后; ue_tier_shadow_eval 05:50 同窗)。
产物: data/shadow/ue_tier_gate_eval_latest.json (atomic write)。
用法: venv/bin/python scripts/ue_tier_gate_shadow_eval.py [--min-days 15] [--min-n 30]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "ue_tier_gate_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "ue_tier_gate_eval_latest.json"

MIN_DAYS = 15   # 预注册: 攒样交易日下限
MIN_N = 30      # 预注册: 被影子化桶 fwd 回填样本下限
RETIRE_RET = 0.003  # 预注册: 撤闸认错线 (被拦桶 fwd > +0.3% 且 n≥30)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_days() -> list:
    if not SHADOW.exists():
        return []
    days = []
    for ln in SHADOW.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            days.append(json.loads(ln))
        except Exception as e:  # 坏行留痕不静默
            print(f"[ue_tier_gate_eval] 影子档坏行跳过: {e} | {ln[:120]!r}", file=sys.stderr)
    return days


def _bucket_stats(rows: list) -> dict:
    fwd = [r["fwd"] for r in rows if r.get("fwd") is not None]
    n = len(fwd)
    mean = (sum(fwd) / n) if n else None
    return {"n_rows": len(rows), "n_fwd": n,
            "fwd_mean_pct": round(mean * 100, 3) if mean is not None else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    days = _load_days()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    joined = []
    for day in days:
        for c in day.get("rows", []):
            sym = c.get("symbol") or ""
            full = sym if sym.startswith("US.") else f"US.{sym}"
            inst = conn.execute(
                "SELECT direction, fwd_return_t1, COALESCE(executed,0) AS executed "
                "FROM playbook_instructions WHERE date=? AND symbol=? "
                "AND decision_id LIKE 'universe_expansion%' LIMIT 1",
                (day.get("date"), full)).fetchone()
            joined.append({
                "date": day.get("date"), "symbol": full,
                "direction": (c.get("direction") or "").upper(),
                "tier": c.get("tier"), "would_block": bool(c.get("would_block")),
                "rule": c.get("rule"),
                "fwd": (inst["fwd_return_t1"] if inst else None),
                "executed": bool(inst["executed"]) if inst else False,
                "inst_dir": (inst["direction"] if inst else None),
            })
    conn.close()

    n_days = len({d.get("date") for d in days})
    blocked = [r for r in joined if r["would_block"]]
    passed = [r for r in joined if not r["would_block"]]

    def _slice(rows, direction=None):
        sel = rows if direction is None else [r for r in rows if r["direction"] == direction]
        return _bucket_stats(sel)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_days": n_days,
        "n_candidates": len(joined),
        "prereg": (f"攒样≥{args.min_days}交易日 ∧ 被影子化桶 fwd n≥{args.min_n}; "
                   f"毕业=被拦桶 fwd 均值≤0 ∧ 放行桶均笔>0 → 用户裁决; "
                   f"撤闸认错=被拦桶 fwd>+{RETIRE_RET*100:.1f}% ∧ n≥{args.min_n}"),
        "buckets": {
            "ALL": {"blocked": _slice(blocked), "passed": _slice(passed)},
            "LONG": {"blocked": _slice(blocked, "LONG"), "passed": _slice(passed, "LONG")},
            "SHORT": {"blocked": _slice(blocked, "SHORT"), "passed": _slice(passed, "SHORT")},
        },
        "verdict": "KEEP_SAMPLING",
    }

    b_all, p_all = report["buckets"]["ALL"]["blocked"], report["buckets"]["ALL"]["passed"]
    days_ok = n_days >= args.min_days
    n_ok = b_all["n_fwd"] >= args.min_n
    if days_ok and n_ok:
        bm, pm = b_all["fwd_mean_pct"], p_all["fwd_mean_pct"]
        if bm is not None and bm > RETIRE_RET * 100:
            report["verdict"] = "RETIRE_GATE (撤闸认错: 被拦桶前瞻>+0.3%)"
        elif bm is not None and bm <= 0 and (pm or 0) > 0:
            report["verdict"] = "READY_TO_ADJUDICATE (毕业判据过线, 提请用户裁决; 方向分层并列见 buckets)"
        else:
            report["verdict"] = "KEEP_SAMPLING (判据未过线)"
    _atomic_write(OUT, json.dumps(report, ensure_ascii=False, indent=1))

    print(f"[ue_tier_gate_eval] days={n_days}/{args.min_days} candidates={len(joined)} "
          f"blocked={len(blocked)} → {OUT.name}")
    for k in ("ALL", "LONG", "SHORT"):
        bk, pk = report["buckets"][k]["blocked"], report["buckets"][k]["passed"]
        print(f"  {k:5s} 被拦: n_fwd={bk['n_fwd']:3d} fwd均值={bk['fwd_mean_pct']}% | "
              f"放行: n_fwd={pk['n_fwd']:3d} fwd均值={pk['fwd_mean_pct']}%")
    print(f"  verdict: {report['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
