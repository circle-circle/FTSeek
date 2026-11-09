#!/usr/bin/env python3
"""Offline DA evidence-card replay with no LLM, database, or checkpoint writes."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import llm_synth
from recommendation_evidence import build_recommendation_evidence, canonical_json


def _candidate_from_analysis(analysis: dict) -> dict:
    return {key[1:]: value for key, value in analysis.items() if key.startswith("_ue_")}


def replay(trade_date: str) -> dict:
    checkpoint_dir = ROOT / "data" / "da_checkpoint" / trade_date
    paths = sorted(checkpoint_dir.glob("*.raw.json"))
    rows = []
    status_counts = Counter()
    event_status_counts = Counter()
    direction_counts = Counter()
    totals = Counter()
    prompt_mismatch_symbols = []
    mutated_symbols = []
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        symbol = raw.get("symbol") or path.name.replace(".raw.json", "")
        collected = copy.deepcopy(raw.get("collected") or {})
        collected = llm_synth._apply_prev_close_fix(collected)
        analysis_path = checkpoint_dir / f"{symbol}.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else {}
        candidate = _candidate_from_analysis(analysis)
        prompt_before = llm_synth._format_data_for_prompt(collected)
        collected_before = json.dumps(collected, ensure_ascii=False, sort_keys=True, default=str)
        evidence = build_recommendation_evidence(collected, analysis, candidate)
        prompt_after = llm_synth._format_data_for_prompt(collected)
        collected_after = json.dumps(collected, ensure_ascii=False, sort_keys=True, default=str)
        if prompt_before != prompt_after:
            prompt_mismatch_symbols.append(symbol)
        if collected_before != collected_after:
            mutated_symbols.append(symbol)

        events = evidence["event_references"]
        lineage = evidence["claim_lineage"]
        timing = evidence["market_timing"]
        price = evidence["price_alignment"]
        counter = evidence["counter_evidence"]
        references = events["references"]
        totals["cards_built"] += 1
        totals["analysis_files"] += int(analysis_path.exists())
        totals["news_references"] += len(references)
        totals["unique_stories"] += len({item["story_id"] for item in references})
        totals["prompt_visible_news_references"] += sum(bool(item["prompt_visible"]) for item in references)
        totals["news_time_parsed"] += sum(item["published"]["status"] == "PARSED" for item in references)
        totals["news_time_missing"] += sum(item["published"]["status"] == "MISSING" for item in references)
        totals["news_time_unparsed"] += sum(
            item["published"]["status"] not in ("PARSED", "MISSING") for item in references)
        totals["claims"] += lineage["claim_count"]
        totals["linked_claims"] += lineage["linked_claim_count"]
        totals["unlinked_claims"] += lineage["unlinked_claim_count"]
        totals["vix_proxy_values"] += int(timing["vix"]["status"] == "AVAILABLE_PROXY")
        totals["vix_as_of_basis_present"] += int(bool(timing["vix"]["as_of_basis"]))
        totals["vix_calendar_date_available"] += int(bool(timing["vix"]["as_of"]))
        totals["price_as_of_present"] += int(bool(price["price_as_of"]))
        totals["price_alignment_known"] += int(price["price_alignment"] in ("ALIGNED", "OPPOSED", "NEUTRAL"))
        totals["industry_relative_available"] += int(price["relative_to_industry_pct"] is not None)
        totals["evidence_gaps"] += len(counter["evidence_gaps"])
        status_counts[counter["status"]] += 1
        event_status_counts[events["status"]] += 1
        direction_counts[price["direction"]] += 1
        rows.append({
            "symbol": symbol,
            "direction_pre_gate": price["direction"],
            "evidence_sha256": hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest(),
            "news_references": len(references),
            "unique_stories": len({item["story_id"] for item in references}),
            "prompt_visible_news_references": sum(bool(item["prompt_visible"]) for item in references),
            "news_time_parsed": sum(item["published"]["status"] == "PARSED" for item in references),
            "claims": lineage["claim_count"],
            "linked_claims": lineage["linked_claim_count"],
            "unlinked_claims": lineage["unlinked_claim_count"],
            "vix_status": timing["vix"]["status"],
            "vix_as_of_basis": timing["vix"]["as_of_basis"],
            "price_alignment": price["price_alignment"],
            "technical_trend_alignment": price["technical_trend_alignment"],
            "relative_to_industry_pct": price["relative_to_industry_pct"],
            "counter_evidence_status": counter["status"],
            "counter_evidence_items": len(counter["items"]),
            "evidence_gaps": len(counter["evidence_gaps"]),
        })
    return {
        "trade_date": trade_date,
        "mode": "offline_recommendation_evidence_replay_no_llm",
        "schema_version": "recommendation_evidence_v1",
        "input_files": len(paths),
        "candidate_symbols_before": len(paths),
        "candidate_symbols_after": len(rows),
        "unique_candidate_symbols": len({row["symbol"] for row in rows}),
        "prompt_unchanged": len(paths) - len(prompt_mismatch_symbols),
        "prompt_mismatch_symbols": prompt_mismatch_symbols,
        "collected_unmutated": len(paths) - len(mutated_symbols),
        "mutated_symbols": mutated_symbols,
        "llm_calls": 0,
        "db_writes": 0,
        "checkpoint_writes": 0,
        "totals": dict(sorted(totals.items())),
        "counter_evidence_status": dict(sorted(status_counts.items())),
        "event_reference_status": dict(sorted(event_status_counts.items())),
        "direction_pre_gate": dict(sorted(direction_counts.items())),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(replay(args.date), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
