import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edge_discovery_screener.py"
SPEC = importlib.util.spec_from_file_location("edge_discovery_screener", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _row(day, order, net):
    return {
        "trade_date": day, "entry_time": f"{day}T08:{order:02d}:00-05:00",
        "pnl_net": net, "realized_pnl": net, "fees_est": 0.0, "mfe_pct": 1.0,
        "entry_price": 100.0, "quantity": 10, "owner_strategy": "extended_hours_entry",
        "entry_session": "premarket", "market_regime": "UNKNOWN", "direction": "LONG",
        "exit_reason": "SL_TRAIL_LOCK", "metadata_json": "{}",
    }


def _days(count, start_day=1, month="2026-10"):
    return [f"{month}-{start_day + i:02d}" for i in range(count)]


def test_net_fallback_subtracts_fees_from_gross_realized_pnl():
    row = _row("2026-11-10", 30, 8.0)
    row["pnl_net"] = None
    row["fees_est"] = 2.0
    assert MODULE._net(row) == pytest.approx(6.0)


def test_robust_candidate_passes_all_gates():
    rows = []
    days = _days(7, 1, "2026-10") + _days(8, 1, "2026-11")
    for index, day in enumerate(days):
        for order in (30, 45):
            rows.append(_row(day, order, 10.0 + (index % 3) * 0.5))
    metrics = MODULE.slice_metrics(rows)
    assert metrics["n"] == 30
    assert metrics["day_t"] >= MODULE.T_HURDLE
    assert metrics["loo_min_t"] > 0
    assert metrics["top3_share_pct"] < 70
    assert metrics["months_same_sign"] is True
    assert metrics["robust_candidate"] is True


def test_concentrated_single_winner_not_robust():
    rows = []
    days = _days(7, 1, "2026-10") + _days(8, 1, "2026-11")
    for index, day in enumerate(days):
        rows.append(_row(day, 30, 100.0 if index == 0 else 0.5))
        rows.append(_row(day, 45, 0.5))
    metrics = MODULE.slice_metrics(rows)
    assert metrics["top3_share_pct"] > 70
    assert metrics["robust_candidate"] is False


def test_small_sample_not_robust():
    rows = [_row(f"2026-10-{i + 1:02d}", 30, 10.0) for i in range(10)]
    metrics = MODULE.slice_metrics(rows)
    assert metrics["n"] == 10
    assert metrics["robust_candidate"] is False


def test_build_report_excludes_recovery_artifacts():
    ext = _row("2026-11-10", 30, 10.0)
    artifact = {**_row("2026-11-10", 40, -20.0), "owner_strategy": "playbook_sim",
                "entry_reason": "sync_recovery_backfill", "market_regime": "RANGE"}
    rep = MODULE.build_report([ext, artifact])
    assert rep["n_signal"] == 1
    assert rep["n_artifact_excluded"] == 1
    owners = [r["value"] for r in rep["results"] if r["slice"] == "owner_strategy"]
    assert "playbook_sim" not in owners
    assert "extended_hours_entry" in owners
