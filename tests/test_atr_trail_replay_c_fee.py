"""ATR 回放费用 NULL 不得提前洗成零费用的契约。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from scripts import atr_trail_replay_c as atr


def test_load_metadata_preserves_null_fee_and_real_zero(tmp_path, monkeypatch):
    db_path = tmp_path / "fees.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE trade_records (id INTEGER, fees_est REAL, data_quality TEXT, metadata_json TEXT)")
    con.execute("INSERT INTO trade_records VALUES (1, NULL, 'clean', '{}')")
    con.execute("INSERT INTO trade_records VALUES (2, 0.0, 'clean', '{}')")
    con.commit()
    con.close()
    monkeypatch.setattr(atr.eng, "DB_PATH", db_path)

    rows = atr._load_metadata([1, 2])
    assert rows[1]["fees_est"] is None
    assert rows[2]["fees_est"] == 0.0  # 真实零费用与 NULL 必须可区分


def test_main_skips_fee_unknown_instead_of_replaying_with_zero(tmp_path, monkeypatch):
    et = atr.ET
    trade = {
        "id": 1, "symbol": "US.TEST", "direction": "LONG",
        "entry_et": et.localize(datetime(2026, 10, 20, 9, 50)),
        "exit_et": et.localize(datetime(2026, 10, 20, 16, 0)),
        "entry": 100.0, "live_exit": 100.4, "qty": 10.0,
        "live_pnl": 4.0, "live_gross": 4.0, "live_net": None,
        "live_fee_est": None, "fees_source": None,
        "live_reason": "SL_INITIAL", "live_mfe": 1.0,
        "conditions": [],
    }
    monkeypatch.setattr(atr, "_base_adaptive_from_config", lambda: {"adaptive_trailing_enabled": True})
    monkeypatch.setattr(atr.eng, "_load_configs", lambda: ({}, {}))
    monkeypatch.setattr(atr.eng, "load_trades", lambda since, until: [trade])
    monkeypatch.setattr(atr, "_load_metadata", lambda ids: {
        1: {"fees_est": None, "data_quality": "clean", "metadata_json": "{}"}
    })
    monkeypatch.setattr(atr, "_load_cached_bars", lambda *args: ([], None))
    monkeypatch.setattr(atr, "atr_fraction_from_metadata", lambda _meta: 0.01)
    monkeypatch.setattr(atr, "is_modelable_live_reason", lambda _reason: True)
    monkeypatch.setattr(atr, "bucket_from_metadata", lambda _meta: "test")
    monkeypatch.setattr(atr.eng, "_get_factor_exit_config", lambda *args, **kwargs: {})
    monkeypatch.setattr("sys.argv", ["atr_trail_replay_c.py", "--out-dir", str(tmp_path)])
    captured = {}

    def capture(report, _rows, _out_dir):
        captured.update(report)

    monkeypatch.setattr(atr, "_write_report", capture)
    assert atr.main() == 2
    assert captured["sample"]["prepared_n"] == 0
    assert captured["sample"]["excluded"]["fee_unknown"] == 1
    assert captured["verdicts"] == {"C": "INSUFFICIENT_N"}
