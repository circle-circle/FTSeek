import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pnl_expectancy_pivot_analysis.py"
SPEC = importlib.util.spec_from_file_location("pnl_expectancy_pivot_analysis", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _row(day, order, net, fees=0.0, mfe=1.0, price=100.0, qty=10):
    return {
        "trade_date": day,
        "entry_time": f"{day}T09:{order:02d}:00-05:00",
        "pnl_net": net,
        "realized_pnl": net,
        "fees_est": fees,
        "mfe_pct": mfe,
        "entry_price": price,
        "quantity": qty,
    }


def test_bps_unit_is_notional_weighted():
    row = _row("2026-11-10", 30, net=10.0)
    assert MODULE._bps(row) == pytest.approx(100.0)


def test_decompose_gross_fee_net_and_dead_rate():
    rows = [
        _row("2026-11-10", 30, net=10.0, fees=2.0, mfe=1.0),
        _row("2026-11-10", 40, net=-6.0, fees=2.0, mfe=0.1),
    ]
    dec = MODULE.decompose(rows)
    assert dec["n"] == 2
    assert dec["gross_usd"] == pytest.approx(8.0)
    assert dec["fees_usd"] == pytest.approx(4.0)
    assert dec["net_usd"] == pytest.approx(4.0)
    assert dec["dead_n"] == 1
    assert dec["dead_rate_pct"] == pytest.approx(50.0)
    assert dec["dead_net_usd"] == pytest.approx(-6.0)
    assert dec["live_net_usd"] == pytest.approx(10.0)


def test_frequency_cap_takes_first_n_in_execution_order():
    rows = [
        _row("2026-11-10", 30, net=10.0),
        _row("2026-11-10", 40, net=-10.0),
        _row("2026-11-10", 50, net=-10.0),
        _row("2026-11-11", 30, net=10.0),
    ]
    fc = MODULE.frequency_cap(rows, max_cap=2)
    cap1 = fc["caps"][0]
    assert cap1["cap"] == 1
    assert cap1["kept_trades"] == 2
    assert cap1["delta_day_mean_bps"] == pytest.approx(66.67, abs=0.01)
    assert cap1["cap_day_mean_usd"] == pytest.approx(10.0)


def test_clustered_t_matches_formula():
    mean, sd, days, t = MODULE._clustered([10.0, 20.0, 30.0])
    assert mean == pytest.approx(20.0)
    assert days == 3
    assert sd == pytest.approx(10.0)
    assert t == pytest.approx(20.0 / (10.0 / 3 ** 0.5))


def test_build_report_stratifies_signal_and_preserves_all_total():
    rows = [
        {**_row("2026-11-10", 30, net=10.0, fees=2.0, mfe=1.0), "entry_reason": "playbook_futu_simulate"},
        {**_row("2026-11-10", 40, net=-6.0, fees=2.0, mfe=0.1), "entry_reason": "playbook_futu_simulate"},
        {**_row("2026-11-10", 50, net=-20.0, fees=2.0, mfe=0.0), "entry_reason": "sync_recovery_backfill"},
    ]
    rep = MODULE.build_report(rows, max_cap=2)
    assert rep["decomposition"]["n"] == 2
    assert rep["decomposition"]["net_usd"] == pytest.approx(4.0)
    assert rep["decomposition_all"]["n"] == 3
    assert rep["decomposition_all"]["net_usd"] == pytest.approx(-16.0)
    assert rep["artifact"]["n"] == 1
    assert rep["artifact"]["net_usd"] == pytest.approx(-20.0)
    assert rep["n_artifact_excluded"] == 1
    assert rep["frequency_cap"]["uncapped"]["days"] == 1
