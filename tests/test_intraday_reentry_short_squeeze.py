"""卖空压力+内部人背离检查测试 (2026-08-22)

验证 evaluate_candidate 的两个新证伪门:
- SHORT 方向 + short_percent>30% → 拒绝 (squeeze 风险)
- LONG 方向 + insider_sell → 拒绝 (内部人背离)
- SHORT 方向 + insider_buy → 拒绝 (内部人背离)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.intraday_reentry import evaluate_candidate


def test_short_squeeze_reject():
    """SHORT + short_percent>30% → 拒绝"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "short_squeeze_check_enabled": True,
           "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "SHORT", price=99.0, vwap=99.5,
        slope_5m=-0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, short_percent=35.0
    )
    assert not ok, "应拒绝但通过"
    assert any("short_squeeze_risk" in r for r in reasons), f"缺 squeeze 原因: {reasons}"


def test_short_squeeze_pass():
    """SHORT + short_percent<30% → 通过"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "short_squeeze_check_enabled": True,
           "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "SHORT", price=99.0, vwap=99.5,
        slope_5m=-0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, short_percent=20.0
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_insider_sell_not_long_counterevidence():
    """LONG + insider_sell → 通过 (2026-08-22c 收窄: 10b5-1 例行减持是常态噪音, 不构成 LONG 反证)"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "insider_divergence_check_enabled": True,
           "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, insider_signal="sell"
    )
    assert ok, f"insider_sell 例行噪音应放行但被拒: {reasons}"


def test_insider_divergence_short_reject():
    """SHORT + insider_buy → 拒绝"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "insider_divergence_check_enabled": True,
           "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "SHORT", price=99.0, vwap=99.5,
        slope_5m=-0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, insider_signal="buy"
    )
    assert not ok, "应拒绝但通过"
    assert any("insider_divergence" in r for r in reasons), f"缺背离原因: {reasons}"


def test_insider_aligned_pass():
    """方向与内部人一致 → 通过"""
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "insider_divergence_check_enabled": True,
           "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, insider_signal="buy"
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_long_high_short_percent_not_rejected():
    """LONG + short_percent>40% → 通过 (ADR-032b 删除 392f018e 的 LONG 对称分支).

    本门族语义是"方向与证据背离才拒": 高空头仓位不构成 LONG 反证(常规读法反而是
    squeeze 燃料), 原分支注释自述"底部确认"却用于拒绝做多、尾句"拒绝追空"又与
    is_long 分支矛盾。本测试钉死删除, 防矛盾逻辑静默回归; 若确要"激战名波动率
    风控", 按验证期纪律另立机制带数据裁决。
    """
    cfg = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
           "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0,
           "short_squeeze_check_enabled": True, "short_squeeze_threshold_pct": 30.0}
    ok, reasons = evaluate_candidate(
        "LONG", price=100.0, vwap=99.5,
        slope_5m=0.1, atr_pct=3.0, vol_ratio=1.0, spread_bps=10.0,
        cfg=cfg, short_percent=50.0
    )
    assert ok, f"高空头仓位不应拒绝 LONG: {reasons}"
    assert not any("short_squeeze" in r for r in reasons)
