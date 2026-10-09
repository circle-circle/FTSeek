import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "deb20261008_gate_attribution.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "deb20261008_gate_attribution", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_filter_starts_at_g5_load_and_keeps_later_hours():
    module = _load_module()
    assert module._after_g5_load("2026-10-08 23:09:59 old event") is False
    assert module._after_g5_load("2026-10-08 23:10:00 load event") is True
    assert module._after_g5_load("2026-10-09 00:11:12 later event") is True
    assert module._after_g5_load("2026-10-08 21:39:30 shadow event") is False
    assert module._after_g5_load("malformed line") is False
