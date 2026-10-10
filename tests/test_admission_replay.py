from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import admission_replay as A


def _create_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            trade_date TEXT,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            quantity INTEGER,
            realized_pnl REAL,
            pnl_net REAL,
            fees_est REAL,
            exit_reason TEXT,
            exit_time TEXT,
            source_type TEXT,
            decision_id TEXT
        );
        CREATE TABLE playbook_instructions (
            id INTEGER PRIMARY KEY,
            decision_id TEXT,
            take_profit REAL,
            confidence INTEGER,
            raw_instruction_json TEXT
        );
        INSERT INTO trade_records VALUES
            (1, '2026-10-01', 'US.AAA', 'LONG', 100, 10, 20, 18, 2,
             'SL_BREAKEVEN', '2026-10-01 15:00', 'PLAYBOOK_SIM', 'D1'),
            (2, '2026-10-01', 'US.BBB', 'LONG', 100, 10, 20, 18, 2,
             '', NULL, 'PLAYBOOK_SIM', 'D2'),
            (3, '2026-10-01', 'US.CCC', 'LONG', 100, 10, 20, 18, 2,
             'SL_BREAKEVEN', '2026-10-01 15:00', 'MAIN_PIPELINE', 'D3');
        INSERT INTO playbook_instructions VALUES
            (10, 'D1', 110, 80, '{"source":"deep_analysis"}'),
            (11, 'D1', 111, 81, '{"source":"deep_analysis_latest"}');
        """
    )
    con.commit()
    con.close()


def test_load_filters_sim_closed_and_dedupes_decision_join(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _create_db(db_path)
    monkeypatch.setattr(A, "DB", str(db_path))

    rows = A.load()

    assert len(rows) == 1
    assert rows[0]["src"] == "deep_analysis_latest"
    assert rows[0]["net"] == 18
    assert rows[0]["rt_fee"] > 0
    assert rows[0]["fee_bps"] > 0


def test_fee_bps_gate_blocks_only_when_ex_ante_fee_exceeds_threshold():
    row = {"fee_bps": 201.0}
    assert A.blocked(row, {"max_fee_bps": 200}) == "FEE_BPS"
    assert A.blocked({"fee_bps": 199.0}, {"max_fee_bps": 200}) is None
    assert A.blocked({"fee_bps": None}, {"max_fee_bps": 200}) is None


def test_script_contract_uses_read_only_and_net_ssot():
    source = Path(A.__file__).read_text(encoding="utf-8")
    assert 'sqlite3.connect(f"file:{DB}?mode=ro", uri=True)' in source
    assert "COALESCE(t.pnl_net, t.realized_pnl)" in source
    assert "from app.execution.fee_model import estimate_round_trip" in source


def test_window_uses_et_trade_date_not_sqlite_utc_now():
    source = Path(A.__file__).read_text(encoding="utf-8")
    assert "def _et_today()" in source
    assert "timedelta(days=int(window_days))" in source
    assert "date('now', '-{int(window_days)} day')" not in source
