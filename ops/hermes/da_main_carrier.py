#!/opt/ftseek/FTSeek-US/venv/bin/python
"""DA main carrier for the dual-season ET schedule.

2026-10-23 hardening:
- preflight subprocesses are fail-soft (a Futu/OpenD stall must not kill the batch);
- a start event + heartbeat make the running batch visible before completion;
- stderr is streamed to logs/da_pipeline_stderr.log (no longer only post-run);
- a hard ET deadline triggers ``pipeline.py --mode finalize`` from checkpoints;
- success is verified from bridge/DB artifacts, not only stdout JSON;
- a process lock prevents two carriers writing the same batch concurrently.

2026-11-13 ledger-lifecycle hardening (root cause of the "ledger disappeared"
investigation was a path/reader mismatch — ``data/da_main_schedule.jsonl`` and
relative opens from a non-repo cwd both raise FileNotFoundError while the one
true ledger lives at ``data/shadow/da_main_schedule.jsonl`` and was never
unlinked). The carrier nevertheless now defends the ledger itself:
- every event is one ``O_APPEND`` write(2) under ``flock`` + ``fsync`` so a
  heartbeat and a terminal event can never interleave or be lost to a crash;
- if the ledger vanishes after this process has written it, the carrier
  recreates it and records ``ledger_missing_recreated`` in-band;
- the heartbeat thread is stopped and joined before any terminal event, so a
  heartbeat can never land after the terminal line;
- write/verify failures are printed loudly and mirrored to
  ``da_main_schedule.jsonl.fallback`` instead of vanishing into stdout.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
MAIN_POOL_TOP_N = 8
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


def _encode_event(event: dict) -> bytes:
    """Serialize exactly one ledger line (newline included)."""
    return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _fsync_parent(path: Path) -> None:
    """Make the directory entry durable when this append created the ledger."""
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        print(f"[DA-main] parent fsync failed (non-blocking): {exc}", file=sys.stderr)
    finally:
        os.close(dir_fd)


def _append_bytes(path: Path, payload: bytes) -> None:
    """Append one encoded event as a single, durable ``O_APPEND`` write.

    ``flock`` + one write(2) keeps every line whole even when the heartbeat
    thread and the main terminal path append at the same moment; ``fsync``
    makes the event survive a crash instead of living only in the page cache.
    The ledger is only ever opened append-only (never truncated/deleted).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(f"short append write to {path}")
                view = view[written:]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    if created:
        _fsync_parent(path)


def _persisted(path: Path, payload: bytes) -> bool:
    """True when the ledger's tail is byte-identical to the event just written."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(max(0, end - len(payload)))
            return handle.read() == payload
    except OSError:
        return False


def _fallback_path() -> Path:
    return EVENT_PATH.with_name(EVENT_PATH.name + ".fallback")


def _report_ledger_failure(event: dict, exc: Exception) -> None:
    """Telemetry writes must never be silent: echo loudly and mirror the event."""
    status = event.get("status")
    line = (f"[DA-main] schedule event write failed (non-blocking): path={EVENT_PATH} "
            f"status={status} error={type(exc).__name__}: {exc}")
    print(line)
    print(line, file=sys.stderr)
    fallback = _fallback_path()
    try:
        _append_bytes(fallback, _encode_event({
            **event,
            "ledger_fallback_for": str(EVENT_PATH),
            "ledger_error": f"{type(exc).__name__}: {exc}"[:300],
        }))
        print(f"[DA-main] schedule event preserved in fallback ledger: {fallback}")
    except Exception as fallback_exc:  # noqa: BLE001 - last-resort diagnostics
        print(f"[DA-main] schedule event fallback write failed too: "
              f"{type(fallback_exc).__name__}: {fallback_exc}", file=sys.stderr)


_LEDGER_WRITES: dict[str, int] = {}
_LEDGER_WRITE_LOCK = threading.Lock()


def append_event(event: dict, *, verify: bool = False) -> bool:
    """Append one schedule event; return True when it is durably on disk.

    ``verify`` re-reads the ledger tail after fsync and fails loudly when the
    terminal event did not land. Write failures stay non-blocking for the DA
    chain (legacy exit codes unchanged) but are visible on stdout+stderr and
    preserved in the fallback ledger.
    """
    try:
        payload = _encode_event(event)
    except Exception as exc:  # noqa: BLE001 - payload must not kill the batch
        _report_ledger_failure(event, exc)
        return False
    key = str(EVENT_PATH)
    with _LEDGER_WRITE_LOCK:
        vanished = _LEDGER_WRITES.get(key, 0) > 0 and not EVENT_PATH.exists()
        try:
            if vanished:
                # The ledger existed earlier in this run and is now gone:
                # record that fact in-band, then keep appending (self-heal).
                _append_bytes(EVENT_PATH, _encode_event({
                    "type": "da_main_schedule",
                    "status": "ledger_missing_recreated",
                    "date": event.get("date"),
                    "pid": os.getpid(),
                    "path": str(EVENT_PATH),
                    "at": now_et().isoformat(),
                    "note": "ledger vanished after this carrier appended to it; "
                            "recreated without dropping prior-process history",
                }))
                print(f"[DA-main] schedule ledger vanished mid-run; recreated: "
                      f"{EVENT_PATH}", file=sys.stderr)
            _append_bytes(EVENT_PATH, payload)
            _LEDGER_WRITES[key] = _LEDGER_WRITES.get(key, 0) + 1
        except Exception as exc:  # noqa: BLE001 - non-blocking telemetry
            _report_ledger_failure(event, exc)
            return False
        if verify and not _persisted(EVENT_PATH, payload):
            _report_ledger_failure(
                event, OSError("event missing from ledger tail after fsync"))
            return False
        return True


def _checkpoint_count(date: str) -> int:
    try:
        root = REPO / "data" / "da_checkpoint" / date
        return sum(1 for p in root.glob("*.json")
                   if not p.name.endswith((".raw.json", ".bak", ".tmp")))
    except Exception:
        return 0


_HEARTBEAT_STOP: threading.Event | None = None
_HEARTBEAT_THREAD: threading.Thread | None = None


def _stop_heartbeat(timeout: float = 2.0) -> None:
    """Stop and join the heartbeat so a terminal event stays the last line."""
    stop = _HEARTBEAT_STOP
    thread = _HEARTBEAT_THREAD
    if stop is not None:
        stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


def _heartbeat(date: str, base_event: dict, stop: threading.Event) -> None:
    """[DEB-20261027 N5-B] 心跳追加阶段可见性 (record-only, 不改任何等待/超时行为):

    - `remaining_sec`: 距 deadline 的剩余秒数 (可判「还剩多少预算」);
    - `first_checkpoint`: 首个完整 analysis 的轮询观测时刻，raw 采集材料不计入；
      它是 heartbeat 观测时间，不是文件精确落盘时间；
    - `deadline_approaching`: 剩余 <600s 且仍未完成时发一次，终态批次完整性另见
      `batch_observation`，不以 artifacts.ok 冒充完整。
    """
    seen_first = False
    warned = False
    while not stop.wait(max(5, HEARTBEAT_SEC)):
        done = _checkpoint_count(date)
        stamp = now_et()
        remaining = int(_deadline_seconds(stamp))
        append_event({**base_event, "status": "heartbeat", "analyses_done": done,
                      "remaining_sec": remaining, "at": stamp.isoformat()})
        if done > 0 and not seen_first:
            seen_first = True
            append_event({**base_event, "status": "first_checkpoint",
                          "analyses_done": done, "remaining_sec": remaining,
                          "at": stamp.isoformat()})
        if not warned and remaining <= 600:
            warned = True
            append_event({**base_event, "status": "deadline_approaching",
                          "analyses_done": done, "remaining_sec": remaining,
                          "at": stamp.isoformat()})


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


def _batch_observation(event: dict, result=None) -> dict:
    """Record-only coverage; same-day artifacts alone never prove this run complete.

    A parsed result supplies this run's selected/analysed symbols (including
    validated resume hits). Without it, fresh files only prove progress, not
    membership or completeness. Raw inputs are not publishable verdicts.
    """
    started = datetime.fromisoformat(event["started_at"]).timestamp()
    date = event["date"]

    def symbol(value):
        return str(value or "").removeprefix("US.")

    fresh = {"raw": set(), "analysis": set()}
    unreadable = []
    for path in (BRIDGE_DIR / "da_checkpoint" / date).glob("*.json"):
        if path.stat().st_mtime < started:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(row, dict):
                raise ValueError("checkpoint is not an object")
            key = "raw" if path.name.endswith(".raw.json") else "analysis"
            if key == "analysis" and row.get("rating") == "错误":
                continue
            name = symbol(row.get("symbol"))
            if name:
                fresh[key].add(name)
        except (OSError, ValueError):
            unreadable.append(path.name)

    stages = (result or {}).get("stages") or {}
    selection = stages.get("filter") or {}
    expected = selection.get("selected_symbols")
    expected = ({symbol(s) for s in expected}
                if isinstance(expected, list)
                and all(isinstance(s, str) and symbol(s) for s in expected)
                and len({symbol(s) for s in expected}) == len(expected) else None)
    rows = (stages.get("output") or {}).get("analyses")
    output_known = isinstance(rows, list)
    rows = rows if output_known else []
    analysed = {symbol(a.get("symbol")) for a in rows
                if isinstance(a, dict) and a.get("rating") != "错误"
                and a.get("action") in ("BUY", "SELL_SHORT", "SKIP")
                and symbol(a.get("symbol"))}
    gate_unknown = {symbol(a.get("symbol")) for a in rows
                    if isinstance(a, dict) and symbol(a.get("symbol"))
                    and a.get("rating") != "错误"
                    and a.get("action") in ("BUY", "SELL_SHORT")
                    and (a.get("_signal_gate") or {}).get("decision")
                    not in ("PASS", "WARN", "BLOCK")}
    publishable = {symbol(a.get("symbol")) for a in rows
                   if isinstance(a, dict) and symbol(a.get("symbol"))
                   and a.get("rating") != "错误"
                   and a.get("action") in ("BUY", "SELL_SHORT")
                   and (a.get("_signal_gate") or {}).get("decision") in ("PASS", "WARN")}

    bridge = BRIDGE_DIR / f"opportunity_scan_{date}.json"
    bridge_fresh = False
    bridge_symbols = set()
    bridge_error = None
    try:
        if bridge.exists():
            data = json.loads(bridge.read_text(encoding="utf-8"))
            bridge_fresh = (bridge.stat().st_mtime >= started
                            and data.get("trade_date") == date
                            and isinstance(data.get("candidates"), list))
            bridge_symbols = {symbol(c.get("symbol")) for c in data.get("candidates", [])
                              if isinstance(c, dict) and symbol(c.get("symbol"))}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        bridge_error = type(exc).__name__

    missing = sorted(expected - analysed) if expected is not None else None
    unexpected = sorted(analysed - expected) if expected is not None else None
    failed_statuses = {"deadline_missed", "pipeline_timeout_finalized",
                       "pipeline_spawn_failed", "pipeline_failed", "pipeline_json_parse_failed"}
    signal = stages.get("signal") or {}
    signal_failed = (signal.get("error") or
                     str(signal.get("market_condition") or "").startswith("扫描失败"))
    if signal_failed:
        state, reason = ("partial" if analysed else "failed"), "upstream_signal_failed"
    elif expected is None:
        state = ("partial" if fresh["raw"] or fresh["analysis"] else
                 "failed" if event.get("status") in failed_statuses else "unknown")
        reason = "current_selection_unavailable"
    elif not output_known:
        state, reason = "unknown", "analysis_output_unavailable"
    elif missing or unexpected or len(rows) != len(analysed):
        state = "partial" if analysed or fresh["raw"] or fresh["analysis"] else "failed"
        reason = "analysis_coverage_mismatch"
    elif gate_unknown:
        state, reason = "unknown", "gate_decision_unknown"
    elif not bridge_fresh or bridge_symbols != publishable:
        state, reason = "partial", "current_bridge_unverified"
    else:
        state = "complete"
        reason = "complete_zero_candidates" if not publishable else "complete"
    return {
        "state": state, "reason": reason,
        "batch_id": selection.get("screen_batch_id"),
        "selection_policy_version": selection.get("screen_policy_version"),
        "analysis_checkpoint_policies": sorted({
            str(a.get("_ckpt_policy")) for a in rows
            if isinstance(a, dict) and a.get("_ckpt_policy")}),
        "selected_count": len(expected) if expected is not None else None,
        "analysed_count": len(analysed) if expected is not None else None,
        "publishable_count": len(publishable) if expected is not None else None,
        "missing_symbols": missing, "unexpected_symbols": unexpected,
        "gate_unknown_symbols": sorted(gate_unknown),
        "fresh_raw_count": len(fresh["raw"]),
        "fresh_analysis_count": len(fresh["analysis"]),
        "file_identity_basis": "mtime_since_carrier_start_only",
        "unreadable_checkpoints": unreadable,
        "bridge_fresh": bridge_fresh, "bridge_error": bridge_error,
        "legal_zero_candidates": state == "complete" and not publishable,
        "legacy_status": event.get("status"),
    }


def _append_terminal_event(event: dict, result=None) -> None:
    """Observation failure must not change legacy status, artifact ok, or exit code.

    The heartbeat is stopped first so the terminal line is the ledger's last
    line, and the write uses ``verify=True`` so a lost final event is reported
    loudly instead of leaving the last visible state as ``running``/heartbeat.
    """
    _stop_heartbeat()
    try:
        observation = _batch_observation(event, result)
    except Exception as exc:
        observation = {"state": "unknown", "reason": "observation_error",
                       "error": type(exc).__name__}
    append_event({**event, "batch_observation": observation}, verify=True)


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
    global _HEARTBEAT_STOP, _HEARTBEAT_THREAD
    _HEARTBEAT_STOP, _HEARTBEAT_THREAD = None, None
    stamp = now_et()
    date = stamp.strftime("%Y-%m-%d")
    minute = stamp.hour * 100 + stamp.minute
    base_event = {"type": "da_main_schedule", "date": date,
                  "started_at": stamp.isoformat(),
                  "main_pool_top_n": MAIN_POOL_TOP_N,
                  "main_pool_budget": MAIN_POOL_TOP_N + 2}
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
            _HEARTBEAT_STOP, _HEARTBEAT_THREAD = heartbeat_stop, heartbeat

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
            _append_terminal_event({**base_event, "status": "deadline_missed",
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
                         "--mode", "full", "--top", str(MAIN_POOL_TOP_N),
                         "--symbols", symbol_arg, "--format", "json"],
                        pipeline_timeout),
                    cwd=SKILL_SCRIPTS, text=True, stdout=subprocess.PIPE,
                    stderr=slog, timeout=pipeline_timeout + 30, check=False)
        except subprocess.TimeoutExpired:
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            _append_terminal_event({**base_event, "status": "pipeline_timeout_finalized",
                          "finished_at": now_et().isoformat(),
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline timeout; finalize rc="
                  f"{finalized.get('returncode')} artifacts={artifacts}")
            return 0 if artifacts.get("ok") else 1
        except OSError as exc:
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            _append_terminal_event({**base_event, "status": "pipeline_spawn_failed",
                          "error": str(exc)[:300],
                          "finalize_rc": finalized.get("returncode"),
                          "artifacts": artifacts})
            return 0 if artifacts.get("ok") else 1
        finally:
            _stop_heartbeat()

        if completed.stdout:
            try:
                output_path.write_text(completed.stdout, encoding="utf-8")
            except OSError as exc:
                print(f"[DA-main] output snapshot write failed: {exc}")

        finished_at = now_et().isoformat()
        if completed.returncode in (124, 137, 143):
            finalized = _finalize(date)
            artifacts = _verify_artifacts(date)
            _append_terminal_event({**base_event, "status": "pipeline_timeout_finalized",
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
            _append_terminal_event({**base_event, "status": "pipeline_failed",
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
                _append_terminal_event({**base_event, "status": "complete_unparsed_stdout",
                              "finished_at": finished_at,
                              "artifacts": artifacts})
                print(f"[DA-main] {date} stdout unparsed but artifacts ok: "
                      f"{artifacts}")
                return 0
            _append_terminal_event({**base_event, "status": "pipeline_json_parse_failed",
                          "finished_at": finished_at,
                          "artifacts": artifacts})
            print(f"[DA-main] {date} pipeline JSON parse failed; artifacts={artifacts}")
            print((completed.stdout or "")[-2000:])
            return 1

        if result.get("skipped"):
            _append_terminal_event({**base_event, "status": "pipeline_skipped",
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
        _append_terminal_event({**base_event, "status": "complete",
                                "candidates_total": stages.get("signal", {}).get(
                                    "candidates_total", 0),
                                "analyses_count": len(analyses),
                                "artifacts": artifacts,
                                "finished_at": finished_at}, result=result)
        return 0
    finally:
        _stop_heartbeat()
        _release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
