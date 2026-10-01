import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "fix_playbook_user_feedback_orphans.py"
)
SPEC = importlib.util.spec_from_file_location(
    "fix_playbook_user_feedback_orphans", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _create_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT)"
    )
    connection.execute(
        "CREATE TABLE playbook_user_feedback ("
        "id INTEGER PRIMARY KEY, instruction_id INTEGER NOT NULL, action TEXT, "
        "skip_reason TEXT, modified_field TEXT, original_value TEXT, "
        "modified_value TEXT, created_at TEXT)"
    )
    connection.execute(
        "INSERT INTO playbook_instructions VALUES (1, '2026-09-30', 'US.TEST', 'BUY')"
    )
    connection.executemany(
        "INSERT INTO playbook_user_feedback "
        "(instruction_id, action, created_at) VALUES (?, ?, ?)",
        [(1, "executed", "now"), (999, "skipped", "old")],
    )
    connection.commit()
    connection.close()


def test_find_orphans_reports_only_missing_instruction(tmp_path):
    db_path = tmp_path / "test.db"
    _create_db(db_path)

    rows = MODULE.find_orphans(db_path)

    assert len(rows) == 1
    assert rows[0]["instruction_id"] == 999


def test_apply_archives_and_deletes_orphans(tmp_path):
    db_path = tmp_path / "test.db"
    audit_path = tmp_path / "audit.jsonl"
    _create_db(db_path)
    rows = MODULE.find_orphans(db_path)

    MODULE.apply_repair(db_path, rows, audit_path)

    assert MODULE.find_orphans(db_path) == []
    assert MODULE.foreign_key_violations(db_path) == []
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT count(*) FROM playbook_user_feedback"
    ).fetchone()[0] == 1
    connection.close()
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    assert '"instruction_id": 999' in audit_lines[0]
