"""资金流背离检查测试 (2026-08-22)

验证 evaluate_candidate 的资金流背离门:
- LONG 方向 + 大单净流出 → 拒绝
- SHORT 方向 + 大单净流入 → 拒绝
- 同向 → 通过
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.intraday_reentry import evaluate_candidate


def test_long_inflow_pass():
    """LONG + 大单净流入 → 通过"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,  # 在 VWAP 上方 0.5%
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction="INFLOW"
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_long_outflow_reject():
    """LONG + 大单净流出 → 拒绝"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction="OUTFLOW"
    )
    assert not ok, "应拒绝但通过"
    assert any("capital_flow_divergence" in r for r in reasons), f"缺背离原因: {reasons}"


def test_short_outflow_pass():
    """SHORT + 大单净流出 → 通过"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "SHORT", price=99.0, vwap=99.5,  # 在 VWAP 下方
        slope_5m=-0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction="OUTFLOW"
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_short_inflow_reject():
    """SHORT + 大单净流入 → 拒绝"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "SHORT", price=99.0, vwap=99.5,
        slope_5m=-0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction="INFLOW"
    )
    assert not ok, "应拒绝但通过"
    assert any("capital_flow_divergence" in r for r in reasons), f"缺背离原因: {reasons}"


def test_no_capital_flow_data_pass():
    """无资金流数据 → 不拦 (fail-open)"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction=None
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_disabled_pass():
    """开关关闭 → 不拦"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "capital_flow_divergence_check_enabled": False}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, capital_flow_direction="OUTFLOW"
    )
    assert ok, f"应通过但被拒: {reasons}"
