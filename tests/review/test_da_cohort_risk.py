import json

from app.review import bundle_builder
from app.review.bundle_builder import build, build_da_cohort_risk
from app.review.bundle_render import render_md
from tests.review.test_bundle_builder import _create_db


def _insert(connection, symbol, direction, decision_id, exit_time, net, metadata, entry=100.0, quantity=10):
    connection.execute(
        "insert into trade_records(trade_date,symbol,direction,entry_time,exit_time,entry_session,"
        "exit_reason,source_type,entry_price,quantity,realized_pnl,pnl_net,fees_est,decision_id,metadata_json)"
        " values ('2026-11-09',?,?,'2026-11-09T10:00:00-05:00',?,'regular',"
        " 'TEST','PLAYBOOK_SIM',?,?,?,?,0.0,?,?)",
        (symbol, direction, exit_time, entry, quantity, net, net, decision_id, json.dumps(metadata)),
    )


def _cache(tmp_path):
    path = tmp_path / "daily.json"
    path.write_text(json.dumps({"symbols": {
        "US.LONG": {"bars": [{"time": "2026-11-09", "close": 102.0}]},
        "US.SHORT": {"bars": [{"time": "2026-11-09", "close": 51.0}]},
    }}), encoding="utf-8")
    return path


def test_da_cohort_risk_separates_realized_net_and_open_mtm(tmp_path, monkeypatch):
    connection = _create_db()
    _insert(connection, "US.CLOSED", "LONG", "DA-CLOSED", "2026-11-09T11:00:00-05:00", 10.0, {})
    _insert(connection, "US.LONG", "LONG", "DA-LONG", None, None, {"_effective_sl_observed": 95.0})
    _insert(connection, "US.SHORT", "SHORT", "DA-SHORT", "", None, {"stop_loss": 55.0}, entry=50.0, quantity=5)
    cache = _cache(tmp_path)

    out = build_da_cohort_risk(connection, "2026-11-09", cache)

    assert out["realized_trade_count"].value == 1
    assert out["realized_net_pnl"].value == 10.0
    assert out["realized_gross_pnl"].value == 10.0
    assert out["realized_fees"].value == 0.0
    assert out["realized_fee_to_gross"].value == 0.0
    assert out["open_trade_count"].value == 2
    assert out["open_gross_mtm"].value == 15.0
    assert out["open_mtm_complete"].value is True
    assert out["positions"][0]["effective_stop"] == 95.0
    assert out["positions"][1]["effective_stop"] == 55.0

    monkeypatch.setattr(bundle_builder, "_DAILY_KLINE_CACHE", cache)
    full_md, _ = render_md(build(connection, "2026-11-09"))
    assert "新 DA cohort 常规收盘风险（非 A9）" in full_md
    assert "毛 MTM +$15.00" in full_md
    assert "US.LONG" in full_md


def test_da_cohort_risk_keeps_total_unknown_when_exact_close_is_missing(tmp_path):
    connection = _create_db()
    _insert(connection, "US.MISSING", "LONG", "DA-MISSING", None, None, {"stop_loss": 95.0})

    out = build_da_cohort_risk(connection, "2026-11-09", tmp_path / "missing.json")

    assert out["open_trade_count"].value == 1
    assert out["open_gross_mtm"].value is None
    assert out["open_mtm_complete"].value is False
    assert out["open_mtm_missing_symbols"].value == ["US.MISSING"]
