#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UE/DA 链路契约健康检查 (只读, 2026-10-18).

回答:
  - 最新 UE scan 的 prompt/brief 版本、material stats、surfaced 方向
  - surfaced SHORT 的 borrow basis/feasible、event_meta 覆盖
  - borrow_on_demand 补查计数
  - 最新 DA instructions 的 score_band/evidence_gaps/UE 输入持久化覆盖

不写库、不调 LLM、不下单.
用法:
  PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py
  ... --json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCAN_PATH = ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"
DB_PATH = ROOT / "data" / "ftseek_us.db"
CHECKPOINT_DIR = ROOT / "data" / "da_checkpoint"
UE_ORIGINS = {"ue_candidate", "ue_fresh_event"}
CURRENT_ERA_START = "2026-07-01"
Z80 = 1.2815515655446004


def _load_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def latest_scan(path: Path = SCAN_PATH) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError) as _rec_e:
            # 2026-10-28 except:pass 清剿: 链路健康读数靠这份账本, 坏行必须可见。
            print(f"[warn] 链路账本坏行跳过: {_rec_e} | {line[:120]}", file=sys.stderr)
            continue
        if record.get("type") == "scan":
            latest = record
    return latest


def summarize_scan(scan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not scan:
        return {"scan_found": False}
    surfaced = scan.get("surfaced") or []
    directions = Counter(str(s.get("direction") or "UNKNOWN") for s in surfaced)
    short_basis = Counter()
    short_feasible = Counter()
    event_meta_coverage = 0
    event_reaction_coverage = 0
    reaction_basis = Counter()
    for item in surfaced:
        if str(item.get("direction") or "") != "SHORT":
            continue
        sc = item.get("short_check") or {}
        short_basis[str(sc.get("borrow_data_basis") or "MISSING")] += 1
        short_feasible[str(sc.get("borrow_feasible"))] += 1
    for item in surfaced:
        if isinstance(item.get("evidence_matched_event_meta"), dict) and item.get("evidence_matched_event_meta"):
            event_meta_coverage += 1
        reaction = item.get("evidence_matched_event_reaction")
        if isinstance(reaction, dict) and reaction.get("post_event_move_pct") is not None:
            event_reaction_coverage += 1
            reaction_basis[str(reaction.get("reaction_basis") or "MISSING")] += 1
    return {
        "scan_found": True,
        "batch_id": scan.get("batch_id"),
        "date": scan.get("date"),
        "scanned_at": scan.get("scanned_at"),
        "window": scan.get("scan_window"),
        "batch_status": scan.get("batch_status"),
        "prompt_version": scan.get("prompt_version"),
        "brief_template_version": scan.get("brief_template_version"),
        "brief_material_stats": scan.get("brief_material_stats") or {},
        "surfaced_n": len(surfaced),
        "directions": dict(directions),
        "short_basis": dict(short_basis),
        "short_feasible": dict(short_feasible),
        "event_meta_coverage": event_meta_coverage,
        "event_reaction_coverage": event_reaction_coverage,
        "reaction_basis": dict(reaction_basis),
        "borrow_on_demand": scan.get("borrow_on_demand") or {},
        "actionable": [
            {"symbol": _norm_symbol(item.get("symbol")),
             "direction": str(item.get("direction") or "").upper()}
            for item in surfaced
            if str(item.get("direction") or "").upper() in ("LONG", "SHORT")
        ],
    }


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("US.")


def latest_complete_scan(
    path: Path = SCAN_PATH,
    date: Optional[str] = None,
    window: Optional[str] = "da_feed",
) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError) as exc:
            print(f"[warn] 链路账本坏行跳过: {exc} | {line[:120]}", file=sys.stderr)
            continue
        if record.get("type") != "scan":
            continue
        if record.get("batch_status") != "complete":
            continue
        if window is not None and record.get("scan_window") != window:
            continue
        if date is not None and str(record.get("date") or "") != date:
            continue
        latest = record
    return latest


def latest_da_rows(db_path: Path = DB_PATH, limit: int = 30) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, date, symbol, direction, decision_id, raw_instruction_json,
                  confidence, execution_state
           FROM playbook_instructions
           WHERE direction IN ('BUY', 'SELL_SHORT')
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _checkpoint_prompt_fields(
    date: Any,
    symbol: Any,
    checkpoint_dir: Path = CHECKPOINT_DIR,
) -> Dict[str, Any]:
    """读取 DA checkpoint 的 user_prompt, 核对 UE 上下文是否真的送达 LLM.

    2026-10-21: raw_instruction_json 的 ue_event_meta/ue_short_check 是
    prompt-only 字段, 设计上不回显到 DA 输出 — 用 raw 覆盖率会误报 0/20.
    真正的送达证据在 data/da_checkpoint/<date>/<symbol>.raw.json 的 user_prompt。
    """
    empty = {
        "prompt_found": False,
        "prompt_ue_context": False,
        "prompt_ue_short_check": False,
        "prompt_ue_event_meta": False,
        "prompt_ue_event_reaction": False,
        "prompt_event_reaction_pct": None,
        "prompt_borrow_basis": None,
        "prompt_event_family": None,
    }
    if not date or not symbol:
        return empty
    path = Path(checkpoint_dir) / str(date) / f"{symbol}.raw.json"
    if not path.exists():
        return empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return empty
    prompt = str(payload.get("user_prompt") or "")
    if not prompt:
        return empty
    has_context = "事件扫描器(UE)" in prompt
    has_short = "UE SHORT三问" in prompt
    has_meta = "UE 事件属性" in prompt
    has_reaction = "UE 事件反应" in prompt
    reaction_pct = None
    if has_reaction:
        match = re.search(r"UE 事件反应:\s*([+-]?\d+(?:\.\d+)?)%", prompt)
        reaction_pct = float(match.group(1)) if match else None
    borrow = None
    if has_short:
        match = re.search(r"basis=([A-Za-z_]+)", prompt)
        borrow = match.group(1) if match else None
    family = None
    if has_meta:
        match = re.search(r"family=([A-Za-z0-9_]+)\s+form=([^\s]+)", prompt)
        family = match.group(1) if match else None
    return {
        "prompt_found": True,
        "prompt_ue_context": has_context,
        "prompt_ue_short_check": has_short,
        "prompt_ue_event_meta": has_meta,
        "prompt_ue_event_reaction": has_reaction,
        "prompt_event_reaction_pct": reaction_pct,
        "prompt_borrow_basis": borrow,
        "prompt_event_family": family,
    }


def _checkpoint_analysis_fields(date: str, symbol: str, checkpoint_dir: Path) -> Dict[str, Any]:
    path = Path(checkpoint_dir) / str(date) / f"{symbol}.json"
    if not path.exists():
        return {"analysis_found": False, "analysis_ue_context": False,
                "analysis_ue_batch_id": None, "analysis_ue_origin": None,
                "analysis_prompt_eligible": False, "analysis_supply_stage": None,
                "analysis_action": None, "analysis_ue_direction": None,
                "analysis_decision_id": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"analysis_found": False, "analysis_ue_context": False,
                "analysis_ue_batch_id": None, "analysis_ue_origin": None,
                "analysis_prompt_eligible": False, "analysis_supply_stage": None,
                "analysis_action": None, "analysis_ue_direction": None,
                "analysis_decision_id": None}
    context_keys = ("_ue_origin", "_ue_evidence", "_ue_batch_id", "_ue_causal_chain")
    has_context = any(payload.get(key) for key in context_keys)
    supply = payload.get("_supply_selection")
    if not isinstance(supply, dict):
        supply = {}
    return {
        "analysis_found": True,
        "analysis_ue_context": bool(has_context),
        "analysis_ue_batch_id": payload.get("_ue_batch_id"),
        "analysis_ue_origin": payload.get("_ue_origin"),
        "analysis_prompt_eligible": (
            payload.get("_ue_origin") is True and bool(payload.get("_ue_causal_chain"))
        ),
        "analysis_supply_stage": supply.get("stage"),
        "analysis_action": payload.get("action"),
        "analysis_ue_direction": payload.get("_ue_direction"),
        "analysis_decision_id": payload.get("_ic_decision_id"),
    }


def load_day_analyses(checkpoint_dir: Path, date: str) -> List[Dict[str, Any]]:
    day_dir = Path(checkpoint_dir) / str(date)
    if not day_dir.exists():
        return []
    rows = []
    for path in sorted(day_dir.glob("US.*.json")):
        if path.name.endswith(".raw.json"):
            continue
        symbol = path.stem
        fields = _checkpoint_analysis_fields(date, symbol, checkpoint_dir)
        fields.update(_checkpoint_prompt_fields(date, symbol, checkpoint_dir))
        rows.append({"date": date, "symbol": symbol, **fields})
    return rows


def load_db_rows(db_path: Path, start: str, end: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, date, symbol, direction, skip_reason, triggered, executed,
                      decision_id, execution_state, row_kind, raw_instruction_json
               FROM playbook_instructions
               WHERE date >= ? AND date <= ?
               ORDER BY id""",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["raw"] = _load_json(item.get("raw_instruction_json"))
        item["row_kind"] = item.get("row_kind") or "instruction"
        result.append(item)
    return result


def effective_window_start(requested_start: str) -> str:
    """Return the actual lower bound used by queries (never earlier than current era)."""
    return max(str(requested_start), CURRENT_ERA_START)


def load_closed_da_trades(db_path: Path, start: str, end: str) -> List[Dict[str, Any]]:
    effective_start = effective_window_start(start)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, trade_date, symbol, direction, decision_id, entry_price,
                      quantity, realized_pnl, pnl_net, fees_est, fees_source,
                      exit_reason, metadata_json
               FROM trade_records
               WHERE source_type = 'PLAYBOOK_SIM'
                 AND strategy_id = 'playbook_sim'
                 AND trade_date >= ? AND trade_date <= ?
                 AND exit_price > 0
                 AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
                 AND COALESCE(data_quality,'') IN ('clean','real')
               ORDER BY trade_date, id""",
            (effective_start, end),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        item = dict(row)
        metadata = _load_json(item.get("metadata_json"))
        item["a9_excluded"] = bool(metadata.get("exclusion_reason"))
        result.append(item)
    return result


def _instruction_class(raw: Dict[str, Any]) -> str:
    if str(raw.get("origin") or "") in UE_ORIGINS:
        return "ue_origin"
    if raw.get("ue_batch_id"):
        return "ue_overlap"
    return "non_ue"


def _day_cluster_stats(values: List[float]) -> Dict[str, Any]:
    count = len(values)
    if not count:
        return {"n_days": 0, "mean": None, "t": None, "mde_80pct": None,
                "ci80_normal_approx": None, "distinguishable": False}
    mean = statistics.mean(values)
    if count < 2:
        return {"n_days": count, "mean": round(mean, 2), "t": None,
                "mde_80pct": None, "ci80_normal_approx": None,
                "distinguishable": False}
    stdev = statistics.stdev(values)
    standard_error = stdev / math.sqrt(count)
    mde = 2.8 * standard_error
    t_value = mean / standard_error if standard_error > 0 else None
    return {
        "n_days": count,
        "mean": round(mean, 2),
        "t": round(t_value, 3) if t_value is not None else None,
        "mde_80pct": round(mde, 2),
        "ci80_normal_approx": [round(mean - Z80 * standard_error, 2),
                               round(mean + Z80 * standard_error, 2)],
        "distinguishable": bool(standard_error > 0 and abs(mean) > mde),
    }


def _trade_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"n": 0, "win_n": 0, "gross": 0.0, "fees": 0.0, "net": 0.0,
                "net_bps": None, "expectancy": None, "fees_source": [],
                "day_clustered": _day_cluster_stats([])}
    nets = [
        float(trade.get("pnl_net") if trade.get("pnl_net") is not None
              else trade.get("realized_pnl") or 0.0)
        for trade in trades
    ]
    gross = sum(float(trade.get("realized_pnl") or 0.0) for trade in trades)
    fees = sum(float(trade.get("fees_est") or 0.0) for trade in trades)
    notional = sum(
        float(trade.get("entry_price") or 0.0) * float(trade.get("quantity") or 0.0)
        for trade in trades
    )
    by_day: Dict[str, List[float]] = {}
    for trade, net in zip(trades, nets):
        by_day.setdefault(str(trade.get("trade_date") or ""), []).append(net)
    day_means = [sum(values) / len(values) for values in by_day.values()]
    return {
        "n": len(trades),
        "win_n": sum(1 for net in nets if net > 0),
        "gross": round(gross, 2),
        "fees": round(fees, 2),
        "net": round(sum(nets), 2),
        "net_bps": round(sum(nets) / notional * 10000.0, 2) if notional else None,
        "expectancy": round(statistics.mean(nets), 2),
        "fees_source": sorted({str(trade.get("fees_source") or "unknown") for trade in trades}),
        "day_clustered": _day_cluster_stats(day_means),
    }


def contribution_audit(
    scan_path: Path,
    db_path: Path,
    checkpoint_dir: Path,
    start: str,
    end: str,
) -> Dict[str, Any]:
    effective_start = effective_window_start(start)
    scan = latest_complete_scan(scan_path, date=end, window="da_feed")
    scan_summary = summarize_scan(scan)
    db_rows = load_db_rows(db_path, start, end)
    instructions = [row for row in db_rows if row["row_kind"] != "supply_placeholder"]
    instruction_by_decision = {
        row["decision_id"]: row for row in instructions if row.get("decision_id")
    }
    rows_by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in db_rows:
        rows_by_key.setdefault((row["date"], _norm_symbol(row["symbol"])), []).append(row)
    rows_by_decision: Dict[str, List[Dict[str, Any]]] = {}
    for row in db_rows:
        if row.get("decision_id"):
            rows_by_decision.setdefault(row["decision_id"], []).append(row)

    analyses: List[Dict[str, Any]] = []
    cursor = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    while cursor <= stop:
        day = cursor.strftime("%Y-%m-%d")
        analyses.extend(load_day_analyses(checkpoint_dir, day))
        cursor += timedelta(days=1)

    classes = ("ue_origin", "ue_overlap", "non_ue")
    funnel = {
        name: {
            "analyses_n": 0,
            "prompt_eligible_n": 0,
            "prompt_context_n": 0,
            "prompt_missing_n": 0,
            "analyzed_to_instruction_n": 0,
            "supply_placeholder_n": 0,
            "no_db_row_n": 0,
            "instructions_n": 0,
            "actionable_n": 0,
            "skip_n": 0,
            "triggered_n": 0,
            "executed_n": 0,
            "ue_direction_confirmed_n": 0,
            "ue_direction_flipped_n": 0,
        }
        for name in classes
    }
    origin_mismatch_n = 0
    overlap_prompt_context_n = 0
    for analysis in analyses:
        is_origin = analysis.get("analysis_ue_origin") is True
        has_batch = bool(analysis.get("analysis_ue_batch_id"))
        name = "ue_origin" if is_origin else ("ue_overlap" if has_batch else "non_ue")
        item = funnel[name]
        item["analyses_n"] += 1
        eligible = bool(analysis.get("analysis_prompt_eligible"))
        context = bool(analysis.get("prompt_ue_context"))
        item["prompt_eligible_n"] += int(eligible)
        item["prompt_context_n"] += int(context)
        if eligible and not context:
            item["prompt_missing_n"] += 1
        if name == "ue_overlap" and context:
            overlap_prompt_context_n += 1
        key_matches = rows_by_key.get((analysis["date"], _norm_symbol(analysis["symbol"])), [])
        decision_id = analysis.get("analysis_decision_id")
        if decision_id:
            true_matches = [
                row for row in rows_by_decision.get(decision_id, [])
                if row["row_kind"] != "supply_placeholder"
            ]
        else:
            true_matches = [row for row in key_matches if row["row_kind"] != "supply_placeholder"]
        placeholder_matches = [row for row in key_matches if row["row_kind"] == "supply_placeholder"]
        matches = true_matches + placeholder_matches
        item["supply_placeholder_n"] += int(bool(placeholder_matches))
        item["no_db_row_n"] += int(not matches)
        if true_matches:
            item["analyzed_to_instruction_n"] += 1
            raw_is_origin = any(
                _instruction_class(row["raw"]) == "ue_origin" for row in true_matches
            )
            if is_origin != raw_is_origin:
                origin_mismatch_n += 1

    for row in instructions:
        raw = row["raw"]
        item = funnel[_instruction_class(raw)]
        direction = str(row.get("direction") or "").upper()
        item["instructions_n"] += 1
        item["actionable_n"] += int(direction in ("BUY", "SELL_SHORT"))
        item["skip_n"] += int(direction == "SKIP")
        item["triggered_n"] += int(row.get("triggered") or 0)
        item["executed_n"] += int(row.get("executed") or 0)
        ue_direction = str(raw.get("ue_direction") or "").upper()
        if direction in ("BUY", "SELL_SHORT") and ue_direction in ("LONG", "SHORT"):
            expected = "BUY" if ue_direction == "LONG" else "SELL_SHORT"
            if direction == expected:
                item["ue_direction_confirmed_n"] += 1
            else:
                item["ue_direction_flipped_n"] += 1

    clean_trades = {name: [] for name in classes}
    included_trades = {name: [] for name in classes}
    unmatched_trades_n = 0
    for trade in load_closed_da_trades(db_path, start, end):
        instruction = instruction_by_decision.get(trade.get("decision_id"))
        if instruction is None:
            unmatched_trades_n += 1
            continue
        name = _instruction_class(instruction["raw"])
        included_trades[name].append(trade)
        if not trade.get("a9_excluded"):
            clean_trades[name].append(trade)
    performance = {
        name: {
            "a9_clean": _trade_summary(clean_trades[name]),
            "a9_included": _trade_summary(included_trades[name]),
            "a9_excluded_n": len(included_trades[name]) - len(clean_trades[name]),
        }
        for name in classes
    }

    scan_date = scan_summary.get("date")
    day_analyses = [row for row in analyses if row["date"] == scan_date]
    analyzed_symbols = {_norm_symbol(row["symbol"]) for row in day_analyses}
    origin_symbols = {
        _norm_symbol(row["symbol"]) for row in day_analyses
        if row.get("analysis_ue_origin") is True
    }
    overlap_symbols = {
        _norm_symbol(row["symbol"]) for row in day_analyses
        if row.get("analysis_ue_batch_id") and row.get("analysis_ue_origin") is not True
    }
    actionable = scan_summary.get("actionable") or []
    scan_funnel = {
        "scan_actionable_n": len(actionable),
        "scan_analyzed_n": sum(1 for row in actionable if row["symbol"] in analyzed_symbols),
        "scan_ue_origin_n": sum(1 for row in actionable if row["symbol"] in origin_symbols),
        "scan_ue_overlap_n": sum(1 for row in actionable if row["symbol"] in overlap_symbols),
        "scan_not_analyzed_n": sum(1 for row in actionable if row["symbol"] not in analyzed_symbols),
        "scan_not_analyzed_symbols": [
            row["symbol"] for row in actionable if row["symbol"] not in analyzed_symbols
        ],
    }

    warnings = []
    prompt_missing_n = sum(item["prompt_missing_n"] for item in funnel.values())
    if prompt_missing_n:
        warnings.append({"code": "UE_PROMPT_MISSING", "n": prompt_missing_n})
    if overlap_prompt_context_n:
        warnings.append({"code": "UE_OVERLAP_PROMPT_DRIFT", "n": overlap_prompt_context_n})
    if origin_mismatch_n:
        warnings.append({"code": "UE_ORIGIN_MISMATCH", "n": origin_mismatch_n})
    if unmatched_trades_n:
        warnings.append({"code": "TRADE_DECISION_UNMATCHED", "n": unmatched_trades_n})
    ue_origin_n = performance["ue_origin"]["a9_clean"]["n"]
    if ue_origin_n < 30:
        warnings.append({"code": "UE_ORIGIN_SAMPLE_LT_30", "n": ue_origin_n})

    return {
        # 2026-11-12: 交易查询实际从 effective_start 起 (不早于 CURRENT_ERA_START),
        #   旧输出却回显 caller 传入的 start ⇒ 窗口撒谎。显式给 requested_start/clamped。
        "window": {
            "start": effective_start,
            "end": end,
            "requested_start": start,
            "clamped": effective_start != start,
            "basis": "entry_cohort_closed_A9_A10",
        },
        "scan": scan_summary,
        "scan_funnel": scan_funnel,
        "funnel": funnel,
        "performance": performance,
        "data_quality": {
            "db_rows_n": len(db_rows),
            "instructions_n": len(instructions),
            "supply_placeholder_n": len(db_rows) - len(instructions),
            "analyses_n": len(analyses),
            "origin_mismatch_n": origin_mismatch_n,
            "overlap_prompt_context_n": overlap_prompt_context_n,
            "unmatched_trades_n": unmatched_trades_n,
        },
        "warnings": warnings,
        "decision_use": "display_only_not_a_trading_gate",
    }


def summarize_instruction(
    row: Dict[str, Any],
    checkpoint_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    raw = _load_json(row.get("raw_instruction_json"))
    origin = raw.get("origin") if isinstance(raw.get("origin"), dict) else {}
    event_meta = origin.get("evidence_matched_event_meta")
    if not isinstance(event_meta, dict):
        event_meta = raw.get("ue_event_meta") if isinstance(raw.get("ue_event_meta"), dict) else {}
    short_check = raw.get("ue_short_check") if isinstance(raw.get("ue_short_check"), dict) else {}
    reaction = raw.get("ue_event_reaction") if isinstance(raw.get("ue_event_reaction"), dict) else {}
    gaps = raw.get("evidence_gaps")
    result = {
        "id": row.get("id"),
        "date": row.get("date"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "decision_id": row.get("decision_id"),
        "score_band": raw.get("score_band") or "UNKNOWN",
        "evidence_gaps_n": len(gaps) if isinstance(gaps, (list, dict)) else (1 if gaps else 0),
        "debate_outcome": str(raw.get("debate_outcome") or "")[:120],
        "ue_event_meta": bool(event_meta),
        "ue_event_family": event_meta.get("event_family") if isinstance(event_meta, dict) else None,
        "ue_borrow_basis": short_check.get("borrow_data_basis") or raw.get("ue_borrow_data_basis"),
        "ue_borrow_feasible": short_check.get("borrow_feasible"),
        "ue_event_reaction": bool(reaction),
        "event_age_h": reaction.get("age_h"),
        "post_event_move_pct": reaction.get("post_event_move_pct"),
        "reaction_basis": reaction.get("reaction_basis"),
    }
    if checkpoint_dir is not None:
        result.update(_checkpoint_prompt_fields(
            row.get("date"), row.get("symbol"), checkpoint_dir))
        result.update(_checkpoint_analysis_fields(
            row.get("date"), row.get("symbol"), checkpoint_dir))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="UE/DA chain contract health check")
    parser.add_argument("--scan-path", type=Path, default=SCAN_PATH)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--checkpoint-dir", type=Path, default=CHECKPOINT_DIR)
    parser.add_argument("--date")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--scan-window", choices=("da_feed", "premarket", "any"), default="da_feed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.date:
        start = end = args.date
    elif args.start or args.end:
        if not (args.start and args.end):
            parser.error("--start and --end must be provided together")
        start, end = args.start, args.end
    else:
        latest = latest_complete_scan(args.scan_path, window=args.scan_window)
        start = end = str(latest.get("date") or "") if latest else None
    if start and end and start > end:
        parser.error("--start must be <= --end")

    scan = summarize_scan(latest_complete_scan(
        args.scan_path,
        date=end if start else None,
        window=None if args.scan_window == "any" else args.scan_window,
    ))
    if start:
        window_rows = [
            row for row in load_db_rows(args.db, start, end)
            if row["row_kind"] != "supply_placeholder"
        ]
        rows = [summarize_instruction(row, checkpoint_dir=args.checkpoint_dir)
                for row in window_rows]
    else:
        rows = [summarize_instruction(row, checkpoint_dir=args.checkpoint_dir)
                for row in latest_da_rows(args.db, args.limit)]
    da_score_bands = Counter(r["score_band"] for r in rows)
    da_event_meta = sum(1 for r in rows if r["ue_event_meta"])
    da_reaction = sum(1 for r in rows if r["ue_event_reaction"])
    da_borrow_basis = Counter(str(r["ue_borrow_basis"] or "MISSING") for r in rows)
    da_gaps = sum(1 for r in rows if r["evidence_gaps_n"] > 0)
    prompt_context = sum(1 for r in rows if r.get("prompt_ue_context"))
    prompt_short = sum(1 for r in rows if r.get("prompt_ue_short_check"))
    prompt_reaction = sum(1 for r in rows if r.get("prompt_ue_event_reaction"))
    prompt_basis = Counter(
        str(r.get("prompt_borrow_basis") or "MISSING") for r in rows
        if r.get("prompt_ue_context"))
    analysis_context = sum(1 for r in rows if r.get("analysis_ue_context"))
    analysis_batch = sum(1 for r in rows if r.get("analysis_ue_batch_id"))
    payload = {
        "ue_scan": scan,
        "da": {
            "n": len(rows),
            "score_bands": dict(da_score_bands),
            "ue_event_meta_coverage": da_event_meta,
            "event_reaction_coverage": da_reaction,
            "borrow_basis": dict(da_borrow_basis),
            "persisted_ue_event_meta_coverage": da_event_meta,
            "persisted_event_reaction_coverage": da_reaction,
            "persisted_borrow_basis": dict(da_borrow_basis),
            "rows_with_evidence_gaps": da_gaps,
            "prompt_ue_context_coverage": prompt_context,
            "prompt_ue_short_check_coverage": prompt_short,
            "prompt_ue_event_reaction_coverage": prompt_reaction,
            "prompt_borrow_basis": dict(prompt_basis),
            "analysis_ue_context_coverage": analysis_context,
            "analysis_ue_batch_id_coverage": analysis_batch,
            "prompt_vs_analysis_context_gap": analysis_context - prompt_context,
            "recent": rows[:10],
        },
    }
    if start:
        payload["ue_contribution"] = contribution_audit(
            args.scan_path, args.db, args.checkpoint_dir, start, end
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not scan.get("scan_found"):
        print("UE scan: NOT FOUND")
    else:
        print(f"UE scan: {scan['date']} {scan['window']} prompt={scan['prompt_version']} "
              f"brief={scan['brief_template_version']} surfaced={scan['surfaced_n']} "
              f"dirs={scan['directions']}")
        print(f"  material_stats={scan['brief_material_stats']}")
        print(f"  short_basis={scan['short_basis']} feasible={scan['short_feasible']} "
              f"event_meta_coverage={scan['event_meta_coverage']} "
              f"event_reaction_coverage={scan.get('event_reaction_coverage', 0)} "
              f"reaction_basis={scan.get('reaction_basis', {})}")
        print(f"  borrow_on_demand={scan['borrow_on_demand']}")
    print(f"DA persisted: n={len(rows)} score_bands={dict(da_score_bands)} "
          f"ue_event_meta={da_event_meta}/{len(rows)} "
          f"event_reaction={da_reaction}/{len(rows)} "
          f"borrow_basis={dict(da_borrow_basis)} gaps_rows={da_gaps}")
    print(f"DA prompt: ue_context={prompt_context}/{len(rows)} "
          f"ue_short_check={prompt_short}/{len(rows)} "
          f"ue_event_reaction={prompt_reaction}/{len(rows)} "
          f"borrow_basis={dict(prompt_basis)}")
    print(f"DA analysis: ue_context={analysis_context}/{len(rows)} "
          f"ue_batch_id={analysis_batch}/{len(rows)} "
          f"prompt_vs_analysis_gap={analysis_context - prompt_context}")
    if start:
        contribution = payload["ue_contribution"]
        print(f"UE contribution: {start}→{end} "
              f"scan_actionable={contribution['scan_funnel']['scan_actionable_n']} "
              f"ue_origin_analyses={contribution['funnel']['ue_origin']['analyses_n']} "
              f"prompt={contribution['funnel']['ue_origin']['prompt_context_n']}/"
              f"{contribution['funnel']['ue_origin']['prompt_eligible_n']} "
              f"ue_origin_closed={contribution['performance']['ue_origin']['a9_clean']['n']} "
              f"warnings={[warning['code'] for warning in contribution['warnings']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
