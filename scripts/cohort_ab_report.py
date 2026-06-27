#!/usr/bin/env python3
"""RFC-061 cohort A/B 退出实验度量 + OKR 决策门禁 (CLI 包装).

核心逻辑在 app/review/cohort_ab.py (供周复盘 import 复用).

用法:
  python scripts/cohort_ab_report.py
  python scripts/cohort_ab_report.py --since 2026-06-19 --min-n 30
  python scripts/cohort_ab_report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.review.cohort_ab import compute_cohort_report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="仅计 trade_date >= 此值 (如 2026-06-19 OKR 启动)")
    ap.add_argument("--min-n", type=int, default=30, help="决策门禁每组最小样本 (附录 B.4 默认 50, 冷启动可降)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    r = compute_cohort_report(since=args.since, min_n=args.min_n)

    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
        return

    print(f"=== RFC-061 cohort A/B 退出实验度量 ===")
    print(f"db: {r['db']}  since={r['since'] or '(all)'}  closed={r['closed_total']}  open={r['open_total']}")
    print(f"未平仓(持夜中) by cohort: {r['open_positions']}  ← B>0 说明实验真的让仓过夜")
    print()
    for c in ("A", "B", "PRE_EXPERIMENT"):
        s = (r["cohorts"] or {}).get(c, {}) or {}
        if s.get("n", 0) == 0:
            print(f"[{c}] n=0")
            continue
        print(f"[cohort {c}] n={s['n']}  sum_pnl=${s['sum_pnl']}  avg_pnl={s['avg_pnl_pct']}%  "
              f"avg_hold={s['avg_hold_h']}h  WR={s.get('win_rate_pct')}%  (W{s['wins']}/L{s['losses']})")
        print(f"   exit_reasons: {s.get('exit_reasons')}")
        print(f"   hold_buckets: {s.get('hold_buckets')}")
    print()
    print(f"决策门禁: {r['decision_gate']}")
    print()
    print("OKR 映射: cohort B vs A 的 sum_pnl/WR 差值 = 退出规则对齐 T+5 alpha 的 PnL/胜率增量")
    print("  KR2.1 胜率≥55%: 看 B 组 win_rate_pct 是否达标且 >A")
    print("  KR2.2 周PnL≥$1000: 看 B 组 sum_pnl 趋势 (需周维度积累)")


if __name__ == "__main__":
    main()
