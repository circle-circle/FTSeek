#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
for path in (ROOT, SKILL_SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.event_stream.instructions import (
    CHALLENGER_EVENT_CONTRACT,
    MAIN_EVENT_CONTRACT,
    build_intraday_instruction_card,
    compact_symbol_event_section,
    compare_instruction_cards,
)


def _write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def run_arm(
    collected: Dict[str, Any],
    *,
    symbol: str,
    session_date: str,
    valid_until: str,
    brief: Dict[str, Any] | None,
    arm: str,
) -> Dict[str, Any]:
    import llm_synth

    captured = []
    original_format = llm_synth._format_data_for_prompt
    original_call = llm_synth._call_llm
    relevance_text = json.dumps({
        "symbol": symbol,
        "company_profile": collected.get("company_profile"),
        "plates": collected.get("plates"),
    }, ensure_ascii=False, default=str)
    section = compact_symbol_event_section(
        brief, valid_until=valid_until, symbol=symbol, relevance_text=relevance_text,
    )
    allowed_event_ids = None
    if brief:
        section_payload = json.loads(section[section.index("{"):])
        allowed_event_ids = {
            str(event_id)
            for fact in section_payload["facts"]
            for event_id in (
                [fact["event_id"]]
                + [member["event_id"] for member in fact.get("member_facts") or []]
            )
        }

    def format_with_event(data):
        return original_format(data) + "\n\n" + section

    def call_with_contract(system, user, **kwargs):
        role = "challenger" if "你是投资证伪官" in system else "main"
        if role == "challenger":
            system = system + CHALLENGER_EVENT_CONTRACT
        result = original_call(system, user, **kwargs)
        captured.append({"role": role, "raw": copy.deepcopy(result)})
        return result

    with patch.object(llm_synth, "_format_data_for_prompt", format_with_event), \
         patch.object(llm_synth, "_call_llm", call_with_contract), \
         patch.object(llm_synth, "_load_negative_claims_warning_for_prompt", lambda: ""), \
         patch.object(llm_synth, "_load_condition_blacklist_for_prompt", lambda: ""), \
         patch.object(llm_synth, "_load_symbol_card_for_prompt", lambda *args: ("", False)), \
         patch.object(llm_synth, "_load_trading_card_for_prompt", lambda *args: ""), \
         patch.dict(os.environ, {"DA_CHALLENGER_BLIND_SHADOW": "0"}):
        result = llm_synth.synthesize(
            copy.deepcopy(collected), trade_experience="",
            system_override=llm_synth.SYSTEM_PROMPT + MAIN_EVENT_CONTRACT,
        )
    challenger_raw = next(
        (item["raw"] for item in reversed(captured) if item["role"] == "challenger"), {},
    )
    card = build_intraday_instruction_card(
        symbol=symbol, session_date=session_date, valid_until=valid_until,
        result=result, challenger_raw=challenger_raw, brief=brief, arm=arm,
        allowed_event_ids=allowed_event_ids,
    )
    return {
        "card": card,
        "result": result,
        "challenger_raw": challenger_raw,
        "calls": captured,
        "user_prompt": result.get("_user_prompt"),
        "event_section": section,
        "dynamic_history_injection": "disabled_in_both_arms",
        "production_write": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Paired offline DA Event Intelligence R2 replay")
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbols", default="AMD,HCA,NVDA,TSLA")
    parser.add_argument("--session-date", default="2026-11-05")
    parser.add_argument("--valid-until", default="2026-11-05T16:00:00-05:00")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        raise SystemExit("dry-run does not call DA; pass --execute for the isolated paired replay")
    brief = json.loads(args.brief.read_text(encoding="utf-8"))
    if brief.get("validation_errors"):
        raise ValueError(f"brief validation failed: {brief['validation_errors']}")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    symbols = [item.strip().upper().removeprefix("US.") for item in args.symbols.split(",") if item.strip()]
    comparisons = []
    for symbol in symbols:
        raw_path = args.raw_root / f"US.{symbol}.raw.json"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        if raw.get("trade_date") != args.session_date:
            raise ValueError(f"session mismatch for {symbol}: {raw.get('trade_date')}")
        collected = raw["collected"]
        arms = {}
        for arm, arm_brief in (("control", None), ("treatment", brief)):
            print(f"[R2] {symbol} {arm} start", file=sys.stderr)
            output = run_arm(
                collected, symbol=f"US.{symbol}", session_date=args.session_date,
                valid_until=args.valid_until, brief=arm_brief, arm=arm,
            )
            arms[arm] = output
            stem = f"US.{symbol}.{arm}"
            _write_json_new(args.output_dir / f"{stem}.json", output)
            if output.get("user_prompt"):
                with (args.output_dir / f"{stem}.prompt.txt").open("x", encoding="utf-8") as handle:
                    handle.write(output["user_prompt"] + "\n")
            print(
                f"[R2] {symbol} {arm} direction={output['card']['direction']} "
                f"status={output['card']['status']} errors={output['card']['validation_errors']}",
                file=sys.stderr,
            )
        comparison = compare_instruction_cards(arms["control"]["card"], arms["treatment"]["card"])
        comparisons.append(comparison)
        _write_json_new(args.output_dir / f"US.{symbol}.comparison.json", comparison)
    summary = {
        "schema": "da_event_instruction_replay_v1",
        "session_date": args.session_date,
        "brief_id": brief["brief_id"],
        "symbols": [f"US.{item}" for item in symbols],
        "comparisons": comparisons,
        "direction_changes": sum(item["direction_changed"] for item in comparisons),
        "valid_treatment_cards": sum(item["treatment_status"] == "VALID" for item in comparisons),
        "economic_value_claimed": False,
        "production_db_write": False,
        "pool_write": False,
        "notifier_calls": 0,
    }
    _write_json_new(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
