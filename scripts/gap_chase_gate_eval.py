#!/usr/bin/env python3
"""gap_chase_gate 影子门评估 (2026-08-29 注册, 用户裁决).

回答一个问题: "若拦截 aligned_gap>=5% 的顺向追缺口入场, 躲过的交易总体是亏是赚?"
数据源: trade_records.metadata_json.entry_gap (入场时已采集, 零新采集线)。

预注册判据 (注册表 gap_chase_gate, 2026-08-29):
  n>=30 且 拦截组笔均PnL<0 且 单侧符号检验 p<=0.05 → GRADUATE_CANDIDATE (转硬门待用户裁决)
  否则 KEEP_SAMPLING (继续攒样不重置)

只读, 幂等, 零副作用。挂载: 周检/盘后复盘按需。
"""
import argparse
import json
import sqlite3
from math import comb
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "ftseek_us.db"
GATE_PCT = 5.0
MIN_N = 30
SIGN_P = 0.05


def sign_test_one_sided(neg: int, n: int) -> float:
    """P(X >= neg | Binomial(n, 0.5)) — 亏损笔数显著多于一半的单侧概率。"""
    if n <= 0:
        return 1.0
    return sum(comb(n, k) for k in range(neg, n + 1)) / (2 ** n)


def load_cohorts(db_path: str, since: str, gate_pct: float = GATE_PCT):
    """返回 (拦截组pnl列表, 对照组pnl列表, 覆盖率)。"""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT COALESCE(pnl_net, realized_pnl) pnl, metadata_json
           FROM trade_records
           WHERE source_type LIKE 'PLAYBOOK%' AND entry_time >= ?""",
        (since,),
    ).fetchall()
    con.close()
    blocked, rest, covered = [], [], 0
    for r in rows:
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:
            md = {}
        g = (md.get("entry_gap") or {}).get("aligned_gap_pct")
        if g is None:
            continue
        covered += 1
        pnl = float(r["pnl"] or 0.0)
        (blocked if g >= gate_pct else rest).append(pnl)
    coverage = covered / len(rows) if rows else 0.0
    return blocked, rest, coverage


def verdict(blocked: list, min_n: int = MIN_N, sign_p: float = SIGN_P) -> str:
    n = len(blocked)
    if n < min_n:
        return "KEEP_SAMPLING"
    neg = sum(1 for p in blocked if p < 0)
    mean = sum(blocked) / n
    p = sign_test_one_sided(neg, n)
    return "GRADUATE_CANDIDATE" if (mean < 0 and p <= sign_p) else "KEEP_SAMPLING"


def _stats(pnls: list) -> str:
    n = len(pnls)
    if not n:
        return "n=0"
    wins = sum(1 for p in pnls if p > 0)
    tot = sum(pnls)
    return "n=%-3d WR=%5.1f%% 累计PnL=%+9.1f 笔均=%+7.1f" % (n, wins / n * 100, tot, tot / n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--since", default="2026-07-01", help="entry_time 起点 (entry_gap 采集覆盖期)")
    args = ap.parse_args()

    blocked, rest, cov = load_cohorts(args.db, args.since)
    neg = sum(1 for p in blocked if p < 0)
    p = sign_test_one_sided(neg, len(blocked))
    v = verdict(blocked)

    print("═══ gap_chase_gate 影子门评估 (aligned_gap>=%.1f%%, %s 起) ═══" % (GATE_PCT, args.since))
    print("覆盖率: %.0f%% (entry_gap 字段采集期外的记录不计)" % (cov * 100))
    print("拦截组: %s" % _stats(blocked))
    print("对照组: %s" % _stats(rest))
    print("亏损笔数: %d/%d, 单侧符号检验 p=%.4f (阈值 %.2f)" % (neg, len(blocked), p, SIGN_P))
    print("预注册判据: n>=%d %s | 笔均<0 %s | p<=%.2f %s" % (
        MIN_N, "✓" if len(blocked) >= MIN_N else "✗(%d)" % len(blocked),
        "✓" if (blocked and sum(blocked) / len(blocked) < 0) else "✗",
        SIGN_P, "✓" if p <= SIGN_P else "✗"))
    print("裁决: %s" % v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
