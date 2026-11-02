#!/usr/bin/env python3
"""C: ATR-normalised trail floor and delayed activation, read-only replay.

Pre-registration: docs/debates/DEB-20261028-bucket-layering-and-tiered-exit.md §17.

The script reads SQLite in ``mode=ro``, reads only the shared 1min bar cache,
and writes JSON/JSONL under ``data/audits``.  It never calls a trading API,
updates ``trade_records``/``playbook_instructions``, or changes production
configuration.  The exit maths is imported from
``scripts/exit_replay_backtest.py`` (and therefore from
``app.playbook.exit_engine``); this file only assembles the pre-registered arms.

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/atr_trail_replay_c.py \
      --since 2026-09-18 --until 2026-10-27
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import scripts.exit_replay_backtest as eng  # noqa: E402
from app.playbook.atr_trail_replay import (  # noqa: E402
    C1B_FLOOR_HI,
    C1B_K,
    C1_FLOOR_HI,
    C1_FLOOR_LO,
    C1_K,
    C2B_ACTIVATION_PCT,
    C2_ACTIVATION_PCT,
    SCHEMA,
    adaptive_cfg_with_floor,
    atr_floor_pct,
    atr_fraction_from_metadata,
    build_activation_anchor,
    bucket_from_metadata,
    direction_sign,
    evaluate_gates,
    fidelity_summary,
    find_activation_bar,
    grouped_paired_summaries,
    is_modelable_live_reason,
    paired_delta_summary,
)
from app.utils.holiday_calendar import US_HOLIDAYS  # noqa: E402
from app.utils.timezone import ET  # noqa: E402


DEFAULT_SINCE = "2026-09-18"
DEFAULT_UNTIL = "2026-10-27"
SLIPPAGE_BPS = 3.0
BE_TRIGGER_PAD_PCT = 0.35
DEFAULT_OUT_DIR = ROOT / "data" / "audits"
BAR_DIR = ROOT / "data" / "replay_bars"

ARM_SPECS = {
    "C0": {"kind": "baseline", "label": "production adaptive trailing"},
    "C1": {"kind": "floor", "k": C1_K, "floor_hi": C1_FLOOR_HI,
           "label": "floor=clamp(0.15*ATR%, 0.40, 1.00)"},
    "C1b": {"kind": "floor", "k": C1B_K, "floor_hi": C1B_FLOOR_HI,
            "label": "floor=clamp(0.10*ATR%, 0.40, 0.80) sensitivity"},
    "C2": {"kind": "activation", "activation": C2_ACTIVATION_PCT,
           "label": "trail activation=1.0% peak MFE"},
    "C2b": {"kind": "activation", "activation": C2B_ACTIVATION_PCT,
            "label": "trail activation=0.7% peak MFE sensitivity"},
}


def _load_metadata(ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    if not ids:
        return {}
    con = sqlite3.connect(f"file:{eng.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: Dict[int, Dict[str, Any]] = {}
    try:
        qmarks = ",".join(str(int(i)) for i in ids)
        rows = con.execute(
            f"SELECT id, fees_est, data_quality, metadata_json "
            f"FROM trade_records WHERE id IN ({qmarks})"
        )
        for row in rows:
            out[int(row["id"])] = {
                "fees_est": float(row["fees_est"] or 0.0),
                "data_quality": str(row["data_quality"] or ""),
                "metadata_json": row["metadata_json"] or "{}",
            }
    finally:
        con.close()
    return out


def _load_cached_bars(
    symbol: str, entry_et: datetime, exit_et: datetime
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Load only ``data/replay_bars``; never touch OpenD."""
    stem = symbol.replace(".", "_")
    day = entry_et.date()
    end = exit_et.date()
    required: List[str] = []
    while day <= end:
        if day.weekday() < 5 and day not in US_HOLIDAYS:
            required.append(day.isoformat())
        day += timedelta(days=1)
    if not required:
        return None, "no_trading_days"

    bars: List[Dict[str, Any]] = []
    missing: List[str] = []
    for day_text in required:
        path = BAR_DIR / f"{stem}_{day_text}.json"
        if not path.exists():
            missing.append(day_text)
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            missing.append(day_text + "#read")
            continue
        if eng.bars_truncated(raw, day_text):
            missing.append(day_text + "#truncated")
            continue
        for row in raw:
            if len(row) < 5:
                continue
            try:
                stamp = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            bars.append({
                "t": ET.localize(stamp),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
            })
    if missing:
        return None, "cache_missing:" + ",".join(missing[:6])

    entry_minute = entry_et.replace(second=0, microsecond=0)
    bars = [b for b in bars if b["t"] > entry_minute]
    bars.sort(key=lambda b: b["t"])
    dedup: Dict[datetime, Dict[str, Any]] = {}
    for bar in bars:
        dedup[bar["t"]] = bar
    bars = [dedup[t] for t in sorted(dedup)]
    if len(bars) < 5:
        return None, "post_entry_bars_lt5"
    if bars[0]["t"].date() != entry_et.date():
        return None, "entry_day_not_first"
    if bars[-1]["t"].date() != exit_et.date():
        return None, "exit_day_not_last"
    return bars, None


def _net_result(tr: Mapping[str, Any], replay: Mapping[str, Any], fee: float) -> Dict[str, Any]:
    sign = direction_sign(tr["direction"])
    if sign is None:
        raise ValueError(f"unknown direction: {tr['direction']}")
    entry = float(tr["entry"])
    qty = float(tr["qty"])
    exit_price = float(replay["exit_price"])
    gross = qty * sign * (exit_price - entry)
    net = gross - fee
    notional = entry * qty
    return {
        "net": round(net, 4),
        "gross": round(gross, 4),
        "net_bps": round(net / notional * 10000.0, 4) if notional else 0.0,
        "exit_price": round(exit_price, 6),
        "exit_pct": round((exit_price - entry) * sign / entry * 100.0, 6),
        "reason": str(replay.get("reason") or ""),
        "held_min": round(float(replay.get("held_min") or 0.0), 4),
        "mfe_pct": round(float(replay.get("mfe_pct") or 0.0), 6),
    }


def _run_c2(
    tr: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    exit_cfg: Mapping[str, Any],
    params: Mapping[str, Any],
    ordering: str,
    activation_pct: float,
    base_adaptive: Mapping[str, Any],
) -> Dict[str, Any]:
    """Pure trail activation: pass A has no trail; pass B anchors after activation."""
    if not bool(exit_cfg.get("trailing_enabled", False)):
        return eng.replay_trade(
            tr, bars, dict(exit_cfg), {"name": "C2_no_trail", "mfe_stall": False},
            params, ordering,
            SLIPPAGE_BPS, adaptive_cfg=dict(base_adaptive))
    pre_cfg = dict(exit_cfg)
    pre_cfg["drawdown_from_peak_pct"] = 99.0
    pass_a = eng.replay_trade(
        tr, bars, pre_cfg, {"name": "C2_pre", "mfe_stall": False}, params, ordering,
        SLIPPAGE_BPS, adaptive_cfg=None)
    exit_time = tr["entry_et"] + timedelta(minutes=float(pass_a.get("held_min") or 0.0))
    activation = find_activation_bar(
        bars, tr["direction"], tr["entry"], activation_pct, exit_time)
    if not activation:
        return pass_a
    anchor = build_activation_anchor(bars, int(activation["index"]), float(activation["peak_pct"]))
    if not anchor:
        return pass_a
    return eng.replay_trade(
        tr, bars, dict(exit_cfg), {"name": "C2", "mfe_stall": False}, params, ordering,
        SLIPPAGE_BPS, anchor=anchor, adaptive_cfg=dict(base_adaptive))


def _base_adaptive_from_config() -> Dict[str, Any]:
    raw = yaml.safe_load(eng.UNIFIED_YAML.read_text())
    pb = (raw or {}).get("playbook", {}).get("execution_parameters", {}) or {}
    return {
        "adaptive_trailing_enabled": bool(pb.get("adaptive_trailing_enabled", False)),
        "adaptive_trailing_target_capture_pct": float(
            pb.get("adaptive_trailing_target_capture_pct", 60.0)),
        "adaptive_trailing_min_drawdown_pct": float(
            pb.get("adaptive_trailing_min_drawdown_pct", 0.4)),
        "adaptive_trailing_max_drawdown_pct": float(
            pb.get("adaptive_trailing_max_drawdown_pct", 1.2)),
    }


def _atr_band(atr_pct: float) -> str:
    if atr_pct < 3.5:
        return "<3.5%"
    if atr_pct < 5.0:
        return "3.5-5%"
    if atr_pct < 8.0:
        return "5-8%"
    return ">=8%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=DEFAULT_SINCE)
    parser.add_argument("--until", default=DEFAULT_UNTIL)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    factor_rules, params = eng._load_configs()
    params = dict(params)
    params["fee_cushion_pct"] = BE_TRIGGER_PAD_PCT
    base_adaptive = _base_adaptive_from_config()
    if not base_adaptive.get("adaptive_trailing_enabled"):
        print("PREREG_STALE: adaptive trailing is off; §17 assumes it is on")
        return 4

    trades = eng.load_trades(args.since, args.until)
    print(f"loader: {len(trades)} 笔 ({args.since}..{args.until})")
    metadata = _load_metadata([int(t["id"]) for t in trades])

    excluded: Counter = Counter()
    data_quality: Counter = Counter()
    prepared: List[Dict[str, Any]] = []
    for tr in trades:
        meta = metadata.get(int(tr["id"]))
        if not meta:
            excluded["no_metadata"] += 1
            continue
        data_quality[meta["data_quality"] or "NULL"] += 1
        meta_obj = json.loads(meta["metadata_json"] or "{}")
        if meta_obj.get("exclusion_reason"):
            excluded["exclusion_reason"] += 1
            continue
        live_reason = str(tr.get("live_reason") or "").strip().upper()
        if not is_modelable_live_reason(live_reason):
            excluded[f"reason:{live_reason or 'EMPTY'}"] += 1
            continue
        atr = atr_fraction_from_metadata(meta_obj)
        if atr is None:
            excluded["no_atr"] += 1
            continue
        bars, error = _load_cached_bars(tr["symbol"], tr["entry_et"], tr["exit_et"])
        if bars is None:
            excluded[f"bars:{error}"] += 1
            continue
        fee = float(meta["fees_est"] or 0.0)
        if fee <= 0:
            fee = float(eng._fee_of(tr) or 0.0)
            excluded["fee_fallback"] += 1
        exit_cfg = eng._get_factor_exit_config(
            tr["conditions"], factor_rules, direction=tr["direction"])
        prepared.append({
            "tr": tr,
            "bars": bars,
            "exit_cfg": exit_cfg,
            "fee": fee,
            "atr": atr,
            "atr_pct": atr * 100.0,
            "atr_band": _atr_band(atr * 100.0),
            "bucket": bucket_from_metadata(meta_obj),
            "trailing": bool(exit_cfg.get("trailing_enabled", False)),
            "live_reason": live_reason,
            "live_exit_pct": round(
                (float(tr["live_exit"]) - float(tr["entry"]))
                * float(direction_sign(tr["direction"]))
                / float(tr["entry"]) * 100.0, 6),
        })

    days_full = sorted({p["tr"]["entry_et"].date().isoformat() for p in prepared})
    treat = [p for p in prepared if p["trailing"]]
    days_treat = sorted({p["tr"]["entry_et"].date().isoformat() for p in treat})
    sample = {
        "loaded_trades": len(trades),
        "prepared_n": len(prepared),
        "excluded": dict(sorted(excluded.items())),
        "data_quality": dict(sorted(data_quality.items())),
        "n_full": len(prepared),
        "days_full": len(days_full),
        "n_treat": len(treat),
        "days_treat": len(days_treat),
        "bucket_counts": dict(sorted(Counter(p["bucket"] for p in prepared).items())),
        "atr_band_counts": dict(sorted(Counter(p["atr_band"] for p in prepared).items())),
        "atr_pct_min": round(min(p["atr_pct"] for p in prepared), 4) if prepared else None,
        "atr_pct_max": round(max(p["atr_pct"] for p in prepared), 4) if prepared else None,
        "atr_pct_mean": round(sum(p["atr_pct"] for p in prepared) / len(prepared), 4)
        if prepared else None,
    }
    print(f"prepared: {len(prepared)} 笔 / {len(days_full)} 日; "
          f"treated: {len(treat)} 笔 / {len(days_treat)} 日")
    if excluded:
        print(f"excluded: {dict(excluded)}")

    if len(prepared) < 30 or len(days_full) < 10 or len(treat) < 30 or len(days_treat) < 10:
        report = {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": [args.since, args.until],
            "prereg": "DEB-20261028-bucket-layering-and-tiered-exit §17",
            "sample": sample,
            "fidelity": None,
            "arms": {},
            "verdicts": {"C": "INSUFFICIENT_N"},
        }
        _write_report(report, [], Path(args.out_dir))
        print("===> INSUFFICIENT_N: 不改生产 trail/SL")
        return 2

    # ── C0 baseline (still not a variant): fidelity gate ──
    c0: Dict[str, Dict[int, Dict[str, Any]]] = {"pess": {}, "opt": {}}
    for ordering in ("pess", "opt"):
        for item in prepared:
            tr = item["tr"]
            rp = eng.replay_trade(
                tr, item["bars"], item["exit_cfg"],
                {"name": "C0", "mfe_stall": False}, params,
                ordering, SLIPPAGE_BPS, adaptive_cfg=base_adaptive)
            c0[ordering][int(tr["id"])] = _net_result(tr, rp, item["fee"])

    fidelity_rows = []
    for item in treat:
        tr = item["tr"]
        replay = c0["pess"][int(tr["id"])]
        fidelity_rows.append({
            "id": int(tr["id"]),
            "symbol": tr["symbol"],
            "day": tr["entry_et"].date().isoformat(),
            "live_reason": item["live_reason"],
            "replay_reason": replay["reason"],
            "live_exit_pct": item["live_exit_pct"],
            "replay_exit_pct": replay["exit_pct"],
        })
    fidelity = fidelity_summary(fidelity_rows)
    print(f"\n── 保真门 (C0 vs live, treated n={fidelity.get('n')}) ──")
    print(f"  reason_match={fidelity.get('reason_match_pct')} (≥0.80): "
          f"{fidelity.get('reason_match_pass')}")
    print(f"  BE {fidelity.get('be_bucket_replay')} vs live {fidelity.get('be_bucket_live')} "
          f"{fidelity.get('be_bucket_range')}: {fidelity.get('be_bucket_pass')}")
    print(f"  exit% median delta={fidelity.get('exit_pct_delta_median_pp')}pp (≤0.35): "
          f"{fidelity.get('exit_pct_delta_pass')}")

    if not fidelity.get("PASS"):
        report = {
            "schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": [args.since, args.until],
            "prereg": "DEB-20261028-bucket-layering-and-tiered-exit §17",
            "params": {
                "slippage_bps": SLIPPAGE_BPS,
                "fee_cushion_pct": params["fee_cushion_pct"],
                "base_adaptive": base_adaptive,
            },
            "sample": sample,
            "fidelity": fidelity,
            "arms": {},
            "verdicts": {"C": "FIDELITY_FAIL"},
        }
        jsonl_rows = [
            {"arm": "C0", "ordering": "pess", **row} for row in fidelity_rows
        ]
        _write_report(report, jsonl_rows, Path(args.out_dir))
        print("===> FIDELITY_FAIL: 不读 C1/C2，不改生产 trail/SL")
        return 3

    # ── Variants (only after the fidelity gate) ──
    arm_results: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
    for arm, spec in ARM_SPECS.items():
        if arm == "C0":
            continue
        arm_results[arm] = {"pess": {}, "opt": {}}
        for ordering in ("pess", "opt"):
            for item in prepared:
                tr = item["tr"]
                if spec["kind"] == "floor":
                    floor = atr_floor_pct(
                        item["atr"], k=spec["k"], floor_lo=C1_FLOOR_LO,
                        floor_hi=spec["floor_hi"])
                    adaptive = adaptive_cfg_with_floor(base_adaptive, floor)
                    rp = eng.replay_trade(
                        tr, item["bars"], item["exit_cfg"],
                        {"name": arm, "mfe_stall": False}, params,
                        ordering, SLIPPAGE_BPS, adaptive_cfg=adaptive)
                else:
                    rp = _run_c2(
                        tr, item["bars"], item["exit_cfg"], params, ordering,
                        float(spec["activation"]), base_adaptive)
                arm_results[arm][ordering][int(tr["id"])] = _net_result(
                    tr, rp, item["fee"])

    arm_reports: Dict[str, Any] = {}
    verdicts: Dict[str, str] = {}
    jsonl_rows: List[Dict[str, Any]] = []
    for arm, spec in ARM_SPECS.items():
        if arm == "C0":
            continue
        arm_reports[arm] = {"definition": spec["label"]}
        for ordering in ("pess", "opt"):
            rows = []
            for item in prepared:
                tr = item["tr"]
                row = {
                    "id": int(tr["id"]),
                    "day": tr["entry_et"].date().isoformat(),
                    "symbol": tr["symbol"],
                    "bucket": item["bucket"],
                    "atr_pct": round(item["atr_pct"], 4),
                    "atr_band": item["atr_band"],
                    "trailing": item["trailing"],
                    "baseline": c0[ordering][int(tr["id"])],
                    "arm": arm_results[arm][ordering][int(tr["id"])],
                }
                rows.append(row)
                jsonl_rows.append({
                    "arm": arm,
                    "ordering": ordering,
                    "id": row["id"],
                    "day": row["day"],
                    "symbol": row["symbol"],
                    "bucket": row["bucket"],
                    "atr_pct": row["atr_pct"],
                    "trailing": row["trailing"],
                    "baseline_net": row["baseline"]["net"],
                    "arm_net": row["arm"]["net"],
                    "delta_net": round(row["arm"]["net"] - row["baseline"]["net"], 4),
                    "baseline_reason": row["baseline"]["reason"],
                    "arm_reason": row["arm"]["reason"],
                })
            full = paired_delta_summary(rows)
            treat_rows = [r for r in rows if r["trailing"]]
            treat_summary = paired_delta_summary(treat_rows)
            arm_reports[arm][f"full_{ordering}"] = full
            arm_reports[arm][f"treat_{ordering}"] = treat_summary
            arm_reports[arm][f"bucket_{ordering}"] = grouped_paired_summaries(
                treat_rows, "bucket")
            arm_reports[arm][f"atr_band_{ordering}"] = grouped_paired_summaries(
                treat_rows, "atr_band")

        treat_pess = arm_reports[arm]["treat_pess"]
        treat_opt = arm_reports[arm]["treat_opt"]
        gates = evaluate_gates(
            n_full=sample["n_full"],
            days_full=sample["days_full"],
            n_treat=sample["n_treat"],
            days_treat=sample["days_treat"],
            treat=treat_pess,
            pess=treat_pess,
            opt=treat_opt,
        )
        arm_reports[arm]["gates"] = gates
        if arm in ("C1", "C2"):
            verdicts[arm] = gates["verdict"]

    # Dose fragility: primary pass but lower-dose sensitivity does not improve.
    for primary, sensitivity in (("C1", "C1b"), ("C2", "C2b")):
        primary_pass = verdicts.get(primary) == "PASS_CANDIDATE"
        sens = arm_reports.get(sensitivity, {}).get("treat_pess", {})
        fragile = bool(primary_pass and float(sens.get("delta_net_sum") or 0.0) <= 0)
        arm_reports[primary]["dose_fragile"] = fragile
        if fragile:
            arm_reports[primary]["dose_note"] = (
                f"{primary} passes but {sensitivity} treat Δnet <= 0; label DOSE_FRAGILE")

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": [args.since, args.until],
        "prereg": "DEB-20261028-bucket-layering-and-tiered-exit §17",
        "params": {
            "slippage_bps": SLIPPAGE_BPS,
            "fee_cushion_pct": params["fee_cushion_pct"],
            "be_trigger_pad_note": "0.15 fee + 0.20 slip = 0.35; engine settles at line",
            "base_adaptive": base_adaptive,
        },
        "sample": sample,
        "fidelity": fidelity,
        "arms": arm_reports,
        "verdicts": verdicts,
        "caveats": [
            "1min bars cannot order high/low within a bar; pess/opt are bounds.",
            "Engine settles BE at the trigger line; live limit fills may differ by up to ~0.2pp.",
            "Production fee-aware cushion is approximated by fixed 0.15 + 0.20.",
            "Production cooldown exemption for BE-armed positions is not modelled; fidelity gate is the guard.",
            "No production config/trail/SL was changed; PASS_CANDIDATE is not an approval.",
        ],
    }
    _write_report(report, jsonl_rows, Path(args.out_dir))

    print("\n── C 结果 (pess, treated) ──")
    for arm in ("C1", "C1b", "C2", "C2b"):
        row = arm_reports[arm]["treat_pess"]
        print(f"  {arm}: n={row.get('n')} Δnet=${row.get('delta_net_sum'):,.2f} "
              f"mean={row.get('avg_delta_bps')}bps "
              f"t={(row.get('daily_cluster_bps') or {}).get('t')} "
              f"MDE80={row.get('mde80_bps')} "
              f"verdict={arm_reports[arm]['gates']['verdict']}")
    print("\n===> verdicts: " + ", ".join(f"{k}={v}" for k, v in verdicts.items()))
    return 0


def _write_report(report: Mapping[str, Any], jsonl_rows: Iterable[Mapping[str, Any]],
                  out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"atr_trail_replay_c_{stamp}.json"
    jsonl_path = out_dir / f"atr_trail_replay_c_{stamp}.jsonl"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in jsonl_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\nartefacts: {json_path} / {jsonl_path}")


if __name__ == "__main__":
    raise SystemExit(main())
