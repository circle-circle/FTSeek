"""Shared playbook instruction source attribution."""
from __future__ import annotations

from typing import Dict, Iterable, List


PLAYBOOK_SOURCE_SQL = """CASE
        WHEN raw_instruction_json LIKE '%topup_pool%' THEN 'topup'
        WHEN raw_instruction_json LIKE '%universe_expansion%' THEN 'UE直写'
        WHEN raw_instruction_json LIKE '%ue_candidate%' THEN 'DA-UE确认'
        ELSE 'DA自选' END"""


def playbook_source_sql() -> str:
    return PLAYBOOK_SOURCE_SQL


def aggregate_source_direction(rows: Iterable[Dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        source = str(row.get("source") or "unknown")
        direction = str(row.get("direction") or "unknown")
        executed_suffix = "" if row.get("executed") else "(未执行)"
        key = f"{source}·{direction}{executed_suffix}"
        counts[key] = counts.get(key, 0) + int(row.get("cnt") or 0)
    return dict(sorted(counts.items()))


def source_labels() -> List[str]:
    return ["DA自选", "DA-UE确认", "UE直写", "topup"]
