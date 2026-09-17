import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.settle_extended_hours as settle


def test_source_window_filter_is_strict():
    premarket_in = {
        "_source": "premarket_exit", "hhmm_et": 925, "in_premarket_window": True
    }
    premarket_flag_false = {
        "_source": "premarket_exit", "hhmm_et": 925, "in_premarket_window": False
    }
    afterhours_in = {
        "_source": "afterhours_exit", "hhmm_et": 1800, "in_afterhours_window": True
    }
    unknown = {"_source": "unknown", "hhmm_et": 925}
    assert settle._in_source_window(premarket_in)
    assert not settle._in_source_window(premarket_flag_false)
    assert settle._in_source_window(afterhours_in)
    assert not settle._in_source_window(unknown)


def test_historical_settled_records_are_window_filtered():
    records = [
        {"source": "premarket_exit", "hhmm_et": 925},
        {"source": "premarket_exit", "hhmm_et": 955},
        {"source": "afterhours_exit", "hhmm_et": 1800},
        {"source": "unknown", "hhmm_et": 1000},
    ]
    kept = [record for record in records if settle._settled_record_in_window(record)]
    assert kept == [
        {"source": "premarket_exit", "hhmm_et": 925},
        {"source": "afterhours_exit", "hhmm_et": 1800},
    ]
