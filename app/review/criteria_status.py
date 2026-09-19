"""Read-only adjudication status backed by the shadow registry."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.playbook.shadow_registry import REGISTRY, ShadowStatus, refresh_counts
from app.utils.atomic_write import atomic_write_json


def pending_adjudications() -> List[Dict[str, Any]]:
    refresh_counts()
    rows: List[Dict[str, Any]] = []
    for item in REGISTRY:
        status = getattr(item.status, "value", item.status)
        current_n = getattr(item, "current_n", None)
        rows.append(
            {
                "id": item.id,
                "status": status,
                "n": current_n,
                "min_n": item.min_n,
                "progress": f"{current_n or 0}/{item.min_n}",
                "ready": status == ShadowStatus.READY_TO_EVALUATE.value,
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
