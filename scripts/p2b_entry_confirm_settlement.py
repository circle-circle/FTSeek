#!/usr/bin/env python3
"""P2b: entry_confirm 弃单的反事实结算 (DEB-20261106-score-calibration-and-entry-quality §P2b)。

回答: **这道门（30 天拦 80 条，最大拦截量）到底在救人还是在误杀？**
做法: 对每条 ABANDONED 指令，用 metadata.entry_confirm.px_at_confirm 作为"若未弃单则成交价"，
      按原计划的 planned_stop_loss / planned_take_profit / 当日日K结算，再扣费用。
      给出两个括号 (避免伪造日内路径):
        A) 收盘括号: 忽略止损止盈，直接持到当日收盘 (上界偏乐观)
        B) 穿越括号: 当日 high/low 触及 SL/TP 即出场; 若同时触及则**按止损先**(下界偏差)
局限(必须随结论一起读):
  · 日K的 high/low 包含入场之前的价格 → B 括号对多头偏悲观、对空头偏乐观;
  · 未建模盘后/隔夜, 未建模保本/追踪移动止损(实际系统会移动) → A/B 是两个包络, 不是精确复现。
  · **n<100 时按预注册不得裁决**, 本脚本只出 interim 读数。

只读。用法:
  cd /opt/ftseek/FTSeek-US && venv/bin/python3.11 scripts/p2b_entry_confirm_settlement.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
A10_START = "2026-07-01"
FEE_BPS_CAP = 11.54          # admission.max_fee_bps (仅作参照, 结算用真实佣金公式)
COMMISSION_FLOOR = 1.99
SEED, BOOT = 20261106, 2000
PRE_REG_N = 100


def load_bars(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for sym, payload in (d.get("symbols") or {}).items():
        out[sym] = {b["time"][:10]: b for b in (payload.get("bars") or [])}
    return out


def fee_pct(entry: float, notional: float) -> float:
    """往返佣金(含地板) as % of notional；qty = notional/entry。"""
    if entry <= 0 or notional <= 0:
        return 0.0
    qty = notional / entry
    per_leg = max(min(0.01 * qty, 0.003 * notional), COMMISSION_FLOOR)
    return 2 * per_leg / notional * 100.0


def checkpoint_direction(day: str, sym: str, alt_day: str | None = None) -> str | None:
    """从 DA checkpoint 回收方向 (历史弃单行的 entry_confirm.direction 已被浅合并抹掉)。

    2026-11-06 实测: 48/80 行缺方向, 但 data/da_checkpoint/<date>/<SYM>.json 的
    _llm_output.direction 100% 可回收 (48/48)。
    """
    for d in (day, alt_day):
        if not d:
            continue
        p = ROOT / "data" / "da_checkpoint" / str(d)[:10] / f"{sym}.json"
        if not p.exists():
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[p2b_entry_confirm_settlement] checkpoint 解析失败跳过 {p.name}: {type(exc).__name__}", file=sys.stderr)
            continue
        cand = ((j.get("_llm_output") or {}).get("direction")) or j.get("direction")
        cu = str(cand or "").upper()
        if cu in ("BUY", "LONG"):
            return "BUY"
        if cu in ("SELL_SHORT", "SHORT"):
            return "SELL_SHORT"
    return None


def settle(direction: str, entry: float, sl, tp, bar, notional: float) -> dict | None:
    if not bar or entry <= 0:
        return None
    hi, lo, close = bar["high"], bar["low"], bar["close"]
    long_ = direction in ("BUY", "LONG")
    sign = 1.0 if long_ else -1.0
    # A) 收盘括号
    r_close = (close / entry - 1.0) * 100.0 * sign
    # B) 穿越括号 (同时触及 → 止损先)
    stop_hit = sl is not None and ((lo <= sl) if long_ else (hi >= sl))
    tp_hit = tp is not None and ((hi >= tp) if long_ else (lo <= tp))
    if stop_hit:
        r_path = (sl / entry - 1.0) * 100.0 * sign
        how = "SL"
    elif tp_hit:
        r_path = (tp / entry - 1.0) * 100.0 * sign
        how = "TP"
    else:
        r_path = r_close
        how = "CLOSE"
    f = fee_pct(entry, notional)
    return {"r_close_pct": r_close - f, "r_path_pct": r_path - f,
            "exit_kind": how, "fee_pct": f}


def boot_ci_by_day(rows, key, seed=SEED, iters=BOOT):
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r[key])
    days = list(by_day)
    rng = random.Random(seed)
    vals = []
    for _ in range(iters):
        s = []
        for _ in range(len(days)):
            s.extend(by_day[days[rng.randrange(len(days))]])
        if s:
            vals.append(sum(s) / len(s))
    if not vals:
        return (float("nan"), float("nan"))
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    ap.add_argument("--bars", default=str(ROOT / "data" / "cache" / "daily_kline_v1.json"))
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    bars = load_bars(Path(args.bars))
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c = con.cursor()
    rows = list(c.execute(
        "select date, symbol, direction, raw_instruction_json, metadata_json "
        "from playbook_instructions where row_kind='instruction' "
        "and skip_reason like '%entry_confirm%' and date >= ?", (A10_START,)))

    notional_med = None
    try:
        ns = [ (p or 0) * (q or 0) for p, q in c.execute(
            "select entry_price, quantity from trade_records where exit_price>0 "
            "and trade_date >= ? and coalesce(data_quality,'') in ('clean','real')", (A10_START,))]
        ns = [x for x in ns if x > 0]
        notional_med = st.median(ns) if ns else 5618.0
    except Exception:
        notional_med = 5618.0

    settled, skipped = [], Counter()
    for date, sym, dirn, raw, meta in rows:
        try:
            d = json.loads(raw or "{}")
            m = json.loads(meta or "{}")
        except Exception:
            skipped["bad_json"] += 1
            continue
        ec = m.get("entry_confirm") or {}
        if ec.get("status") != "ABANDONED":
            skipped["status_" + str(ec.get("status"))] += 1
            continue
        entry = ec.get("px_at_confirm") or d.get("planned_entry")
        sl = d.get("planned_stop_loss") or d.get("planned_stop")
        tp = d.get("planned_take_profit") or d.get("planned_target")
        ts = str(ec.get("abandoned_at") or "")
        day = ts[:10] or date
        bar = (bars.get(sym) or {}).get(day)
        if bar is None:
            skipped["no_bar"] += 1
            continue
        # 方向解析: raw.direction → raw.original_direction → DB 列。
        # ⚠️ 弃单后 DB 列会被写成 SKIP, raw.direction 可能为 None (2026-11-06 实测: 80 行里 48 行无方向)
        #    ⇒ 无方向的行必须跳过, 否则会被当成长单, 复现 2026-11-06 的假结果 (+2.52%/76.6%)。
        resolved = None
        for cand in (d.get("direction"), d.get("original_direction"), dirn):
            cu = str(cand or "").upper()
            if cu in ("BUY", "LONG"):
                resolved = "BUY"
                break
            if cu in ("SELL_SHORT", "SHORT"):
                resolved = "SELL_SHORT"
                break
        if resolved is None:
            resolved = checkpoint_direction(day, sym, date)
            if resolved:
                skipped["direction_recovered_from_checkpoint"] += 1
        if resolved is None:
            skipped["unknown_direction"] += 1
            continue
        out = settle(resolved, float(entry), sl, tp, bar, notional_med)
        if out is None:
            skipped["bad_price"] += 1
            continue
        out.update({"date": day, "symbol": sym, "direction": resolved,
                    "entry": float(entry), "sl": sl, "tp": tp, "score": d.get("score")})
        settled.append(out)

    n = len(settled)
    print(f"== P2b entry_confirm 弃单反事实结算 (A10 起, 弃单行 {len(rows)})")
    print(f"   结算成功 {n} 行; 跳过 {dict(skipped)}; 参考名义额 ${notional_med:,.0f}")
    if n == 0:
        print("无可结算样本")
        return 1

    print(f"\n{'括号':10s} | {'n':>3s} | {'均值%':>8s} | {'中位%':>7s} | {'胜率':>6s} | 日聚类CI95")
    stats = {}
    for label, key in (("A 收盘", "r_close_pct"), ("B 穿越", "r_path_pct")):
        v = [r[key] for r in settled]
        lo, hi = boot_ci_by_day(settled, key)
        stats[label] = {"n": n, "mean": st.mean(v), "median": st.median(v),
                        "win": sum(1 for x in v if x > 0) / n, "ci": [lo, hi]}
        print(f"{label:10s} | {n:3d} | {st.mean(v):+8.3f} | {st.median(v):+7.3f} | "
              f"{sum(1 for x in v if x > 0) / n:6.1%} | [{lo:+.3f}, {hi:+.3f}]")
    print(f"\n出场构成(B): {dict(Counter(r['exit_kind'] for r in settled))}")
    print(f"平均单边费用 {st.mean([r['fee_pct'] for r in settled]):.2f}% (名义 ${notional_med:,.0f})")

    by_dir = defaultdict(list)
    for r in settled:
        by_dir[r["direction"]].append(r["r_path_pct"])
    for k, v in by_dir.items():
        print(f"   方向 {k:10s} n={len(v):3d} 均值 {st.mean(v):+.3f}% 胜率 {sum(1 for x in v if x>0)/len(v):.1%}")

    verdict = "UNPOWERED_INTERIM" if n < PRE_REG_N else "POWERED"
    print(f"\n预注册裁决线 n>={PRE_REG_N} → 当前 {n} ⇒ **{verdict}**（不得据此改动该门）")
    out = {"schema": "p2b_entry_confirm_settlement_v1", "abandoned_rows": len(rows),
           "settled": n, "skipped": dict(skipped), "notional_ref": notional_med,
           "brackets": stats, "exit_kinds": dict(Counter(r["exit_kind"] for r in settled)),
           "by_direction": {k: {"n": len(v), "mean": st.mean(v)} for k, v in by_dir.items()},
           "pre_registered_n": PRE_REG_N, "verdict": verdict,
           "limitations": ["日K high/low 含入场前价格 ⇒ B 括号对多头偏悲观",
                           "未建模保本/追踪移动止损与隔夜",
                           "费用按佣金地板公式估算, 名义取 A10 中位"],
           "rows": settled}
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"-> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())