#!/usr/bin/env python3
"""UE premarket fresh events → same-day DA incremental candidate batch.

Default mode is an isolated shadow run: DA analyzes UE candidates but writes to
`data/shadow/da_fresh_event/<ET date>/` and skips production DB/pool writes.
Pass `--execute` only after the shadow contract has been approved.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.playbook.ue_batch import latest_valid_batch
from app.utils.timezone import now_et

SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
DEFAULT_PIPELINE = SKILL_SCRIPTS / "pipeline.py"
DEFAULT_SCAN_PATH = REPO_ROOT / "data/shadow/llm_universe_scan.jsonl"
DEFAULT_EVENT_PATH = REPO_ROOT / "data/shadow/ue_fresh_da_candidate_pool.jsonl"
DEFAULT_MAX_CANDIDATES = 3
DEFAULT_MAX_AGE_HOURS = 1.0


def _read_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    rows = []
    n_bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            n_bad += 1  # 2026-10-08 棘轮清剿: 坏行计数留痕 (原为静默 continue)
            continue
    if n_bad:
        print(f"[warn] {path.name}: 跳过坏行 {n_bad} 条", file=sys.stderr)
    return rows


def _append_event(path: Path, event: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _completed_today(event_path: Path, date: str, mode: str) -> bool:
    return any(
        row.get("type") == "fresh_event_batch"
        and row.get("date") == date
        and row.get("mode") == mode
        and row.get("status") == "complete"
        for row in _read_jsonl(event_path)
    )


def _is_trading_day(day) -> bool:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return bool(get_holiday_calendar().is_trading_day("US", day))
    except Exception:
        return True


def load_fresh_scan(
    scan_path: Path = DEFAULT_SCAN_PATH,
    now: Optional[datetime] = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> Tuple[Optional[Dict], str]:
    now = now or now_et()
    today = now.strftime("%Y-%m-%d")
    return latest_valid_batch(
        _read_jsonl(scan_path), today, now, max_age_hours, window="premarket")


def select_fresh_candidates(
    scan: Optional[Dict], max_candidates: int = DEFAULT_MAX_CANDIDATES
) -> List[Dict]:
    if not scan:
        return []
    selected = []
    for row in scan.get("surfaced", []):
        direction = str(row.get("direction") or "").upper()
        if direction not in ("LONG", "SHORT"):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        selected.append({
            "symbol": symbol if "." in symbol else f"US.{symbol}",
            "direction": direction,
            "confidence": row.get("confidence"),
            "thesis": row.get("thesis", ""),
            "ue_batch_id": scan.get("batch_id", ""),
        })
        if len(selected) >= max_candidates:
            break
    return selected


def build_command(
    python: str,
    pipeline: Path,
    execute: bool,
    output_dir: Path,
    context_dir: Path,
) -> List[str]:
    command = [
        python,
        str(pipeline),
        "--mode", "full",
        "--top", "0",
        "--fresh-event",
        "--format", "json",
        "--fresh-context-dir", str(context_dir),
    ]
    if not execute:
        command.extend(["--no-db-write", "--no-pool-write", "--output-dir", str(output_dir)])
    return command


def run(args: argparse.Namespace) -> int:
    now = now_et()
    date = now.strftime("%Y-%m-%d")
    mode = "execute" if args.execute else "shadow"
    base_event = {
        "type": "fresh_event_batch",
        "date": date,
        "mode": mode,
        "started_at": now.isoformat(),
    }

    if not args.force and not _is_trading_day(now.date()):
        _append_event(args.event_path, {**base_event, "status": "skipped_non_trading_day"})
        print(f"[fresh-event] {date} non-trading day, skipped")
        return 0

    minute_of_day = now.hour * 100 + now.minute
    if not args.force and not 900 <= minute_of_day <= 929:
        _append_event(args.event_path, {
            **base_event, "status": "skipped_outside_window", "et_minute": minute_of_day,
        })
        print(f"[fresh-event] ET {minute_of_day:04d} outside 0900-0929 start window, skipped")
        return 0

    if _completed_today(args.event_path, date, mode):
        _append_event(args.event_path, {**base_event, "status": "skipped_idempotent"})
        print(f"[fresh-event] {date} {mode} already complete")
        return 0

    scan, reason = load_fresh_scan(
        args.scan_path, now=now, max_age_hours=args.max_age_hours)
    if scan is None:
        _append_event(args.event_path, {
            **base_event, "status": "complete_no_scan", "reason": reason,
        })
        print(f"[fresh-event] no valid premarket scan: {reason}")
        return 0

    candidates = select_fresh_candidates(scan, args.max_candidates)
    if not candidates:
        _append_event(args.event_path, {
            **base_event, "status": "complete_no_candidates",
            "ue_batch_id": scan.get("batch_id", ""),
        })
        print("[fresh-event] no LONG/SHORT fresh candidates")
        return 0

    output_dir = args.output_dir if args.output_dir else (
        REPO_ROOT / "data/shadow/da_fresh_event" / date)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(
        args.python, args.pipeline, args.execute, output_dir,
        args.context_dir)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=args.timeout,
            check=False,
        )
        status = "complete" if completed.returncode == 0 else "pipeline_failed"
        event = {
            **base_event,
            "status": status,
            "ue_batch_id": scan.get("batch_id", ""),
            "scanned_at": scan.get("scanned_at", ""),
            "candidates": candidates,
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "finished_at": now_et().isoformat(),
        }
        _append_event(args.event_path, event)
        if completed.stdout:
            print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        print(f"[fresh-event] {status}: {len(candidates)} candidates")
        return completed.returncode
    except subprocess.TimeoutExpired as exc:
        _append_event(args.event_path, {
            **base_event,
            "status": "pipeline_timeout",
            "ue_batch_id": scan.get("batch_id", ""),
            "candidates": candidates,
            "command": command,
            "timeout": args.timeout,
            "finished_at": now_et().isoformat(),
        })
        print(f"[fresh-event] pipeline timeout after {args.timeout}s: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="write production DA instructions (default is isolated shadow)")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--pipeline", type=Path, default=DEFAULT_PIPELINE)
    parser.add_argument("--scan-path", type=Path, default=DEFAULT_SCAN_PATH)
    parser.add_argument("--event-path", type=Path, default=DEFAULT_EVENT_PATH)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--context-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--force", action="store_true",
                        help="skip ET window/trading-day/idempotency guards (testing)")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
