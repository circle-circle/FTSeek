"""Read-only adjudication status backed by the shadow registry."""
from __future__ import annotations

import fcntl
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List

from app.playbook.shadow_registry import REGISTRY, ShadowStatus, adjudication_state, refresh_counts
from app.utils.atomic_write import atomic_write_json


# ── 逾期升级双门槛 (DEB-20260919 §13.4 / §20.2④, 2026-09-24 用户裁决落地) ──────
# 双门槛 = ① 状态 READY ② ready_since 距今 >14 天。
# 每天最多 1 条催办 (caller 侧状态 + flock; 与推送去重同形态)。
# 该常量是门槛值, 不是调参旋钮: 改动须用户裁决 + DEB 留痕。
ESCALATION_OVERDUE_DAYS = 14


def _days_ready(ready_since: str, today: str) -> int:
    """ready_since → 已等待天数 (闭区间; 同天=0)。空值/非法 → 0 (保守: 不升级)。"""
    try:
        return max(0, (date.fromisoformat(today) - date.fromisoformat(ready_since)).days)
    except (TypeError, ValueError):
        return 0


def escalation_candidates(
    rows: Iterable[Dict[str, Any]],
    ready_state: Dict[str, Dict[str, str]],
    today: str,
) -> List[Dict[str, Any]]:
    """READY 且逾期 >14 天的项, 按 ready_since 升序 (最久排先)。

    只负责**筛选**, 不负责"每日最多 1 条"限流 — 限流在推送载体侧做
    (caller 按 `escalation_state.json` 的 `last_escalated_at` 守门)。
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not row.get("ready"):
            continue
        since = (ready_state.get(row["id"]) or {}).get("ready_since")
        if not since:
            continue
        days = _days_ready(since, today)
        if days > ESCALATION_OVERDUE_DAYS:
            out.append({**row, "ready_since": since, "days_ready": days})
    out.sort(key=lambda r: r["ready_since"])
    return out


def read_escalation_state(state_path: Path) -> Dict[str, Any]:
    """读限流状态; 不存在/损坏 → 空 dict (首次催办放行)。"""
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guard_escalation(
    candidates: List[Dict[str, Any]],
    state_path: Path,
    lock_path: Path,
    today: str,
) -> Dict[str, Any] | None:
    """每日最多 1 条催办 (caller 侧限流, flock 守门)。

    返回被放行的那一条 (含 `days_ready` / `ready_since`), 或 None (今日已催过)。
    副作用: 放行时把 `last_escalated_at` 写 today。
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = read_escalation_state(state_path)
            if state.get("last_escalated_at") == today:
                return None
            if not candidates:
                return None
            pick = candidates[0]
            atomic_write_json(state_path, {
                "last_escalated_at": today,
                "last_escalated_id": pick["id"],
                "days_ready": pick["days_ready"],
            })
            return pick
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
