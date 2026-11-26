"""退出回放毛/费/净口径与保真机门契约。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from scripts import exit_replay_backtest as erb


def _trade(*, fee_known=True):
    et = erb.ET
    gross = 4.0
    fee = 6.0 if fee_known else None
    return {
        "id": 1, "symbol": "US.TEST", "direction": "LONG",
        "entry_et": et.localize(datetime(2026, 10, 20, 9, 50)),
        "exit_et": et.localize(datetime(2026, 10, 20, 16, 0)),
        "entry": 100.0, "live_exit": 100.4, "qty": 10.0,
        "live_pnl": gross, "live_gross": gross,
        "live_net": gross - fee if fee is not None else None,
        "live_fee_est": fee, "fees_source": "est" if fee_known else None,
        "live_reason": "ACTUAL_EXIT", "live_mfe": 1.0,
        "config_version": "", "decision_id": "", "meta_config_version": "",
        "initial_sl": 95.0, "take_profit": 110.0, "conditions": [],
    }


def test_fee_of_uses_net_caliber_and_never_invents_zero_fee():
    tr = _trade()
    assert erb._fee_of(tr) == 6.0
    assert erb._fee_of(_trade(fee_known=False)) is None
    invalid = _trade()
    invalid["live_fee_est"] = 1000.0
    assert erb._fee_of(invalid) is None


def test_load_trades_separates_gross_net_and_fee(tmp_path, monkeypatch):
    db_path = tmp_path / "trades.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
            quantity REAL, realized_pnl REAL, pnl_net REAL, fees_est REAL,
            fees_source TEXT, exit_reason TEXT, mfe_pct REAL, mae_pct REAL,
            config_version TEXT, decision_id TEXT, metadata_json TEXT,
            source_type TEXT
        )
    """)
    row = (
        1, "US.TEST", "LONG", "2026-10-20T09:50:00-04:00",
        "2026-10-20T16:00:00-04:00", 100.0, 104.0, 1.0, 4.0, -2.0, 6.0,
        "est", "ACTUAL_EXIT", 1.0, 0.0, "", "DA-test", "{}", "PLAYBOOK_SIM",
    )
    conn.execute("INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (2, "US.OLD", "LONG", "2026-10-20T09:50:00-04:00",
         "2026-10-20T16:00:00-04:00", 100.0, 104.0, 1.0, 4.0, None, None,
         None, "ACTUAL_EXIT", 1.0, 0.0, "", "DA-old", "{}", "PLAYBOOK_SIM"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(erb, "DB_PATH", db_path)

    rows = erb.load_trades("2026-10-20", "2026-10-20")
    assert rows[0]["live_pnl"] == rows[0]["live_gross"] == 4.0  # 旧名仍装毛，防旧调用方改义
    assert rows[0]["live_net"] == -2.0
    assert rows[0]["live_fee_est"] == 6.0
    assert rows[0]["fees_source"] == "est"
    assert rows[1]["live_gross"] == 4.0
    assert rows[1]["live_net"] is None
    assert rows[1]["live_fee_est"] is None


def test_main_fidelity_failure_writes_diagnostics_without_variants(tmp_path, monkeypatch):
    tr = _trade()
    bars = []
    for i in range(1, 7):
        ts = tr["entry_et"] + timedelta(minutes=i)
        bars.append({"t": ts, "o": 100.0, "h": 105.0, "l": 99.0, "c": 104.0})
    monkeypatch.setattr(erb, "_load_configs", lambda: ({}, {}))
    monkeypatch.setattr(erb, "load_trades", lambda since, until: [tr])
    monkeypatch.setattr(
        erb, "collect_bars", lambda *args, **kwargs: {("US.TEST", "2026-10-20"): bars})
    monkeypatch.setattr(
        erb, "_get_factor_exit_config", lambda *args, **kwargs: {})
    calls = []

    def fake_replay(*args, **kwargs):
        calls.append(1)
        return {"exit_price": 104.0, "reason": "REPLAY_MISMATCH",
                "held_min": 10.0, "mfe_pct": 1.0}

    monkeypatch.setattr(erb, "replay_trade", fake_replay)
    out = tmp_path / "replay.json"
    monkeypatch.setattr(
        "sys.argv", ["exit_replay_backtest.py", "--since", "2026-10-20", "--out", str(out)])

    erb.main()
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["validation"]["reason_match_pct"] == 0.0
    assert report["validation"]["fidelity_ok"] is False
    assert report["frozen_by_fidelity"] is True
    assert report["variants"] == {}
    assert len(calls) == 1, "保真失败后不得继续跑变体网格"
    assert report["live_sum_gross"] == 4.0
    assert report["live_sum_fees"] == 6.0
    assert report["live_sum_net"] == -2.0
    assert report["fee_coverage"]["known_n"] == 1
    assert report["fee_coverage"]["unknown_n"] == 0


def test_main_all_fee_unknown_overwrites_stale_output_with_diagnostic(tmp_path, monkeypatch):
    tr = _trade(fee_known=False)
    bars = []
    for i in range(1, 7):
        ts = tr["entry_et"] + timedelta(minutes=i)
        bars.append({"t": ts, "o": 100.0, "h": 105.0, "l": 99.0, "c": 100.4})
    monkeypatch.setattr(erb, "_load_configs", lambda: ({}, {}))
    monkeypatch.setattr(erb, "load_trades", lambda since, until: [tr])
    monkeypatch.setattr(
        erb, "collect_bars", lambda *args, **kwargs: {("US.TEST", "2026-10-20"): bars})
    monkeypatch.setattr(erb, "_get_factor_exit_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        erb, "replay_trade", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("不可回放样本不得进入 replay_trade")))
    out = tmp_path / "replay.json"
    out.write_text('{"variants": {"stale": {"sum_net": 999}}}', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["exit_replay_backtest.py", "--since", "2026-10-20", "--out", str(out)])

    erb.main()
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "NO_REPLAYABLE_TRADES"
    assert report["trades_replayed"] == 0
    assert report["fee_coverage"]["unknown_n"] == 1
    assert report["exclusions"] == {"no_bars": 0, "fee_unknown": 1}
    assert report["frozen_by_fidelity"] is True
    assert report["variants"] == {}
    assert "stale" not in out.read_text(encoding="utf-8")


def test_main_zero_candidate_window_overwrites_stale_output_without_opend(tmp_path, monkeypatch):
    monkeypatch.setattr(erb, "_load_configs", lambda: ({}, {}))
    monkeypatch.setattr(erb, "load_trades", lambda since, until: [])
    monkeypatch.setattr(
        erb, "collect_bars", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("零候选窗口不得连接 OpenD")))
    out = tmp_path / "replay.json"
    out.write_text('{"variants": {"stale": {"sum_net": 999}}}', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv", ["exit_replay_backtest.py", "--since", "2026-10-20", "--out", str(out)])

    erb.main()
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "NO_CANDIDATE_TRADES"
    assert report["trades_total"] == report["trades_replayed"] == 0
    assert report["fee_coverage"] == {
        "known_n": 0, "unknown_n": 0, "sources": [],
        "policy": "费用未知即跳过，不静默当零；净值优先 pnl_net，缺失时毛利−fees_est",
    }
    assert report["frozen_by_fidelity"] is True
    assert report["variants"] == {}
    assert "stale" not in out.read_text(encoding="utf-8")


def test_be_counterfactual_compares_net_to_net():
    """模块级历史脚本不得把净反事实与毛实盘比较。"""
    source = (Path(__file__).resolve().parents[1] / "scripts" / "be_counterfactual_replay.py").read_text(
        encoding="utf-8")
    assert "live_net=tr['live_net']" in source
    assert "live_gross=tr['live_gross']" in source
    assert "r['nobe_net'] > r['live_net']" in source
    assert "r['nobe_net'] > r['live_pnl']" not in source
    assert "live_net':>9" in source
