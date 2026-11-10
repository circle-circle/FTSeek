#!/usr/bin/env python3
"""Paired offline DA citation-layer replay; production writes are always disabled."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
for path in (ROOT, SKILL_SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from recommendation_evidence import build_event_references


RUN_NAMES = ("control_1", "treatment_1", "control_2", "treatment_2")
QUOTAS = {"BUY": 6, "SELL_SHORT": 2, "SKIP": 2}
SEED = "20261109"
CITATION_MAIN_CONTRACT = """

【离线新闻引用层契约】
- bull_case/bear_case 的每条论据新增 "evidence_ids": ["SRC-...", ...]。
- 只有新闻事实直接支持该论据时才引用对应 SRC ID；技术、估值、基本面等非新闻论据使用空数组。
- 不得编造 ID，不得引用输入未展示的 ID，不得为了提高引用率改变 direction/rating/score。
- 没有直接新闻支持不代表论据无效；使用空数组并在需要时写 evidence_gaps，禁止模糊硬配。
"""
CITATION_CHALLENGER_CONTRACT = """

【离线引用核验】
- 额外输出 "citation_errors": [{"claim":"对应论据","evidence_id":"被核验ID","reason":"不支持/不存在/张冠李戴"}]。
- 只核验证据是否直接支持对应论据；没有错误返回空数组。
- 引用缺失本身不是反向证据，不得仅因 evidence_ids 为空而改变独立方向。
"""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def select_samples(report_path: Path) -> list[dict]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected = []
    for direction, quota in QUOTAS.items():
        eligible = [
            row for row in report["rows"]
            if row["direction_pre_gate"] == direction
            and row["news_references"] > 0
            and row["linked_claims"] == 0
        ]
        eligible.sort(key=lambda row: _sha(f"{SEED}|{row['symbol']}"))
        if len(eligible) < quota:
            raise ValueError(f"insufficient eligible symbols for {direction}: {len(eligible)} < {quota}")
        for row in eligible[:quota]:
            selected.append({"symbol": row["symbol"], "direction_pre_gate": direction})
    selected.sort(key=lambda row: row["symbol"])
    return selected


def citation_references(collected: dict) -> list[dict]:
    events = build_event_references(collected)
    summaries = {}
    for source_name, field_name in (("FUTU", "news"), ("FINNHUB", "news_v2")):
        items = collected.get(field_name)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                summaries[(source_name, str(item.get("title") or ""))] = item.get("summary")
    references = []
    for reference in events["references"]:
        if not reference["prompt_visible"]:
            continue
        enriched = dict(reference)
        enriched["summary"] = summaries.get((reference["source"], reference["title"]))
        references.append(enriched)
    return references


def build_citation_section(references: list[dict]) -> str:
    lines = [
        "## 新闻证据引用表（离线处理臂）",
        "以下 ID 只用于定位新闻，不代表利好/利空评分；新闻正文仍以上方原始材料为准。",
    ]
    for reference in references:
        published = reference["published"]
        time_label = published.get("et") or published.get("raw") or "时间未知"
        lines.append(
            f"- {reference['evidence_id']} | {reference['story_id']} | {time_label} | {reference['title']}"
        )
    if not references:
        lines.append("- 无 prompt 可见新闻引用；不得编造 SRC/STORY ID。")
    return "\n".join(lines)


def run_arm(collected: dict, arm: str) -> dict:
    import llm_synth

    references = citation_references(collected) if arm.startswith("treatment") else []
    section = build_citation_section(references) if references or arm.startswith("treatment") else ""
    captured = []
    original_format = llm_synth._format_data_for_prompt
    original_call = llm_synth._call_llm

    def format_with_citation(data):
        return original_format(data) + "\n\n" + section

    def call_with_contract(system, user, **kwargs):
        role = "challenger" if "你是投资证伪官" in system else "main"
        if arm.startswith("treatment") and role == "challenger":
            system = system + CITATION_CHALLENGER_CONTRACT
        result = original_call(system, user, **kwargs)
        captured.append({"role": role, "raw": copy.deepcopy(result)})
        return result

    format_patch = format_with_citation if arm.startswith("treatment") else original_format
    system_override = llm_synth.SYSTEM_PROMPT + CITATION_MAIN_CONTRACT if arm.startswith("treatment") else None
    with patch.object(llm_synth, "_format_data_for_prompt", format_patch), \
         patch.object(llm_synth, "_call_llm", call_with_contract), \
         patch.object(llm_synth, "_load_negative_claims_warning_for_prompt", lambda: ""), \
         patch.object(llm_synth, "_load_condition_blacklist_for_prompt", lambda: ""), \
         patch.object(llm_synth, "_load_symbol_card_for_prompt", lambda *args: ("", False)), \
         patch.object(llm_synth, "_load_trading_card_for_prompt", lambda *args: ""), \
         patch.dict(os.environ, {"DA_CHALLENGER_BLIND_SHADOW": "0", "DA_SCHEMA_ENFORCE": "0"}):
        result = llm_synth.synthesize(
            copy.deepcopy(collected), trade_experience="", system_override=system_override)
    return {
        "result": result,
        "calls": captured,
        "references": references,
        "citation_section": section,
        "user_prompt": result.get("_user_prompt"),
        "production_write": False,
    }


def _atomic_write(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def execute_sample(raw_root: str, output_dir: str, sample: dict) -> dict:
    symbol = sample["symbol"]
    raw = json.loads((Path(raw_root) / f"{symbol}.raw.json").read_text(encoding="utf-8"))
    completed = []
    for run_name in RUN_NAMES:
        output_path = Path(output_dir) / f"{symbol}.{run_name}.json"
        if output_path.exists():
            completed.append(run_name)
            continue
        try:
            outcome = run_arm(copy.deepcopy(raw["collected"]), run_name)
        except Exception as error:
            outcome = {
                "result": {"_error": f"{type(error).__name__}: {error}"},
                "calls": [],
                "references": [],
                "error": True,
                "production_write": False,
            }
        outcome.update({
            "symbol": symbol,
            "run": run_name,
            "arm": "treatment" if run_name.startswith("treatment") else "control",
            "direction_pre_gate": sample["direction_pre_gate"],
        })
        _atomic_write(output_path, outcome)
        completed.append(run_name)
        direction = (outcome.get("result") or {}).get("direction")
        print(f"[citation-ab] {symbol} {run_name} direction={direction} calls={len(outcome.get('calls') or [])}", flush=True)
    return {"symbol": symbol, "completed": completed}


def _claims(result: dict):
    for section in ("bull_case", "bear_case"):
        claims = result.get(section) or []
        if not isinstance(claims, list):
            continue
        for index, claim in enumerate(claims):
            if isinstance(claim, dict):
                yield section, index, claim


def _direction(record: dict) -> str:
    value = str((record.get("result") or {}).get("direction") or "ERROR").upper()
    return value if value in ("BUY", "SELL_SHORT", "SKIP") else "ERROR"


def _modal_direction(directions: list[str]) -> str:
    counts = Counter(directions)
    if len(counts) != 1:
        return "MIXED"
    return next(iter(counts))


def summarize(output_dir: Path) -> dict:
    records = []
    for run_name in RUN_NAMES:
        records.extend(json.loads(path.read_text(encoding="utf-8")) for path in sorted(output_dir.glob(f"US.*.{run_name}.json")))
    treatments = [record for record in records if record["arm"] == "treatment"]
    audit_rows = []
    invalid_ids = []
    field_claims = 0
    total_claims = 0
    cited_claims = 0
    citation_ids_total = 0
    challenger_errors = 0
    symbols_with_citations = set()
    for record in treatments:
        references = record.get("references") or []
        reference_map = {item["evidence_id"]: item for item in references}
        for section, index, claim in _claims(record.get("result") or {}):
            total_claims += 1
            evidence_ids = claim.get("evidence_ids")
            if isinstance(evidence_ids, list):
                field_claims += 1
            else:
                evidence_ids = []
            evidence_ids = [str(value) for value in evidence_ids]
            citation_ids_total += len(evidence_ids)
            bad_ids = [value for value in evidence_ids if value not in reference_map]
            for value in bad_ids:
                invalid_ids.append({"symbol": record["symbol"], "run": record["run"], "evidence_id": value})
            valid_ids = [value for value in evidence_ids if value in reference_map]
            if valid_ids:
                cited_claims += 1
                symbols_with_citations.add(record["symbol"])
                audit_rows.append({
                    "audit_id": _sha(f"{record['symbol']}|{record['run']}|{section}|{index}|{'|'.join(valid_ids)}")[:16],
                    "symbol": record["symbol"],
                    "run": record["run"],
                    "section": section,
                    "claim_index": index,
                    "thesis": claim.get("thesis"),
                    "evidence_text": claim.get("evidence"),
                    "evidence_ids": valid_ids,
                    "references": [reference_map[value] for value in valid_ids],
                    "adjudication": None,
                })
        challenger_errors += sum(
            len((call.get("raw") or {}).get("citation_errors") or [])
            for call in record.get("calls") or []
            if call.get("role") == "challenger"
        )
    audit_rows.sort(key=lambda row: (row["symbol"], row["run"], row["section"], row["claim_index"], row["audit_id"]))
    audit_sample = audit_rows[:30]
    _atomic_write(output_dir / "citation_audit_sample.json", {"audit_status": "PENDING", "rows": audit_sample})

    direction_rows = []
    for symbol in sorted({record["symbol"] for record in records}):
        symbol_records = {record["run"]: record for record in records if record["symbol"] == symbol}
        controls = [_direction(symbol_records[name]) for name in ("control_1", "control_2") if name in symbol_records]
        treatments_for_symbol = [_direction(symbol_records[name]) for name in ("treatment_1", "treatment_2") if name in symbol_records]
        control_modal = _modal_direction(controls) if len(controls) == 2 else "INCOMPLETE"
        treatment_modal = _modal_direction(treatments_for_symbol) if len(treatments_for_symbol) == 2 else "INCOMPLETE"
        direction_rows.append({
            "symbol": symbol,
            "controls": controls,
            "treatments": treatments_for_symbol,
            "control_stable": len(controls) == 2 and len(set(controls)) == 1,
            "treatment_stable": len(treatments_for_symbol) == 2 and len(set(treatments_for_symbol)) == 1,
            "control_modal": control_modal,
            "treatment_modal": treatment_modal,
            "modal_match": control_modal == treatment_modal,
        })
    valid_treatments = sum(_direction(record) != "ERROR" for record in treatments)
    summary = {
        "schema": "da_citation_ab_replay_v1",
        "sample_symbols": sorted({record["symbol"] for record in records}),
        "runs_expected": 40,
        "runs_completed": len(records),
        "treatment_runs": len(treatments),
        "treatment_valid_results": valid_treatments,
        "treatment_claims": total_claims,
        "evidence_id_field_claims": field_claims,
        "evidence_id_field_coverage": round(field_claims / total_claims, 4) if total_claims else None,
        "cited_claims": cited_claims,
        "symbols_with_citations": len(symbols_with_citations),
        "citation_ids_total": citation_ids_total,
        "invalid_citation_ids": invalid_ids,
        "valid_citation_id_rate": round((citation_ids_total - len(invalid_ids)) / citation_ids_total, 4) if citation_ids_total else None,
        "challenger_citation_errors": challenger_errors,
        "llm_calls_actual": sum(len(record.get("calls") or []) for record in records),
        "main_calls": sum(
            call.get("role") == "main" for record in records for call in record.get("calls") or []),
        "challenger_calls": sum(
            call.get("role") == "challenger" for record in records for call in record.get("calls") or []),
        "control_stable_symbols": sum(row["control_stable"] for row in direction_rows),
        "treatment_stable_symbols": sum(row["treatment_stable"] for row in direction_rows),
        "modal_match_symbols": sum(row["modal_match"] for row in direction_rows),
        "modal_match_control_stable": sum(
            row["modal_match"] for row in direction_rows if row["control_stable"]),
        "direction_rows": direction_rows,
        "manual_audit": {
            "status": "PENDING",
            "cited_claims_total": len(audit_rows),
            "sample_count": len(audit_sample),
            "supported": None,
            "unsupported": None,
            "insufficient": None,
        },
        "production_write": False,
    }
    _atomic_write(output_dir / "summary.pre_manual.json", summary)
    return summary


def finalize_manual_audit(output_dir: Path) -> dict:
    sample = json.loads((output_dir / "citation_audit_sample.json").read_text(encoding="utf-8"))
    adjudicated = json.loads((output_dir / "citation_audit_adjudicated.json").read_text(encoding="utf-8"))
    summary = json.loads((output_dir / "summary.pre_manual.json").read_text(encoding="utf-8"))
    sample_rows = sample["rows"]
    expected_hash = _sha(json.dumps(sample_rows, ensure_ascii=False, sort_keys=True))
    if adjudicated.get("audit_sha256") != expected_hash:
        raise ValueError("manual audit sample hash mismatch")
    decisions = {}
    for decision in adjudicated.get("rows") or []:
        audit_id = decision.get("audit_id")
        if audit_id in decisions:
            raise ValueError("manual audit must cover every sampled claim exactly once")
        decisions[audit_id] = decision
    expected_ids = {row["audit_id"] for row in sample_rows}
    if set(decisions) != expected_ids:
        raise ValueError("manual audit must cover every sampled claim exactly once")
    allowed = {"SUPPORTED", "UNSUPPORTED", "INSUFFICIENT"}
    for row in sample_rows:
        decision = decisions[row["audit_id"]]
        verdict = decision.get("adjudication")
        if verdict not in allowed:
            raise ValueError(f"invalid adjudication for {row['audit_id']}: {verdict}")
        reason = decision.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"missing adjudication reason for {row['audit_id']}")
        row["adjudication"] = verdict
        row["adjudication_reason"] = reason
    counts = Counter(row["adjudication"] for row in sample_rows)
    total = len(sample_rows)
    evaluable = counts["SUPPORTED"] + counts["UNSUPPORTED"]
    support_all = counts["SUPPORTED"] / total if total else None
    support_evaluable = counts["SUPPORTED"] / evaluable if evaluable else None
    summary["manual_audit"] = {
        "status": "COMPLETE",
        "sample_count": total,
        "supported": counts["SUPPORTED"],
        "unsupported": counts["UNSUPPORTED"],
        "insufficient": counts["INSUFFICIENT"],
        "support_rate_all": round(support_all, 4) if support_all is not None else None,
        "support_rate_evaluable": round(support_evaluable, 4) if support_evaluable is not None else None,
    }
    gates = {
        "output_retention": summary["runs_completed"] == 40 and summary["treatment_valid_results"] == 20,
        "evidence_id_field_coverage": (summary["evidence_id_field_coverage"] or 0) >= 0.95,
        "valid_citation_ids": not summary["invalid_citation_ids"] and summary["valid_citation_id_rate"] == 1.0,
        "citation_supply": summary["symbols_with_citations"] >= 8,
        "manual_support": support_all is not None and support_all >= 0.90,
    }
    summary["feasibility_gates"] = gates
    summary["feasibility_pass"] = all(gates.values())
    summary["promotion"] = "NOT_APPROVED_N10_STRUCTURE_ONLY"
    _atomic_write(output_dir / "citation_audit_adjudicated.json", {
        "audit_sha256": expected_hash,
        "rows": list(decisions.values()),
    })
    _atomic_write(output_dir / "citation_audit_sample.adjudicated.json", {
        "audit_status": "COMPLETE", "rows": sample_rows})
    _atomic_write(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-11-06")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/analysis/2026-11-09-da-recommendation-evidence-replay.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--finalize-audit", action="store_true")
    args = parser.parse_args()
    if args.finalize_audit:
        print(json.dumps(finalize_manual_audit(args.output_dir), ensure_ascii=False, indent=2))
        return 0
    if args.summarize:
        print(json.dumps(summarize(args.output_dir), ensure_ascii=False, indent=2))
        return 0
    samples = select_samples(args.report)
    plan = {
        "schema": "da_citation_ab_plan_v1",
        "date": args.date,
        "seed": SEED,
        "quotas": QUOTAS,
        "samples": samples,
        "run_names": RUN_NAMES,
        "synthesize_calls": len(samples) * len(RUN_NAMES),
        "primary_llm_calls_expected": len(samples) * len(RUN_NAMES) * 2,
        "citation_main_contract_sha256": _sha(CITATION_MAIN_CONTRACT),
        "citation_challenger_contract_sha256": _sha(CITATION_CHALLENGER_CONTRACT),
        "production_write": False,
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    import config
    if not config.DASHSCOPE_KEY:
        raise SystemExit("DASHSCOPE_API_KEY unavailable; no output directory created")
    if args.output_dir.exists() and not args.resume:
        raise SystemExit(f"output directory exists; use --resume only for matching plan: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=args.resume)
    plan_path = args.output_dir / "plan.json"
    if args.resume and plan_path.exists():
        old_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if old_plan != plan:
            raise SystemExit("plan mismatch; refusing to mix citation experiments")
    else:
        _atomic_write(plan_path, plan)
    raw_root = ROOT / "data" / "da_checkpoint" / args.date
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(execute_sample, str(raw_root), str(args.output_dir), sample)
            for sample in samples
        ]
        for future in as_completed(futures):
            print(json.dumps(future.result(), ensure_ascii=False), flush=True)
    summary = summarize(args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
