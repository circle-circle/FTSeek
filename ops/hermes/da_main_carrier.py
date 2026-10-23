#!/opt/ftseek/FTSeek-US/venv/bin/python
"""DA main carrier for the dual-season ET schedule.

2026-10-23 hardening:
- preflight subprocesses are fail-soft (a Futu/OpenD stall must not kill the batch);
- a start event + heartbeat make the running batch visible before completion;
- stderr is streamed to logs/da_pipeline_stderr.log (no longer only post-run);
- a hard ET deadline triggers ``pipeline.py --mode finalize`` from checkpoints;
- success is verified from bridge/DB artifacts, not only stdout JSON;
- a process lock prevents two carriers writing the same batch concurrently.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path("/opt/ftseek/FTSeek-US")
SKILL_SCRIPTS = Path("/root/.hermes/skills/deep-stock-analysis/scripts")
PYTHON = REPO / "venv/bin/python"
SCRATCH = Path("/root/.hermes/cache/scratch")
EVENT_PATH = REPO / "data/shadow/da_main_schedule.jsonl"
BRIDGE_DIR = REPO / "data"
DB_PATH = REPO / "data/ftseek_us.db"
PIPELINE_LOG = REPO / "logs/da_pipeline_stderr.log"
PULLBACK_LOG = REPO / "logs/pullback_shadow.log"
FINALIZE_LOG = REPO / "logs/da_finalize.log"
LOCK_PATH = REPO / "data/.da_main_carrier.lock"

# 0857 ET leaves >=3 minutes for finalize + bridge write before the 09:00
# generate window. 2026-10-21's historical max was 08:55:20 ET.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


DEADLINE_ET_HHMM = _env_int("DA_CARRIER_DEADLINE_ET", 857)
HEARTBEAT_SEC = _env_int("DA_CARRIER_HEARTBEAT_SEC", 60)
PIPELINE_MAX_SEC = 3900
TZ = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(TZ)


def _completed(args, returncode=1, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def run_repo_python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    try:
        return subprocess.run(
            [str(PYTHON), "-c", code], cwd=REPO, env=env,
            text=True, capture_output=True, timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[DA-main] preflight python failed (fail-soft): {exc}")
        return _completed([str(PYTHON), "-c"], stderr=str(exc))


def is_trading_day(day) -> bool:
    completed = run_repo_python(
        "from app.utils.holiday_calendar import get_holiday_calendar\n"
        f"print('TRADING_DAY', get_holiday_calendar().is_trading_day('US', {day!r}))")
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("TRADING_DAY "):
            return line.split()[-1] == "True"
    return True  # fail-open: calendar outage must not silently stop the DA chain


def watchlist_symbols() -> list[str]:
    completed = run_repo_python(
        "from app.utils.watchlist_manager import get_stock_codes\n"
        "print(','.join(get_stock_codes() or []))")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    return [symbol for symbol in lines[-1].split(",") if symbol]


def append_event(event: dict) -> None:
    try:
        EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_PATH.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        print(f"[DA-main] schedule event write failed (non-blocking): {exc}")


def _checkpoint_count(date: str) -> int:
    try:
        root = REPO / "data" / "da_checkpoint" / date
        return sum(1 for p in root.glob("*.json")
                   if not p.name.endswith((".raw.json", ".bak", ".tmp")))
    except Exception:
        return 0


def _heartbeat(date: str, base_event: dict, stop: threading.Event) -> None:
    while not stop.wait(max(5, HEARTBEAT_SEC)):
        append_event({**base_event, "status": "heartbeat",
                      "analyses_done": _checkpoint_count(date),
                      "at": now_et().isoformat()})


def _deadline_seconds(stamp: datetime) -> float:
    try:
        hour, minute = divmod(int(DEADLINE_ET_HHMM), 100)
        target = stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (TypeError, ValueError):
        return float(PIPELINE_MAX_SEC)
    return max(0.0, (target - stamp).total_seconds())


def _timeout_command(args: list[str], seconds: int) -> list[str]:
    """Use coreutils timeout so a dead carrier cannot leave an unbounded child."""
    timeout_bin = shutil.which("timeout")
    if not timeout_bin:
        return args
    return [timeout_bin, "-k", "30", str(max(1, int(seconds))), *args]


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[-limit:]
    except Exception:
        return ""


def _parse_json_output(text: str):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            try:
                return json.loads(text[first:last + 1])
            except json.JSONDecodeError:
                return None
        return None


def _verify_artifacts(date: str) -> dict:
    """Verify the real handoff surfaces, independent of stdout formatting."""
    bridge_path = BRIDGE_DIR / f"opportunity_scan_{date}.json"
    out = {"bridge_exists": bridge_path.exists(), "bridge_date": None,
           "candidate_count": None, "db_instruction_count": None}
    if bridge_path.exists():
        try:
            data = json.loads(bridge_path.read_text(encoding="utf-8"))
            out["bridge_date"] = data.get("trade_date")
            out["candidate_count"] = len(data.get("candidates") or [])
        except Exception as exc:
            out["bridge_error"] = str(exc)[:200]
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            out["db_instruction_count"] = conn.execute(
                "SELECT COUNT(*) FROM playbook_instructions "
                "WHERE date=? AND (decision_id LIKE 'DA-%' "
                "OR decision_id LIKE 'universe_expansion%')", (date,)).fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        out["db_error"] = str(exc)[:200]
    out["bridge_current"] = bool(out["bridge_exists"] and out["bridge_date"] == date)
    out["ok"] = bool(out["bridge_current"]
                     or (out["db_instruction_count"] or 0) > 0)
    return out


def _finalize(date: str) -> dict:
    """Write bridge/DB from today's per-symbol checkpoints (no LLM, no scan)."""
    cmd = [str(PYTHON), str(SKILL_SCRIPTS / "pipeline.py"),
           "--mode", "finalize", "--date", date, "--format", "json"]
    FINALIZE_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FINALIZE_LOG.open("a", encoding="utf-8") as log:
            completed = subprocess.run(
                _timeout_command(cmd, 300), cwd=SKILL_SCRIPTS,
                text=True, capture_output=True, timeout=330, check=False)
            log.write((completed.stdout or "") + (completed.stderr or ""))
        parsed = _parse_json_output(completed.stdout or "")
        return {"returncode": completed.returncode, "result": parsed,
                "stdout_tail": (completed.stdout or "")[-1000:],
                "stderr_tail": (completed.stderr or "")[-1000:]}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "result": None, "error": "finalize_timeout"}
    except OSError as exc:
        return {"returncode": 1, "result": None, "error": str(exc)}


def _acquire_lock():
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = LOCK_PATH.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (BlockingIOError, OSError):
        return None


def _release_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def main() -> int:
    stamp = now_et()
    date = stamp.strftime("%Y-%m-%d")
    minute = stamp.hour * 100 + stamp.minute
    base_event = {"type": "da_main_schedule", "date": date,
                  "started_at": stamp.isoformat()}
    if not is_trading_day(stamp.date()):
        append_event({**base_event, "status": "skipped_non_trading_day"})
        return 0
    if not 755 <= minute <= 815:
        append_event({**base_event, "status": "skipped_outside_window",
                      "et_minute": minute})
        return 0

    lock = _acquire_lock()
    if lock is None:
        append_event({**base_event, "status": "skipped_already_running"})
        return 0

    heartbeat_stop = threading.Event()
    heartbeat = None
    try:
        append_event({**base_event, "status": "running", "pid": os.getpid(),
                      "deadline_et": f"{int(DEADLINE_ET_HHMM):04d}"})
        if HEARTBEAT_SEC > 0:
            heartbeat = threading.Thread(
                target=_heartbeat, args=(date, base_event, heartbeat_stop),
                name="da-carrier-heartbeat", daemon=True)
            heartbeat.start()

        symbols = watchlist_symbols()
        if not symbols:
            # scanner-only fallback: a watchlist outage must not zero the DA batch
            append_event({**base_event, "status": "watchlist_empty_continue"})
            print(f"[DA-main] {date} watchlist empty, continue scanner-only")
        symbol_arg = ",".join(symbols)

        pullback = None
        try:
            PULLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
            with PULLBACK_LOG.open("a", encoding="utf-8") as plog:
                pullback = subprocess.run(
                    _timeout_command(
                        [str(PYTHON), str(SKILL_SCRIPTS / "pullback_stats.py"),
                         symbol_arg], 300),
                    cwd=SKILL_SCRIPTS, text=True, stdout=plog, stderr=plog,
                    timeout=330, check=False)
        except subprocess.TimeoutExpired:
            print(f"[DA-main] {date} pullback timeout (non-blocking)")
        except OSError as exc:
            print(f"[DA-main] {date} pullback spawn failed (non-blocking): {exc}")
        if pullback is not None and pullback.returncode != 0:
            print(f"[DA-main] {date} pullback failed rc={pullback.returncode} "
                  f"(non-blocking; see {PULLBACK_LOG.name})")

        remaining = _deadline_seconds(now_et())
        if remaining < 60:
            append_event({**base_event, "status": "deadline_missed",
                          "finished_at": now_et().isoformat()})
            print(f"[DA-main] {date} past deadline {DEADLINE_ET_HHMM:04d} ET; "
                  f"pipeline not started")
            return 1
        pipeline_timeout = int(min(PIPELINE_MAX_SEC, remaining))

        SCRATCH.mkdir(parents=True, exist_ok=True)
        output_path = SCRATCH / f"da_pipeline_out_{date.replace('-', '')}.json"
        completed = None
        try:
            PIPELINE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with PIPELINE_LOG.open("a", encoding="utf-8") as slog:
                completed = subprocess.run(
                    _timeout_command(
                        [str(PYTHON), str(SKILL_SCRIPTS / "pipeline.py"),
                         "--mode", "full", "--top", "5",
                         "--symbols", symbol_arg, "--format", "json"],
                        pipeline_timeout),
                    cwd=SKILL_SCRIPTS, text=True, stdout=subprocess.PIPE,
                    stderr=slog, timeout=pipeline_timeout + 30, check=False)
        except subprocess.TimeoutExpired:
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            append_event({**base_event, "status": "pipeline_timeout_finalized",
                          "finished_at": now_et().isoformat(),
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline timeout; finalize rc="
                  f"{finalized.get('returncode')} artifacts={artifacts}")
            return 0 if artifacts.get("ok") else 1
        except OSError as exc:
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            append_event({**base_event, "status": "pipeline_spawn_failed",
                          "error": str(exc)[:300],
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            return 0 if artifacts.get("ok") else 1
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=2)

        if completed.stdout:
            try:
                output_path.write_text(completed.stdout, encoding="utf-8")
            except OSError as exc:
                print(f"[DA-main] output snapshot write failed: {exc}")

        finished_at = now_et().isoformat()
        if completed.returncode in (124, 137, 143):
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            append_event({**base_event, "status": "pipeline_timeout_finalized",
                          "returncode": completed.returncode,
                          "finished_at": finished_at,
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline timeout rc={completed.returncode}; "
                  f"finalize rc={finalized.get('returncode')} artifacts={artifacts}")
            return 0 if artifacts.get("ok") else 1
        if completed.returncode != 0:
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            append_event({**base_event, "status": "pipeline_failed",
                          "returncode": completed.returncode,
                          "finished_at": finished_at,
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline failed rc={completed.returncode}; "
                  f"finalize rc={finalized.get('returncode')} artifacts={artifacts}")
            print(_tail(PIPELINE_LOG, 2000))
            return completed.returncode or 1

        result = _parse_json_output(completed.stdout or "")
        if result is None:
            artifacts = _verify_artifacts(date)
            if artifacts.get("ok"):
                # 10/12 precedent: stdout noise must not turn a real batch into failure.
                append_event({**base_event, "status": "complete_unparsed_stdout",
                              "finished_at": finished_at,
                              "artifacts": artifacts})
                print(f"[DA-main] {date} stdout unparsed but artifacts ok: "
                      f"{artifacts}")
                return 0
            append_event({**base_event, "status": "pipeline_json_parse_failed",
                          "finished_at": finished_at,
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline JSON parse failed; artifacts={artifacts}")
            print((completed.stdout or "")[-2000:])
            return 1

        if result.get("skipped"):
            append_event({**base_event, "status": "pipeline_skipped",
                          "skip_reason": result.get("skip_reason"),
                          "finished_at": finished_at})
            print(f"[DA-main] {date} pipeline skipped: {result.get('skip_reason')}")
            return 0

        stages = result.get("stages", {})
        analyses = stages.get("output", {}).get("analyses", [])
        directions = {}
        for row in analyses:
            direction = str(row.get("direction") or row.get("rating") or "?")
            directions[direction] = directions.get(direction, 0) + 1
        artifacts = _verify_artifacts(date)
        print(f"[DA-main] {date} "
              f"candidates={stages.get('signal', {}).get('candidates_total', 0)} "
              f"analyses={len(analyses)} directions={directions} "
              f"output={output_path} artifacts={artifacts}")
        append_event({**base_event, "status": "complete",
                      "candidates_total": stages.get("signal", {}).get(
                          "candidates_total", 0),
                      "analyses_count": len(analyses),
                      "artifacts": artifacts,
                      "finished_at": finished_at})
        return 0
    finally:
        heartbeat_stop.set()
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
