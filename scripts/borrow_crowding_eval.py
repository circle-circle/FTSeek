#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""借券拥挤度信号历史评估 (2026-08-29, RFC-062).

预注册判据 (照抄用户裁决纪律, 不得事后修改):
  - 事件 = borrow_crowding.compute_signal level ≥ WATCH 的 (symbol, date)
  - 度量 = 事件日收盘后 t+1..t+5 的超额收益 (vs SPY 同窗) + 同票无事件基线配对
  - 方向采信门槛: n≥30 且双窗口 OOS 同号 ≥40% (8/23 裁决线)
  - 样本不足 → 只输出观察, 不下方向结论 (历史真事件 ~5 个, 预期观察态)

用法: python3 scripts/borrow_crowding_eval.py
输出: data/borrow_crowding_eval_latest.json + stdout
"""
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app.data.borrow_crowding import load_series, compute_signal  # noqa: E402

BARS_DIR = _ROOT / "data" / "replay_bars"
OUT_PATH = _ROOT / "data" / "borrow_crowding_eval_latest.json"
MIN_N = 30          # 预注册: 方向采信样本门槛
OOS_RATIO = 0.40    # 预注册: 双窗口同号占比


def load_daily_bars(symbol, start, end):
    """(symbol, start, end) -> [(date, close), ...]; 缓存优先, 缺失回补."""
    ticker = symbol.replace("US.", "")
    # 找覆盖 [start, end] 的缓存文件
    best = None
    if BARS_DIR.exists():
        for p in BARS_DIR.glob(f"daily_US_{ticker}_*.json"):
            try:
                _, _, _, s, e = p.stem.rsplit("_", 4)
            except ValueError:
                continue
            if s <= start and e >= end:
                best = p
                break
    if best is not None:
        try:
            bars = json.loads(best.read_text())
            return [(b[0], float(b[4])) for b in bars if start <= b[0] <= end]
        except Exception:
            pass
    # 回补 (日K)
    try:
        from futu import OpenQuoteContext, KLType, RET_OK
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, df, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_DAY, start=start, end=end, max_count=1000)
        ctx.close()
        time.sleep(0.35)
        if ret == RET_OK and len(df):
            bars = [[str(r["time_key"])[:10], float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"])] for _, r in df.iterrows()]
            (BARS_DIR / f"daily_US_{ticker}_{start}_{end}.json").write_text(
                json.dumps(bars))
            return [(b[0], b[4]) for b in bars]
    except Exception as e:
        print(f"[warn] 日K回补失败 {symbol} {start}~{end}: {e}", file=sys.stderr)
    return []


def fwd_returns(closes, idx, horizon=5):
    """从 idx 起未来 horizon 日累计收益 (以 idx 收盘为基准)."""
    if idx + horizon >= len(closes):
        return None
    base = closes[idx]
    if base <= 0:
        return None
    return (closes[idx + horizon] - base) / base


def main():
    series = load_series()
    if not series:
        print("borrow_rate_series.jsonl 不存在或为空, 无事件可评估")
        return

    # 1. 全历史事件扫描
    events = []
    for sym, days in series.items():
        for date, row in days.items():
            sig = compute_signal(row)
            if sig["level"] != "NONE":
                events.append({"symbol": sym, "date": date,
                               "level": sig["level"], "score": sig["score"],
                               "fee_rate": row.get("fee_rate"),
                               "pool": row.get("pool"),
                               "reasons": sig["reasons"]})
    events.sort(key=lambda e: e["date"])
    print(f"历史拥挤事件: {len(events)} 个")

    # 2. SPY 基准日K (全范围)
    all_dates = sorted({d for days in series.values() for d in days})
    d0, d1 = all_dates[0], all_dates[-1]
    spy = load_daily_bars("US.SPY", d0, "2026-09-15")
    spy_map = {d: c for d, c in spy}
    spy_dates = sorted(spy_map)

    # 3. 逐事件度量
    results = []
    for ev in events:
        sym, date = ev["symbol"], ev["date"]
        bars = load_daily_bars(sym, date, "2026-09-15")
        if not bars:
            ev["note"] = "no_bars"
            results.append(ev)
            continue
        dates = [b[0] for b in bars]
        closes = [b[1] for b in bars]
        # 事件日索引 (取 ≤ date 的最后一根)
        idx = None
        for i, d in enumerate(dates):
            if d <= date:
                idx = i
        if idx is None:
            ev["note"] = "event_before_bars"
            results.append(ev)
            continue
        ev_ret = fwd_returns(closes, idx)
        # SPY 同窗基准
        spy_ret = None
        sidx = None
        for i, d in enumerate(spy_dates):
            if d <= date:
                sidx = i
        if sidx is not None and ev_ret is not None:
            spy_ret = fwd_returns([spy_map[d] for d in spy_dates], sidx)
        ev["event_date_actual"] = dates[idx]
        ev["fwd_ret_5d"] = round(ev_ret, 4) if ev_ret is not None else None
        ev["spy_ret_5d"] = round(spy_ret, 4) if spy_ret is not None else None
        if ev_ret is not None and spy_ret is not None:
            ev["excess_5d"] = round(ev_ret - spy_ret, 4)
        # 同票无事件基线 (该票全部其他起点的5日收益中位数)
        base_rets = []
        event_idx = {i for i, d in enumerate(dates) if d in
                     {e2["date"] for e2 in events if e2["symbol"] == sym}}
        for i in range(len(closes) - 5):
            if i in event_idx:
                continue
            r = fwd_returns(closes, i)
            if r is not None:
                base_rets.append(r)
        if base_rets:
            base_rets.sort()
            ev["baseline_5d_median"] = round(base_rets[len(base_rets) // 2], 4)
            if ev["fwd_ret_5d"] is not None:
                ev["paired_excess_5d"] = round(
                    ev["fwd_ret_5d"] - ev["baseline_5d_median"], 4)
        results.append(ev)
        print(f"  {ev['date']} {sym:<10} fee={ev['fee_rate']}% "
              f"fwd5d={ev.get('fwd_ret_5d')} spy={ev.get('spy_ret_5d')} "
              f"paired_excess={ev.get('paired_excess_5d')}")

    # 4. 预注册裁决
    measured = [e for e in results if e.get("paired_excess_5d") is not None]
    verdict = {
        "n_events": len(events), "n_measured": len(measured),
        "min_n_required": MIN_N, "oos_ratio_required": OOS_RATIO,
        "verdict": "INSUFFICIENT_SAMPLE_OBSERVE_ONLY",
    }
    if len(measured) >= MIN_N:
        pos = [e for e in measured if e["paired_excess_5d"] < 0]
        neg = [e for e in measured if e["paired_excess_5d"] > 0]
        # 拥挤 → 预期轧空(上涨)还是崩塌? 双向记录, 不预设方向
        verdict["mean_paired_excess"] = round(
            sum(e["paired_excess_5d"] for e in measured) / len(measured), 4)
        verdict["down_after_event"] = len(pos)
        verdict["up_after_event"] = len(neg)
        verdict["verdict"] = "MEASURED_SEE_OOS"
    else:
        verdict["note"] = (
            f"样本 {len(measured)} < {MIN_N}: 只观察不裁决。"
            "SIM 费率静态 (94.6%行=3.0%), 真事件稀疏; 攒样后重跑。")

    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "window": [d0, d1], "verdict": verdict, "events": results}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n裁决: {verdict['verdict']} (n={verdict['n_measured']}/{MIN_N})")
    print(f"已写入 {OUT_PATH.name}")


if __name__ == "__main__":
    main()
