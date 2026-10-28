"""桶选择双实现一致性 (2026-10-28, DEB-20261028 §15.8 P2-F3).

`app/playbook/bucket_lineage.py::select_vol_bucket` 是审计侧的纯函数, 生产选择逻辑住在
`app/playbook/instruction_generator.py::_compute_volatility_based_sl_tp`。两份实现必须对
同一 ATR/同一桶配置给出同一归桶, 否则血缘记录会与真实止损参数错位。

钉住的语义 (与生产逐字对齐):
  · 按 `atr_pct_max` 升序, 取第一个 `atr < bound`;
  · 缺 `atr_pct_max` 的桶按 999.0 兜底 = catch-all;
  · 无任何匹配 (atr ≥ 最大边界) ⇒ None。
"""
import yaml

from app.playbook.bucket_lineage import select_vol_bucket
from app.playbook.instruction_generator import _compute_volatility_based_sl_tp

CONFIG_PATH = "config/playbook_horizon.yaml"

ATR_GRID = [0.005, 0.01, 0.0149, 0.015, 0.0151, 0.02, 0.0349, 0.035, 0.036,
            0.05, 0.079, 0.08, 0.081, 0.12, 0.20]


class _StubConfig:
    """生产函数只用到 pb_cfg.get(...) 的兜底; 这里强制走 _horizon_params。"""

    def get(self, key, default=None):
        return default


def _load_horizon():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _production_bucket(atr_pct, buckets):
    result = _compute_volatility_based_sl_tp(
        atr_pct, "BUY", {"vol_buckets": buckets}, _StubConfig()
    )
    return None if not result else result["bucket"]


def test_parity_on_live_short_and_long_buckets():
    horizon = _load_horizon()
    for section in ("short_horizon", "long_horizon"):
        buckets = horizon[section]["vol_buckets"]
        for atr in ATR_GRID:
            assert select_vol_bucket(atr, buckets) == _production_bucket(atr, buckets), (
                section, atr
            )


def test_missing_bound_bucket_is_catch_all_in_both_implementations():
    buckets = {
        "low": {"sl_floor_pct": 2.0, "sl_ceil_pct": 2.5},
        "high": {"atr_pct_max": 0.08, "sl_floor_pct": 3.5, "sl_ceil_pct": 5.0},
    }
    assert select_vol_bucket(0.20, buckets) == "low" == _production_bucket(0.20, buckets)
    assert select_vol_bucket(0.05, buckets) == "high" == _production_bucket(0.05, buckets)


def test_no_bucket_when_atr_exceeds_every_bound():
    buckets = {"high": {"atr_pct_max": 0.08, "sl_floor_pct": 3.5, "sl_ceil_pct": 5.0}}
    assert select_vol_bucket(0.09, buckets) is None
    assert _production_bucket(0.09, buckets) is None