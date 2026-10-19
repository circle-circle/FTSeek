"""ext_earnings_criterion_eval 台账轴契约 (2026-10-18)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ext_earnings_criterion_eval import compute_ledger  # noqa: E402


def test_compute_ledger_dedupes_days_not_rows():
    rows = [
        {"et_date": "2026-09-22", "symbol": "US.A", "legacy_blocked_new_pass": True,
         "legacy_reason": "surprise_stale", "new_surprise_pct": 16.0},
        {"et_date": "2026-09-22", "symbol": "US.B", "legacy_blocked_new_pass": True,
         "legacy_reason": "surprise_stale", "new_surprise_pct": 3.0},
        {"et_date": "2026-09-23", "symbol": "US.A", "legacy_blocked_new_pass": False,
         "legacy_reason": None, "new_surprise_pct": None},
    ]
    m = compute_ledger(rows)
    assert m["rows"] == 3
    assert m["distinct_et_days"] == 2
    assert m["distinct_symbols"] == 2
    assert m["legacy_blocked_new_pass_rows"] == 2
    assert m["legacy_blocked_new_pass_days"] == 1
    assert m["legacy_blocked_new_pass_per_day"] == {"2026-09-22": 2}
    assert m["legacy_reason"] == {"surprise_stale": 2}
    assert m["surprise_pct"]["n"] == 2
    assert m["surprise_pct"]["median"] == 16.0
