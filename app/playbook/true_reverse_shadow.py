"""Analyst-versus-independent-challenger true reverse classification."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger("ftseek.playbook.true_reverse_shadow")


def normalize_direction(value: Any) -> str:
    direction = str(value or "").upper()
    if direction in ("LONG", "BUY"):
        return "BUY"
    if direction in ("SHORT", "SELL_SHORT"):
        return "SELL_SHORT"
    return direction


def _opposite(first: Any, second: Any) -> bool:
    left = normalize_direction(first)
    right = normalize_direction(second)
    return {left, right} == {"BUY", "SELL_SHORT"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def classify_true_reverse(
    analysis: Mapping[str, Any], debate: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Classify only BUY/SELL_SHORT disagreement; SKIP is abstention, not reverse."""

    if not debate:
        return None
    analyst_direction = normalize_direction(analysis.get("direction"))
    challenger_direction = normalize_direction(debate.get("challenger_direction"))
    independent_direction = normalize_direction(
        debate.get("challenger_independent_direction")
    )
    if not _opposite(analyst_direction, challenger_direction):
        return None

    self_debate = bool(debate.get("challenger_self_debate"))
    independent = not self_debate
    independent_confirms_reverse = (
        independent
        and independent_direction == challenger_direction
    )
    classification = (
        "independent_true_reverse"
        if independent_confirms_reverse
        else "anchored_or_non_independent_reverse"
    )
    analyst_score = _number(analysis.get("score"))
    challenger_score = _number(debate.get("challenger_score"))
    score_gap = (
        abs(analyst_score - challenger_score)
        if analyst_score is not None and challenger_score is not None
        else None
    )
    proposed_resolution = "record_only"
    if independent_confirms_reverse:
        proposed_resolution = (
            "skip_if_score_gap_gt_3"
            if score_gap is not None and score_gap > 3
            else "no_trade_pending_independent_adjudication"
        )
    return {
        "classification": classification,
        "independent": independent,
        "independent_confirms_reverse": independent_confirms_reverse,
        "score_gap": score_gap,
        "proposed_resolution": proposed_resolution,
        "analyst_direction": analyst_direction,
        "challenger_direction": challenger_direction,
        "challenger_independent_direction": independent_direction,
    }


def build_true_reverse_event(
    date: str,
    symbol: str,
    analysis: Mapping[str, Any],
    debate: Mapping[str, Any],
    artifact: str,
) -> dict[str, Any] | None:
    classification = classify_true_reverse(analysis, debate)
    if classification is None:
        return None

    event = {
        "schema_version": "da_true_reverse_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": date,
        "symbol": symbol,
        "artifact": artifact,
        "analyst": {
            "model": analysis.get("_model_reported") or "",
            "direction": classification["analyst_direction"],
            "score": analysis.get("score"),
        },
        "challenger": {
            "model": debate.get("challenger_model") or "",
            "tier": debate.get("challenger_tier") or "",
            "self_debate": bool(debate.get("challenger_self_debate")),
            "direction": classification["challenger_direction"],
            "independent_direction": classification[
                "challenger_independent_direction"
            ],
            "score": debate.get("challenger_score"),
            "verdict": debate.get("challenger_verdict") or "",
            "reasoning": debate.get("challenger_skip_reason") or "",
            "falsification_target": debate.get("falsification_target") or "",
            "flip_condition": debate.get("flip_condition") or "",
        },
        **classification,
        "final_direction": normalize_direction(analysis.get("direction")),
        "current_outcome": analysis.get("debate_outcome") or "",
        "policy": "record-only; DA synthesis and execution unchanged",
    }
    event.pop("analyst_direction", None)
    event.pop("challenger_direction", None)
    event.pop("challenger_independent_direction", None)
    signature = json.dumps(
        {
            "date": date,
            "symbol": symbol,
            "analyst": event["analyst"],
            "challenger": {
                key: event["challenger"][key]
                for key in (
                    "model", "tier", "self_debate", "direction",
                    "independent_direction", "score", "verdict",
                )
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event["event_id"] = (
        "DA-TRUE-REVERSE:"
        + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    )
    return event


def append_events(
    events: Iterable[Mapping[str, Any]], output_path: Path | str
) -> tuple[int, int]:
    """Append new events once. Returns (appended, skipped_existing)."""

    path = Path(output_path)
    existing_ids = set()
    bad_lines = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                existing_ids.add(str(json.loads(line).get("event_id")))
            except json.JSONDecodeError:
                bad_lines += 1
    if bad_lines:
        logger.warning(
            "[true-reverse-shadow] 台账 %s 有 %d 行坏 JSON, 去重扫描已跳过这些行",
            path, bad_lines,
        )

    appended = 0
    skipped = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output_file:
        for event in events:
            event_id = str(event.get("event_id") or "")
            if event_id in existing_ids:
                skipped += 1
                continue
            output_file.write(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
            )
            existing_ids.add(event_id)
            appended += 1
    return appended, skipped
