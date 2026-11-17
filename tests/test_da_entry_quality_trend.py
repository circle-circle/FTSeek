import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "da_entry_quality_trend.py"
SPEC = importlib.util.spec_from_file_location("da_entry_quality_trend", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _row(day, net, mfe):
    return {"trade_date": day, "entry_price": 100.0, "quantity": 10,
            "pnl_net": net, "realized_pnl": net, "mfe_pct": mfe}


def test_net_fallback_subtracts_fees_from_gross_realized_pnl():
    row = _row("2026-11-10", -6.0, 1.0)
    row["pnl_net"] = None
    row["fees_est"] = 2.0
    assert MODULE._net(row) == pytest.approx(-8.0)


def test_breakeven_formula():
    assert MODULE.breakeven_dead_rate(-100.0, 50.0) == pytest.approx(50.0 / 150.0)
    assert MODULE.breakeven_dead_rate(-147.38, 35.62) == pytest.approx(35.62 / (35.62 + 147.38))


def test_breakeven_none_when_live_nonpositive():
    assert MODULE.breakeven_dead_rate(-100.0, 0.0) is None
    assert MODULE.breakeven_dead_rate(-100.0, -5.0) is None


def test_analyze_dead_rate_and_below_breakeven():
    rows = [_row(f"2026-10-{i + 1:02d}", 30.0, 1.0) for i in range(8)]
    rows += [_row(f"2026-10-{i + 9:02d}", -10.0, 0.1) for i in range(2)]
    report = MODULE.analyze(rows)
    assert report["n"] == 10
    assert report["dead_n"] == 2
    assert report["dead_rate_pct"] == pytest.approx(20.0)
    assert report["live_avg_bps"] == pytest.approx(300.0)
    assert report["dead_avg_bps"] == pytest.approx(-100.0)
    assert report["breakeven_dead_rate_pct"] == pytest.approx(75.0)
    assert report["below_breakeven"] is True


def test_analyze_stratifies_recovery_artifacts_out_of_dead_rate():
    def sig(day, net, mfe):
        return {"trade_date": day, "entry_price": 100.0, "quantity": 10,
                "pnl_net": net, "realized_pnl": net, "mfe_pct": mfe,
                "entry_reason": "playbook_futu_simulate"}
    rows = [sig(f"2026-11-{i + 1:02d}", 30.0, 1.0) for i in range(8)]
    rows += [sig(f"2026-11-{i + 9:02d}", -10.0, 0.1) for i in range(2)]
    rows += [{"trade_date": f"2026-11-{i + 11:02d}", "entry_price": 100.0, "quantity": 10,
              "pnl_net": -20.0, "realized_pnl": -20.0, "mfe_pct": 0.0,
              "entry_reason": "sync_recovery_backfill"} for i in range(5)]
    report = MODULE.analyze(rows)
    assert report["n"] == 10
    assert report["dead_n"] == 2
    assert report["dead_rate_pct"] == pytest.approx(20.0)
    assert report["n_all"] == 15
    assert report["dead_n_all"] == 7
    assert report["dead_rate_all_pct"] == pytest.approx(46.7, abs=0.1)
    assert report["artifact"]["n"] == 5
    assert report["artifact"]["dead_n"] == 5
    assert report["artifact"]["net_usd"] == pytest.approx(-100.0)
