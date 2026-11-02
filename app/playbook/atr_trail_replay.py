"""Pure helpers for the C-class ATR trail replay (research-only).

This module deliberately has no runtime imports and performs no I/O.  It is
loaded only by ``scripts/atr_trail_replay_c.py`` and its tests.  The replay
itself must use ``scripts/exit_replay_backtest.py::replay_trade`` and the
production ``_compute_dynamic_effective_sl``; this module only encodes the
pre-registered C sample rules, ATR floor mapping, activation lookup, daily
clustering/MDE, and gate arithmetic.

Pre-registration: DEB-20261028-bucket-layering-and-tiered-exit §17.

Units:
    atr_fraction              fraction of price (0.0461 == 4.61%)
    floor_pct / dd_pct        percent of price (0.40 == 0.40%)
    net / gross               USD
    net_bps / delta_bps       basis points of entry notional
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA = "atr_trail_replay_v1"
MDE80_Z = 1.959963984540054 + 0.8416212335729143
LEFT_TAIL_NET_BPS = -100.0

MODELABLE_LIVE_REASONS = frozenset({
    "SL_INITIAL",
    "SL_BREAKEVEN",
    "SL_TRAIL_LOCK",
    "TAKE_PROFIT",
    "PRE_CLOSE_EXIT",
    "MFE_STALL_EXIT",
    "TIME_EXIT",
    "REPLAY_EOD",
})

NON_MODELABLE_LIVE_REASONS = frozenset({
    "DEAD_SIGNAL_EXIT",
    "OVERNIGHT_GAP_STOP",
    "PATIENT_EXIT_EOD",
    "SL_CERT_LOCK",
})

C1_K = 0.15
C1_FLOOR_LO = 0.40
C1_FLOOR_HI = 1.00
C1B_K = 0.10
C1B_FLOOR_HI = 0.80
C2_ACTIVATION_PCT = 1.00
C2B_ACTIVATION_PCT = 0.70
FIDELITY_REASON_MATCH_MIN = 0.80
FIDELITY_EXIT_PCT_MEDIAN_MAX = 0.35
FIDELITY_BE_RANGE = (0.75, 1.25)


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> Optional[float]:
    number = _finite_float(value)
    return number if number is not None and number > 0 else None


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def direction_sign(direction: Any) -> Optional[float]:
    text = str(direction or "").strip().upper()
    if text in ("LONG", "BUY"):
        return 1.0
    if text in ("SHORT", "SELL", "SELL_SHORT"):
        return -1.0
    return None


def atr_fraction_from_metadata(metadata: Any) -> Optional[float]:
    """Return the effective ATR fraction: repriced first, generated second."""
    obj = parse_json_object(metadata)
    lineage = obj.get("bucket_lineage")
    if not isinstance(lineage, Mapping):
        return None
    for key in ("atr_pct_repriced", "atr_pct_generated"):
        value = _positive_float(lineage.get(key))
        if value is not None:
            return value
    return None


def bucket_from_metadata(metadata: Any) -> str:
    obj = parse_json_object(metadata)
    lineage = obj.get("bucket_lineage")
    if isinstance(lineage, Mapping):
        bucket = str(lineage.get("bucket_effective") or "").strip()
        if bucket:
            return bucket
    attribution = obj.get("attribution")
    if isinstance(attribution, Mapping):
        bucket = str(attribution.get("vol_bucket") or "").strip()
        if bucket:
            return bucket
    return "unknown"


def atr_floor_pct(
    atr_fraction: Any,
    *,
    k: float = C1_K,
    floor_lo: float = C1_FLOOR_LO,
    floor_hi: float = C1_FLOOR_HI,
) -> Optional[float]:
    """Map an ATR fraction to a bounded trail floor in percent.

    ``floor = clamp(k * ATR%, floor_lo, floor_hi)``.  The default k=0.15 is
    pre-registered C1; at ATR 2.67% it equals the current 0.40% floor, so
    low-volatility behaviour is preserved rather than uniformly widened.
    """
    atr = _positive_float(atr_fraction)
    if atr is None:
        return None
    if k <= 0 or floor_lo <= 0 or floor_hi < floor_lo:
        return None
    raw = k * atr * 100.0
    return round(min(max(raw, floor_lo), floor_hi), 6)


def adaptive_cfg_with_floor(
    base_cfg: Mapping[str, Any], floor_pct: Any
) -> Dict[str, Any]:
    """Copy a production adaptive config and replace only the floor.

    The max drawdown is never reduced below the floor; with the pre-registered
    floor <= 1.00 and production max 1.20 the max is unchanged.
    """
    cfg = dict(base_cfg or {})
    floor = _positive_float(floor_pct)
    if floor is None:
        return cfg
    max_dd = _positive_float(cfg.get("adaptive_trailing_max_drawdown_pct")) or 1.2
    if max_dd < floor:
        max_dd = floor
    cfg["adaptive_trailing_min_drawdown_pct"] = round(floor, 6)
    cfg["adaptive_trailing_max_drawdown_pct"] = round(max_dd, 6)
    return cfg


def is_modelable_live_reason(reason: Any) -> bool:
    return str(reason or "").strip().upper() in MODELABLE_LIVE_REASONS


def find_activation_bar(
    bars: Sequence[Mapping[str, Any]],
    direction: Any,
    entry_price: float,
    activation_pct: float,
    exit_time: Optional[datetime],
) -> Optional[Dict[str, Any]]:
    """Find the first bar whose favourable extreme reaches activation.

    The bar must be strictly earlier than the pass-A exit bar.  Trail starts on
    the *next* bar; if activation happens on the exit bar itself, this returns
    ``None`` because the pre-registered model does not activate same-bar.
    """
    sign = direction_sign(direction)
    entry = _positive_float(entry_price)
    activation = _positive_float(activation_pct)
    if sign is None or entry is None or activation is None or not bars:
        return None
    cutoff = exit_time - timedelta(seconds=1) if exit_time is not None else None
    peak = 0.0
    for index, bar in enumerate(bars):
        bar_time = bar.get("t")
        if cutoff is not None and isinstance(bar_time, datetime) and bar_time >= cutoff:
            return None
        favourable = bar.get("h") if sign > 0 else bar.get("l")
        price = _finite_float(favourable)
        if price is None:
            continue
        pct = (price - entry) * sign / entry * 100.0
        if pct > peak:
            peak = pct
        if peak >= activation:
            return {
                "index": index,
                "bar_time": bar_time,
                "peak_pct": round(peak, 6),
            }
    return None


def build_activation_anchor(
    bars: Sequence[Mapping[str, Any]],
    activation_index: int,
    peak_pct: float,
) -> Optional[Dict[str, Any]]:
    """Anchor on the bar after activation; same-bar activation is not modelled."""
    if activation_index < 0 or activation_index + 1 >= len(bars):
        return None
    anchor_bar = bars[activation_index + 1]
    bar_time = anchor_bar.get("t")
    if not isinstance(bar_time, datetime):
        return None
    return {"t": bar_time, "peak_pct": round(float(peak_pct), 6)}


def daily_cluster_stats(values_by_day: Mapping[str, Sequence[float]]) -> Dict[str, Any]:
    """Equal-weight daily means -> clustered mean/t/95% CI/MDE80."""
    daily_means = [
        statistics.fmean(float(v) for v in values)
        for _day, values in sorted(values_by_day.items())
        if values
    ]
    n_days = len(daily_means)
    if n_days == 0:
        return {"days": 0, "mean": None, "t": None, "ci95_low": None,
                "ci95_high": None, "mde80": None}
    mean = statistics.fmean(daily_means)
    if n_days < 2:
        return {"days": 1, "mean": round(mean, 6), "t": None,
                "ci95_low": None, "ci95_high": None, "mde80": None}
    sd = statistics.stdev(daily_means)
    se = sd / math.sqrt(n_days)
    if se == 0:
        return {"days": n_days, "mean": round(mean, 6), "t": None,
                "ci95_low": round(mean, 6), "ci95_high": round(mean, 6),
                "mde80": 0.0}
    return {
        "days": n_days,
        "mean": round(mean, 6),
        "t": round(mean / se, 4),
        "ci95_low": round(mean - 1.959963984540054 * se, 6),
        "ci95_high": round(mean + 1.959963984540054 * se, 6),
        "mde80": round(MDE80_Z * se, 6),
    }


def _rr(nets: Sequence[float]) -> Optional[float]:
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    if not wins or not losses:
        return None
    avg_win = statistics.fmean(wins)
    avg_loss = abs(statistics.fmean(losses))
    return round(avg_win / avg_loss, 4) if avg_loss > 0 else None


def _win_rate(nets: Sequence[float]) -> Optional[float]:
    if not nets:
        return None
    return round(sum(1 for x in nets if x > 0) / len(nets), 4)


def paired_delta_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Summarise an arm against C0 on paired rows.

    Each row::

        {"id", "day", "bucket", "atr_pct", "direction",
         "baseline": {"net", "net_bps", "reason"},
         "arm": {"net", "net_bps", "reason"}}
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "days": 0}
    base_net = [float(r["baseline"]["net"]) for r in rows]
    arm_net = [float(r["arm"]["net"]) for r in rows]
    base_bps = [float(r["baseline"]["net_bps"]) for r in rows]
    arm_bps = [float(r["arm"]["net_bps"]) for r in rows]
    delta_net = [a - b for a, b in zip(arm_net, base_net)]
    delta_bps = [a - b for a, b in zip(arm_bps, base_bps)]

    by_day_bps: Dict[str, List[float]] = {}
    by_day_usd: Dict[str, List[float]] = {}
    by_day_sum_usd: Dict[str, float] = {}
    for row, d_net, d_bps in zip(rows, delta_net, delta_bps):
        day = str(row.get("day") or "")
        by_day_bps.setdefault(day, []).append(d_bps)
        by_day_usd.setdefault(day, []).append(d_net)
        by_day_sum_usd[day] = by_day_sum_usd.get(day, 0.0) + d_net

    avoided = [(b, a) for b, a in zip(base_net, arm_net) if b < 0 and a > b]
    killed = [(b, a) for b, a in zip(base_net, arm_net) if b > 0 and a <= 0]
    rescued = [(b, a) for b, a in zip(base_net, arm_net) if b <= 0 and a > 0]
    winner_pairs = [(b, a) for b, a in zip(base_net, arm_net) if b > 0]

    left_tail = [(b, a) for b, a, bp in zip(base_net, arm_net, base_bps)
                 if bp <= LEFT_TAIL_NET_BPS]
    right_tail = [(b, a) for b, a, bp in zip(base_net, arm_net, base_bps)
                  if bp >= -LEFT_TAIL_NET_BPS]

    transitions: Dict[str, int] = {}
    for row in rows:
        key = f"{row['baseline'].get('reason', '')}->{row['arm'].get('reason', '')}"
        transitions[key] = transitions.get(key, 0) + 1

    max_day = max(by_day_sum_usd.items(), key=lambda kv: abs(kv[1])) if by_day_sum_usd else ("", 0.0)
    max_trade_index = max(range(n), key=lambda i: abs(delta_net[i]))

    return {
        "n": n,
        "days": len(by_day_usd),
        "baseline_net_sum": round(sum(base_net), 2),
        "arm_net_sum": round(sum(arm_net), 2),
        "delta_net_sum": round(sum(delta_net), 2),
        "avg_delta_net": round(statistics.fmean(delta_net), 4),
        "avg_delta_bps": round(statistics.fmean(delta_bps), 4),
        "median_delta_bps": round(statistics.median(delta_bps), 4),
        "daily_cluster_bps": daily_cluster_stats(by_day_bps),
        "daily_cluster_usd": daily_cluster_stats(by_day_usd),
        "mde80_bps": daily_cluster_stats(by_day_bps).get("mde80"),
        "mde80_usd": daily_cluster_stats(by_day_usd).get("mde80"),
        "baseline_rr": _rr(base_net),
        "arm_rr": _rr(arm_net),
        "baseline_win_rate": _win_rate(base_net),
        "arm_win_rate": _win_rate(arm_net),
        "baseline_sl_initial_n": sum(
            1 for r in rows if str(r["baseline"].get("reason")) == "SL_INITIAL"),
        "arm_sl_initial_n": sum(
            1 for r in rows if str(r["arm"].get("reason")) == "SL_INITIAL"),
        "avoided_loss_n": len(avoided),
        "avoided_loss_usd": round(sum(a - b for b, a in avoided), 2),
        "killed_winners_n": len(killed),
        "killed_winners_profit_usd": round(sum(b - a for b, a in killed), 2),
        "rescued_losers_n": len(rescued),
        "rescued_losers_usd": round(sum(a - b for b, a in rescued), 2),
        "winner_profit_erosion_usd": round(
            sum(max(0.0, b - a) for b, a in winner_pairs), 2),
        "left_tail_n": len(left_tail),
        "left_tail_baseline_net_usd": round(sum(b for b, _a in left_tail), 2),
        "left_tail_arm_net_usd": round(sum(a for _b, a in left_tail), 2),
        "left_tail_reduction_usd": round(
            sum(a for _b, a in left_tail) - sum(b for b, _a in left_tail), 2),
        "right_tail_n": len(right_tail),
        "right_tail_baseline_net_usd": round(sum(b for b, _a in right_tail), 2),
        "right_tail_arm_net_usd": round(sum(a for _b, a in right_tail), 2),
        "right_tail_change_usd": round(
            sum(a for _b, a in right_tail) - sum(b for b, _a in right_tail), 2),
        "positive_delta_n": sum(1 for x in delta_net if x > 0),
        "negative_delta_n": sum(1 for x in delta_net if x < 0),
        "max_day": max_day[0],
        "max_day_delta_usd": round(max_day[1], 2),
        "max_day_abs_delta_usd": round(abs(max_day[1]), 2),
        "max_trade_id": rows[max_trade_index].get("id"),
        "max_trade_delta_usd": round(delta_net[max_trade_index], 2),
        "max_trade_abs_delta_usd": round(abs(delta_net[max_trade_index]), 2),
        "reason_transitions": dict(sorted(transitions.items())),
    }


def fidelity_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """C0 replay-vs-live fidelity on the pre-registered treatment cohort."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "PASS": False, "reason": "no_rows"}
    matches = sum(1 for r in rows if r.get("replay_reason") == r.get("live_reason"))
    reason_match = matches / n
    be_replay = sum(1 for r in rows if r.get("replay_reason") == "SL_BREAKEVEN")
    be_live = sum(1 for r in rows if r.get("live_reason") == "SL_BREAKEVEN")
    be_lo = int(round(be_live * FIDELITY_BE_RANGE[0]))
    be_hi = int(round(be_live * FIDELITY_BE_RANGE[1]))
    deltas = sorted(abs(float(r.get("replay_exit_pct") or 0.0)
                        - float(r.get("live_exit_pct") or 0.0)) for r in rows)
    median_delta = deltas[len(deltas) // 2] if deltas else None
    reason_ok = reason_match >= FIDELITY_REASON_MATCH_MIN
    be_ok = be_lo <= be_replay <= be_hi
    pct_ok = median_delta is not None and median_delta <= FIDELITY_EXIT_PCT_MEDIAN_MAX
    transitions: Dict[str, int] = {}
    for row in rows:
        key = f"{row.get('live_reason', '')}->{row.get('replay_reason', '')}"
        transitions[key] = transitions.get(key, 0) + 1
    return {
        "n": n,
        "reason_match_pct": round(reason_match, 4),
        "reason_match_gate": FIDELITY_REASON_MATCH_MIN,
        "reason_match_pass": reason_ok,
        "be_bucket_replay": be_replay,
        "be_bucket_live": be_live,
        "be_bucket_range": [be_lo, be_hi],
        "be_bucket_pass": be_ok,
        "exit_pct_delta_median_pp": round(median_delta, 4) if median_delta is not None else None,
        "exit_pct_delta_gate_pp": FIDELITY_EXIT_PCT_MEDIAN_MAX,
        "exit_pct_delta_pass": pct_ok,
        "reason_transitions": dict(sorted(transitions.items())),
        "PASS": bool(reason_ok and be_ok and pct_ok),
    }


def evaluate_gates(
    *,
    n_full: int,
    days_full: int,
    n_treat: int,
    days_treat: int,
    treat: Mapping[str, Any],
    pess: Mapping[str, Any],
    opt: Mapping[str, Any],
) -> Dict[str, Any]:
    """Apply §17.6 gates.  ``treat``/``pess``/``opt`` are paired summaries."""
    cluster_t = (treat.get("daily_cluster_bps") or {}).get("t")
    mde = treat.get("mde80_bps")
    delta_sum = float(treat.get("delta_net_sum") or 0.0)
    checks = {
        "sample_full_n": n_full >= 30,
        "sample_full_days": days_full >= 10,
        "sample_treat_n": n_treat >= 30,
        "sample_treat_days": days_treat >= 10,
        "g1_net": delta_sum > 0 and float(treat.get("avg_delta_bps") or 0.0) >= 10.0,
        "g2_t": cluster_t is not None and float(cluster_t) >= 2.0,
        "g3_mde": (mde is not None and float(mde) > 0
                   and float(treat.get("avg_delta_bps") or 0.0) >= float(mde)),
        "g4_both_orders": (float(pess.get("delta_net_sum") or 0.0) > 0
                           and float(opt.get("delta_net_sum") or 0.0) > 0),
        "g5_harm_save": (float(treat.get("killed_winners_profit_usd") or 0.0)
                         < float(treat.get("rescued_losers_usd") or 0.0)),
        "g6_sl_initial": (int(treat.get("arm_sl_initial_n") or 0)
                          <= int(treat.get("baseline_sl_initial_n") or 0)),
        "g7_day_concentration": (delta_sum > 0
                                 and float(treat.get("max_day_abs_delta_usd") or 0.0)
                                 <= 0.5 * delta_sum),
        "g7_trade_concentration": (delta_sum > 0
                                   and float(treat.get("max_trade_abs_delta_usd") or 0.0)
                                   <= 0.5 * delta_sum),
    }
    sample_keys = ("sample_full_n", "sample_full_days", "sample_treat_n", "sample_treat_days")
    econ_keys = ("g1_net", "g2_t", "g3_mde", "g4_both_orders", "g5_harm_save",
                 "g6_sl_initial", "g7_day_concentration", "g7_trade_concentration")
    sample_ok = all(checks[k] for k in sample_keys)
    econ_ok = all(checks[k] for k in econ_keys)
    verdict = "INSUFFICIENT_N" if not sample_ok else (
        "PASS_CANDIDATE" if econ_ok else "FAIL_NO_CHANGE")
    return {
        "checks": checks,
        "sample_ok": sample_ok,
        "econ_ok": econ_ok,
        "verdict": verdict,
    }


def grouped_paired_summaries(
    rows: Sequence[Mapping[str, Any]], key: str
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        groups.setdefault(label, []).append(row)
    return {label: paired_delta_summary(group) for label, group in sorted(groups.items())}
