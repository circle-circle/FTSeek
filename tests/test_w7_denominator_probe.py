import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "w7_denominator_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("w7_denominator_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_equal_adjacent_closes_do_not_report_stale_pair():
    module = _load_module()
    assert module._stale_pair_hit(14.79, 14.79, 14.79, 14.79) is False


def test_distinct_adjacent_closes_can_report_stale_pair():
    module = _load_module()
    assert module._stale_pair_hit(15.47, 15.26, 15.26, 15.47) is True
