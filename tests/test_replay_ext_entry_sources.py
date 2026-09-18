from scripts.replay_ext_entry_sources import _simulate, _simulate_ah


BARS = [
    {"t": "2026-09-18 04:05:00", "c": 100.0, "h": 100.0, "l": 100.0, "v": 1000.0},
    {"t": "2026-09-18 04:10:00", "c": 100.8, "h": 100.9, "l": 100.7, "v": 1000.0},
    {"t": "2026-09-18 04:15:00", "c": 100.3, "h": 100.4, "l": 100.2, "v": 1000.0},
]


def test_premarket_replay_uses_shared_profit_exit_rules():
    result = _simulate(BARS, prev_close=99.0, entry_bar_i=0)

    assert result["rule_action"] == "SL_TRAIL_LOCK"
    assert result["rule"]["ret_pct"] == 0.3
    assert result["rule"]["net_pct"] == 0.215


def test_afterhours_replay_uses_shared_profit_exit_rules():
    result = _simulate_ah(BARS, next_day_bars=[], entry_bar_i=0)

    assert result["rule_action"] == "SL_TRAIL_LOCK"
    assert result["rule"]["ret_pct"] == 0.3
    assert result["rule"]["net_pct"] == 0.215
