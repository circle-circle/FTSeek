"""入场成本分解契约测试 (DEB-20261031).

钉住方向感知的符号 (2026-10-17 踩过: 方向盲版本把 SHORT 的"卖得更低"记成改善)。
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "entry_cost_decomposition.py"


def _load():
    spec = importlib.util.spec_from_file_location("entry_cost_decomposition", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_cost_sign_is_direction_aware():
    m = _load()
    # LONG: 买得更贵 = 正
    assert m._cost_bps(100.0, 101.0, "LONG") > 0
    # LONG: 买得更便宜 = 负
    assert m._cost_bps(100.0, 99.0, "LONG") < 0
    # SHORT: 卖得更低 = 正 (更贵/更差)
    assert m._cost_bps(100.0, 99.0, "SHORT") > 0
    # SHORT: 卖得更高 = 负 (更好)
    assert m._cost_bps(100.0, 101.0, "SHORT") < 0


def test_cost_bps_scale():
    m = _load()
    # 1% 的价格差 = 100bps
    assert abs(m._cost_bps(100.0, 101.0, "LONG") - 100.0) < 1e-6


def test_thresholds_are_pre_registered_constants():
    m = _load()
    assert m.REACHABLE_THRESHOLD_BPS == 5.0
    assert m.PLAN_ANCHOR_THRESHOLD_BPS == 30.0