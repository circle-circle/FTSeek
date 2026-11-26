"""RFC-145 B1 record-only chase_cap_anchor_headroom 契约。"""
from pathlib import Path

import pytest

from app.playbook.entry_routing import chase_cap_anchor_headroom_pct

SRC = (Path(__file__).resolve().parents[1] / "app" / "playbook" /
       "pipeline_parts" / "auto_execute_flow.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("direction,cap,expected", [
    ("BUY", 104.0, 4.0), ("LONG", 105.0, 5.0), ("BUY", 105.01, 5.01),
    ("SHORT", 96.0, 4.0), ("SELL_SHORT", 95.0, 5.0), ("SHORT", 94.99, 5.01),
])
def test_direction_and_percentage_units(direction, cap, expected):
    assert chase_cap_anchor_headroom_pct(direction, 100.0, cap) == pytest.approx(expected)


def test_inverted_cap_keeps_negative_evidence():
    assert chase_cap_anchor_headroom_pct("BUY", 100.0, 95.0) == pytest.approx(-5.0)
    assert chase_cap_anchor_headroom_pct("SHORT", 100.0, 105.0) == pytest.approx(-5.0)


@pytest.mark.parametrize("direction,anchor,cap", [
    ("BUY", None, 100), ("BUY", 100, None), ("BUY", 0, 100), ("BUY", 100, 0),
    ("BUY", "bad", 100), ("BUY", 100, "bad"), ("HOLD", 100, 104),
    ("BUY", float("nan"), 100), ("BUY", 100, float("inf")),
])
def test_invalid_inputs_do_not_emit(direction, anchor, cap):
    assert chase_cap_anchor_headroom_pct(direction, anchor, cap) is None


def test_record_only_block_precedes_and_is_independent_of_enforce():
    headroom = SRC.index('_cc_headroom_pct(_dir, _gen_cc, _cc_orig)')
    violation = SRC.index('if _cc_violated(_dir, _new_entry, _cc_f):', headroom)
    assert headroom < violation
    block = SRC[headroom:violation]
    assert '"type": "chase_cap_anchor_headroom"' in block
    assert '"headroom_pct": round(_cc_headroom, 4)' in block
    assert '"chase_zone_threshold_pct": round(_cc_chase_pct * 100.0, 4)' in block
    assert '"entry_archetype": _sem_cc.get("entry_archetype")' in block
    assert '"direction": _dir' in block
    assert '"chase_cap_price_original": _cc_orig' in block
    assert '"record_only": True' in block
    assert "_cc_gate_ok" not in block
    assert "chase_cap_enforced" not in block


def test_threshold_is_strictly_below_five_percent_not_ratio_or_percentage_points():
    assert "if _cc_headroom is not None and _cc_headroom < _cc_chase_pct * 100.0:" in SRC
    assert chase_cap_anchor_headroom_pct("BUY", 100, 104.99) < 5.0
    assert chase_cap_anchor_headroom_pct("BUY", 100, 105.0) == pytest.approx(5.0)
    assert chase_cap_anchor_headroom_pct("BUY", 100, 105.01) > 5.0
