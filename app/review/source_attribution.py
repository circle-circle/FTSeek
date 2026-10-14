"""Shared playbook instruction source attribution."""
from __future__ import annotations

from typing import Dict, Iterable, List


PLAYBOOK_SOURCE_SQL = """CASE
        WHEN raw_instruction_json LIKE '%topup_pool%' THEN 'topup'
        WHEN raw_instruction_json LIKE '%universe_expansion%'
             OR decision_id LIKE 'universe_expansion:%' THEN 'UE直写'
        WHEN raw_instruction_json LIKE '%ue_fresh_event%' THEN 'DA-UE新事件'
        WHEN raw_instruction_json LIKE '%ue_candidate%' THEN 'DA-UE确认'
        ELSE 'DA自选' END"""
# [2026-10-14 RFC-136 execute转正 · rfc117_v3] 'DA-UE新事件' 腿: fresh-event 批
#   (da_fresh_event_batch --execute) 的指令由 pipeline Stage 6b 写
#   origin="ue_fresh_event" (仅 ue_only 模式; Path A da_feed 并入 DA 主链的仍
#   origin="ue_candidate" → 'DA-UE确认' 腿)。用户裁决 2026-10-14: 三源分账
#   (DA自选 / DA-UE确认 / DA-UE新事件) 以实盘(SIM)交易测量各腿效果。
#   CASE 序: ue_fresh_event 腿必须在 ue_candidate 腿**之前** — 若 raw 同时含两个
#   字面量 (如透传字段带 _source="ue_candidate"), 更具体的腿先归因。
#   版本号 rfc117_v2→v3 (daily/weekly funnel 落库字段同步)。
# [RFC-117 P2 复核 2026-09-29] decision_id 前缀腿 (rfc117_v2): UE inject 的 SKIP 行
#   raw_instruction_json 只含 {"admission":...} 块、无 universe_expansion 字面量,
#   仅靠 raw LIKE 会把 9 条历史 UE SKIP 行误归 'DA自选' (实测 DB), 污染
#   §4.4 采纳率分母。decision_id 是 UE 源的权威标识 (inject 写入
#   'universe_expansion:{date}:{SYM}')。边界实测: 与 ue_candidate/topup 零交集,
#   CASE 优先级无冲突。版本号 rfc117_v1→v2 (daily/weekly funnel 落库字段同步)。
# [2026-10-06 UE fresh-event 批·留痕] UE inject 已退役 (crontab inject/push 行改串
#   da_fresh_event_batch.py) ⇒ 'UE直写' 腿自此**恒 0 新增** (历史行保留, 读取面不删);
#   UE 源的在役承接腿 = 'DA-UE确认' (raw 含 ue_candidate, DA --fresh-event/UE 供料注入
#   origin="ue_candidate")。SQL 判据字面量未变、decision_id 前缀腿保留为历史行权威标识
#   (删了旧账本读不出归属)。后人解读口径: 按日期分层, 退役日起 UE直写=0 是设计而非通道死。


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
    return ["DA自选", "DA-UE确认", "DA-UE新事件", "UE直写", "topup"]
