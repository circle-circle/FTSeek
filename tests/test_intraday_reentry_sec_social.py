"""重大事件/多源情绪/逐笔情绪证伪门测试 (2026-08-22)

验证 evaluate_candidate 的三个新证伪门:
- 重大事件: sec_event_negative=True → 拒绝
- 多源情绪背离: finnhub 看多但 social 看空 → 拒绝
- 逐笔情绪背离: LONG+bearish_tick / SHORT+bullish_tick → 拒绝
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.intraday_reentry import evaluate_candidate

_BASE_CFG = {"vwap_band_low_pct": 0.3, "vwap_band_high_pct": 1.0, "min_slope_5m_pct": 0.0,
             "min_atr_pct": 2.0, "min_vol_ratio": 0.8, "max_spread_bps": 25.0}


def test_sec_event_negative_reject():
    """重大负面事件 → 拒绝"""
    cfg = {**_BASE_CFG, "sec_event_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, sec_event_negative=True
    )
    assert not ok and any("sec_event_divergence" in r for r in reasons)


def test_sec_event_none_pass():
    """无重大负面事件 → 通过"""
    cfg = {**_BASE_CFG, "sec_event_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, sec_event_negative=False
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_social_sentiment_divergence_reject():
    """多源情绪背离 → 拒绝"""
    cfg = {**_BASE_CFG, "sentiment_extreme_check_enabled": False,
           "social_sentiment_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg,
        sentiment_score=0.5, social_sentiment_score=-0.5  # finnhub 看多, social 看空
    )
    assert not ok and any("social_sentiment_divergence" in r for r in reasons)


def test_social_sentiment_aligned_pass():
    """多源情绪一致 → 通过"""
    cfg = {**_BASE_CFG, "sentiment_extreme_check_enabled": False,
           "social_sentiment_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg,
        sentiment_score=0.5, social_sentiment_score=0.3  # 都看多
    )
    assert ok, f"应通过但被拒: {reasons}"


def test_ticker_sentiment_long_reject():
    """LONG + bearish_tick → 拒绝"""
    cfg = {**_BASE_CFG, "ticker_sentiment_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, ticker_sentiment="bearish"
    )
    assert not ok and any("ticker_sentiment_divergence" in r for r in reasons)


def test_ticker_sentiment_aligned_pass():
    """方向与逐笔情绪一致 → 通过"""
    cfg = {**_BASE_CFG, "ticker_sentiment_divergence_check_enabled": True}
    ok, reasons = evaluate_candidate(
        "LONG", 100.0, 99.5, 0.1, 3.0, 1.0, 10.0, cfg, ticker_sentiment="bullish"
    )
    assert ok, f"应通过但被拒: {reasons}"
