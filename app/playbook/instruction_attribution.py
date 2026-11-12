"""Strict trade-to-Playbook instruction attribution for review reports."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping

logger = logging.getLogger(__name__)


def canonical_plan_direction(value: Any) -> str:
    direction = str(value or "").upper()
    if direction in ("LONG", "BUY"):
        return "BUY"
    if direction in ("SHORT", "SELL_SHORT"):
        return "SELL_SHORT"
    return direction


def _metadata(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


class _AmbiguousInstructionId:
    """Sentinel: decision_id matched several instructions that trade context cannot split.

    Kept distinct from ``None`` so reports can say "ambiguous" instead of falsely
    claiming the fill was unplanned. Never equal to a real instruction id.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - repr is for debugging/logs
        return "AMBIGUOUS"

    def __bool__(self) -> bool:
        return False


AMBIGUOUS = _AmbiguousInstructionId()


def _context_matches(instruction: Mapping[str, Any], trade: Mapping[str, Any]) -> bool:
    """Narrow duplicate decision_ids with trade date / symbol / direction context.

    Only compares fields present on both sides; missing context never rejects a
    candidate, so older callers that pass ``id, decision_id`` only keep working.
    """
    trade_date = str(trade.get("trade_date") or trade.get("date") or "").strip()
    instruction_date = str(instruction.get("date") or "").strip()
    if trade_date and instruction_date and trade_date != instruction_date:
        return False

    trade_symbol = str(trade.get("symbol") or "").strip().upper()
    instruction_symbol = str(instruction.get("symbol") or "").strip().upper()
    if trade_symbol and instruction_symbol and trade_symbol != instruction_symbol:
        return False

    trade_direction = canonical_plan_direction(trade.get("direction"))
    instruction_direction = canonical_plan_direction(instruction.get("direction"))
    if (trade_direction and instruction_direction
            and trade_direction != instruction_direction):
        return False
    return True


def resolve_trade_instruction_ids(
    trades: Iterable[Mapping[str, Any]],
    instruction_rows: Iterable[Mapping[str, Any]],
) -> List[Any]:
    """Resolve metadata instruction_id first, then a unique decision_id.

    Returns one item per trade: an ``int`` instruction id, ``None`` when no
    instruction can be attributed, or :data:`AMBIGUOUS` when a duplicated
    decision_id survives date/symbol/direction disambiguation (e.g. same-day
    reentry rounds sharing a decision_id). Callers must treat ``AMBIGUOUS`` as
    "not attributable", not as "unplanned".
    """
    instructions_by_id: Dict[int, Mapping[str, Any]] = {}
    ids_by_decision_id: Dict[str, List[int]] = defaultdict(list)
    for row in instruction_rows:
        try:
            instruction_id = int(row.get("id"))
        except (TypeError, ValueError):
            logger.warning("跳过非法 Playbook instruction id: %r", row.get("id"), exc_info=True)
            continue
        instructions_by_id[instruction_id] = row
        decision_id = str(row.get("decision_id") or "").strip()
        if decision_id:
            ids_by_decision_id[decision_id].append(instruction_id)

    resolved: List[Any] = []
    for trade in trades:
        metadata = _metadata(trade.get("metadata_json"))
        raw_instruction_id = metadata.get("playbook_instruction_id")
        try:
            instruction_id = int(raw_instruction_id)
        except (TypeError, ValueError):
            instruction_id = None
        if instruction_id in instructions_by_id:
            resolved.append(instruction_id)
            continue

        decision_id = str(trade.get("decision_id") or "").strip()
        candidates = ids_by_decision_id.get(decision_id, []) if decision_id else []
        if len(candidates) == 1:
            resolved.append(candidates[0])
            continue
        if len(candidates) > 1:
            narrowed = [
                candidate for candidate in candidates
                if _context_matches(instructions_by_id[candidate], trade)
            ]
            resolved.append(narrowed[0] if len(narrowed) == 1 else AMBIGUOUS)
            continue
        resolved.append(None)
    return resolved


__all__ = [
    "AMBIGUOUS",
    "canonical_plan_direction",
    "resolve_trade_instruction_ids",
]
