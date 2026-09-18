#!/usr/bin/env python3
"""One-minute ET-calendar entry point for extended-hours exit collectors."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_market_session import get_extended_hours_session

LOCK_FILE = ROOT / "logs" / "extended_hours_exit_cron.lock"
NEXT_OPEN_START_MINUTES = 570
NEXT_OPEN_END_MINUTES = 585


def collector_command(session):
    if session == "premarket":
        return [sys.executable, str(ROOT / "scripts" / "premarket_exit_shadow.py")]
    if session == "afterhours":
        return [sys.executable, str(ROOT / "scripts" / "afterhours_exit_shadow.py")]
    if session == "next_open":
        return [sys.executable, str(ROOT / "scripts" / "premarket_exit_shadow.py")]
    return None


def is_next_open_window(now):
    from app.utils.holiday_calendar import get_holiday_calendar

    minutes = now.hour * 60 + now.minute
    return (
        NEXT_OPEN_START_MINUTES <= minutes < NEXT_OPEN_END_MINUTES
        and get_holiday_calendar().is_trading_day("US", now.date())
    )


def get_exit_cron_session(now=None):
    if now is None:
        from app.utils.timezone import now_et
        now = now_et()
    session = get_extended_hours_session(now)
    if session is not None:
        return session
    return "next_open" if is_next_open_window(now) else None


def run_once(dry=False, timeout_s=120, now=None):
    session = get_exit_cron_session(now)
    command = collector_command(session)
    if session is None or command is None:
        return {"ok": True, "skip": "outside_extended_hours", "session": session}

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "skip": "previous_run_still_active", "session": session}
        if dry:
            return {"ok": True, "dry": True, "session": session, "command": command}
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "session": session,
                "error": f"collector_timeout_after_{timeout_s}s",
            }
        return {
            "ok": completed.returncode == 0,
            "session": session,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-1000:],
            "stderr_tail": (completed.stderr or "")[-1000:],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    result = run_once(args.dry, args.timeout)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
