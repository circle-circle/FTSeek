#!/opt/ftseek/FTSeek-US/venv/bin/python
"""DA main carrier for the dual-season ET schedule."""

from __future__ import annotations

import json
import fcntl
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path("/opt/ftseek/FTSeek-US")
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
PYTHON = REPO / "venv/bin/python"
SCRATCH = Path("/root/.hermes/cache/scratch")
EVENT_PATH = REPO / "data/shadow/da_main_schedule.jsonl"


def now_et() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def run_repo_python(code: str) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    return subprocess.run(
        [str(PYTHON), "-c", code], cwd=REPO, env=env,
        text=True, capture_output=True, timeout=120, check=False)


def is_trading_day(day) -> bool:
    completed = run_repo_python(
        "from app.utils.holiday_calendar import get_holiday_calendar\n"
        f"print('TRADING_DAY', get_holiday_calendar().is_trading_day('US', {day!r}))")
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("TRADING_DAY "):
            return line.split()[-1] == "True"
    return True


def watchlist_symbols() -> list[str]:
    completed = run_repo_python(
        "from app.utils.watchlist_manager import get_stock_codes\n"
        "print(','.join(get_stock_codes() or []))")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    return [symbol for symbol in lines[-1].split(",") if symbol]


def append_event(event: dict) -> None:
    EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_PATH.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> int:
    stamp = now_et()
    date = stamp.strftime("%Y-%m-%d")
    minute = stamp.hour * 100 + stamp.minute
    base_event = {"type": "da_main_schedule", "date": date, "started_at": stamp.isoformat()}
    if not is_trading_day(stamp.date()):
        append_event({**base_event, "status": "skipped_non_trading_day"})
        return 0
    if not 755 <= minute <= 815:
        append_event({**base_event, "status": "skipped_outside_window", "et_minute": minute})
        return 0

    symbols = watchlist_symbols()
    if not symbols:
        append_event({**base_event, "status": "failed_empty_watchlist"})
        print(f"[DA-main] {date} watchlist empty, skipped pipeline")
        return 1
    symbol_arg = ",".join(symbols)

    pullback = subprocess.run(
        [str(PYTHON), str(SKILL_SCRIPTS / "pullback_stats.py"), symbol_arg],
        cwd=SKILL_SCRIPTS,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if pullback.returncode != 0:
        print(f"[DA-main] pullback refresh failed (non-blocking): "
              f"{pullback.stderr[-500:]}")

    SCRATCH.mkdir(parents=True, exist_ok=True)
    output_path = SCRATCH / f"da_pipeline_out_{date.replace('-', '')}.json"
    try:
        completed = subprocess.run(
            [str(PYTHON), str(SKILL_SCRIPTS / "pipeline.py"), "--mode", "full",
             "--top", "5", "--symbols", symbol_arg, "--format", "json"],
            cwd=SKILL_SCRIPTS,
            text=True,
            capture_output=True,
            timeout=3900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        append_event({**base_event, "status": "pipeline_timeout"})
        print(f"[DA-main] {date} pipeline timeout")
        return 1

    output_path.write_text(completed.stdout, encoding="utf-8")
    finished_at = now_et().isoformat()
    if completed.returncode != 0:
        append_event({**base_event, "status": "pipeline_failed",
                      "returncode": completed.returncode, "finished_at": finished_at})
        print(f"[DA-main] {date} pipeline failed rc={completed.returncode}")
        print(completed.stderr[-2000:])
        return completed.returncode or 1

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        append_event({**base_event, "status": "pipeline_json_parse_failed",
                      "finished_at": finished_at})
        print(f"[DA-main] {date} pipeline JSON parse failed")
        print(completed.stdout[-2000:])
        return 1
    if result.get("skipped"):
        append_event({**base_event, "status": "pipeline_skipped",
                      "skip_reason": result.get("skip_reason"), "finished_at": finished_at})
        print(f"[DA-main] {date} pipeline skipped: {result.get('skip_reason')}")
        return 0

    stages = result.get("stages", {})
    analyses = stages.get("output", {}).get("analyses", [])
    directions = {}
    for row in analyses:
        direction = str(row.get("direction") or row.get("rating") or "?")
        directions[direction] = directions.get(direction, 0) + 1
    print(f"[DA-main] {date} candidates={stages.get('signal', {}).get('candidates_total', 0)} "
          f"analyses={len(analyses)} directions={directions} output={output_path}")
    append_event({**base_event, "status": "complete",
                  "candidates_total": stages.get("signal", {}).get("candidates_total", 0),
                  "analyses_count": len(analyses), "finished_at": finished_at})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
