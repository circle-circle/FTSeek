"""P0 路由反事实契约: 限价触碰/市价可成交/方向归一/聚合口径。"""
from __future__ import annotations

from datetime import datetime

import scripts.entry_routing_counterfactual as erc


def _bar(ts, o, h, l, c=100.0):
    return [ts, o, h, l, c]


def test_simulate_short_filled_and_skipped():
    entry = datetime(2026, 10, 20, 9, 44)
    bars = [_bar("2026-10-20 09:45:00", 100, 101.2, 99.5)]
    hit = erc.simulate_limit(bars, "SHORT", entry, 100.0, 101.0, 300)
    assert hit["state"] == "filled" and hit["fill"] == 101.0
    miss = erc.simulate_limit([_bar("2026-10-20 09:45:00", 100, 100.8, 99.5)],
                              "SHORT", entry, 100.0, 101.0, 300)
    assert miss["state"] == "skipped"


def test_simulate_long_filled_and_skipped():
    entry = datetime(2026, 10, 20, 9, 44)
    bars = [_bar("2026-10-20 09:45:00", 100, 100.5, 98.9)]
    hit = erc.simulate_limit(bars, "LONG", entry, 100.0, 99.0, 300)
    assert hit["state"] == "filled" and hit["fill"] == 99.0
    miss = erc.simulate_limit([_bar("2026-10-20 09:45:00", 100, 100.5, 99.2)],
                              "LONG", entry, 100.0, 99.0, 300)
    assert miss["state"] == "skipped"


def test_marketable_limit_uses_actual_entry():
    entry = datetime(2026, 10, 20, 9, 44)
    short = erc.simulate_limit([], "SHORT", entry, 100.0, 99.0, 300)
    assert short["state"] == "marketable" and short["fill"] == 100.0
    long = erc.simulate_limit([], "LONG", entry, 100.0, 101.0, 300)
    assert long["state"] == "marketable" and long["fill"] == 100.0


def test_entry_improvement_direction():
    assert erc._entry_improvement_bps("SHORT", 100.0, 101.0) == 100.0
    assert erc._entry_improvement_bps("LONG", 100.0, 99.0) == 100.0
    assert erc._entry_improvement_bps("SHORT", 100.0, 99.0) == -100.0
    assert erc._entry_improvement_bps("LONG", 100.0, 101.0) == -100.0


def test_aggregate_skipped_opportunity_cost():
    rows = [
        {"actual_pnl": 10.0, "left_tail": False, "archetype": "FADE_SHORT",
         "variants": {"v": {"state": "filled", "improvement_bps": 100.0, "cf_pnl": 20.0}}},
        {"actual_pnl": -30.0, "left_tail": True, "archetype": "PULLBACK_LIMIT",
         "variants": {"v": {"state": "skipped", "improvement_bps": 0.0, "cf_pnl": 0.0}}},
    ]
    out = erc._aggregate(rows, "v")
    assert out["eligible_n"] == 2 and out["filled_n"] == 1 and out["skipped_n"] == 1
    assert out["actual_net"] == -20.0 and out["cf_net"] == 20.0
    assert out["delta_net"] == 40.0
    assert out["skipped_actual_net"] == -30.0
    assert out["skipped_winners_n"] == 0
    assert out["skipped_left_tail_n"] == 1
    assert out["by_archetype"]["PULLBACK_LIMIT"]["fill_rate_pct"] == 0.0
