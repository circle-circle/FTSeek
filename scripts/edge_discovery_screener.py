#!/usr/bin/env python3
"""高频模拟流「真实可用 edge」发现筛（record-only，零交易行为变更）。

用途（用户 2026-11-14「在每日高频交易找到真正交易可用的策略和逻辑」）：
  把每日高频模拟成交按**事前可识别**特征切片，筛出**稳健、可交易**的正期望子集。
  核心纪律=反过拟合：切片只是「假设生成」，不是「证明」；任何子集要称「可用」必须前瞻确认。

只读承诺：sqlite `mode=ro`，不写库/不下单/不改生产；默认仅控制台，`--json PATH` 才写产物。

稳健门（同时满足才标 ROBUST_CANDIDATE，缺一即「探索性」）：
  n≥30 ∧ 日聚类 t≥3.0（Harvey-Liu-Zhu 多重检验门槛）∧ 留一日 t 全>0 ∧ 前后半段同号
  ∧ top3 赢家集中度<70% ∧ ≥2 个月同号。门内仍属探索，转正须前瞻预注册（另立 RFC）。

口径：A9 干净；bps=pnl_net/(entry_price×qty)×1e4（含费）；日聚类 Z80=1.28155/MDE80=2.802×se。
样本分层（2026-11-14 修尺子）：收养/补录记账行（regime 硬编码 RANGE、MFE/ATR 为收养时
  近似值）会造出虚假切片，故从发现样本中剔除并单列计数；EXT 盘前切片不受影响（补录行
  全属 playbook_sim）。详见 scripts/utils/da_sample.py。
用法：PYTHONPATH=. venv/bin/python scripts/edge_discovery_screener.py --start 2026-10-06
"""
import argparse
import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

from scripts.utils.da_sample import split_signal_artifact

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
Z80 = 1.2815515655446004
MDE_Z = 2.802
T_HURDLE = 3.0
MIN_N = 30
TOP3_MAX = 0.70


def _connect():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load(conn, start):
    return [dict(r) for r in conn.execute(
        """
        SELECT symbol, trade_date, entry_time, entry_price, quantity, pnl_net, realized_pnl,
               fees_est, mfe_pct, owner_strategy, entry_session, market_regime, direction,
               exit_reason, metadata_json, entry_reason
          FROM trade_records
         WHERE source_type='PLAYBOOK_SIM' AND trade_date>=? AND exit_price>0
           AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
           AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''
        """, (start,)).fetchall()]


def _net(row):
    if row["pnl_net"] is not None:
        return row["pnl_net"]
    # realized_pnl 是毛盈亏；回退必须扣费，不能把毛额当净额（项目净 PnL 约定）。
    return (row["realized_pnl"] or 0.0) - (row.get("fees_est") or 0.0)


def _notional(row):
    return (row["entry_price"] or 0.0) * (row["quantity"] or 0)


def _bps(row):
    denom = _notional(row)
    return _net(row) / denom * 1e4 if denom else 0.0


def _meta(row, key):
    try:
        return json.loads(row["metadata_json"] or "{}").get(key)
    except Exception:
        return None


def _cluster_t(values_by_day):
    daily = [sum(v) / len(v) for v in values_by_day.values() if v]
    if len(daily) < 2 or st.stdev(daily) == 0:
        return None
    mean = sum(daily) / len(daily)
    return mean / (st.stdev(daily) / len(daily) ** 0.5)


def _by_day(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["trade_date"]].append(_bps(row))
    return grouped


def slice_metrics(rows):
    n = len(rows)
    notional = sum(_notional(r) for r in rows)
    net = sum(_net(r) for r in rows)
    by_day = _by_day(rows)
    daily = [sum(v) / len(v) for v in by_day.values()]
    day_t = _cluster_t(by_day)
    se = (st.stdev(daily) / len(daily) ** 0.5) if len(daily) >= 2 else None
    loo = []
    for day in by_day:
        sub = {d: v for d, v in by_day.items() if d != day}
        t = _cluster_t(sub)
        if t is not None:
            loo.append(t)
    ordered = sorted(rows, key=lambda r: r["trade_date"])
    half = len(ordered) // 2
    half_t = (_cluster_t(_by_day(ordered[:half])), _cluster_t(_by_day(ordered[half:])))
    top3 = sum(_net(r) for r in sorted(rows, key=lambda r: -_net(r))[:3])
    top3_share = (top3 / net) if net > 0 else None
    by_month = defaultdict(list)
    for row in rows:
        by_month[row["trade_date"][:7]].append(_net(row))
    month_signs = [1 if sum(v) > 0 else -1 for v in by_month.values()]
    months_same = len(month_signs) >= 2 and (all(s > 0 for s in month_signs) or all(s < 0 for s in month_signs))
    halves_same = (half_t[0] is not None and half_t[1] is not None
                   and (half_t[0] > 0) == (half_t[1] > 0))
    robust = (n >= MIN_N and day_t is not None and day_t >= T_HURDLE
              and loo and min(loo) > 0 and halves_same
              and top3_share is not None and top3_share < TOP3_MAX and months_same)
    return {
        "n": n, "net_usd": round(net, 2),
        "net_bps": round(net / notional * 1e4, 2) if notional else None,
        "win_rate_pct": round(100 * sum(1 for r in rows if _net(r) > 0) / n, 1) if n else None,
        "days": len(by_day), "day_t": round(day_t, 2) if day_t is not None else None,
        "ci80": [round(sum(daily) / len(daily) - Z80 * se, 2), round(sum(daily) / len(daily) + Z80 * se, 2)] if se else None,
        "mde80": round(MDE_Z * se, 2) if se else None,
        "loo_min_t": round(min(loo), 2) if loo else None,
        "half_t": [round(t, 2) if t is not None else None for t in half_t],
        "top3_share_pct": round(100 * top3_share, 1) if top3_share is not None else None,
        "months_same_sign": months_same,
        "robust_candidate": bool(robust),
    }


SLICES = [
    ("owner_strategy", lambda r: r["owner_strategy"]),
    ("entry_session", lambda r: r["entry_session"] or "blank"),
    ("market_regime", lambda r: r["market_regime"]),
    ("direction", lambda r: r["direction"]),
    ("session_x_source", lambda r: f"{r['entry_session'] or 'blank'}|{r['owner_strategy']}"),
    ("ext_research_source", lambda r: _meta(r, "research_source") if r["owner_strategy"] == "extended_hours_entry" else None),
    ("ext_entry_grade", lambda r: _meta(r, "entry_grade") if r["owner_strategy"] == "extended_hours_entry" else None),
    ("ext_gate_mode", lambda r: _meta(r, "gate_mode") if r["owner_strategy"] == "extended_hours_entry" else None),
]


def screen(rows):
    results = []
    for name, keyfn in SLICES:
        grouped = defaultdict(list)
        for row in rows:
            key = keyfn(row)
            if key is not None:
                grouped[str(key)].append(row)
        for key, subset in grouped.items():
            metrics = slice_metrics(subset)
            metrics["slice"] = name
            metrics["value"] = key
            results.append(metrics)
    return results


def build_report(rows):
    """剔除收养/补录记账行后再切片（其特征为收养时近似值，会造虚假切片）。"""
    signal, artifact = split_signal_artifact(rows)
    results = screen(signal)
    return {
        "results": results, "n_signal": len(signal),
        "n_artifact_excluded": len(artifact), "slices_tested": len(results),
        "robust": [r for r in results if r["robust_candidate"]],
    }


def main():
    parser = argparse.ArgumentParser(description="高频模拟流 edge 发现筛（反过拟合）")
    parser.add_argument("--start", default="2026-10-06")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()
    rows = _load(_connect(), args.start)
    rep = build_report(rows)
    results, tested, robust = rep["results"], rep["slices_tested"], rep["robust"]
    print("== edge discovery screener | start=%s | 真实信号 n=%d (剔除收养/补录 %d) | slices_tested=%d ==" % (
        args.start, rep["n_signal"], rep["n_artifact_excluded"], tested))
    print("多重检验提示：测了 %d 切片，t≥%.1f 门槛(Harvey-Liu-Zhu)；ROBUST 仍属探索，转正须前瞻预注册。" % (tested, T_HURDLE))
    for r in sorted(results, key=lambda x: -(x["day_t"] or -99)):
        flag = "★ROBUST" if r["robust_candidate"] else "exploratory"
        print("  %-18s %-26s n=%3d bps=%7.2f t=%5s loo=%5s top3=%5s%% %s" % (
            r["slice"], r["value"][:26], r["n"], r["net_bps"] or 0, str(r["day_t"]),
            str(r["loo_min_t"]), str(r["top3_share_pct"]), flag))
    print("ROBUST_CANDIDATES: %s" % [(r["slice"], r["value"]) for r in robust])
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"start": args.start, "n": rep["n_signal"], "n_artifact_excluded": rep["n_artifact_excluded"],
             "slices_tested": tested, "results": results},
            ensure_ascii=False, indent=2))
        print("wrote", args.json)


if __name__ == "__main__":
    main()
