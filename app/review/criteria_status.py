"""Read-only adjudication status backed by the shadow registry."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.playbook.shadow_registry import REGISTRY, ShadowStatus, adjudication_state, refresh_counts
from app.utils.atomic_write import atomic_write_json


def pending_adjudications() -> List[Dict[str, Any]]:
    refresh_counts()
    rows: List[Dict[str, Any]] = []
    for item in REGISTRY:
        status = getattr(item.status, "value", item.status)
        current_n = getattr(item, "current_n", None)
        adj = adjudication_state(item)
        ready = status == ShadowStatus.READY_TO_EVALUATE.value
        # 待裁 = 样本量达标 ∧ 无挂账原因 ∧ (判据从未读过 ∨ 读过了但处置待用户裁 ∨ 重看闹钟已到)。
        # 语义说明: READY 只代表"样本量够"; 已读判据且未到 recheck_after 的项属"已读数待攒样",
        # 不构成待裁面 (2026-09-21 RFC-092 §8.6-2/§10.3 字段级修法)。
        needs = bool(ready and not item.no_adjudication_reason
                     and (not adj["adjudicated"] or adj["needs_user"] or adj["overdue"]))
        rows.append(
            {
                "id": item.id,
                "status": status,
                "n": current_n,
                "min_n": item.min_n,
                "progress": f"{current_n or 0}/{item.min_n}",
                "ready": ready,
                "adjudicated_at": adj["read_at"],
                "recheck_after": adj["recheck_after"],
                "needs_adjudication": needs,
                "criteria": item.graduation_criteria,
                "artifact": item.data_path,
                "eval_script": item.eval_script,
                "count_mode": getattr(item, "count_mode", None),
                "evidence": {
                    "kind": "artifact",
                    "ref": f"app/playbook/shadow_registry.py :: id={item.id}",
                },
            }
        )
    return rows


def update_ready_state(
    rows: Iterable[Dict[str, Any]],
    state_path: Path,
    lock_path: Path,
    report_date: str,
) -> Dict[str, Dict[str, str]]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            state = {
                row["id"]: previous.get(row["id"])
                or {"ready_since": report_date}
                for row in rows
                if row.get("ready")
            }
            atomic_write_json(state_path, state)
            return state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
