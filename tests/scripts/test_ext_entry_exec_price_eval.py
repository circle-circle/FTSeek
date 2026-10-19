"""ext_entry_exec_price_eval 纯读数契约 (2026-10-19)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ext_entry_exec_price_eval import compute_metrics  # noqa: E402


def test_compute_metrics_basic():
    rows = [
        {"trade_date": "2026-09-28", "session": "premarket", "candidates_n": 47,
         "current_price_pool_n": 10, "shadow_execution_pool_n": 2, "prefiltered_out_n": 8,
         "prefiltered_out": [{"symbol": "US.LOW", "decision_price": 6.0, "change_pct": 10.0}],
         "shadow_execution_pool": [{"symbol": "US.HIGH", "decision_price": 60.0, "change_pct": 3.0}]},
        {"trade_date": "2026-09-29", "session": "afterhours", "candidates_n": 50,
         "current_price_pool_n": 12, "shadow_execution_pool_n": 0, "prefiltered_out_n": 12,
         "prefiltered_out": [{"symbol": "US.MID", "decision_price": 20.0, "change_pct": 5.0}],
         "shadow_execution_pool": []},
    ]
    m = compute_metrics(rows)
    assert m["rows"] == 2
    assert m["distinct_days"] == 2
    assert m["sessions"] == {"premarket": 1, "afterhours": 1}
    assert m["candidates_n"]["median"] == 50.0
    assert m["current_price_pool_n"]["median"] == 12.0
    assert m["shadow_execution_pool_n"]["median"] == 2.0
    assert m["prefiltered_out"]["entries"] == 2
    assert m["prefiltered_out"]["price_buckets"] == {"5-10": 1, "20-50": 1}
    assert m["shadow_execution_pool"]["price_buckets"] == {">=50": 1}
    assert m["shrink_ratio"]["median"] is not None
