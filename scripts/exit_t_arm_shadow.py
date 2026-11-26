#!/usr/bin/env python3
"""RFC-154 T 臂影子: adaptive trailing min drawdown 0.4 -> 0.8 (只读全路径回放).

只做一件事: 用生产同一套退出数学 (exit_replay_backtest 的 replay_trade +
exit_engine SSOT), 在同一批历史 SIM 交易上对比:
  baseline = adaptive trailing min_dd 0.4 (live)
  T arm    = adaptive trailing min_dd 0.8
不写任何交易表/配置; 只写 data/shadow/exit_t_arm_latest.json.

预注册 (RFC-154 §2.2):
  - 全路径入场→出场重放; 双序 pess/opt 各跑;
  - 主指标扣费净 bps/笔 (再报 stress −30bps 往返);
  - 样本门 ≥10 ET 日且 ≥30 笔;
  - 提案门: T−baseline 均值 ≥+10bps, 日聚类 t≥2.0, SL_INITIAL 不恶化,
    赢家被误杀净额 < 亏损被避免净额.
不自动改生产; 转生产须另立 RFC + 用户批准.

用法: PYTHONPATH=. venv/bin/python scripts/exit_t_arm_shadow.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import exit_replay_backtest as erb  # noqa: E402

OUT = REPO / "data" / "shadow" / "exit_t_arm_latest.json"
BAR_DIR = REPO / "data" / "replay_bars"
MDE_Z = 2.802
STRESS_BPS = 30.0

BASE_ADAPTIVE = {
    "adaptive_trailing_enabled": True,
    "adaptive_trailing_target_capture_pct": 60.0,
    "adaptive_trailing_min_drawdown_pct": 0.4,
    "adaptive_trailing_max_drawdown_pct": 1.2,
}
T_ADAPTIVE = {**BASE_ADAPTIVE, "adaptive_trailing_min_drawdown_pct": 0.8}

logger = logging.getLogger(__name__)


def _load_cached_bars(symbol: str, entry_et: datetime, exit_et: datetime):
    """只读 data/replay_bars 缓存; 缺 entry/exit 日 bar ⇒ None (不触 OpenD)."""
    stem = symbol.replace(".", "_")
    entry_day = entry_et.date()
    exit_day = exit_et.date()
    end = max(exit_day, entry_day)
    if exit_day > entry_day:
        end = exit_day + timedelta(days=1)
    out = []
    d = entry_day
    while d <= end:
        path = BAR_DIR / f"{stem}_{d.strftime('%Y-%m-%d')}.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:
                logger.warning("bar 读取失败 %s: %s", path, error, exc_info=True)
                raw = []
            for b in raw:
                if len(b) < 5:
                    continue
                out.append({
                    "t": erb.ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
                    "o": float(b[1]), "h": float(b[2]),
                    "l": float(b[3]), "c": float(b[4]),
                })
        d += timedelta(days=1)
    if not out:
        return None
    entry_minute = entry_et.replace(second=0, microsecond=0)
    out = [b for b in out if b["t"] > entry_minute]
    if len(out) < 5:
        return None
    return out


def _net_bps(tr: dict, replay: dict, fee: float) -> float:
    sign = 1.0 if tr["direction"] == "LONG" else -1.0
    gross = tr["qty"] * sign * (float(replay["exit_price"]) - tr["entry"])
    net = gross - fee
    notional = tr["entry"] * tr["qty"]
    return round(net / notional * 10000.0, 2) if notional else 0.0


def _capture_pct(replay: dict, net_bps: float) -> float | None:
    mfe = float(replay.get("mfe_pct") or 0)
    if mfe <= 0:
        return None
    return round((net_bps / 100.0) / mfe * 100.0, 1)


def _stats(rows: list[dict]) -> dict:
    nets = [r["net_bps"] for r in rows]
    if not nets:
        return {"n": 0}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    return {
        "n": len(nets),
        "mean_bps": round(statistics.mean(nets), 2),
        "median_bps": round(statistics.median(nets), 2),
        "win_rate_pct": round(100.0 * len(wins) / len(nets), 1),
        "avg_win_bps": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss_bps": round(statistics.mean(losses), 2) if losses else None,
        "rr": (round(statistics.mean(wins) / abs(statistics.mean(losses)), 2)
               if wins and losses else None),
        "sl_initial_n": sum(1 for r in rows if r["reason"] == "SL_INITIAL"),
        "mean_capture_pct": (round(statistics.mean(
            [c for c in (r.get("capture_pct") for r in rows) if c is not None]), 1)
            if any(r.get("capture_pct") is not None for r in rows) else None),
    }


def _day_clustered(pairs: list[tuple[str, float]]) -> dict | None:
    by_day: dict[str, list[float]] = defaultdict(list)
    for day, value in pairs:
        by_day[day].append(value)
    daily = [statistics.mean(v) for v in by_day.values() if v]
    if len(daily) < 3:
        return None
    sd = statistics.stdev(daily)
    if sd == 0:
        return None
    mean = statistics.mean(daily)
    t_stat = mean / (sd / math.sqrt(len(daily)))
    return {
        "n_days": len(daily),
        "day_mean_bps": round(mean, 2),
        "t": round(t_stat, 2),
        "mde_80pct": round(MDE_Z * sd / math.sqrt(len(daily)), 2),
        "distinguishable": bool(abs(t_stat) >= 2.0),
    }


def _compare(rows: list[dict]) -> dict:
    delta = [r["t_net_bps"] - r["base_net_bps"] for r in rows]
    if not delta:
        return {"n": 0}
    pairs = [(r["day"], r["t_net_bps"] - r["base_net_bps"]) for r in rows]
    win = [r for r in rows if r["base_net_bps"] > 0]
    lose = [r for r in rows if r["base_net_bps"] < 0]
    harm = -sum(min(0.0, r["t_net_bps"] - r["base_net_bps"]) for r in win)
    save = sum(max(0.0, r["t_net_bps"] - r["base_net_bps"]) for r in lose)
    return {
        "n": len(delta),
        "mean_delta_bps": round(statistics.mean(delta), 2),
        "median_delta_bps": round(statistics.median(delta), 2),
        "pct_positive": round(100.0 * sum(1 for x in delta if x > 0) / len(delta), 1),
        "total_delta_bps": round(sum(delta), 2),
        "day_clustered": _day_clustered(pairs),
        "harm_to_winners_bps": round(harm, 2),
        "save_from_losers_bps": round(save, 2),
    }


def _gate(sample: dict, base: dict, t_arm: dict, delta: dict,
          fidelity_ok: bool | None = None) -> dict:
    clustered = delta.get("day_clustered") or {}
    checks = {
        "sample_n": sample["n"] >= 30,
        "sample_days": sample["distinct_days"] >= 10,
        "delta_mean_ge_10bps": (delta.get("mean_delta_bps") or -999) >= 10.0,
        "delta_day_clustered_t_ge_2": (clustered.get("t") or -999) >= 2.0,
        "sl_initial_not_worse": t_arm.get("sl_initial_n", 0) <= base.get("sl_initial_n", 0),
        "harm_lt_save": (delta.get("harm_to_winners_bps") or 0)
                        < (delta.get("save_from_losers_bps") or 0),
    }
    if fidelity_ok is not None:
        checks["fidelity_ok"] = bool(fidelity_ok)
    return {"checks": checks, "pass": all(checks.values()),
            "readout_only": True,
            "frozen_by_fidelity": (fidelity_ok is False),
            "note": "通过只代表可提交转生产提案, 不自动改生产"}


def compute(trades: list[dict], factor_rules: dict, params: dict,
            slippage_bps: float) -> dict:
    by_ordering: dict[str, list[dict]] = {"pess": [], "opt": []}
    skipped = 0
    skipped_fee_unknown = 0
    for tr in trades:
        fee = erb._fee_of(tr)
        if fee is None:
            skipped_fee_unknown += 1
            continue
        bars = _load_cached_bars(tr["symbol"], tr["entry_et"], tr["exit_et"])
        if bars is None:
            skipped += 1
            continue
        exit_cfg = erb._get_factor_exit_config(
            tr["conditions"], factor_rules, direction=tr["direction"])
        for ordering in ("pess", "opt"):
            base_rp = erb.replay_trade(
                tr, bars, exit_cfg, {"name": "adaptive_live"}, params, ordering,
                slippage_bps, adaptive_cfg=BASE_ADAPTIVE)
            t_rp = erb.replay_trade(
                tr, bars, exit_cfg, {"name": "t_arm"}, params, ordering,
                slippage_bps, adaptive_cfg=T_ADAPTIVE)
            base_bps = _net_bps(tr, base_rp, fee)
            t_bps = _net_bps(tr, t_rp, fee)
            by_ordering[ordering].append({
                "id": tr["id"], "symbol": tr["symbol"],
                "day": tr["entry_et"].strftime("%Y-%m-%d"),
                "base_net_bps": base_bps, "t_net_bps": t_bps,
                "base_exit": float(base_rp["exit_price"]),
                "t_exit": float(t_rp["exit_price"]),
                "base_reason": base_rp["reason"], "t_reason": t_rp["reason"],
                "reason": base_rp["reason"],
                "base_capture_pct": _capture_pct(base_rp, base_bps),
                "capture_pct": _capture_pct(t_rp, t_bps),
            })
    days = sorted({r["day"] for r in by_ordering["pess"]})
    result: dict = {
        "schema": "exit_t_arm_shadow_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"baseline": BASE_ADAPTIVE, "t_arm": T_ADAPTIVE,
                   "slippage_bps": slippage_bps, "stress_bps": STRESS_BPS},
        "sample": {"trades_total": len(trades), "replayed": len(by_ordering["pess"]),
                   "skipped_no_bars": skipped, "skipped_fee_unknown": skipped_fee_unknown,
                   "distinct_days": len(days),
                   "n": len(by_ordering["pess"])},
        "by_ordering": {},
        "caveats": [
            "只读全路径回放; baseline/T 均走 adaptive trailing 公式 (生产 SIM 路径),"
            " 与官方 exit_replay_backtest 的静态 dd live_baseline 不同。",
            "bar 缓存缺失的标的会跳过, 不触 OpenD; 覆盖数见 sample。",
            "双序 pess/opt 并报; pess 为保守主口径。",
            "费用用实际交易反推; 滑点只按引擎 slippage_bps, 另报 stress −30bps 供参考。",
            "通过 gate 只代表可提交转生产提案, 不自动改生产。",
        ],
    }
    for ordering, rows in by_ordering.items():
        base = _stats([{"net_bps": r["base_net_bps"], "reason": r["base_reason"],
                        "capture_pct": r.get("base_capture_pct")} for r in rows])
        t_arm = _stats([{"net_bps": r["t_net_bps"], "reason": r["t_reason"],
                         "capture_pct": r["capture_pct"]} for r in rows])
        delta = _compare(rows)
        result["by_ordering"][ordering] = {
            "baseline": base, "t_arm": t_arm, "delta": delta,
            "t_arm_stress_mean_bps": (round(t_arm.get("mean_bps", 0) - STRESS_BPS, 2)
                                      if t_arm.get("n") else None),
            "baseline_stress_mean_bps": (round(base.get("mean_bps", 0) - STRESS_BPS, 2)
                                         if base.get("n") else None),
        }
    primary = result["by_ordering"]["pess"]
    result["primary"] = "pess"
    # baseline vs 实盘保真度 (回放引擎自己的冻结门: reason_match < 0.80 ⇒ 变体读数冻结)
    live_by_id = {tr["id"]: tr for tr in trades}
    pess_rows = by_ordering["pess"]
    match = 0
    exit_deltas = []
    for row in pess_rows:
        live = live_by_id.get(row["id"])
        if not live:
            continue
        if row["base_reason"] == live["live_reason"]:
            match += 1
        sign = 1.0 if live["direction"] == "LONG" else -1.0
        replay_pct = (row["base_exit"] - live["entry"]) * sign / live["entry"] * 100.0
        live_pct = (live["live_exit"] - live["entry"]) * sign / live["entry"] * 100.0
        exit_deltas.append(abs(replay_pct - live_pct))
    reason_match = round(match / len(pess_rows), 3) if pess_rows else 0.0
    exit_deltas.sort()
    result["validation"] = {
        "reason_match_pct": reason_match,
        "exit_pct_delta_median": (round(exit_deltas[len(exit_deltas) // 2], 4)
                                  if exit_deltas else None),
        "fidelity_ok": reason_match >= 0.80,
        "note": "baseline vs 实盘; <0.80 时按引擎纪律冻结变体读数",
    }
    result["gate"] = _gate(
        result["sample"], primary["baseline"], primary["t_arm"], primary["delta"],
        fidelity_ok=reason_match >= 0.80)
    return result


def render(report: dict) -> None:
    print("=" * 72)
    print("RFC-154 T 臂影子 (adaptive min DD 0.4 → 0.8) — 只读")
    sample = report["sample"]
    print(f"样本: trades={sample['trades_total']} replayed={sample['replayed']} "
          f"days={sample['distinct_days']} skipped_no_bars={sample['skipped_no_bars']} "
          f"skipped_fee_unknown={sample.get('skipped_fee_unknown', 0)}")
    for ordering in ("pess", "opt"):
        block = report["by_ordering"][ordering]
        b, t, d = block["baseline"], block["t_arm"], block["delta"]
        clustered = d.get("day_clustered") or {}
        print(f"-- {ordering} --")
        print(f"  baseline: n={b['n']} mean={b['mean_bps']}bps median={b['median_bps']} "
              f"SL_INITIAL={b['sl_initial_n']} capture={b['mean_capture_pct']}%")
        print(f"  T arm:    n={t['n']} mean={t['mean_bps']}bps median={t['median_bps']} "
              f"SL_INITIAL={t['sl_initial_n']} capture={t['mean_capture_pct']}%")
        print(f"  delta:    mean={d['mean_delta_bps']}bps median={d['median_delta_bps']} "
              f"positive={d['pct_positive']}% t={clustered.get('t')} "
              f"MDE80={clustered.get('mde_80pct')}")
        print(f"  harm={d['harm_to_winners_bps']}bps save={d['save_from_losers_bps']}bps "
              f"stress_mean(T)={block['t_arm_stress_mean_bps']}bps")
    validation = report["validation"]
    print(f"保真度: reason_match={validation['reason_match_pct']} "
          f"exit_delta_med={validation['exit_pct_delta_median']}% "
          f"fidelity_ok={validation['fidelity_ok']}")
    gate = report["gate"]
    print(f"GATE: {'PASS' if gate['pass'] else 'NOT YET'} {gate['checks']}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-09-20")
    parser.add_argument("--until", default="2026-10-19")
    parser.add_argument("--slippage-bps", type=float, default=3.0)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    factor_rules, params = erb._load_configs()
    trades = erb.load_trades(args.since, args.until)
    print(f"交易: {len(trades)} 笔 (since {args.since} until {args.until})")
    report = compute(trades, factor_rules, params, args.slippage_bps)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(report)
    print(f"已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
