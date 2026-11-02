#!/usr/bin/env python3
"""A1: read-only invalidation replay on 1min bars (2026-10-28).

Three layers, all review-only:
  1. hard initial SL, otherwise actual exit (baseline);
  2. structured close-price invalidation;
  3. legacy text price invalidation.

The script reads SQLite in ``mode=ro``, reads/writes only the shared 1min bar
cache, and writes JSON/JSONL audit artefacts under ``data/audits``.  It never
calls a trading API, updates ``trade_records``/``playbook_instructions``, or
changes production configuration.

Protocol details live in ``app/playbook/invalidation_replay.py``.  The primary
replay is near-close 15:50 proxy + next-bar open + 3bps adverse slippage +
round-trip fee model; 16:00 close→next-open and optimistic intrabar ordering
are explicit sensitivities.

Usage:
  PYTHONPATH=. ./venv/bin/python scripts/invalidation_replay_a1.py \
      --since 2026-06-26 --until 2026-10-26
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.playbook.invalidation_contract import (  # noqa: E402
    ACTION_REVIEW_ONLY,
    compile_invalidation_contract,
    parse_json_object,
    validate_invalidation_contract,
)
from app.playbook.invalidation_replay import (  # noqa: E402
    DEFAULT_SLIPPAGE_BPS,
    PRIMARY_CLOSE_MODE,
    PRIMARY_TRIGGER_ORDER,
    REPLAY_SCHEMA,
    SENSITIVITY_CLOSE_MODE,
    SENSITIVITY_TRIGGER_ORDER,
    SUMMARY_SCHEMA,
    classify_text_price_rule,
    grouped_summaries,
    normalise_bars,
    parse_et_timestamp,
    select_structured_close_trigger,
    simulate_invalidation_trade,
    summarize_arm,
    trigger_semantics_counts,
    trigger_status_counts,
)
from app.utils.holiday_calendar import US_HOLIDAYS, get_us_close_minutes_et  # noqa: E402


DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_BAR_DIR = ROOT / "data" / "replay_bars"
DEFAULT_OUT_DIR = ROOT / "data" / "audits"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _trading_days_between(start: date, end: date) -> List[date]:
    days: List[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in US_HOLIDAYS:
            days.append(current)
        current += timedelta(days=1)
    return days


def _bucket_from_metadata(metadata: Mapping[str, Any]) -> str:
    lineage = metadata.get("bucket_lineage")
    if isinstance(lineage, dict):
        bucket = lineage.get("bucket_effective")
        if bucket:
            return str(bucket)
    attribution = metadata.get("attribution")
    if isinstance(attribution, dict) and attribution.get("vol_bucket"):
        return str(attribution["vol_bucket"])
    return "unknown"


def load_trades(
    db_path: Path, since: Optional[str], until: Optional[str], limit: Optional[int]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    sql = (
        "SELECT t.id AS trade_id, t.symbol, t.direction, t.entry_time, t.exit_time, "
        "t.entry_price, t.exit_price, t.quantity, t.pnl_net, t.metadata_json AS trade_meta, "
        "i.id AS instruction_id, i.entry_price AS instruction_entry, i.stop_loss, "
        "i.invalidation, i.raw_instruction_json "
        "FROM trade_records t "
        "JOIN playbook_instructions i "
        "ON CAST(json_extract(t.metadata_json,'$.playbook_instruction_id') AS INTEGER)=i.id "
        "WHERE t.source_type='PLAYBOOK_SIM' AND t.exit_time IS NOT NULL "
        "AND t.entry_price>0 AND t.exit_price>0 AND t.quantity>0"
    )
    params: List[Any] = []
    if since:
        sql += " AND substr(t.entry_time,1,10)>=?"
        params.append(since)
    if until:
        sql += " AND substr(t.entry_time,1,10)<=?"
        params.append(until)
    sql += " ORDER BY t.id"

    conn = _connect_ro(db_path)
    try:
        rows = [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()
    if limit:
        rows = rows[:limit]

    out: List[Dict[str, Any]] = []
    exclusion = Counter()
    for raw in rows:
        entry_et = parse_et_timestamp(raw.get("entry_time"))
        exit_et = parse_et_timestamp(raw.get("exit_time"))
        if entry_et is None or exit_et is None:
            exclusion["bad_timestamp"] += 1
            continue
        if not (9 <= entry_et.hour < 16):
            exclusion["entry_outside_rth"] += 1
            continue
        if exit_et <= entry_et:
            exclusion["exit_not_after_entry"] += 1
            continue
        trade_meta = parse_json_object(raw.get("trade_meta"))
        stop_loss = trade_meta.get("stop_loss") or raw.get("stop_loss")
        contract = compile_invalidation_contract(
            invalidation=raw.get("invalidation"),
            raw_instruction_json=raw.get("raw_instruction_json"),
            direction=raw.get("direction"),
            entry_price=raw.get("instruction_entry"),
        )
        errors = validate_invalidation_contract(contract)
        if errors:
            exclusion["contract_validation_error"] += 1
            continue
        text_rule = next(
            (rule for rule in contract.get("rules") or []
             if rule.get("source") == "text" and rule.get("metric") == "价格"),
            None,
        )
        text_semantics = None
        if text_rule:
            text_semantics = classify_text_price_rule(
                raw.get("invalidation"), text_rule.get("value"), raw.get("direction")
            )
        structured = select_structured_close_trigger(
            raw.get("raw_instruction_json"), raw.get("direction")
        )
        out.append({
            "id": int(raw["trade_id"]),
            "instruction_id": int(raw["instruction_id"]),
            "symbol": str(raw.get("symbol") or ""),
            "direction": str(raw.get("direction") or "").upper(),
            "entry_time": raw.get("entry_time"),
            "exit_time": raw.get("exit_time"),
            "entry_et": entry_et,
            "exit_et": exit_et,
            "entry_price": float(raw["entry_price"]),
            "exit_price": float(raw["exit_price"]),
            "quantity": float(raw["quantity"]),
            "actual_net_pnl": float(raw.get("pnl_net") or 0.0),
            "stop_loss": float(stop_loss) if stop_loss not in (None, "") else None,
            "bucket_effective": _bucket_from_metadata(trade_meta),
            "contract": contract,
            "contract_validation_errors": errors,
            "structured_trigger": structured,
            "text_rule": text_rule,
            "text_semantics": text_semantics,
        })
    return out, {
        "joined_rows": len(rows),
        "eligible_rows": len(out),
        "excluded": dict(sorted(exclusion.items())),
    }


def _cache_path(bar_dir: Path, symbol: str, day: str) -> Path:
    return bar_dir / f"{symbol.replace('.', '_')}_{day}.json"


def _expected_last_hhmm(day: str) -> str:
    try:
        close = int(get_us_close_minutes_et(date.fromisoformat(day)))
    except Exception:
        close = 16 * 60
    minute = close - 5
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _raw_bars_complete(raw: Any, day: str) -> bool:
    if not isinstance(raw, list) or len(raw) < 30:
        return False
    try:
        first_time = str(raw[0][0] if isinstance(raw[0], (list, tuple)) else raw[0].get("time_key"))
        last_time = str(raw[-1][0] if isinstance(raw[-1], (list, tuple)) else raw[-1].get("time_key"))
        return first_time[11:16] <= "09:35" and last_time[11:16] >= _expected_last_hhmm(day)
    except Exception:
        return False


def _read_cache(path: Path, day: str) -> Tuple[str, Optional[List[Any]]]:
    if not path.exists():
        return "missing", None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "bad_cache", None
    if _raw_bars_complete(raw, day):
        return "cache", raw
    if isinstance(raw, list) and not raw:
        return "empty_cache", raw
    return "truncated", raw


def _fetch_bars(ctx: Any, symbol: str, day: str) -> List[Any]:
    from futu import AuType, KLType, RET_OK
    ret, data, _ = ctx.request_history_kline(
        symbol, ktype=KLType.K_1M, start=day, end=day, max_count=1000,
        autype=AuType.NONE,
    )
    if ret != RET_OK:
        raise RuntimeError(str(data))
    rows: List[Any] = []
    for _, row in data.iterrows():
        rows.append([
            str(row["time_key"]), float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]),
        ])
    return rows


def ensure_bars(
    *,
    trades: Sequence[Mapping[str, Any]],
    bar_dir: Path,
    fetch: bool,
    refresh: bool,
) -> Tuple[Dict[Tuple[str, str], List[Mapping[str, Any]]], Dict[str, Any]]:
    needed: set[Tuple[str, str]] = set()
    for trade in trades:
        for day in _trading_days_between(trade["entry_et"].date(), trade["exit_et"].date()):
            needed.add((trade["symbol"], day.isoformat()))

    status_counts = Counter()
    malformed: List[str] = []
    normalized: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    ctx = None
    try:
        if fetch:
            missing_or_bad = []
            for symbol, day in sorted(needed):
                status, raw = _read_cache(_cache_path(bar_dir, symbol, day), day)
                status_counts[status] += 1
                if (refresh or raw is None
                        or (status != "cache" and status != "empty_cache")):
                    missing_or_bad.append((symbol, day))
            if missing_or_bad:
                from futu import OpenQuoteContext
                ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
                for index, (symbol, day) in enumerate(missing_or_bad, 1):
                    path = _cache_path(bar_dir, symbol, day)
                    try:
                        raw = _fetch_bars(ctx, symbol, day)
                        path.write_text(json.dumps(raw), encoding="utf-8")
                        status_counts["fetched"] += 1
                        if not raw:
                            status_counts["fetched_empty"] += 1
                    except Exception as exc:
                        status_counts["fetch_error"] += 1
                        malformed.append(f"{symbol} {day}: {exc}")
                    if index < len(missing_or_bad):
                        time.sleep(1.1)
        else:
            for symbol, day in sorted(needed):
                status, _raw = _read_cache(_cache_path(bar_dir, symbol, day), day)
                status_counts[status] += 1

        for symbol, day in sorted(needed):
            status, raw = _read_cache(_cache_path(bar_dir, symbol, day), day)
            if status not in ("cache", "empty_cache") or not raw:
                continue
            bars, bad = normalise_bars(raw)
            if bad:
                status_counts["bars_bad_rows"] += bad
            if bars:
                normalized[(symbol, day)] = bars
    finally:
        if ctx is not None:
            ctx.close()
    return normalized, {
        "symbol_days": len(needed),
        "status_counts": dict(sorted(status_counts.items())),
        "fetch_errors_sample": malformed[:20],
    }


def _trade_bars(
    trade: Mapping[str, Any], cache: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]]
) -> List[Mapping[str, Any]]:
    bars: List[Mapping[str, Any]] = []
    for day in _trading_days_between(trade["entry_et"].date(), trade["exit_et"].date()):
        bars.extend(cache.get((trade["symbol"], day.isoformat()), []))
    bars.sort(key=lambda item: item["t"])
    return bars


def _details_for_trigger(
    *,
    trade: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    source: str,
    trigger: Mapping[str, Any],
    variants: Sequence[Tuple[str, str]],
    slippage_bps: float,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for trigger_order, close_mode in variants:
        result = simulate_invalidation_trade(
            trade=trade,
            bars=bars,
            trigger=trigger,
            slippage_bps=slippage_bps,
            trigger_order=trigger_order,
            close_mode=close_mode,
        )
        if result.get("status") != "ok":
            out.append({
                "source": source,
                "variant": f"{close_mode}__{trigger_order}",
                "trade_id": trade["id"],
                "symbol": trade["symbol"],
                "direction": trade["direction"],
                "status": result.get("status"),
                "trigger": dict(trigger),
            })
            continue
        out.append({
            "source": source,
            "variant": f"{close_mode}__{trigger_order}",
            "semantic_status": trigger.get("semantic_status"),
            "condition_class": trigger.get("condition_class"),
            "price_basis": trigger.get("price_basis"),
            **result,
        })
    return out


def _baseline_fidelity(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    errors: List[float] = []
    for row in rows:
        if (row.get("baseline") or {}).get("exit_reason") != "ACTUAL_EXIT":
            continue
        actual = row.get("_actual_net_pnl")
        if actual is None:
            continue
        errors.append(float(row["baseline"]["net_pnl"]) - float(actual))
    if not errors:
        return {"n": 0}
    absolute = [abs(value) for value in errors]
    return {
        "n": len(errors),
        "mean_error_usd": round(sum(errors) / len(errors), 4),
        "median_error_usd": round(sorted(errors)[len(errors) // 2], 4),
        "mae_usd": round(sum(absolute) / len(absolute), 4),
        "within_1usd_pct": round(sum(value <= 1.0 for value in absolute) / len(absolute), 4),
    }


def _arm_payload(
    rows: Sequence[Mapping[str, Any]], arm_label: str
) -> Dict[str, Any]:
    eligible = [
        row for row in rows
        if row.get("source_semantic_status") in (None, "exit")
    ]
    return {
        "pooled": summarize_arm(eligible, arm_label=arm_label),
        "trigger_status": trigger_status_counts(eligible),
        "by_condition_class": grouped_summaries(
            eligible, "condition_class", arm_label=arm_label
        ),
        "by_bucket": grouped_summaries(eligible, "bucket_effective", arm_label=arm_label),
        "by_direction": grouped_summaries(eligible, "direction", arm_label=arm_label),
        "by_price_basis": grouped_summaries(eligible, "price_basis", arm_label=arm_label),
    }


def run_replay(
    *,
    db_path: Path,
    bar_dir: Path,
    since: Optional[str],
    until: Optional[str],
    limit: Optional[int],
    fetch: bool,
    refresh: bool,
    slippage_bps: float,
    out_dir: Path,
    write_outputs: bool,
) -> Dict[str, Any]:
    trades, universe = load_trades(db_path, since, until, limit)
    bars_cache, bar_audit = ensure_bars(
        trades=trades, bar_dir=bar_dir, fetch=fetch, refresh=refresh
    )

    all_details: List[Dict[str, Any]] = []
    missing_bars = Counter()
    no_trigger = Counter()
    contract_stats = Counter()
    for trade in trades:
        if trade["structured_trigger"]:
            contract_stats["structured_close_trades"] += 1
        if trade["contract"].get("structured_raw_count", 0) > 0:
            contract_stats["structured_contract_trades"] += 1
        if trade["text_rule"]:
            contract_stats["text_price_rules"] += 1
        if trade["text_semantics"]:
            contract_stats[f"text_{trade['text_semantics']['semantic_status']}"] += 1
        if not trade["structured_trigger"] and not trade["text_rule"]:
            no_trigger["no_price_contract"] += 1
            continue
        bars = _trade_bars(trade, bars_cache)
        entry_day = trade["entry_et"].date().isoformat()
        if not any(bar["t"].date().isoformat() == entry_day for bar in bars):
            missing_bars["entry_day_bars"] += 1
            continue
        common = {
            "id": trade["id"],
            "symbol": trade["symbol"],
            "direction": trade["direction"],
            "entry_et": trade["entry_et"],
            "exit_et": trade["exit_et"],
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "quantity": trade["quantity"],
            "stop_loss": trade["stop_loss"],
            "bucket_effective": trade["bucket_effective"],
        }
        if trade["structured_trigger"]:
            trigger = {
                **trade["structured_trigger"],
                "contract_source": trade["contract"].get("source"),
                "contract_hash": trade["contract"].get("contract_hash"),
                "action": ACTION_REVIEW_ONLY,
            }
            all_details.extend(_details_for_trigger(
                trade=common, bars=bars, source="structured", trigger=trigger,
                variants=[
                    (PRIMARY_TRIGGER_ORDER, PRIMARY_CLOSE_MODE),
                    (PRIMARY_TRIGGER_ORDER, SENSITIVITY_CLOSE_MODE),
                ],
                slippage_bps=slippage_bps,
            ))
        if trade["text_rule"]:
            semantics = trade["text_semantics"] or {}
            trigger = {
                **trade["text_rule"],
                **semantics,
                "contract_source": trade["contract"].get("source"),
                "text_parse_status": trade["contract"].get("text_parse_status"),
                "contract_hash": trade["contract"].get("contract_hash"),
                "action": ACTION_REVIEW_ONLY,
            }
            all_details.extend(_details_for_trigger(
                trade=common, bars=bars, source="text", trigger=trigger,
                variants=[
                    (PRIMARY_TRIGGER_ORDER, PRIMARY_CLOSE_MODE),
                    (SENSITIVITY_TRIGGER_ORDER, PRIMARY_CLOSE_MODE),
                    (PRIMARY_TRIGGER_ORDER, SENSITIVITY_CLOSE_MODE),
                ],
                slippage_bps=slippage_bps,
            ))

    for row in all_details:
        if row.get("status") != "ok":
            continue
        # Keep per-trade actual PnL only in memory for fidelity aggregation.
        match = next((trade for trade in trades if trade["id"] == row["trade_id"]), None)
        if match:
            row["_actual_net_pnl"] = match["actual_net_pnl"]
            row["source_semantic_status"] = row.get("semantic_status")

    arms: Dict[str, Any] = {}
    variant_specs = [
        ("structured", PRIMARY_CLOSE_MODE, PRIMARY_TRIGGER_ORDER),
        ("structured", SENSITIVITY_CLOSE_MODE, PRIMARY_TRIGGER_ORDER),
        ("text", PRIMARY_CLOSE_MODE, PRIMARY_TRIGGER_ORDER),
        ("text", PRIMARY_CLOSE_MODE, SENSITIVITY_TRIGGER_ORDER),
        ("text", SENSITIVITY_CLOSE_MODE, PRIMARY_TRIGGER_ORDER),
    ]
    for source, close_mode, order in variant_specs:
        label = f"{source}__{close_mode}__{order}"
        rows = [row for row in all_details
                if row.get("status") == "ok" and row.get("source") == source
                and row.get("close_mode") == close_mode
                and row.get("trigger_order") == order]
        arms[label] = _arm_payload(rows, label)

    text_all = [row for row in all_details
                if row.get("status") == "ok" and row.get("source") == "text"
                and row.get("close_mode") == PRIMARY_CLOSE_MODE
                and row.get("trigger_order") == PRIMARY_TRIGGER_ORDER]
    simulation_errors = Counter(
        str(row.get("status")) for row in all_details if row.get("status") != "ok"
    )
    fidelity_rows: List[Dict[str, Any]] = []
    seen_fidelity: set[int] = set()
    for row in all_details:
        if row.get("status") != "ok" or row.get("trade_id") in seen_fidelity:
            continue
        if (row.get("baseline") or {}).get("exit_reason") == "ACTUAL_EXIT":
            fidelity_rows.append(row)
            seen_fidelity.add(int(row["trade_id"]))

    fee_error_rows = [
        row for row in all_details
        if (row.get("baseline") or {}).get("fee_error")
        or (row.get("arm") or {}).get("fee_error")
    ]

    summary: Dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"since": since, "until": until},
        "protocol": {
            "replay_schema": REPLAY_SCHEMA,
            "entry_bar": "skipped",
            "intrabar_fill": "next_bar_open + adverse_slippage; gap uses current open",
            "primary_close_confirmation": "10min_before_close_near_close_proxy",
            "primary_close_fill": "next_bar_open + adverse_slippage",
            "sensitivity_close_confirmation": "official_session_close",
            "sensitivity_close_fill": "next_session_open + adverse_slippage",
            "primary_intrabar_order": "pessimistic_sl_first",
            "sensitivity_intrabar_order": "optimistic_invalidation_first",
            "slippage_bps": slippage_bps,
            "fees": "fee_model.estimate_round_trip; gross and net both reported",
            "baseline": "hard_initial_sl_if_touched_else_actual_exit",
            "left_tail_threshold_net_bps": -100.0,
            "mde80": "2.8015852 * daily_cluster_se; per-trade bps/USD",
        },
        "universe": universe,
        "bars": bar_audit,
        "missing_bars": dict(sorted(missing_bars.items())),
        "no_price_contract": dict(sorted(no_trigger.items())),
        "contract": dict(sorted(contract_stats.items())),
        "text_semantics_all": trigger_semantics_counts(text_all),
        "simulation_errors": dict(sorted(simulation_errors.items())),
        "fee_errors": {
            "n_rows": len(fee_error_rows),
            "sample": [
                str((row.get("baseline") or {}).get("fee_error")
                    or (row.get("arm") or {}).get("fee_error"))
                for row in fee_error_rows[:3]
            ],
        },
        "baseline_fidelity": _baseline_fidelity(fidelity_rows),
        "arms": arms,
        "text_primary_including_all_semantics": summarize_arm(
            text_all, arm_label="text__all_semantics"
        ),
    }

    if write_outputs:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = out_dir / f"invalidation_replay_a1_{stamp}.json"
        jsonl_path = out_dir / f"invalidation_replay_a1_{stamp}.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as handle:
            for row in all_details:
                clean = {key: value for key, value in row.items()
                         if not key.startswith("_")}
                handle.write(json.dumps(clean, ensure_ascii=False, default=str) + "\n")
        summary["artifacts"] = {"json": str(json_path), "jsonl": str(jsonl_path)}
        json_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A1 read-only invalidation replay")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--bar-dir", default=str(DEFAULT_BAR_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--since", default="2026-06-26")
    parser.add_argument("--until", default="2026-10-26")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument(
        "--fetch", action="store_true",
        help="缺 bar 时连本机 OpenD(127.0.0.1:11111) 拉取并写 data/replay_bars 缓存; "
             "默认离线只读既有缓存",
    )
    parser.add_argument(
        "--refresh-bars", action="store_true",
        help="强制重拉缓存 (需同时给 --fetch)",
    )
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    if args.refresh_bars and not args.fetch:
        print("[invalidation-replay] --refresh-bars 需要同时给 --fetch", file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"[invalidation-replay] DB not found: {args.db}")
        return 2
    summary = run_replay(
        db_path=Path(args.db),
        bar_dir=Path(args.bar_dir),
        since=args.since,
        until=args.until,
        limit=args.limit,
        fetch=args.fetch,
        refresh=args.refresh_bars,
        slippage_bps=args.slippage_bps,
        out_dir=Path(args.out_dir),
        write_outputs=not args.no_write,
    )
    if summary.get("fee_errors", {}).get("n_rows"):
        print(
            f"[invalidation-replay] 警告: {summary['fee_errors']['n_rows']} 行费用模型失败, "
            f"该行 net 被高估 (fee 记 0) —— net 读数不可直接引用; "
            f"样本: {summary['fee_errors']['sample']}",
            file=sys.stderr,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
