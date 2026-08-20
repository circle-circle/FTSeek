"""方向条件化 IC 净分契约测试 (DEB §五十四, 2026-08-20).

背景: pooled IC (~91% LONG 样本) 对 SHORT 候选系统性误杀 —
AVGO 8/19 被 pooled net_ic=+0.09 拦截, 当日 -5.2% 方向正确。
修法: 按候选方向取同侧 IC (ic_long/ic_short), n_<side>≥30 才计分,
不足者不参与 (不用 pooled 顶替)。

契约:
1. SHORT 候选用 ic_short 计分 (AVGO 案复现: news_positive SHORT侧 -0.39)
2. BUY 候选用 ic_long 计分
3. n_<side> < 30 的因子不计分 (fail-open, 不顶替 pooled)
4. 无分方向字段时全部不计分 (旧数据 fail-open, 净分 0 不拦)
5. bearish/bullish 分类不影响计分 (双重方向化禁止)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.playbook.pipeline import _direction_conditioned_ic_net


# 真实 factor_ic_latest.json 8/20 生成值 (独立 Spearman 复算逐格一致)
FACTOR_IC = {
    "news_positive": {"ic": 0.0913, "n": 519, "ic_long": 0.1083, "n_long": 474,
                      "ic_short": -0.3859, "n_short": 45},
    "insider_net_sell": {"ic": -0.0448, "n": 51, "ic_long": 0.1972, "n_long": 19,
                         "ic_short": 0.2278, "n_short": 32},
    "below_ma20": {"ic": 0.4484, "n": 18, "ic_long": None, "n_long": 0,
                   "ic_short": 0.4484, "n_short": 18},
    "sentiment_bullish": {"ic": 0.4313, "n": 74, "ic_long": 0.4313, "n_long": 74,
                          "ic_short": None, "n_short": 0},
}


def test_avgo_case_short_uses_short_side_ic():
    """AVGO 案: SHORT 候选命中 insider_net_sell + below_ma20 + news_positive.
    SHORT 侧: insider_net_sell +0.2278 (n=32≥30 计分), below_ma20 n=18<30 不计,
    news_positive -0.3859 (n=45 计分) → net = -0.158 < -0.05 → 仍拦。
    (该日 AVGO 实际 -5.2%, 此例显示门在弱证据下仍可能拦, 由影子账裁决)"""
    net, scored = _direction_conditioned_ic_net(
        "SELL_SHORT",
        bearish_hits=["insider_net_sell", "below_ma20"],
        bullish_hits=["news_positive"],
        factor_ic=FACTOR_IC)
    assert "insider_net_sell" in scored
    assert "news_positive" in scored
    assert "below_ma20" not in scored  # n_short=18 < 30 不计分
    assert abs(net - (0.2278 - 0.3859)) < 1e-6


def test_short_supporting_factors_pass():
    """SHORT 候选命中纯支持因子 (ic_short 均正) → net>0 不拦."""
    net, scored = _direction_conditioned_ic_net(
        "SELL_SHORT",
        bearish_hits=["insider_net_sell"],
        bullish_hits=[],
        factor_ic=FACTOR_IC)
    assert net > 0
    assert scored == ["insider_net_sell"]


def test_buy_uses_long_side_ic():
    """BUY 候选命中 sentiment_bullish (ic_long=+0.43, n=74) → 支持, 不拦."""
    net, scored = _direction_conditioned_ic_net(
        "BUY",
        bearish_hits=[],
        bullish_hits=["sentiment_bullish"],
        factor_ic=FACTOR_IC)
    assert net > 0
    assert scored == ["sentiment_bullish"]


def test_insufficient_n_not_scored_no_pooled_fallback():
    """below_ma20 对 BUY 候选: n_long=0 → 不计分, 绝不用 pooled +0.45 顶替."""
    net, scored = _direction_conditioned_ic_net(
        "BUY",
        bearish_hits=["below_ma20"],
        bullish_hits=[],
        factor_ic=FACTOR_IC)
    assert net == 0.0
    assert scored == []


def test_legacy_ic_table_fail_open():
    """旧版 factor_ic_latest.json (无 ic_long/ic_short 字段) → 净分 0, 门不误拦."""
    legacy = {"news_positive": {"ic": 0.0913, "n": 519}}
    net, scored = _direction_conditioned_ic_net(
        "SELL_SHORT", ["news_positive"], [], legacy)
    assert net == 0.0
    assert scored == []


def test_bearish_bullish_classification_ignored():
    """同一因子放 bearish 或 bullish 桶, 计分结果必须相同 (双重方向化禁止)."""
    net1, s1 = _direction_conditioned_ic_net(
        "SELL_SHORT", ["insider_net_sell"], [], FACTOR_IC)
    net2, s2 = _direction_conditioned_ic_net(
        "SELL_SHORT", [], ["insider_net_sell"], FACTOR_IC)
    assert net1 == net2
    assert s1 == s2


def test_empty_hits_zero():
    net, scored = _direction_conditioned_ic_net("BUY", [], [], FACTOR_IC)
    assert net == 0.0
    assert scored == []


if __name__ == "__main__":
    test_avgo_case_short_uses_short_side_ic()
    test_short_supporting_factors_pass()
    test_buy_uses_long_side_ic()
    test_insufficient_n_not_scored_no_pooled_fallback()
    test_legacy_ic_table_fail_open()
    test_bearish_bullish_classification_ignored()
    test_empty_hits_zero()
    print("7/7 方向条件化 IC 契约测试通过")
