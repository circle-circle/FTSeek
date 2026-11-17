import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "settle_da_frequency_cap_pivot.py"
SPEC = importlib.util.spec_from_file_location("settle_da_frequency_cap_pivot", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _row(day, order, net):
    return {
        "trade_date": day,
        "entry_time": f"{day}T09:{order:02d}:00-05:00",
        "pnl_net": net,
        "realized_pnl": net,
        "fees_est": 0.0,
        "mfe_pct": 1.0,
        "entry_price": 100.0,
        "quantity": 10,
    }


def test_preregistration_is_frozen():
    assert MODULE.COHORT_START == "2026-11-16"
    assert MODULE.CAPS == (2, 3)
    assert MODULE.MIN_DAYS == 30


def test_day_cluster_ci80_and_mde80():
    cluster = MODULE._day_cluster({"a": 10.0, "b": 20.0, "c": 30.0})
    assert cluster["n_days"] == 3
    assert cluster["mean_delta"] == pytest.approx(20.0)
    se = 10.0 / 3 ** 0.5
    assert cluster["ci80"][0] == pytest.approx(20.0 - MODULE.Z80 * se, abs=1e-3)
    assert cluster["mde80"] == pytest.approx((MODULE.Z80 + MODULE.Z_POWER80) * se, abs=1e-3)


def test_settle_too_early_below_30_days():
    rows = [_row("2026-11-16", 30, 10.0), _row("2026-11-16", 40, -5.0),
            _row("2026-11-17", 30, 10.0)]
    report = MODULE.settle(rows)
    assert report["verdict"] == "TOO_EARLY"
    assert report["uncapped"]["days"] == 2
    assert report["caps"][0]["kept_trades"] == 3


def test_settle_graduation_candidate_when_cap2_clearly_helps():
    rows = []
    for index in range(31):
        day = f"2026-12-{index + 1:02d}"
        later = -(5.0 + (index % 3))
        rows.append(_row(day, 30, 10.0))
        rows.append(_row(day, 40, later))
        rows.append(_row(day, 50, later))
    report = MODULE.settle(rows)
    assert report["uncapped"]["days"] == 31
    cap2 = report["caps"][0]
    assert cap2["cap"] == 2
    assert cap2["ci80"][0] > 0
    assert cap2["mean_delta_bps"] > cap2["mde80"]
    assert cap2["pass"] is True
    assert report["verdict"] == "GRADUATION_CANDIDATE"


def test_settle_frequency_population_keeps_recovery_pnl_but_excludes_compensation():
    rows = [_row("2026-11-16", 30, 10.0), _row("2026-11-16", 40, -5.0)]
    rows += [{**_row("2026-11-16", 50, -20.0), "entry_reason": "sync_recovery_backfill"}]
    rows += [{**_row("2026-11-16", 60, -100.0),
              "entry_reason": "playbook_entry_bookkeeping_compensation"}]
    report = MODULE.settle(rows)
    assert report["uncapped"]["n_trades"] == 3
    assert report["n_artifact_excluded"] == 1
    assert report["n_recovery_included"] == 1
    assert report["caps"][0]["kept_trades"] == 2
    assert report["caps"][1]["kept_trades"] == 3


def test_negative_capped_expectancy_cannot_graduate_even_when_delta_positive():
    rows = []
    for day in range(1, 31):
        date = f"2026-11-{day:02d}"
        rows.extend([
            _row(date, 30, -0.1), _row(date, 40, -0.1), _row(date, 50, -29.8),
        ])
    report = MODULE.settle(rows)
    cap2 = report["caps"][0]
    assert cap2["mean_delta_bps"] > 0
    assert cap2["cap_day_mean_bps"] < 0
    assert cap2["criteria"]["day_cluster_ci80_lower>0"] is True
    assert cap2["criteria"]["cap_day_mean_bps>0"] is False
    assert cap2["pass"] is False
    assert report["verdict"] != "GRADUATION_CANDIDATE"


def test_significantly_harmful_caps_are_not_called_indistinguishable():
    rows = []
    for day in range(1, 31):
        date = f"2026-11-{day:02d}"
        rows.extend([
            _row(date, 30, -1.0), _row(date, 40, -1.0),
            _row(date, 50, 2.0), _row(date, 60, 2.0),
        ])
    report = MODULE.settle(rows)
    assert all(c["ci80"][1] < 0 for c in report["caps"])
    assert report["verdict"] == "FREQUENCY_CAP_HARMFUL"
