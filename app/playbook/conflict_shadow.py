"""Record-only conflict shadow at the Playbook instruction write boundary."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger("ftseek.playbook.conflict_shadow")

# 埋点路径提为模块常量 — 测试经 conftest 缝表/offline_chain_driver 改道沙箱,
# 不写生产 data/ (REVIEW-20260903 §四.8 纪律)。
_CONFLICT_SHADOW_PATH = (
    Path(__file__).resolve().parents[2]
    / "data" / "shadow" / "instruction_conflict.jsonl"
)


def _value(row: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _direction(value: Any) -> str:
    direction = str(value or "").upper()
    if direction in ("BUY", "LONG"):
        return "BUY"
    if direction in ("SELL_SHORT", "SHORT"):
        return "SELL_SHORT"
    return direction


def _source(decision_id: Any) -> str:
    did = str(decision_id or "")
    if did.startswith("DA-"):
        return "DA"
    if did.startswith("universe_expansion"):
        return "UE"
    if did.startswith("intraday_reentry-"):
        return "REENTRY"
    if did:
        return "OTHER"
    return "EMPTY"


def _same_direction(first: Any, second: Any) -> bool:
    return _direction(first) == _direction(second)


def _same_number(first: Any, second: Any) -> bool:
    try:
        return abs(float(first) - float(second)) < 1e-9
    except (TypeError, ValueError):
        return (first or None) == (second or None)


def _material_fields(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    return {
        "direction": _direction(_value(row, "direction")),
        "entry_price": _value(row, "entry_price"),
        "stop_loss": _value(row, "stop_loss"),
        "take_profit": _value(row, "take_profit"),
        "confidence": _value(row, "confidence"),
        "skip_reason": _value(row, "skip_reason"),
    }


def _material_conflict(inst: Mapping[str, Any], existing: Mapping[str, Any] | Any) -> bool:
    old = _material_fields(existing)
    new = _material_fields(inst)
    if old["direction"] != new["direction"]:
        return True
    if (old["skip_reason"] or "") != (new["skip_reason"] or ""):
        return True
    for key in ("entry_price", "stop_loss", "take_profit", "confidence"):
        if not _same_number(old[key], new[key]):
            return True
    return False


def _opposite_direction(first: Any, second: Any) -> bool:
    left = _direction(first)
    right = _direction(second)
    return {left, right} == {"BUY", "SELL_SHORT"}


def build_save_conflict_event(
    inst: Mapping[str, Any], existing: Iterable[Mapping[str, Any] | Any]
) -> dict[str, Any] | None:
    """Return one deterministic event when identity or channel rules disagree."""

    rows = list(existing)
    if not rows:
        return None

    decision_id = str(inst.get("decision_id") or "")
    date = str(inst.get("date") or "")
    symbol = str(inst.get("symbol") or "")
    incoming_source = _source(decision_id)
    existing_sources = [_source(_value(row, "decision_id")) for row in rows]
    same_did_rows = [
        row for row in rows
        if str(_value(row, "decision_id") or "") == decision_id and decision_id
    ]

    duplicate_identity = len(same_did_rows) > 1
    same_id_material_conflict = any(
        _material_conflict(inst, row) for row in same_did_rows
    )
    active_incoming = _direction(inst.get("direction")) in ("BUY", "SELL_SHORT")
    channel_direction_conflicts = []
    for row, source in zip(rows, existing_sources):
        existing_direction = _direction(_value(row, "direction"))
        if (
            active_incoming
            and existing_direction in ("BUY", "SELL_SHORT")
            and _opposite_direction(inst.get("direction"), existing_direction)
            and {incoming_source, source} <= {"DA", "UE", "REENTRY", "OTHER"}
            and incoming_source != "EMPTY"
            and source != "EMPTY"
        ):
            channel_direction_conflicts.append({
                "source": source,
                "decision_id": str(_value(row, "decision_id") or ""),
                "direction": existing_direction,
            })

    if not (duplicate_identity or same_id_material_conflict or channel_direction_conflicts):
        return None

    classification = []
    if duplicate_identity:
        classification.append("duplicate_decision_id")
    if same_id_material_conflict:
        classification.append("same_decision_id_material_conflict")
    if channel_direction_conflicts:
        classification.append("channel_direction_conflict")

    event = {
        "schema_version": "instruction_conflict_v1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": date,
        "symbol": symbol,
        "decision_id": decision_id,
        "incoming_source": incoming_source,
        "incoming_direction": _direction(inst.get("direction")),
        "existing_sources": existing_sources,
        "classification": classification,
        "channel_direction_conflicts": channel_direction_conflicts,
        "existing_count": len(rows),
        "same_decision_id_count": len(same_did_rows),
        "policy": "record-only; save_instruction arbitration unchanged",
    }
    signature = json.dumps(
        {key: event[key] for key in (
            "date", "symbol", "decision_id", "incoming_source",
            "incoming_direction", "existing_sources", "classification",
            "channel_direction_conflicts", "existing_count",
            "same_decision_id_count",
        )},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event["event_id"] = (
        "SAVE-CONFLICT:"
        + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    )
    return event


def append_event(
    event: Mapping[str, Any], output_path: Path | str
) -> bool:
    """Append an event once. Returns False when the event_id already exists."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event_id = str(event.get("event_id") or "")
    if event_id:
        if path.exists():
            bad_lines = 0
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    if str(json.loads(line).get("event_id")) == event_id:
                        return False
                except json.JSONDecodeError:
                    bad_lines += 1
            if bad_lines:
                logger.warning(
                    "[instruction-conflict-shadow] 台账 %s 有 %d 行坏 JSON, "
                    "去重扫描已跳过这些行", path, bad_lines,
                )
    with path.open("a", encoding="utf-8") as output_file:
        output_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def emit_save_conflict_shadow(
    inst: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any] | Any],
) -> None:
    """Best-effort write-boundary telemetry; never changes save behavior."""

    event = build_save_conflict_event(inst, existing)
    if event is None:
        return
    append_event(event, _CONFLICT_SHADOW_PATH)
    logger.warning(
        "[instruction-conflict-shadow] %s %s %s (record-only)",
        event["date"], event["symbol"], ",".join(event["classification"]),
    )
