#!/usr/bin/env python3
"""Post-deployment readiness snapshot for the MOM-ETF-V1 SIM plumbing pilot."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CODE_FILES = [
    "app/playbook/pilot_policy.py",
    "app/playbook/pipeline_parts/auto_execute_flow.py",
    "app/playbook/pipeline_parts/exit_monitor_flow.py",
    "app/playbook/pipeline_parts/overnight_cleanup.py",
    "app/playbook/playbook_service.py",
    "app/review/source_attribution.py",
    "scripts/mom_etf_v1_generate.py",
    "scripts/settle_entry_confirm_forward.py",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mom_cron_et_aware(cron_text: str) -> bool:
    """MOM 行必须以 ET 09:35 运行，并在其后恢复主机 CST，避免污染其他任务。"""
    if "mom_etf_v1_generate.py" not in cron_text:
        return False
    lines = cron_text.splitlines()
    mom_index = next(i for i, line in enumerate(lines) if "mom_etf_v1_generate.py" in line)
    before = lines[max(0, mom_index - 2):mom_index]
    after = lines[mom_index + 1:mom_index + 3]
    return (
        "CRON_TZ=America/New_York" in before
        and any(line.startswith("35 9 * * 1-5 ") for line in lines[mom_index:mom_index + 1])
        and "CRON_TZ=Asia/Shanghai" in after
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    from app.core.config_container import get_config
    from app.core.paths import get_trade_records_db_for_arm
    from app.playbook.db import route_playbook_db

    config = get_config()
    entries_enabled = bool(config.get_dotted("playbook.mom_etf_v1_entries_enabled", False))
    overnight_hold_enabled = bool(config.get_dotted(
        "playbook.execution_parameters.overnight_hold_enabled", False))
    pid = subprocess.check_output(
        ["systemctl", "show", "ftseek-us", "-p", "MainPID", "--value"], text=True).strip()
    active_enter = subprocess.check_output(
        ["systemctl", "show", "ftseek-us", "-p", "ActiveEnterTimestamp", "--value"], text=True).strip()
    cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    cron_mounted = "mom_etf_v1_generate.py" in cron
    cron_et_aware = _mom_cron_et_aware(cron)

    trade_path = get_trade_records_db_for_arm("sim")
    trade_conn = sqlite3.connect(f"file:{trade_path}?mode=ro", uri=True)
    mom_trades = trade_conn.execute(
        "SELECT COUNT(*) FROM trade_records WHERE decision_id LIKE 'MOM-ETF-V1-%'").fetchone()[0]
    mom_open = trade_conn.execute(
        "SELECT COUNT(*) FROM trade_records WHERE decision_id LIKE 'MOM-ETF-V1-%' "
        "AND (exit_time IS NULL OR exit_time='')").fetchone()[0]
    trade_conn.close()
    playbook_conn = route_playbook_db("sim")._conn()
    mom_instructions = playbook_conn.execute(
        "SELECT COUNT(*) FROM playbook_instructions WHERE decision_id LIKE 'MOM-ETF-V1-%'").fetchone()[0]

    tests = None
    if args.run_tests:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             "tests/playbook/test_pilot_policy.py", "tests/playbook/test_mom_etf_v1_flow.py"],
            cwd=REPO, capture_output=True, text=True)
        tests = {"returncode": completed.returncode,
                 "tail": (completed.stdout or completed.stderr).splitlines()[-3:]}

    payload = {
        "generated_for": "MOM-ETF-V1 SIM plumbing pilot",
        "code_sha256": {name: _sha256(REPO / name) for name in CODE_FILES},
        "mom_etf_v1_entries_enabled": entries_enabled,
        "overnight_hold_enabled": overnight_hold_enabled,
        "service_main_pid": pid,
        "service_active_enter": active_enter,
        "writer_cron_mounted": cron_mounted,
        "writer_cron_et_aware": cron_et_aware,
        "mom_instructions": mom_instructions,
        "mom_trades": mom_trades,
        "mom_open_positions": mom_open,
        "focused_tests": tests,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    if tests is not None and tests["returncode"] != 0:
        return 1
    if cron_mounted and not cron_et_aware:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
