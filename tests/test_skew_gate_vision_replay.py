import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_skew_gate_vision.py"
SPEC = importlib.util.spec_from_file_location("replay_skew_gate_vision", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _create_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, "
        "confidence REAL, skip_reason TEXT, entry_price REAL, position_size REAL)"
    )
    rows = []
    for index in range(4):
        rows.append((index + 1, "2026-09-02", f"US.B{index}", "BUY", 70, "", 100, 1))
    for index in range(4):
        rows.append((
            index + 5, "2026-09-02", f"US.S{index}", "SELL_SHORT", 50,
            "TRIPWIRE_INVALIDATED", 100, 1,
        ))
    for index in range(5):
        rows.append((
            index + 9, "2026-09-02", f"US.C{index}", "SKIP", 70,
            "方向偏斜超限 guardrail 降级 (多数方 BUY, 原size=1)", 100, 1,
        ))
    connection.executemany(
        "INSERT INTO playbook_instructions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()


def _create_bars(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for symbol in [f"US.C{index}" for index in range(5)]:
        (path / f"{symbol.replace('.', '_')}.json").write_text(
            json.dumps([["2026-09-02", 100, 110, 90, 110]]),
            encoding="utf-8",
        )


def test_terminal_filter_changes_rescue_count(tmp_path):
    db_path = tmp_path / "test.db"
    bars_dir = tmp_path / "bars"
    _create_db(db_path)
    _create_bars(bars_dir)

    unfiltered = MODULE.replay(
        db_path, bars_dir, filter_terminal=False, order="id"
    )
    filtered = MODULE.replay(
        db_path, bars_dir, filter_terminal=True, order="id"
    )

    assert unfiltered["rescued"] == 5
    assert unfiltered["rescued_gross_pnl"] == 50.0
    assert filtered["rescued"] == 0
    assert filtered["still_cut"] == 5
