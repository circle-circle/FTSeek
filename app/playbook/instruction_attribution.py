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

# 归因来源 (2026-11-12): 报表必须能分辨「metadata 精确归属」与「decision_id 猜的归属」,
#   否则执行率里混着猜测值而无从审计。SOURCE_METADATA_STALE = metadata 记过 id 但该指令
#   已不在指令表内 (被删/重建), 于是静默降级到模糊匹配 —— 这是最容易被误读成"精确"的形态。
SOURCE_METADATA = "metadata"
SOURCE_METADATA_STALE = "metadata_stale"
SOURCE_DECISION_ID = "decision_id"
SOURCE_AMBIGUOUS = "ambiguous"
SOURCE_UNMATCHED = "unmatched"
ATTRIBUTION_SOURCES = (
    SOURCE_METADATA, SOURCE_METADATA_STALE, SOURCE_DECISION_ID,
    SOURCE_AMBIGUOUS, SOURCE_UNMATCHED,
)


def count_sources(sources: Iterable[str]) -> Dict[str, int]:
    """把来源列表压成 {source: n}, 五类恒有键 (0 也输出, 防"缺键=没发生"的误读)。"""
    counts = {name: 0 for name in ATTRIBUTION_SOURCES}
    for name in sources:
        counts[name] = counts.get(name, 0) + 1
    return counts


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
    sources: List[str] | None = None,
) -> List[Any]:
    """Resolve metadata instruction_id first, then a unique decision_id.

    Returns one item per trade: an ``int`` instruction id, ``None`` when no
    instruction can be attributed, or :data:`AMBIGUOUS` when a duplicated
    decision_id survives date/symbol/direction disambiguation (e.g. same-day
    reentry rounds sharing a decision_id). Callers must treat ``AMBIGUOUS`` as
    "not attributable", not as "unplanned".

    2026-11-12 (核查后加固): 传入 ``sources`` 列表时, 按同一顺序回填每笔的归因来源 —
    ``metadata`` (精确) / ``metadata_stale`` (metadata 记了 id 但该指令已不在表内, 于是
    **静默降级到 decision_id 模糊匹配**) / ``decision_id`` (模糊回退命中) /
    ``ambiguous`` / ``unmatched``。没有这个标记, 报表无法区分「精确归属」与「猜的归属」,
    二者会被算进同一个执行率 —— 那正是本函数存在的意义被抵消的地方。
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
            if sources is not None:
                sources.append(SOURCE_METADATA)
            continue

        decision_id = str(trade.get("decision_id") or "").strip()
        candidates = ids_by_decision_id.get(decision_id, []) if decision_id else []
        if len(candidates) == 1:
            resolved.append(candidates[0])
            if sources is not None:
                sources.append(
                    SOURCE_METADATA_STALE if instruction_id is not None else SOURCE_DECISION_ID)
            continue
        if len(candidates) > 1:
            narrowed = [
                candidate for candidate in candidates
                if _context_matches(instructions_by_id[candidate], trade)
            ]
            resolved.append(narrowed[0] if len(narrowed) == 1 else AMBIGUOUS)
            if sources is not None:
                sources.append(
                    SOURCE_AMBIGUOUS if len(narrowed) != 1
                    else (SOURCE_METADATA_STALE if instruction_id is not None
                          else SOURCE_DECISION_ID))
            continue
        resolved.append(None)
        if sources is not None:
            sources.append(SOURCE_UNMATCHED)
    return resolved


__all__ = [
    "AMBIGUOUS",
    "ATTRIBUTION_SOURCES",
    "SOURCE_AMBIGUOUS",
    "SOURCE_DECISION_ID",
    "SOURCE_METADATA",
    "SOURCE_METADATA_STALE",
    "SOURCE_UNMATCHED",
    "canonical_plan_direction",
    "count_sources",
    "resolve_trade_instruction_ids",
]
