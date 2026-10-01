import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "fix_executed_skip_reason_residue.py"
)
SPEC = importlib.util.spec_from_file_location("fix_executed_skip_reason_residue", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _create_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
        "executed INTEGER, skip_reason TEXT, execution_price REAL, outcome TEXT, "
        "updated_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO playbook_instructions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-09-08", "US.SNDK", "BUY", 1, "chase_cap_enforced", 1752.665, "win", "old"),
            (2, "2026-09-09", "US.TEST", "BUY", 0, "not_triggered", None, None, "old"),
            (3, "2026-09-10", "US.CLEAN", "SELL_SHORT", 1, "", 100.0, "win", "old"),
        ],
    )
    connection.commit()
    connection.close()


def _residue(db_path: Path) -> list:
    connection = sqlite3.connect(db_path)
    rows = connection.execute(
        "SELECT id, skip_reason, updated_at FROM playbook_instructions ORDER BY id"
    ).fetchall()
    connection.close()
    return rows


def test_find_residue_reports_only_executed_with_skip_reason(tmp_path):
    db_path = tmp_path / "test.db"
    _create_db(db_path)

    rows = MODULE.find_residue(db_path)

    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["skip_reason"] == "chase_cap_enforced"


def test_apply_clears_residue_and_writes_audit(tmp_path):
    db_path = tmp_path / "test.db"
    audit_path = tmp_path / "audit.jsonl"
    _create_db(db_path)
    rows = MODULE.find_residue(db_path)

    MODULE.apply_repair(db_path, rows, audit_path)

    residue = _residue(db_path)
    assert residue[0][1] == ""
    assert residue[0][2] != "old"
    assert residue[1][1] == "not_triggered"
    assert residue[2][1] == ""
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
    assert '"skip_reason": "chase_cap_enforced"' in audit_lines[0]
