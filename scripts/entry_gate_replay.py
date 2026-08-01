#!/usr/bin/env python3
"""入场门/入场时点回放 L2 — "路径2" (2026-07-26, 用户批"构建回测体系"). 只读, 零 live 影响。

背景: exit_replay (路径1) 边界声明"三门/入场门不可回放属路径2" — 本脚本补上。
数据集 = playbook_instructions 全部方向指令 (含未成交的, 消入场选择偏差) × 当日 1min bar。

回放两件事:
  A. ATR 门网格: lo2.5(live)/lo2.8/lo3.0/hi4.0/lo3.0_hi4.0 — 每档拦掉哪些指令,
     拦掉组 vs 放行组的 bar 级结果对比 (拦掉组均值<0 = 拦对)。
     指令 atr_pct 缺失 (07-24 前未落库) → 用日K缓存回算 ATR14, 只取指令日前一交易日值 (无前视)。
  B. 入场时点变体: 09:35/09:45/10:00/10:30/11:00 ET bar 收盘价进场 → EOD 收盘价出场,
     方向调整收益% + MFE/MAE。变体间成交假设一致 (bar close, 无滑点) → 可比。

诚实边界:
  - LLM 信号生成端不可历史回测 (无新闻快照/prompt漂移/前视), 本工具只回放"给定信号后的门与时点"。
  - VWAP 门/量能门需带量 bar (现缓存无 volume 字段), 列 v2。
  - 结果口径 = 无退出规则的裸持有到收盘 — 度量信号+时点质量, 不含退出工程 (那是路径1的活)。

用法: PYTHONPATH=. ./venv/bin/python scripts/entry_gate_replay.py [--since 2026-06-14]
落盘: data/entry_gate_replay_latest.json (bar 缓存复用 data/replay_bars/ + data/replay_bars_daily/)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from statistics import median

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import exit_replay_backtest as erb  # noqa: E402  (fetch_bars + 共享 1min 缓存)
import historical_robustness_check as hrc  # noqa: E402  (fetch_daily/atr14 + 共享日K缓存)

from app.utils.timezone import ET  # noqa: E402

DB_PATH = REPO / "data" / "ftseek_us.db"
OUT_PATH = REPO / "data" / "entry_gate_replay_latest.json"

TIMING_VARIANTS = ["09:35", "09:45", "10:00", "10:30", "11:00"]
GATE_REF_TIMING = "10:00"  # 门评分的结果参照时点 (DA 晨间触发与 topup 盘中窗的折中, 假设显式声明)
GATES = [
    ("live_lo2.5", 2.5, None),
    ("lo2.8", 2.8, None),
    ("lo3.0", 3.0, None),
    ("hi4.0", 2.5, 4.0),
    ("lo3.0_hi4.0", 3.0, 4.0),
]


def load_instructions(since: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, symbol, direction, atr_pct, market_regime, executed, "
        "fwd_return_intraday, fwd_return_t1, raw_instruction_json FROM playbook_instructions "
        "WHERE direction IN ('BUY','SELL_SHORT') AND date >= ? ORDER BY date", (since,)).fetchall()
    out = []
    for r in rows:
        try:
            src = (json.loads(r["raw_instruction_json"] or "{}").get("source")) or "unknown"
        except Exception:
            src = "unknown"
        fwd = r["fwd_return_intraday"] if r["fwd_return_intraday"] is not None else r["fwd_return_t1"]
        # DB 存 fraction (0.038=3.8%), 门阈值用 percentage (2.5/3.0/4.0) — 统一乘 100
        # 回算补齐源 (atr14_pct_series) 本就返回 percentage, 无需再乘
        raw_atr = r["atr_pct"]
        out.append({"date": r["date"], "symbol": r["symbol"], "direction": r["direction"],
                    "atr_pct": (float(raw_atr) * 100) if raw_atr is not None else None,
                    "regime": r["market_regime"] or "",
                    "executed": bool(r["executed"]), "source": src,
                    "fwd_signed": (float(fwd) if r["direction"] == "BUY" else -float(fwd))
                    if fwd is not None else None})
    conn.close()
    return out


def backfill_atr(instrs: list[dict], ctx) -> int:
    """atr_pct 缺失 → 日K缓存回算 ATR14, 取指令日前最近一个交易日 (无前视). 返回补齐数."""
    need_syms = sorted({i["symbol"] for i in instrs if not i["atr_pct"]})
    atr_by_sym: dict[str, dict[str, float]] = {}
    start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    for sym in need_syms:
        rows = hrc.fetch_daily(ctx, sym, start, end)
        if rows:
            atr_by_sym[sym] = hrc.atr14_pct_series(rows)
    filled = 0
    for ins in instrs:
        if ins["atr_pct"]:
            continue
        series = atr_by_sym.get(ins["symbol"], {})
        prior = [d for d in series if d < ins["date"]]  # 严格早于指令日
        if prior:
            ins["atr_pct"] = series[max(prior)]
            ins["atr_backfilled"] = True
            filled += 1
    return filled


def replay_timing(ins: dict, bars: list[dict]) -> dict[str, dict]:
    """各时点变体: entry=首个 t>=变体时刻 的 bar close, exit=EOD close. 方向调整%."""
    sign = 1 if ins["direction"] == "BUY" else -1
    eod = bars[-1]["c"]
    out = {}
    for tv in TIMING_VARIANTS:
        hh, mm = int(tv[:2]), int(tv[3:])
        entry_bar = next((b for b in bars if b["t"].timetz() >= dtime(hh, mm, tzinfo=b["t"].tzinfo)), None)
        if entry_bar is None or entry_bar["c"] <= 0:
            continue
        ep = entry_bar["c"]
        tail = [b for b in bars if b["t"] > entry_bar["t"]]
        if not tail:
            continue
        favor = max((sign * (b["h"] if sign > 0 else b["l"]) - sign * ep) / ep for b in tail) * 100
        adverse = min((sign * (b["l"] if sign > 0 else b["h"]) - sign * ep) / ep for b in tail) * 100
        out[tv] = {"ret": sign * (eod - ep) / ep * 100, "mfe": max(favor, 0.0), "mae": min(adverse, 0.0)}
    return out


def _agg(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "avg": round(sum(vals) / len(vals), 4),
            "median": round(median(vals), 4),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-14")
    args = ap.parse_args()

    instrs = load_instructions(args.since)
    print(f"L2 入场门回放: {len(instrs)} 方向指令 (since {args.since})")

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    try:
        filled = backfill_atr(instrs, ctx)
        atr_ok = sum(1 for i in instrs if i["atr_pct"])
        print(f"  ATR: 原生 {atr_ok - filled} + 回算补齐 {filled} = {atr_ok}/{len(instrs)}")

        bars_hit = 0
        for i, ins in enumerate(instrs):
            bars = erb.fetch_bars(ctx, ins["symbol"], ins["date"])
            if bars and len(bars) >= 60:
                ins["timing"] = replay_timing(ins, bars)
                bars_hit += 1
            if (i + 1) % 40 == 0:
                print(f"  bars: {i + 1}/{len(instrs)} (就绪 {bars_hit})")
        print(f"  1min bar 覆盖: {bars_hit}/{len(instrs)} 指令")
    finally:
        ctx.close()

    # ── A. 入场时点变体 ──
    timing_report = {}
    print(f"\n═══ A. 入场时点变体 (bar close 进场 → EOD, 方向调整%, 同假设可比) ═══")
    print(f"{'时点':<7} {'n':>4} {'均值%':>8} {'中位%':>8} {'胜率':>6} {'均MFE':>7} {'均MAE':>7}")
    for tv in TIMING_VARIANTS:
        rets = [i["timing"][tv]["ret"] for i in instrs if tv in i.get("timing", {})]
        mfes = [i["timing"][tv]["mfe"] for i in instrs if tv in i.get("timing", {})]
        maes = [i["timing"][tv]["mae"] for i in instrs if tv in i.get("timing", {})]
        a = _agg(rets)
        timing_report[tv] = {**a, "avg_mfe": round(sum(mfes) / len(mfes), 3) if mfes else None,
                             "avg_mae": round(sum(maes) / len(maes), 3) if maes else None}
        if a["n"]:
            print(f"{tv:<7} {a['n']:>4} {a['avg']:>8.3f} {a['median']:>8.3f} {a['win_rate']:>6.2f} "
                  f"{timing_report[tv]['avg_mfe']:>7.3f} {timing_report[tv]['avg_mae']:>7.3f}")

    # ── B. ATR 门网格 (结果参照 = GATE_REF_TIMING bar 结果; 无 bar 用 fwd_signed 兜底) ──
    def outcome(ins) -> float | None:
        t = ins.get("timing", {}).get(GATE_REF_TIMING)
        if t is not None:
            return t["ret"]
        return ins["fwd_signed"]

    gate_report = {}
    print(f"\n═══ B. ATR 门网格 (结果参照 {GATE_REF_TIMING} 进场→EOD; 拦掉组均值<0=拦对) ═══")
    print(f"{'门':<14} {'拦n':>4} {'拦掉组均%':>9} {'放行组均%':>9} {'Δ(放-拦)':>9} {'定性':<6}")
    scored = [i for i in instrs if i["atr_pct"] and outcome(i) is not None]
    for name, lo, hi in GATES:
        blocked, passed = [], []
        for i in scored:
            a = float(i["atr_pct"])
            (blocked if (a < lo or (hi and a >= hi)) else passed).append(outcome(i))
        b, p = _agg(blocked), _agg(passed)
        verdict = ("拦对" if b.get("avg", 0) < 0 and b["n"] >= 10 else
                   "拦错" if b.get("avg", 0) > 0 and b["n"] >= 10 else "样本小")
        gate_report[name] = {"blocked": b, "passed": p, "verdict": verdict}
        if b["n"] or p["n"]:
            print(f"{name:<14} {b['n']:>4} {b.get('avg', 0):>9.3f} {p.get('avg', 0):>9.3f} "
                  f"{p.get('avg', 0) - b.get('avg', 0):>+9.3f} {verdict:<6}")

    report = {
        "generated_at": datetime.now().isoformat(), "since": args.since,
        "n_instructions": len(instrs), "atr_coverage": atr_ok, "atr_backfilled": filled,
        "bar_coverage": bars_hit, "gate_ref_timing": GATE_REF_TIMING,
        "assumptions": "entry=bar close 无滑点 (变体间可比); outcome=裸持有到 EOD, 不含退出工程",
        "timing_variants": timing_report, "atr_gates": gate_report,
        "by_source_n": {s: sum(1 for i in instrs if i["source"] == s)
                        for s in {i["source"] for i in instrs}},
    }
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n落盘: {OUT_PATH}")


if __name__ == "__main__":
    main()
