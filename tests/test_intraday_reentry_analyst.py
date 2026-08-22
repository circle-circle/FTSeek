"""分析师/财报/情绪证伪门测试 (2026-08-22)

验证 evaluate_candidate 的三个新证伪门:
- 分析师背离: LONG+downgrade / SHORT+upgrade → 拒绝
- 财报临近: days_to_earnings<3 → 拒绝
- 情绪极端: LONG+sentiment>0.8 / SHORT+sentiment<-0.8 → 拒绝
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.intraday_reentry import evaluate_candidate

_BASE_CFG = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
             "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0}


def test_analyst_divergence_long_reject():
    """LONG + downgrade → 拒绝"""
    cfg = {**_BASE_CFG, "analyst_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, analyst_trend="downgrade"
    )
    assert not ok and any("analyst_divergence" in r for r in reasons)


def test_analyst_aligned_pass():
    """LONG + upgrade → 通过"""
    cfg = {**_BASE_CFG, "analyst_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, analyst_trend="upgrade"
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_earnings_proximity_reject():
    """财报<3天 → 拒绝"""
    cfg = {**_BASE_CFG, "earnings_proximity_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, days_to_earnings=2
    )
    assert not ok and any("earnings_proximity" in r for r in reasons)


def test_earnings_far_pass():
    """财报>3天 → 通过"""
    cfg = {**_BASE_CFG, "earnings_proximity_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, days_to_earnings=5
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_sentiment_extreme_long_reject():
    """LONG + sentiment>0.8 → 拒绝"""
    cfg = {**_BASE_CFG, "sentiment_extreme_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, sentiment_score=0.85
    )
    assert not ok and any("sentiment_extreme" in r for r in reasons)


def test_sentiment_moderate_pass():
    """情绪温和 → 通过"""
    cfg = {**_BASE_CFG, "sentiment_extreme_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, sentiment_score=0.5
    )
    assert ok, f"应通过但被拒: {reasons}"
