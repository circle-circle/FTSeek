#!/usr/bin/env python3
"""2 分钟门 ABANDONED 后「二次限价挂单」只读回放 (2026-10-18).

人群: data/shadow/entry_confirm_forward.jsonl 的 type=entry_confirm / outcome=ABANDONED。
数据: data/replay_bars_1m + data/replay_bars_1m_extra (Futu QFQ 1m, 已做口径校验)。
规则:
  - 放弃后等 delay 分钟, 在 abandonment price 的「更差值」挂限价:
      LONG : limit = abandon_mid * (1 - retrace)
      SHORT: limit = abandon_mid * (1 + retrace)
  - TTL 内触及即成交; 成交后按原 SL/TP 模拟, 最长 horizon 分钟。
  - 对照臂: 忽略 2 分钟门, 以 abandonment price 立即市价入场 (同一 SL/TP/horizon)。
纯只读: 不写 playbook_instructions、不下单、不改 entry_confirm_gate。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
FWD = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
BAR_DIRS = (ROOT / "data" / "replay_bars_1m",
            ROOT / "data" / "replay_bars_1m_extra")
CALIB_WARN = 0.02
DELAYS = (3, 5, 10, 15)
RULES = ("pct0.3", "pct0.5", "atr0.2", "atr0.3")


def _log(msg: str) -> None:
    print(f"[second-chance] {msg}", file=sys.stderr)


def _load_abandoned() -> list[dict]:
    rows = []
    for line in FWD.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (row.get("type") == "entry_confirm"
                and row.get("outcome") == "ABANDONED"):
            rows.append(row)
    return rows


def _load_insts() -> dict[int, dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, stop_loss, take_profit, atr_pct, vol_bucket, decision_conf "
            "FROM playbook_instructions"
        ).fetchall()
    finally:
        conn.close()
    return {int(r["id"]): dict(r) for r in rows}


def _load_bars(symbol: str) -> Optional[list[tuple]]:
    name = f"{symbol.replace('.', '_')}.json"
    for directory in BAR_DIRS:
        path = directory / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[entry_second_chance] 坏行/坏档跳过: {e}", file=sys.stderr)
            continue
        bars = []
        for row in raw:
            try:
                bars.append((datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S"),
                             float(row[1]), float(row[2]), float(row[3]),
                             float(row[4])))
            except Exception as e:
                print(f"[entry_second_chance] 坏行/坏档跳过: {e}", file=sys.stderr)
                continue
        if bars:
            return bars
    return None


def _bar_at_or_before(bars: list[tuple], stamp: datetime):
    found = None
    for bar in bars:
        if bar[0] <= stamp:
            found = bar
        else:
            break
    return found


def _retrace_frac(rule: str, atr_pct: float) -> float:
    atr = float(atr_pct or 0)
    if atr > 1:
        atr /= 100.0
    if rule == "pct0.3":
        return 0.003
    if rule == "pct0.5":
        return 0.005
    if rule == "atr0.2":
        return min(max(0.2 * atr, 0.002), 0.02)
    if rule == "atr0.3":
        return min(max(0.3 * atr, 0.003), 0.03)
    raise ValueError(rule)


def _limit_price(direction: str, reference: float, frac: float) -> float:
    return reference * (1 - frac) if direction == "LONG" else reference * (1 + frac)


def _simulate_fill(bars: list[tuple], placement: datetime, ttl_end: datetime,
                   direction: str, limit: float):
    for bar in bars:
        stamp, open_, high, low, _close = bar
        if stamp <= placement:
            continue
        if stamp > ttl_end:
            break
        if direction == "LONG" and low <= limit:
            return (min(limit, open_), stamp)
        if direction == "SHORT" and high >= limit:
            return (max(limit, open_), stamp)
    return None


def _simulate_exit(bars: list[tuple], fill_stamp: datetime, direction: str,
                   fill: float, stop_loss: float, take_profit: float,
                   horizon_min: int) -> dict:
    end = fill_stamp + timedelta(minutes=horizon_min)
    mfe = mae = 0.0
    last_close = fill
    for bar in bars:
        stamp, open_, high, low, close = bar
        if stamp <= fill_stamp:
            continue
        if stamp > end:
            break
        last_close = close
        if direction == "LONG":
            mfe = max(mfe, (high - fill) / fill * 100)
            mae = min(mae, (low - fill) / fill * 100)
            if open_ <= stop_loss:
                return {"exit": open_, "reason": "SL_GAP", "mfe": mfe, "mae": mae}
            if low <= stop_loss:
                return {"exit": stop_loss, "reason": "SL", "mfe": mfe, "mae": mae}
            if high >= take_profit:
                return {"exit": take_profit, "reason": "TP", "mfe": mfe, "mae": mae}
        else:
            mfe = max(mfe, (fill - low) / fill * 100)
            mae = min(mae, (fill - high) / fill * 100)
            if open_ >= stop_loss:
                return {"exit": open_, "reason": "SL_GAP", "mfe": mfe, "mae": mae}
            if high >= stop_loss:
                return {"exit": stop_loss, "reason": "SL", "mfe": mfe, "mae": mae}
            if low <= take_profit:
                return {"exit": take_profit, "reason": "TP", "mfe": mfe, "mae": mae}
    return {"exit": last_close, "reason": "TIME", "mfe": mfe, "mae": mae}


def _pnl_pct(direction: str, fill: float, exit_price: float) -> float:
    if direction == "LONG":
        return (exit_price - fill) / fill * 100
    return (fill - exit_price) / fill * 100


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "avg": None, "median": None, "sum": None, "win_rate": None}
    wins = sum(1 for v in values if v > 0)
    return {
        "n": len(values),
        "avg": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "sum": round(sum(values), 4),
        "win_rate": round(wins / len(values) * 100, 1),
    }


def _run_arm(episodes: list[dict], delay: int, rule: str, ttl_min: int,
             horizon_min: int, fee_bps: float) -> dict:
    fills, nets, reasons = [], [], {}
    filled_by_dir = {"LONG": 0, "SHORT": 0}
    no_fill = invalid = no_sl_tp = 0
    for ep in episodes:
        inst = ep["inst"]
        sl = float(inst.get("stop_loss") or 0)
        tp = float(inst.get("take_profit") or 0)
        if sl <= 0 or tp <= 0:
            no_sl_tp += 1
            continue
        frac = _retrace_frac(rule, float(inst.get("atr_pct") or 0))
        limit = _limit_price(ep["direction"], ep["abandon_ref"], frac)
        if (ep["direction"] == "LONG" and limit <= sl) or (
                ep["direction"] == "SHORT" and limit >= sl):
            invalid += 1
            continue
        placement = ep["ts"] + timedelta(minutes=delay)
        ttl_end = placement + timedelta(minutes=ttl_min)
        fill = _simulate_fill(ep["bars"], placement, ttl_end,
                              ep["direction"], limit)
        if fill is None:
            no_fill += 1
            continue
        fill_price, fill_stamp = fill
        result = _simulate_exit(ep["bars"], fill_stamp, ep["direction"],
                                fill_price, sl, tp, horizon_min)
        gross = _pnl_pct(ep["direction"], fill_price, result["exit"])
        net = gross - fee_bps / 100.0
        fills.append(fill_price)
        nets.append(net)
        filled_by_dir[ep["direction"]] += 1
        reasons[result["reason"]] = reasons.get(result["reason"], 0) + 1
    return {
        "delay_min": delay,
        "rule": rule,
        "fills": len(nets),
        "no_fill": no_fill,
        "invalid_limit": invalid,
        "no_sl_tp": no_sl_tp,
        "fill_rate_pct": round(len(nets) / max(1, len(episodes)) * 100, 1),
        "direction_fills": filled_by_dir,
        "exit_reasons": reasons,
        **_stats(nets),
    }


def _run_market_arm(episodes: list[dict], horizon_min: int, fee_bps: float) -> dict:
    nets, reasons = [], {}
    no_sl_tp = 0
    for ep in episodes:
        inst = ep["inst"]
        sl = float(inst.get("stop_loss") or 0)
        tp = float(inst.get("take_profit") or 0)
        if sl <= 0 or tp <= 0:
            no_sl_tp += 1
            continue
        fill = ep["abandon_ref"]
        result = _simulate_exit(ep["bars"], ep["ts"], ep["direction"],
                                fill, sl, tp, horizon_min)
        gross = _pnl_pct(ep["direction"], fill, result["exit"])
        nets.append(gross - fee_bps / 100.0)
        reasons[result["reason"]] = reasons.get(result["reason"], 0) + 1
    return {"fills": len(nets), "no_sl_tp": no_sl_tp,
            "exit_reasons": reasons, **_stats(nets)}


def main() -> int:
    ap = argparse.ArgumentParser(description="二次限价挂单只读回放")
    ap.add_argument("--delays", default=",".join(map(str, DELAYS)))
    ap.add_argument("--rules", default=",".join(RULES))
    ap.add_argument("--ttl", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--fee-bps", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    delays = [int(x) for x in args.delays.split(",") if x.strip()]
    rules = [x.strip() for x in args.rules.split(",") if x.strip()]
    abandoned = _load_abandoned()
    insts = _load_insts()
    bars_cache: dict[str, Optional[list[tuple]]] = {}
    episodes = []
    no_bars = caliber_bad = 0
    for row in abandoned:
        symbol = str(row.get("symbol") or "")
        inst = insts.get(int(row.get("inst_id") or 0))
        if not inst:
            continue
        if symbol not in bars_cache:
            bars_cache[symbol] = _load_bars(symbol)
        bars = bars_cache[symbol]
        if not bars:
            no_bars += 1
            continue
        ts = datetime.fromisoformat(str(row["ts"])).replace(tzinfo=None)
        bar = _bar_at_or_before(bars, ts)
        ref = float(row.get("px_at_confirm") or 0)
        if bar is None or ref <= 0:
            caliber_bad += 1
            continue
        dev = abs(float(bar[4]) - ref) / ref
        if dev > CALIB_WARN:
            caliber_bad += 1
            continue
        episodes.append({
            "row": row,
            "inst": inst,
            "symbol": symbol,
            "direction": str(row.get("direction") or "").upper(),
            "ts": ts,
            "abandon_ref": ref,
            "bars": bars,
        })

    market = _run_market_arm(episodes, args.horizon, args.fee_bps)
    arms = []
    for delay in delays:
        for rule in rules:
            arms.append(_run_arm(episodes, delay, rule, args.ttl,
                                 args.horizon, args.fee_bps))
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cohort": {
            "abandoned_rows": len(abandoned),
            "episodes_with_bars": len(episodes),
            "no_bars": no_bars,
            "caliber_bad": caliber_bad,
        },
        "assumptions": {
            "bar_source": "Futu K_1M QFQ (replay_bars_1m + replay_bars_1m_extra)",
            "ttl_min": args.ttl,
            "horizon_min": args.horizon,
            "fee_bps_round_trip": args.fee_bps,
            "caliber_warn": CALIB_WARN,
        },
        "market_arm": market,
        "limit_arms": arms,
    }
    report_dir = ROOT / "data" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"entry_second_chance_replay_{datetime.now():%Y%m%d}.json"
    report_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    c = out["cohort"]
    print(f"cohort: abandoned={c['abandoned_rows']} with_bars={c['episodes_with_bars']} "
          f"no_bars={c['no_bars']} caliber_bad={c['caliber_bad']}")
    print(f"market arm (ignore gate): n={market['n']} win={market['win_rate']}% "
          f"avgNet={market['avg']}% sumNet={market['sum']}% "
          f"exits={market['exit_reasons']}")
    print(f"{'delay':>5} {'rule':>8} {'fills':>5} {'fill%':>6} {'win%':>5} "
          f"{'avgNet%':>8} {'sumNet%':>8} {'SL/TP/T':>12} {'L/S fills':>10}")
    for arm in arms:
        ex = arm["exit_reasons"]
        print(f"{arm['delay_min']:>5} {arm['rule']:>8} {arm['fills']:>5} "
              f"{arm['fill_rate_pct']:>5.1f} {str(arm['win_rate']):>5} "
              f"{str(arm['avg']):>8} {str(arm['sum']):>8} "
              f"{ex.get('SL',0)+ex.get('SL_GAP',0)}/{ex.get('TP',0)}/{ex.get('TIME',0):<6} "
              f"{arm['direction_fills']['LONG']}/{arm['direction_fills']['SHORT']:<4}")
    print(f"report: {report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
