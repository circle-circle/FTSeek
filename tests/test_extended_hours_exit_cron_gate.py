"""ET 窗口闸: 工作日 04:00-20:05 每分钟, 其余 5 分钟 (2026-10-18)."""
import os
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "extended_hours_exit_cron_gate.sh"


def _run(now, dow):
    env = {
        **os.environ,
        "FTSEEK_ET_NOW_OVERRIDE": now,
        "FTSEEK_ET_DOW_OVERRIDE": str(dow),
        "FTSEEK_GATE_DRYRUN": "1",
    }
    proc = subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_weekday_intraday_runs_every_minute():
    assert _run("1000", 1) == "RUN"
    assert _run("2005", 5) == "RUN"


def test_weekday_edge_minutes_are_gated_outside_window():
    assert _run("0400", 1) == "RUN"
    assert _run("2006", 1) == "SKIP"
    assert _run("2105", 1) == "RUN"
    assert _run("2101", 1) == "SKIP"


def test_weekend_is_five_minute_cadence():
    assert _run("1001", 6) == "SKIP"
    assert _run("1005", 6) == "RUN"
