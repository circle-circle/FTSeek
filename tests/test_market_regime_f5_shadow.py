# -*- coding: utf-8 -*-
"""[DEB-20260924 F5/F14] 契约测试: regime 盘前影子双算 + thesis×archetype 同源。

F5: RegimeContext 影子字段默认值/纯函数 shadow_score_delta/score_to_regime_value 与主路径映射一致
    (9/21 翻转实例钉死)。影子必须 record-only: 主判定字段不因影子字段存在而改变。
F14: _select_thesis_template 的 archetype 优先 + 方向一致性守卫 + 因子兜底不回归。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.playbook.market_regime import (  # noqa: E402
    MarketRegime,
    RegimeContext,
    score_to_regime_value,
    shadow_score_delta,
)
from app.playbook.pipeline import _select_thesis_template  # noqa: E402


def _ctx(**kw):
    base = dict(regime=MarketRegime.CAUTIOUS, vix=18.0, spy_price=761.69, spy_ma20=762.5,
                spy_ma50=755.0, spy_ma200=700.0, spy_change_pct=0.0, score=4,
                top_n=8, position_multiplier=0.5, min_strength=60)
    base.update(kw)
    return RegimeContext(**base)


class TestF5ShadowFields:
    def test_defaults_are_inert(self):
        """影子字段默认 = 无双算 (basis 标注 + 0/None), 主判定字段不受影响。"""
        c = _ctx()
        assert c.spy_price_basis == "unknown"
        assert c.spy_pre_price == 0.0
        assert c.score_shadow is None
        assert c.regime_shadow is None
        # 主判定五要素与旧行为逐位一致
        assert (c.regime, c.score, c.top_n, c.position_multiplier, c.min_strength) == (
            MarketRegime.CAUTIOUS, 4, 8, 0.5, 60)

    def test_to_dict_contains_shadow_keys(self):
        d = _ctx(score_shadow=8, regime_shadow="risk_on").to_dict()
        assert d["score_shadow"] == 8 and d["regime_shadow"] == "risk_on"
        assert d["spy_price_basis"] == "unknown"


class TestShadowScoreDelta:
    def test_sept21_real_flip(self):
        """9/21 实例: 昨收 761.69 < MA20 762.50 (主判定 -2), 盘前 763.96 > MA20 (+2) ⇒ delta=+4。
        MA200=700 两侧同高于 ⇒ 该项 delta=0。score 4 → 8 跨 cautious→risk_on 边界。"""
        delta = shadow_score_delta(761.69, 763.96, 762.50, 700.0)
        assert delta == 4
        assert score_to_regime_value(4) == "cautious"
        assert score_to_regime_value(4 + delta) == "risk_on"

    def test_no_flip_when_same_side(self):
        assert shadow_score_delta(767.81, 763.96, 762.50, 700.0) == 0  # 两侧都 > MA20/MA200

    def test_missing_inputs_zero(self):
        assert shadow_score_delta(0, 763.96, 762.5, 700.0) == 0
        assert shadow_score_delta(761.69, 0.0, 762.5, 700.0) == 0  # pre 不可用 ⇒ 不双算不猜

    def test_ma200_component(self):
        # 昨收 > MA200 (+1) 而盘前 < MA200 (-1) ⇒ delta=-2 (MA20 同侧不计)
        assert shadow_score_delta(701.0, 699.0, 600.0, 700.0) == -2


class TestScoreToRegimeMirror:
    @pytest.mark.parametrize("score,expected", [
        (10, "risk_on"), (5, "risk_on"), (4, "cautious"), (0, "cautious"),
        (-1, "risk_off"), (-5, "risk_off"), (-6, "no_trade"), (-10, "no_trade")])
    def test_thresholds_mirror_main_path(self, score, expected):
        assert score_to_regime_value(score) == expected

    def test_vix_uncertain_downgrade(self):
        """RFC-060: vix_uncertain 时 RISK_ON 降 CAUTIOUS (影子须同规则)。"""
        assert score_to_regime_value(8, vix_uncertain=True) == "cautious"
        assert score_to_regime_value(4, vix_uncertain=True) == "cautious"  # 非 risk_on 不再降


class TestF14ThesisArchetype:
    def test_archetype_overrides_factor_guess(self):
        """矛盾实例修复: macd_golden_cross 因子猜 'breakout', 但 archetype=PULLBACK_LIMIT
        (执行语义=等回踩限价) ⇒ thesis 必须跟 archetype 同源。"""
        factors = {"above_ma20": True, "macd_golden_cross": True}
        assert _select_thesis_template(factors, "BUY", entry_archetype="PULLBACK_LIMIT") == "trend_pullback"
        assert _select_thesis_template(factors, "BUY", entry_archetype="BREAKOUT") == "breakout"

    def test_archetype_none_falls_back_to_factors(self):
        """archetype 缺失 (规则通道/旧数据) ⇒ 因子映射逐条不回归 (与 R4 版行为一致)。"""
        assert _select_thesis_template({"macd_golden_cross": True}, "BUY") == "breakout"
        assert _select_thesis_template({"rsi_oversold": True}, "BUY") == "trend_pullback"
        assert _select_thesis_template({"insider_cluster_buy": True}, "BUY") == "insider_confidence"
        assert _select_thesis_template({}, "BUY") == "momentum_continuation"

    def test_short_side_direction_guard(self):
        """空头侧: FADE_SHORT→fade_short; 多头 archetype 落空头单也不得输出多头文案。"""
        assert _select_thesis_template({"macd_golden_cross": True}, "SELL_SHORT",
                                       entry_archetype="FADE_SHORT") == "fade_short"
        assert _select_thesis_template({"macd_golden_cross": True}, "SELL_SHORT",
                                       entry_archetype="PULLBACK_LIMIT") == "fade_short"
        assert _select_thesis_template({"macd_golden_cross": True}, "SELL_SHORT",
                                       entry_archetype="BREAKOUT") == "trend_short"

    def test_fade_short_on_buy_warns_and_falls_back(self, caplog):
        """矛盾守卫: BUY 单带 FADE_SHORT archetype = 数据矛盾, 不得静默落空头文案。"""
        import logging
        with caplog.at_level(logging.WARNING):
            out = _select_thesis_template({"macd_golden_cross": True}, "BUY",
                                          entry_archetype="FADE_SHORT")
        assert out == "breakout"  # 因子兜底
        assert "矛盾" in caplog.text

    def test_skip_short_circuit(self):
        assert _select_thesis_template({"macd_golden_cross": True}, "SKIP",
                                       entry_archetype="BREAKOUT") == "skip"

    def test_lowercase_archetype_normalized(self):
        assert _select_thesis_template({}, "BUY", entry_archetype="pullback_limit") == "trend_pullback"
