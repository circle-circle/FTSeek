#!/usr/bin/env python3
"""A2 计划质量报告 plan-vs-actual (symbol-profile DEB P1, 只读)。

问题: "盘前预测 vs 盘后实际"的计划级误差从未被系统性测量。
本脚本把 playbook_instructions 的计划几何 (entry_zone/SL/TP/昨收锚) 与
当日真实分钟路径 (data/replay_bars/) 对账, 产出每日可量化指标:

  1. band_reach        计划带触达率: 当日路径是否进入 [entry_low, entry_high]
  2. fake_reach        假触达率: 仅触达一次且触后 5min 内反向走 >0.2% 不再回带
  3. mid_dev           带中点 vs 开盘 30min 最优成交价偏差 (方向调整, %)
  4. sl_margin         SL 距当日极值余量 (+ = SL 日内未被触及空间)
     tp_headroom       TP 距日内极值余量 (+ = 日内最高/低越过 TP, TP 可达)
  5. gap_semantics     隔夜 gap 分桶 × 日内回补 × 计划带是否仍以昨收为锚
                       (A3 解冻的因变量; 顺带观测 A1 重锚需求强度)
  6. execution_join    decision_id 串联 trade_records 实盘结局 (pnl_net)

安全: 只读 DB + 只读 bar 缓存; 落 data/plan_quality_latest.json (报告家族同构)。
用法: PYTHONPATH=. ./venv/bin/python scripts/plan_vs_actual_report.py [--since 2026-06-14] [--no-json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
RB_DIR = REPO / "data" / "replay_bars"          # 分钟 bar: US_{SYM}_{DATE}.json
DAILY_DIR = REPO / "data" / "replay_bars_daily"  # 日 K: US_{SYM}.json (prev_close)
OUT = REPO / "data" / "plan_quality_latest.json"

ENTRY_WINDOW_MIN = 30      # 最优成交价窗口: 开盘后 30min
FAKE_TOUCH_MIN = 5         # 假触达判定窗
FAKE_TOUCH_PCT = 0.2       # 触后反向幅度阈值 %


def load_bars(sym: str, date: str) -> list | None:
    """分钟 bar [ts,o,h,l,c] 升序; 无缓存返回 None (不联网补拉, 只计数)。"""
    fp = RB_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    out = []
    for r in rows:
        try:
            out.append((datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S"), r[1], r[2], r[3], r[4]))
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


def prev_close(sym: str, date: str) -> float | None:
    fp = DAILY_DIR / f"{sym.replace('.', '_')}.json"
    if not fp.exists():
        return None
    try:
        rows = json.loads(fp.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    px = None
    for r in rows:
        if r[0] < date and r[4]:
            px = r[4]
        elif r[0] >= date:
            break
    return px


def analyze_one(inst: dict, bars: list, pc: float) -> dict:
    """单条指令的计划 vs 实际对账。bars 为当日分钟 bar, pc 为昨收。"""
    d = inst["direction"]
    is_buy = d == "BUY"
    day_open = bars[0][1]
    day_low = min(b[3] for b in bars)
    day_high = max(b[2] for b in bars)
    gap_pct = (day_open / pc - 1) * 100

    out = {"gap_pct": round(gap_pct, 3), "day_low": day_low, "day_high": day_high}

    # ── 5. gap 语义 (所有指令可测, 不依赖 entry_zone) ──
    agap = abs(gap_pct)
    out["gap_bucket"] = "big" if agap >= 1.5 else ("mid" if agap >= 0.5 else ("small" if agap >= 0.05 else "flat"))
    out["gap_dir"] = "up" if gap_pct > 0 else ("dn" if gap_pct < 0 else "flat")
    out["gap_filled"] = (day_low <= pc) if gap_pct > 0 else ((day_high >= pc) if gap_pct < 0 else None)
    anchor = inst.get("entry_price")
    if inst.get("entry_low") is not None and inst.get("entry_high") is not None:
        # 计划带仍跨昨收 = 锚未随跳空移动 (等回踩语义); 否则已偏移
        out["band_straddles_prev_close"] = inst["entry_low"] <= pc <= inst["entry_high"]
    elif anchor:
        out["band_straddles_prev_close"] = None  # 点锚无带, 语义不可判

    # 带几何指标需要 entry_zone
    if inst.get("entry_low") is None or inst.get("entry_high") is None:
        return out
    lo, hi = inst["entry_low"], inst["entry_high"]
    mid = (lo + hi) / 2

    # ── 1. 带触达: 存在与 [lo,hi] 相交的 bar (bar_high>=lo 且 bar_low<=hi) ──
    reach = any(b[2] >= lo and b[3] <= hi for b in bars)
    out["band_reached"] = bool(reach)
    if not reach:
        out["miss_margin_pct"] = round(min(abs(b[3] - hi) for b in bars) / pc * 100, 3) if is_buy \
            else round(min(abs(b[2] - lo) for b in bars) / pc * 100, 3)
        return out

    # ── 2. 假触达: 首次触带后 FAKE_TOUCH_MIN 根内反向 >阈值, 且全天不再回带 ──
    touch_idx = None
    for i, b in enumerate(bars):
        if b[2] >= lo and b[3] <= hi:
            touch_idx = i
            break
    if touch_idx is not None:
        after = bars[touch_idx + 1:]
        re_touch = any(b[2] >= lo and b[3] <= hi for b in after)
        win = after[:FAKE_TOUCH_MIN]
        adverse = 0.0
        for b in win:
            adv = (b[4] / bars[touch_idx][4] - 1) * (-100 if is_buy else 100)
            adverse = max(adverse, adv)
        out["fake_reach"] = bool((not re_touch) and adverse > FAKE_TOUCH_PCT)
        out["post_touch_adverse_pct"] = round(adverse, 3)

    # ── 3. 带中点 vs 开盘 30min 最优成交价 ──
    win = bars[:ENTRY_WINDOW_MIN]  # 开盘后前 30 根
    opt = min(b[3] for b in win) if is_buy else max(b[2] for b in win)
    out["optimal_entry_30m"] = opt
    out["mid_dev_pct"] = round((mid - opt) / pc * 100 * (1 if is_buy else -1), 3)
    # 带内是否有可成交价 (开盘 30min 内路径与带相交)
    out["band_fillable_30m"] = bool(any((b[3] <= hi) if is_buy else (b[2] >= lo) for b in win))

    # ── 4. SL/TP 几何 ──
    if inst.get("stop_loss"):
        out["sl_margin_pct"] = round((inst["stop_loss"] - day_low) / mid * 100, 3) if is_buy \
            else round((day_high - inst["stop_loss"]) / mid * 100, 3)
    if inst.get("take_profit"):
        out["tp_headroom_pct"] = round((day_high - inst["take_profit"]) / mid * 100, 3) if is_buy \
            else round((inst["take_profit"] - day_low) / mid * 100, 3)
    return out


def _agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "mean": round(st.mean(vals), 3), "median": round(st.median(vals), 3)}


def main() -> int:
    ap = argparse.ArgumentParser(description="A2 plan-vs-actual 质量报告 (只读)")
    ap.add_argument("--since", default="2026-06-14")
    ap.add_argument("--no-json", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    insts = [dict(r) for r in c.execute(
        "SELECT id, date, symbol, direction, entry_price, entry_low, entry_high, "
        "stop_loss, take_profit, skip_reason, triggered, executed, decision_id, confidence "
        "FROM playbook_instructions WHERE date >= ? AND entry_price > 0 ORDER BY date",
        (args.since,)).fetchall()]
    trades = {r["decision_id"]: dict(r) for r in c.execute(
        "SELECT decision_id, COALESCE(pnl_net, realized_pnl) pnl, exit_reason "
        "FROM trade_records WHERE decision_id != ''")}
    c.close()

    n = len(insts)
    no_bars = no_pc = 0
    rows = []
    for inst in insts:
        bars = load_bars(inst["symbol"], inst["date"])
        if not bars:
            no_bars += 1
            continue
        pc = prev_close(inst["symbol"], inst["date"])
        if not pc:
            no_pc += 1
            continue
        m = analyze_one(inst, bars, pc)
        m.update({"id": inst["id"], "date": inst["date"], "symbol": inst["symbol"],
                  "direction": inst["direction"], "skip_reason": inst["skip_reason"],
                  "triggered": inst["triggered"], "executed": inst["executed"],
                  "has_zone": inst["entry_low"] is not None})
        # 6. 执行结局串联
        tr = trades.get(inst["decision_id"] or "")
        if tr:
            m["exec_pnl"] = tr["pnl"]
            m["exec_exit_reason"] = tr["exit_reason"]
        rows.append(m)

    active = [r for r in rows if not r["skip_reason"]]
    zoned = [r for r in active if r["has_zone"]]

    # ── 聚合 ──
    reach = [r["band_reached"] for r in zoned if r.get("band_reached") is not None]
    fake = [r["fake_reach"] for r in zoned if r.get("fake_reach") is not None]
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": args.since,
        "coverage": {"instructions": n, "with_bars": len(rows), "no_bars": no_bars,
                     "no_prev_close": no_pc, "active": len(active), "with_zone": len(zoned)},
        "band_reach_rate": round(sum(reach) / len(reach), 3) if reach else None,
        "fake_reach_rate": round(sum(fake) / len(fake), 3) if fake else None,
        "mid_dev_pct": _agg([r.get("mid_dev_pct") for r in zoned]),
        "band_fillable_30m_rate": round(sum(1 for r in zoned if r.get("band_fillable_30m")) /
                                        len([r for r in zoned if r.get("band_fillable_30m") is not None]), 3)
                                  if any(r.get("band_fillable_30m") is not None for r in zoned) else None,
        "sl_margin_pct": _agg([r.get("sl_margin_pct") for r in zoned]),
        "tp_headroom_pct": _agg([r.get("tp_headroom_pct") for r in zoned]),
    }

    # ── gap 语义表: bucket × filled × band_straddles_prev_close ──
    gap_table = {}
    for r in active:
        b = r["gap_bucket"]
        t = gap_table.setdefault(b, {"n": 0, "filled": 0, "straddle": 0, "straddle_n": 0})
        t["n"] += 1
        if r.get("gap_filled"):
            t["filled"] += 1
        if r.get("band_straddles_prev_close") is not None:
            t["straddle_n"] += 1
            if r["band_straddles_prev_close"]:
                t["straddle"] += 1
    for t in gap_table.values():
        t["fill_rate"] = round(t["filled"] / t["n"], 3) if t["n"] else None
        t["straddle_rate"] = round(t["straddle"] / t["straddle_n"], 3) if t["straddle_n"] else None
    summary["gap_semantics"] = dict(sorted(gap_table.items()))

    # ── 执行结局串联 ──
    jn = [r for r in rows if "exec_pnl" in r]
    summary["execution_join"] = {
        "n": len(jn),
        "pnl_sum": round(sum(r["exec_pnl"] or 0 for r in jn), 2) if jn else None,
        "win_rate": round(sum(1 for r in jn if (r["exec_pnl"] or 0) > 0) / len(jn), 3) if jn else None,
        "reached_band_win_rate": round(sum(1 for r in jn if r.get("band_reached") and (r["exec_pnl"] or 0) > 0) /
                                       len([r for r in jn if r.get("band_reached")]), 3)
        if any(r.get("band_reached") for r in jn) else None,
    }

    # ── 近 10 日趋势 ──
    by_day = defaultdict(list)
    for r in zoned:
        by_day[r["date"]].append(r)
    summary["daily_trend"] = [
        {"date": d, "n": len(v),
         "reach_rate": round(sum(1 for r in v if r.get("band_reached")) / len(v), 3),
         "mid_dev_median": round(st.median([r["mid_dev_pct"] for r in v if r.get("mid_dev_pct") is not None]), 3)
         if any(r.get("mid_dev_pct") is not None for r in v) else None}
        for d, v in sorted(by_day.items())[-10:]]

    # ── 打印 ──
    cov = summary["coverage"]
    print(f"\n{'=' * 66}")
    print(f"A2 plan-vs-actual 质量报告  (since {args.since})")
    print(f"  覆盖: 指令 {cov['instructions']} | 有分钟bar {cov['with_bars']} | 无bar {cov['no_bars']} | "
          f"活跃(非skip) {cov['active']} | 带zone {cov['with_zone']}")
    print(f"  计划带触达率 {summary['band_reach_rate']} | 假触达率 {summary['fake_reach_rate']} | "
          f"开盘30min带内可成交率 {summary['band_fillable_30m_rate']}")
    md = summary["mid_dev_pct"]
    if md:
        print(f"  带中点偏差(中位) {md['median']}% (均值 {md['mean']}%, n={md['n']}) — 正=带偏劣于最优价")
    for k in ("sl_margin_pct", "tp_headroom_pct"):
        v = summary[k]
        if v:
            print(f"  {k}: 中位 {v['median']}% 均值 {v['mean']}% (n={v['n']})")
    print(f"  ── gap 语义表 (bucket: n / 回补率 / 带仍跨昨收率)")
    for b, t in summary["gap_semantics"].items():
        print(f"    {b:<6} n={t['n']:<4} fill={t['fill_rate']} straddle_prev_close={t['straddle_rate']}(n={t['straddle_n']})")
    ej = summary["execution_join"]
    print(f"  ── 执行串联: n={ej['n']} pnl_sum={ej['pnl_sum']} 胜率={ej['win_rate']} 触带单胜率={ej['reached_band_win_rate']}")
    print(f"  ── 近 10 日趋势 (date n reach mid_dev中位)")
    for d in summary["daily_trend"]:
        print(f"    {d['date']} n={d['n']:<3} reach={d['reach_rate']} mid_dev={d['mid_dev_median']}")

    if not args.no_json:
        OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1))
        print(f"\n  → {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
