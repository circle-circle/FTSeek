"""Scenario definitions — export all registered analysis scenarios."""

from .debate import DEBATE_SCENARIO
from .attribution import ATTRIBUTION_SCENARIO
from .preview import PREVIEW_SCENARIO
from .meta import META_SCENARIO

ALL_SCENARIOS = {
    "debate": DEBATE_SCENARIO,
    "attribution": ATTRIBUTION_SCENARIO,
    "preview": PREVIEW_SCENARIO,
    "meta": META_SCENARIO,
}
