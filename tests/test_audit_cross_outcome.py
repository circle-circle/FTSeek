from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ops" / "audit" / "audit_cross_outcome.py"
_spec = importlib.util.spec_from_file_location("audit_cross_outcome", MODULE_PATH)
audit_cross_outcome = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_cross_outcome)


def test_day_clustered_t_requires_power_and_variance():
    assert audit_cross_outcome.day_clustered_t([]) is None
    assert audit_cross_outcome.day_clustered_t([1.0, 2.0]) is None
    assert audit_cross_outcome.day_clustered_t([5.0, 5.0, 5.0]) is None


def test_day_clustered_t_reports_mde_and_distinguishability():
    result = audit_cross_outcome.day_clustered_t([8.0, 10.0, 12.0])
    assert result["n_days"] == 3
    assert result["day_mean"] == 10.0
    assert result["t"] > 2.0
    assert result["mde_80pct"] > 0
    assert result["distinguishable"] is True

    result = audit_cross_outcome.day_clustered_t([1.0, 2.0, 3.0, 10.0])
    assert result["distinguishable"] is False


def test_paired_day_diffs_weights_days_equally_and_uses_common_dates():
    crossed = [
        {"date": "2026-09-01", "pnl": 10.0},
        {"date": "2026-09-01", "pnl": 20.0},
        {"date": "2026-09-02", "pnl": 30.0},
        {"date": "2026-09-03", "pnl": 100.0},
    ]
    clean = [
        {"date": "2026-09-01", "pnl": 5.0},
        {"date": "2026-09-02", "pnl": 10.0},
        {"date": "2026-09-02", "pnl": 20.0},
    ]
    assert audit_cross_outcome.paired_day_diffs(crossed, clean) == [10.0, 15.0]
