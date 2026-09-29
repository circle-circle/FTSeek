"""Shared playbook instruction source attribution."""
from __future__ import annotations

from typing import Dict, Iterable, List


PLAYBOOK_SOURCE_SQL = """CASE
        WHEN raw_instruction_json LIKE '%topup_pool%' THEN 'topup'
        WHEN raw_instruction_json LIKE '%universe_expansion%'
             OR decision_id LIKE 'universe_expansion:%' THEN 'UE直写'
        WHEN raw_instruction_json LIKE '%ue_candidate%' THEN 'DA-UE确认'
        ELSE 'DA自选' END"""
# [RFC-117 P2 复核 2026-09-29] decision_id 前缀腿 (rfc117_v2): UE inject 的 SKIP 行
#   raw_instruction_json 只含 {"admission":...} 块、无 universe_expansion 字面量,
#   仅靠 raw LIKE 会把 9 条历史 UE SKIP 行误归 'DA自选' (实测 DB), 污染
#   §4.4 采纳率分母。decision_id 是 UE 源的权威标识 (inject 写入
#   'universe_expansion:{date}:{SYM}')。边界实测: 与 ue_candidate/topup 零交集,
#   CASE 优先级无冲突。版本号 rfc117_v1→v2 (daily/weekly funnel 落库字段同步)。


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
