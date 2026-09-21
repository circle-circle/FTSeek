#!/usr/bin/env python3
"""异动榜反向轴 (top_movers) 的**可量口径**: 榜单 × 已成交笔 join (2026-09-21 RFC-092b 追记)

背景: `futu_channel_top_movers` 是纯影子 (零消费者, 不进决策), 因此"用它做追高排除/冷却信号"
(用户裁选项①) **无法直接评估**——没有"被它拦下的交易"。本脚本给出可量口径:

  对每笔已结算 simulate 交易, 取**入场时刻之前最后一次榜观测**, 看该股当时在榜第几
  ⇒ 分桶 rank≤10 / 11-50 / 51-100 / 入场时无榜观测, 结局用 **pnl_net (实盘口径)**。

它回答: "若把榜前 10 当冷却信号 (别碰), 实盘会躲掉什么 / 误杀什么", 并与 kr3 追高门
重叠度对照 (同轴声明, 防两条影子独立裁决同假设)。

对齐口径 (三处时间必须同轴):
  · 快照 `ts` 是 CST naive → ET naive = ts − 12h (EDT; 观测窗 8/29-9/19 全在夏令时);
  · `trade_records.entry_time` 是 ET aware (`...+09:37:51-04:00`) → 直接转 naive ET;
  · 交易所属会话日 = `trade_date` (实测 786/864 同日, 跨日 78 笔多为延长时段)。

纯读, 只写自己的 latest.json。无推送/无 DB 写/无 cron 改动。
用法: python scripts/top_movers_trade_join.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
KR3 = REPO / "data" / "shadow" / "kr3_direction_runup_gate.jsonl"
OUT = REPO / "data" / "shadow" / "top_movers_trade_join_latest.json"
MIN_N = 30
BUCKETS = ("rank<=10", "rank 11-50", "rank 51-100", "入场时无榜观测")


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("tme", REPO / "scripts" / "top_movers_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _et_naive(snapshot_ts: str) -> Optional[dt.datetime]:
    """快照 CST naive → ET naive (EDT = −12h)。"""
    try:
        return dt.datetime.fromisoformat(snapshot_ts) - dt.timedelta(hours=12)
    except Exception:
        return None


def _entry_et_naive(entry_time: str) -> Optional[dt.datetime]:
    """交易 entry_time (ET aware) → naive ET。"""
    try:
        return dt.datetime.fromisoformat(entry_time).replace(tzinfo=None)
    except Exception:
        return None


def load_trades(db: Path = DB) -> List[Dict]:
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "select id as trade_id, trade_date, symbol, direction, entry_time, pnl_net "
            "from trade_records where trade_mode='simulate' and pnl_net is not null"
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        e = _entry_et_naive(r["entry_time"] or "")
        if e is None:
            continue
        out.append({"trade_id": r["trade_id"], "day": str(r["trade_date"])[:10],
                    "symbol": r["symbol"], "direction": r["direction"],
                    "entry_et": e, "pnl_net": float(r["pnl_net"])})
    return out


def _t_mde(vals: List[float]) -> Dict:
    n = len(vals)
    if n < 2:
        return {"n_days": n, "mean": None, "t": None, "mde_80pct": None, "distinguishable": False}
    m = sum(vals) / n
    var = sum((x - m) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n)
    return {"n_days": n, "mean": round(m, 2), "t": round(m / se, 3) if se > 0 else None,
            "mde_80pct": round(2.8 * se, 2),
            "distinguishable": bool(se > 0 and abs(m) > 2.8 * se)}


def evaluate() -> Dict:
    tme = _load_eval_module()
    panel = tme.load_panel(tme.SRC)          # {et_day: {symbol: [(ts_cst, cp, rank)]}}
    trades = load_trades()

    buckets: Dict[str, List[Dict]] = defaultdict(list)
    unmatched_day = 0
    for t in trades:
        day = panel.get(t["day"])
        if not day:
            unmatched_day += 1
            continue
        obs = day.get(t["symbol"]) or []
        preds = [o for o in obs if (_et_naive(o[0]) or dt.datetime.max) <= t["entry_et"]]
        if not preds:
            b = "入场时无榜观测"
            t2 = dict(t, rank=None, cp=None)
        else:
            last = max(preds, key=lambda o: o[0])
            t2 = dict(t, rank=last[2], cp=last[1])
            b = ("rank<=10" if last[2] <= 10 else
                 ("rank 11-50" if last[2] <= 50 else "rank 51-100"))
        buckets[b].append(t2)

    res_buckets = {}
    for b in BUCKETS:
        rows = buckets.get(b) or []
        if not rows:
            res_buckets[b] = {"n": 0}
            continue
        by_day: Dict[str, List[float]] = defaultdict(list)
        for r in rows:
            by_day[r["day"]].append(r["pnl_net"])
        per_day = [sum(v) / len(v) for v in by_day.values()]
        wins = sum(1 for r in rows if r["pnl_net"] > 0)
        res_buckets[b] = {
            "n": len(rows),
            "mean_net": round(sum(r["pnl_net"] for r in rows) / len(rows), 2),
            "sum_net": round(sum(r["pnl_net"] for r in rows), 2),
            "wr": round(wins / len(rows), 3),
            "win_sum": round(sum(r["pnl_net"] for r in rows if r["pnl_net"] > 0), 2),
            "loss_sum": round(sum(r["pnl_net"] for r in rows if r["pnl_net"] <= 0), 2),
            "day_clustered": _t_mde(per_day),
            "verdict": "INSUFFICIENT_N" if len(rows) < MIN_N else "REPORT_ONLY",
        }

    # 反事实: 若把"榜前 10"当冷却信号 → 躲掉的笔与其误杀
    top10 = buckets.get("rank<=10") or []
    others = [r for b, rows in buckets.items() if b != "rank<=10" for r in rows]
    cf = {
        "avoided_n": len(top10),
        "avoided_sum": round(sum(r["pnl_net"] for r in top10), 2),
        "avoided_wins_sum": round(sum(r["pnl_net"] for r in top10 if r["pnl_net"] > 0), 2),
        "avoided_losses_sum": round(sum(r["pnl_net"] for r in top10 if r["pnl_net"] <= 0), 2),
        "kept_sum": round(sum(r["pnl_net"] for r in others), 2),
        "day_clustered_diff_top10_vs_rest": None,
    }
    if top10 and others:
        by_day = defaultdict(lambda: {"t": [], "o": []})
        for r in top10:
            by_day[r["day"]]["t"].append(r["pnl_net"])
        for r in others:
            by_day[r["day"]]["o"].append(r["pnl_net"])
        diffs = [sum(v["t"]) / len(v["t"]) - sum(v["o"]) / len(v["o"])
                 for v in by_day.values() if v["t"] and v["o"]]
        cf["day_clustered_diff_top10_vs_rest"] = _t_mde(diffs)

    # 同轴重叠: 与 kr3 追高门 (would_block) 的重叠
    overlap = {"n_kr3_lines": 0, "n_kr3_would_block": 0}
    if KR3.exists():
        wb = set()
        for line in KR3.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            overlap["n_kr3_lines"] += 1
            if r.get("would_block"):
                wb.add(r.get("trade_id"))
        overlap["n_kr3_would_block"] = len(wb)
        top10_ids = {r["trade_id"] for r in top10}
        wb_all = set()
        for line in KR3.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    wb_all.add(json.loads(line).get("trade_id"))
                except Exception:
                    pass
        overlap["n_kr3_trades"] = len(wb_all)
        overlap["n_top10_within_kr3_coverage"] = len(top10_ids & wb_all)
        overlap["n_intersection_top10_x_kr3_would_block"] = len(top10_ids & wb)
        overlap["note"] = ("重叠小 ⇒ 两轴测的不是同一批笔 (可各自裁决); 重叠大 ⇒ 同假设双影子, "
                           "必须互指 cross_ref 或合并 (否则双转正打架)")
    return {
        "n_trades_joined": sum(len(v) for v in buckets.values()),
        "n_trades_total": len(trades),
        "n_unmatched_day": unmatched_day,
        "buckets": res_buckets,
        "counterfactual_cool_top10": cf,
        "kr3_overlap": overlap,
        "verdict": ("INSUFFICIENT_N" if len(top10) < MIN_N else "REPORT_ONLY"),
        "caliber": ("入场时刻之前最后一次榜观测的 rank; 结局=pnl_net(实盘口径); "
                    "统计=按 ET 日聚类 + MDE(80%); 快照 CST−12h→ET (EDT)"),
        "known_bias": ("①榜覆盖仅 8/29-9/19 (18 会话) ⇒ join 面窄; ②'入场时无榜观测'含延长时段入场与"
                       "榜内未收录标的, 非同质对照组; ③榜单是实时榜, 入场前观测可能比入场早数分钟"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="异动榜反向轴 × 已成交笔 join (record-only)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    res = evaluate()
    res["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    res["n"] = res["n_trades_joined"]
    print("═══ 异动榜反向轴 × 已成交笔 (选项① 冷却信号的可量口径) ═══")
    print("join 上 %d / %d 笔 (会话面无榜 %d)" % (res["n_trades_joined"], res["n_trades_total"],
                                                 res["n_unmatched_day"]))
    print("分桶                   n   笔均     累计    WR     日聚类t  MDE")
    for b in BUCKETS:
        x = res["buckets"].get(b) or {}
        if not x.get("n"):
            print("%-20s   0" % b)
            continue
        dc = x["day_clustered"]
        print("%-20s %4d  %8.2f %8.2f  %.3f  %7s  %7s" % (
            b, x["n"], x["mean_net"], x["sum_net"], x["wr"], dc.get("t"), dc.get("mde_80pct")))
    cf = res["counterfactual_cool_top10"]
    print("若冷却'榜前10': 躲掉 %d 笔 Σ%+.2f (其中盈利笔 Σ%+.2f / 亏损笔 Σ%+.2f) | 留 %+.2f"
          % (cf["avoided_n"], cf["avoided_sum"], cf["avoided_wins_sum"],
             cf["avoided_losses_sum"], cf["kept_sum"]))
    d = cf.get("day_clustered_diff_top10_vs_rest") or {}
    print("  日聚类差 (top10 − 其余): %s  t=%s  MDE=%s" % (d.get("mean"), d.get("t"), d.get("mde_80pct")))
    ov = res["kr3_overlap"]
    print("kr3 重叠: kr3 行 %d (would_block %d) | top10 落在 kr3 覆盖内 %s 笔 | 交集 %s 笔"
          % (ov.get("n_kr3_lines", 0), ov.get("n_kr3_would_block", 0),
             ov.get("n_top10_within_kr3_coverage"), ov.get("n_intersection_top10_x_kr3_would_block")))
    print("裁决: %s" % res["verdict"])
    print("偏差: %s" % res["known_bias"])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→ %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())