#!/usr/bin/env python3
"""One-minute ET-calendar entry point for extended-hours exit collectors."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_market_session import get_extended_hours_session

LOCK_FILE = ROOT / "logs" / "extended_hours_exit_cron.lock"


def collector_command(session):
    if session == "premarket":
        return [sys.executable, str(ROOT / "scripts" / "premarket_exit_shadow.py")]
    if session == "afterhours":
        return [sys.executable, str(ROOT / "scripts" / "afterhours_exit_shadow.py")]
    return None


def run_once(dry=False, timeout_s=120):
    session = get_extended_hours_session()
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
