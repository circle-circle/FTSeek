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
    # G5 实际加载 = 2026-10-07 23:10:46 CST (DEB §4.3 / commit 10adea0c8)
    assert module._after_g5_load("2026-10-07 23:10:45 pre-load event") is False
    assert module._after_g5_load("2026-10-07 23:10:46 load event") is True
    # E10 证据: 5×68 auto_execute 轮次 (10-07 ET 15:47–15:59 = CST 次日凌晨)
    assert module._after_g5_load("2026-10-08 03:47:05 auto_execute 68") is True
    # 10-08 盘中实际成交轮次 (VST/SPCX/COIN) 不得被误滤
    assert module._after_g5_load("2026-10-08 21:36:04 execution round") is True
    assert module._after_g5_load("2026-10-09 00:11:12 later event") is True
    assert module._after_g5_load("2026-10-07 23:09:59 pre-load event") is False
    assert module._after_g5_load("malformed line") is False
