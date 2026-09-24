#!/usr/bin/env python3
"""Build, render, and optionally push the daily review bundle."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.review.bundle_builder import build
from app.review.bundle_render import render_md
from app.review.criteria_status import (
    escalation_candidates,
    guard_escalation,
    pending_adjudications,
    update_ready_state,
)
from app.utils.atomic_write import atomic_write_json, atomic_write_text
from app.utils.timezone import now_et


DB_PATH = ROOT / "data" / "ftseek_us.db"
OUTPUT_DIR = ROOT / "data" / "reviews"
VAULT_DIR = ROOT / "obsidian_vault" / "daily" / "review"


def _default_hub_factory():
    from app.push import get_push_hub

    return get_push_hub()


def _git_provenance() -> dict:
    """N-4 (§26.2): 代码版本溯源 sidecar。

    bundle 本体的 `git={}` 恒空是幂等设计 (hash 不随提交漂移), 版本信息落
    独立 meta 文件 `bundle_<date>.meta.json`, 不参与 bundle hash。
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        )
        return {
            "git_commit": commit.stdout.strip() or None,
            "git_dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        }
    except Exception:
        return {"git_commit": None, "git_dirty": None}


def _push_once(
    report_date: str,
    push_text: str,
    state_path: Path,
    lock_path: Path,
    hub_factory: Callable[[], Any],
) -> bool | str:
    from app.push import daily_review

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if state.get("pushed_at"):
                    return "already"

            hub = hub_factory()
            sent = hub.push(daily_review(report_date, push_text), sync=True)
            if not sent:
                return False
            atomic_write_json(
                state_path,
                {
                    "report_date": report_date,
                    "pushed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "category": "REVIEW_DAILY",
                },
            )
            return True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run(
    report_date: str,
    connection: sqlite3.Connection,
    output_dir: Path,
    vault_dir: Optional[Path] = None,
    push: bool = False,
    hub_factory: Callable[[], Any] = _default_hub_factory,
    print_push: bool = False,
) -> dict[str, Any]:
    bundle = build(connection, report_date)
    pending = pending_adjudications()
    today = now_et().date().isoformat()
    ready_state = update_ready_state(
        pending,
        output_dir / "criteria_ready_state.json",
        output_dir / "criteria_ready_state.lock",
        today,
    )
    for row in pending:
        if row["id"] in ready_state:
            row["ready_since"] = ready_state[row["id"]]["ready_since"]
    bundle.pending_adjudications = pending
    # ── 逾期升级双门槛 (DEB-20260919 §13.4 / §20.2④) ──────────────────────────
    # READY 且逾期 >14 天的项进催办候选; 每天最多 1 条 (caller 侧限流 + flock)。
    # 产物只挂在推送面 (不进 bundle hash, 不影响幂等性); 状态文件单独存。
    esc_candidates = escalation_candidates(pending, ready_state, today)
    escalated = guard_escalation(
        esc_candidates,
        output_dir / "criteria_escalation_state.json",
        output_dir / "criteria_escalation_state.lock",
        today,
    )
    payload = bundle.to_dict()
    full_md, push_text = render_md(bundle, escalated=escalated)

    bundle_path = output_dir / f"bundle_{report_date}.json"
    markdown_path = output_dir / f"REVIEW_{report_date}.md"
    latest_path = output_dir / "bundle_latest.json"
    payload_text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    )
    atomic_write_text(bundle_path, payload_text)
    atomic_write_text(markdown_path, full_md)
    atomic_write_text(latest_path, payload_text)

    # N-4 (§26.2): 版本溯源 sidecar — 独立文件, 不进 bundle hash, 幂等不受影响
    meta_path = output_dir / f"bundle_{report_date}.meta.json"
    atomic_write_text(meta_path, json.dumps({
        "report_date": report_date,
        "schema_note": "version provenance sidecar (DEB-20260919 §26.2 N-4); "
                       "bundle body keeps git={} for hash idempotency",
        **_git_provenance(),
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    if vault_dir is not None:
        atomic_write_text(vault_dir / f"{report_date}_review.md", full_md)

    pushed = False
    if push:
        pushed = _push_once(
            report_date,
            push_text,
            output_dir / f"bundle_{report_date}.push.json",
            output_dir / f"bundle_{report_date}.push.lock",
            hub_factory,
        )

    result = {
        "bundle": str(bundle_path),
        "markdown": str(markdown_path),
        "latest": str(latest_path),
        "meta": str(meta_path),
        "pushed": pushed,
    }
    if print_push:
        # 载体投递面 (2026-09-22): 只把推送文本交出去, 不带 JSON 摘要 —
        # 载体 (Hermes no_agent) 的 stdout 即投递内容, 混入 JSON 会污染推送.
        result["push_text"] = push_text
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="ET report date, YYYY-MM-DD; default yesterday")
    parser.add_argument("--period", choices=("day", "week", "month"), default="day")
    parser.add_argument("--push", action="store_true", help="send one caller-guarded daily push")
    parser.add_argument("--vault", action="store_true", help="write the review page to vault")
    parser.add_argument("--print-push", action="store_true",
                        help="只打印推送文本 (载体投递面; 与 --push 互不影响: 只印不发)")
    args = parser.parse_args(argv)

    if args.period != "day":
        parser.error("P3 implements daily bundles only; weekly/monthly arrive with P5")
    report_date = args.date or (now_et().date() - timedelta(days=1)).isoformat()

    connection = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    try:
        result = run(
            report_date,
            connection,
            OUTPUT_DIR,
            vault_dir=VAULT_DIR if args.vault else None,
            push=args.push,
            print_push=args.print_push,
        )
    finally:
        connection.close()

    if args.print_push:
        print(result["push_text"])
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
