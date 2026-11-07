#!/usr/bin/env python3
"""指标漂移体检 (只读): 区分"真改善"与"出场构成改写指标造成的假改善"。

背景 (DEB-20261106 §28):
`mfe_pct` 是**持仓期间峰值**，而 BE/锁利类出场必须先走到武装线才可能发生 ⇒ 这些笔在定义上
不可能 `mfe<0.3%`。出场链一变(be 参数/限价保本/提前退出)，"死单率"的分母被改写，指标自动"变好"，
与信号质量无关。免疫口径是裸持有收益 `fwd_return_*`(与出场方式无关)。

本脚本给出三个量，缺一个就会误读:
  1. 原始死单率        raw_dead      = mfe<0.3 的占比            (会被出场构成污染)
  2. 同期非 BE 死单率   nonbe_dead    = 非 BE 出场人群里的死单率   (去掉分母膨胀)
  3. 同构成调整死单率   adj_dead      = 按参考期 BE 占比标准化后的死单率 (可比口径)
并列 `fwd_return_intraday` 均值/正占比作为真值参照。

判定:
  raw 变好 ∧ adj/fwd 未变好  ⇒ **DRIFT**(假改善)
  raw 与 adj/fwd 同向变好     ⇒ REAL

性质: 只读 DB, 零写入, 无 cron。回滚 = 删本文件。
用法: venv/bin/python scripts/metric_drift_audit.py [--source DA|ALL] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
DEAD_MFE = 0.3
BE_REASONS = {"SL_BREAKEVEN", "SL_TRAIL_LOCK", "SL_CERT_LOCK", "TAKE_PROFIT", "EXIT_MONITOR_CLOSE"}


# ── 纯函数 ─────────────────────────────────────────────────────────────────
def is_be(exit_reason: str | None) -> bool:
    return (exit_reason or "") in BE_REASONS


def adjusted_dead_rate(be_share: float, nonbe_dead: float, ref_be_share: float) -> float | None:
    """按参考期 BE 占比标准化的死单率。

    死单只可能出现在非 BE 人群里, 故 raw = (1-be_share) × nonbe_dead。
    标准化 = 把当前的非 BE 死单率套到参考期的 BE 占比上。
    """
    if nonbe_dead is None or ref_be_share is None:
        return None
    if not 0.0 <= ref_be_share <= 1.0:
        return None
    return (1.0 - ref_be_share) * nonbe_dead


def verdict(raw_delta: float, adj_delta: float, fwd_delta: float,
            eps: float = 0.01, eps_fwd_pp: float = 0.05) -> str:
    """raw 变好(<0) 但 adj/fwd 没变好 ⇒ DRIFT; 三者同向变好 ⇒ REAL; 其余 MIXED。

    delta 语义: 后期 - 前期; 死单率越低越好, fwd 越高越好(故 fwd_delta 取反比较)。
    """
    # 死单率: 越低越好 ⇒ 负向 delta 才算变好; fwd 收益: 越高越好 ⇒ 正向 delta 才算变好
    raw_better = raw_delta < -eps
    adj_better = adj_delta < -eps
    fwd_better = fwd_delta > eps_fwd_pp
    if raw_better and not (adj_better or fwd_better):
        return "DRIFT"
    if raw_better and adj_better and fwd_better:
        return "REAL"
    if not raw_better and not adj_better:
        return "NO_CHANGE"
    return "MIXED"


# ── 数据 ───────────────────────────────────────────────────────────────────
def load(source: str) -> list[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    where = "t.trade_date >= '2026-07-01' AND t.realized_pnl IS NOT NULL"
    if source == "DA":
        where += " AND t.decision_id LIKE 'DA-%'"
    rows = [dict(r) for r in conn.execute(
        f"""SELECT t.trade_date, t.mfe_pct, t.exit_reason, t.pnl_net, t.holding_hours,
                   i.fwd_return_intraday, i.fwd_return_t1
            FROM trade_records t
            LEFT JOIN (
                SELECT decision_id,
                       MAX(fwd_return_intraday) AS fwd_return_intraday,
                       MAX(fwd_return_t1)       AS fwd_return_t1
                FROM playbook_instructions
                WHERE row_kind = 'instruction'
                GROUP BY decision_id
            ) i ON i.decision_id = t.decision_id
            WHERE {where}""")]
    conn.close()
    return rows


def period_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    be = [r for r in rows if is_be(r["exit_reason"])]
    nonbe = [r for r in rows if not is_be(r["exit_reason"])]
    dead = [r for r in rows if (r["mfe_pct"] or 0) < DEAD_MFE]
    fwd = [r["fwd_return_intraday"] for r in rows if r["fwd_return_intraday"] is not None]
    return {
        "n": n,
        "be_share": round(len(be) / n, 4),
        "raw_dead": round(len(dead) / n, 4),
        "nonbe_n": len(nonbe),
        "nonbe_dead": round(sum(1 for r in nonbe if (r["mfe_pct"] or 0) < DEAD_MFE) / len(nonbe), 4) if nonbe else None,
        "fwd_mean_pct": round(100 * statistics.mean(fwd), 4) if fwd else None,
        "fwd_pos_share": round(sum(1 for x in fwd if x > 0) / len(fwd), 4) if fwd else None,
        "mfe_median_pct": round(statistics.median(r["mfe_pct"] or 0 for r in rows), 3),
        "hold_median_h": round(statistics.median(r["holding_hours"] or 0 for r in rows), 2),
        "net": round(sum(r["pnl_net"] or 0 for r in rows), 2),
    }


def build_report(source: str, split: str) -> dict:
    rows = load(source)
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        day = r["trade_date"]
        if split == "month":
            key = day[:7]
        elif split == "era":
            key = "7-8月" if day < "2026-09-01" else "9-11月"
        else:
            key = day[:7] + ("-A" if int(day[8:10]) <= 15 else "-B")
        groups[key].append(r)
    periods = {k: period_stats(v) for k, v in sorted(groups.items())}

    # 参考构成 = 全窗 BE 占比(比"第一时段"稳定, 首段 n 常很小)
    keys = sorted(periods)
    tot_n = sum(p["n"] for p in periods.values())
    ref = round(sum(p["be_share"] * p["n"] for p in periods.values()) / tot_n, 4) if tot_n else None
    for k in keys:
        p = periods[k]
        p["adj_dead_at_ref_mix"] = (round(adjusted_dead_rate(p["be_share"], p["nonbe_dead"], ref), 4)
                                    if p.get("nonbe_dead") is not None else None)
    first, last = periods[keys[0]], periods[keys[-1]]
    if split == "era":
        keys = [k for k in keys if periods[k].get("n")]
    v = verdict(last["raw_dead"] - first["raw_dead"],
                (last["adj_dead_at_ref_mix"] or 0) - (first["adj_dead_at_ref_mix"] or 0),
                (last["fwd_mean_pct"] or 0) - (first["fwd_mean_pct"] or 0)) if last["n"] and first["n"] else "NA"
    return {"source": source, "split": split, "ref_be_share": ref, "periods": periods,
            "first": keys[0], "last": keys[-1], "verdict": v}


def print_report(rep: dict) -> None:
    print("== 指标漂移体检 | source=%s split=%s | 参考 BE 占比 %.0f%% ==" % (
        rep["source"], rep["split"], 100 * rep["ref_be_share"]))
    print("%-9s %4s %7s %9s %10s %11s %9s %8s %8s %9s" % (
        "时段", "n", "BE占比", "raw死单", "非BE死单", "同构成adj", "fwd均值%", "fwd正", "mfe中位", "持仓h"))
    for k, p in rep["periods"].items():
        if not p.get("n"):
            continue
        fmt = lambda v, s="{:.1f}": (s.format(v * 100) if isinstance(v, float) else "n/a")
        print("%-9s %4d %6s%% %8s%% %9s%% %10s%% %8s%% %7s%% %8.2f %8.2f" % (
            k, p["n"], fmt(p["be_share"]), fmt(p["raw_dead"]), fmt(p.get("nonbe_dead")),
            fmt(p.get("adj_dead_at_ref_mix")), ("%+.3f" % p["fwd_mean_pct"]) if p.get("fwd_mean_pct") is not None else "n/a",
            fmt(p.get("fwd_pos_share")), p["mfe_median_pct"], p["hold_median_h"]))
    print("\n判定: %s   (%s → %s)" % (rep["verdict"], rep["first"], rep["last"]))
    if rep["verdict"] == "DRIFT":
        print("  ⇒ raw 死单率变好, 但同构成调整或 fwd 未变好 ⇒ **指标漂移, 不是真改善**")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["DA", "ALL"], default="DA")
    ap.add_argument("--split", choices=["month", "half", "era"], default="era")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    rep = build_report(a.source, a.split)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())