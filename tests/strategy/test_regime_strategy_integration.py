"""
Regime × Strategy 集成测试 (P2, 2026-06-17)

覆盖场景:
  1. CAUTION regime + ORB 信号 → 放行但门槛提高
  2. BEAR regime + LONG 信号 → 被方向门禁拦截
  3. CRISIS regime → 所有策略阻止
  4. IC SUSPENDED 策略 → 阻止信号生成
  5. Regime 滞回 (Hysteresis) → 边界震荡不切换
  6. Rapid regime change → 绕过滞回立即收紧
"""
import pytest
from unittest.mock import patch, MagicMock

from app.strategy.regime_filter import (
    evaluate_regime, evaluate_regime_with_hysteresis,
    check_rapid_regime_change, Regime, RegimeVerdict,
    RegimeHysteresis, get_regime_hysteresis,
)


# ═══════════════════════════════════════════════════════════════════
#  1. Regime 基础判定测试
# ═══════════════════════════════════════════════════════════════════

class TestRegimeEvaluation:
    """evaluate_regime 基础判定"""

    def test_strong_bull(self):
        """SPY>MA200 + VIX<18 + 宽度健康 → STRONG_BULL"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=15,
            oversold_count=0, scanned_count=50,
        )
        assert v.regime == Regime.STRONG_BULL
        assert v.position_scale == 1.0
        assert v.min_strength == 30
        assert v.min_confidence == 0.35
        assert "LONG" in v.allowed_directions

    def test_bull(self):
        """SPY>MA200 + VIX 18-25 → BULL"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=22,
            oversold_count=1, scanned_count=50,
        )
        assert v.regime == Regime.BULL
        assert v.position_scale == 0.80

    def test_caution_by_vix(self):
        """VIX>=25 → CAUTION"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=28,
            oversold_count=0, scanned_count=50,
        )
        assert v.regime == Regime.CAUTION
        assert v.min_strength == 55
        assert v.min_confidence == 0.55

    def test_caution_by_spy(self):
        """SPY<MA200 → CAUTION (或更差)"""
        v = evaluate_regime(
            spy_price=480, spy_ma200=500, vix=20,
            oversold_count=1, scanned_count=50,
        )
        assert v.regime == Regime.CAUTION

    def test_bear(self):
        """SPY<MA200 + VIX>25 → BEAR"""
        v = evaluate_regime(
            spy_price=450, spy_ma200=500, vix=28,
            oversold_count=2, scanned_count=50,
        )
        assert v.regime == Regime.BEAR
        assert v.position_scale == 0.25
        assert v.allowed_directions == ["SHORT"]
        assert not v.allow_momentum

    def test_crisis(self):
        """VIX>=35 → CRISIS, 全面停止"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=38,
            oversold_count=0, scanned_count=50,
        )
        assert v.regime == Regime.CRISIS
        assert v.position_scale == 0.0
        assert v.allowed_directions == []
        assert not v.allow_momentum
        assert not v.allow_orb
        assert v.min_strength == 99

    def test_breadth_panic_overrides_to_bear(self):
        """oversold>=6 → BEAR (即使 SPY>MA200)"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=20,
            oversold_count=7, scanned_count=50,
        )
        assert v.regime == Regime.BEAR


# ═══════════════════════════════════════════════════════════════════
#  2. Regime 方向门禁测试
# ═══════════════════════════════════════════════════════════════════

class TestRegimeDirectionGate:
    """BEAR regime 下 LONG 应被拦截, SHORT 放行"""

    def test_bear_blocks_long(self):
        """BEAR: allowed_directions=["SHORT"], LONG 被阻止"""
        v = evaluate_regime(
            spy_price=450, spy_ma200=500, vix=28,
        )
        assert v.blocks_direction("LONG")
        assert not v.blocks_direction("SHORT")

    def test_caution_allows_both(self):
        """CAUTION: allowed_directions=["LONG","SHORT"]"""
        v = evaluate_regime(
            spy_price=480, spy_ma200=500, vix=20,
        )
        assert not v.blocks_direction("LONG")
        assert not v.blocks_direction("SHORT")

    def test_crisis_blocks_all(self):
        """CRISIS: allowed_directions=[], 全部阻止"""
        v = evaluate_regime(spy_price=550, spy_ma200=500, vix=40)
        assert v.blocks_direction("LONG")
        assert v.blocks_direction("SHORT")


# ═══════════════════════════════════════════════════════════════════
#  3. Regime 策略准入测试
# ═══════════════════════════════════════════════════════════════════

class TestRegimeStrategyAccess:
    """不同 Regime 下策略的准入控制"""

    def test_crisis_blocks_momentum_and_orb(self):
        """CRISIS: momentum 和 ORB 都被阻止"""
        v = evaluate_regime(spy_price=550, spy_ma200=500, vix=40)
        assert v.blocks("TECHNICAL_MULTI_FACTOR")
        assert v.blocks("orb")
        assert not v.blocks("BATTLE_PLAN_FAST_PATH")  # 快车道永远放行

    def test_bear_blocks_momentum(self):
        """BEAR: momentum 被阻止, 快车道+内部人+分析师放行"""
        v = evaluate_regime(spy_price=450, spy_ma200=500, vix=28)
        assert v.blocks("TECHNICAL_MULTI_FACTOR")
        assert v.blocks("orb")
        assert not v.blocks("BATTLE_PLAN_FAST_PATH")
        assert not v.blocks("INSIDER_CLUSTER_BUY")
        assert not v.blocks("EARNINGS_REVISION_MOMENTUM")

    def test_trend_first_always_blocked(self):
        """trend_first: 所有 regime 下都被阻止 (IC=-0.24)"""
        for vix in [15, 22, 28, 40]:
            v = evaluate_regime(spy_price=550, spy_ma200=500, vix=vix)
            assert v.blocks("trend_first"), f"trend_first should be blocked at VIX={vix}"

    def test_strong_bull_allows_all_active_strategies(self):
        """STRONG_BULL: 所有活跃策略放行"""
        v = evaluate_regime(
            spy_price=550, spy_ma200=500, vix=15,
            oversold_count=0, scanned_count=50,
        )
        assert not v.blocks("TECHNICAL_MULTI_FACTOR")
        assert not v.blocks("orb")
        assert not v.blocks("BATTLE_PLAN_FAST_PATH")
        assert not v.blocks("INSIDER_CLUSTER_BUY")
        assert not v.blocks("EARNINGS_REVISION_MOMENTUM")


# ═══════════════════════════════════════════════════════════════════
#  4. Regime 滞回 (Hysteresis) 测试
# ═══════════════════════════════════════════════════════════════════

class TestRegimeHysteresis:
    """Regime 滞回 — 边界震荡不应导致频繁切换"""

    def setup_method(self):
        """每个测试前重置单例状态"""
        get_regime_hysteresis().reset()

    def test_first_call_adopts_raw(self):
        """首次调用直接采用原始判定"""
        v = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=15,
            oversold_count=0, scanned_count=50,
        )
        assert v.regime == Regime.STRONG_BULL

    def test_boundary_oscillation_does_not_switch(self):
        """VIX 在 24-26 之间震荡 → BULL 不切换到 CAUTION"""
        h = get_regime_hysteresis()
        h.reset()

        # 第 1 次: BULL (VIX=22)
        v1 = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=22,
            oversold_count=0, scanned_count=50,
        )
        assert v1.regime == Regime.BULL

        # 第 2 次: VIX=25.5 → raw=CAUTION, 但滞回保持 BULL (count=1/2)
        v2 = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=25.5,
            oversold_count=0, scanned_count=50,
        )
        assert v2.regime == Regime.BULL  # 滞回保持

        # 第 3 次: VIX=24 → raw=BULL → 重置候选计数
        v3 = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=24,
            oversold_count=0, scanned_count=50,
        )
        assert v3.regime == Regime.BULL

    def test_sustained_caution_triggers_switch(self):
        """连续 2 次 CAUTION → 切换到 CAUTION"""
        h = get_regime_hysteresis()
        h.reset()

        # 初始: BULL
        evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=22,
            oversold_count=0, scanned_count=50,
        )

        # 第 1 次 CAUTION (count=1/2) → 保持 BULL
        v1 = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=28,
            oversold_count=0, scanned_count=50,
        )
        assert v1.regime == Regime.BULL  # 滞回保持

        # 第 2 次 CAUTION (count=2/2) → 切换!
        v2 = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=27,
            oversold_count=0, scanned_count=50,
        )
        assert v2.regime == Regime.CAUTION  # 切换确认

    def test_crisis_bypasses_hysteresis(self):
        """CRISIS: 立即切换, 不走滞回"""
        h = get_regime_hysteresis()
        h.reset()

        # 初始: BULL
        evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=22,
            oversold_count=0, scanned_count=50,
        )

        # VIX 暴涨到 40 → CRISIS, 立即切换
        v = evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=40,
            oversold_count=0, scanned_count=50,
        )
        assert v.regime == Regime.CRISIS  # 立即切换

    def test_bear_bypasses_hysteresis(self):
        """BEAR: 立即切换, 不走滞回"""
        h = get_regime_hysteresis()
        h.reset()

        # 初始: BULL
        evaluate_regime_with_hysteresis(
            spy_price=550, spy_ma200=500, vix=22,
            oversold_count=0, scanned_count=50,
        )

        # SPY<MA200 + VIX>25 → BEAR, 立即切换
        v = evaluate_regime_with_hysteresis(
            spy_price=450, spy_ma200=500, vix=28,
            oversold_count=2, scanned_count=50,
        )
        assert v.regime == Regime.BEAR  # 立即切换


# ═══════════════════════════════════════════════════════════════════
#  5. 快速 Regime 切换检测测试
# ═══════════════════════════════════════════════════════════════════

class TestRapidRegimeChange:
    """check_rapid_regime_change: VIX 急涨 / SPY 急跌"""

    def test_vix_spike_detected(self):
        """VIX 5分钟涨幅 >15% → CAUTION"""
        result = check_rapid_regime_change(
            vix_history=[20.0, 20.5, 21.0, 22.0, 23.5],
            spy_history=[550, 549, 548, 548, 547],
        )
        assert result == "CAUTION"

    def test_spy_drop_detected(self):
        """SPY 5分钟跌幅 >1% → BEAR"""
        result = check_rapid_regime_change(
            vix_history=[20.0, 20.2, 20.3],
            spy_history=[550.0, 548.0, 544.0],  # -1.09%
        )
        assert result == "BEAR"

    def test_no_change(self):
        """正常波动 → None"""
        result = check_rapid_regime_change(
            vix_history=[20.0, 20.1, 20.2],
            spy_history=[550.0, 549.8, 549.5],
        )
        assert result is None

    def test_empty_history(self):
        """空历史 → None"""
        result = check_rapid_regime_change(
            vix_history=[], spy_history=[],
        )
        assert result is None

    def test_single_point(self):
        """单点历史 → None (无法计算变化)"""
        result = check_rapid_regime_change(
            vix_history=[20.0], spy_history=[550.0],
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════
#  6. Strategy Health Monitor 集成测试
# ═══════════════════════════════════════════════════════════════════

class TestStrategyHealthIntegration:
    """IC 三级响应机制"""

    def test_active_strategy_allows_signal(self):
        """ACTIVE 策略允许信号生成"""
        from app.risk.strategy_health_monitor import (
            StrategyHealthMonitor, StrategyStatus,
        )
        monitor = StrategyHealthMonitor()
        monitor._strategy_states["test_strategy"] = StrategyStatus.ACTIVE
        allowed, reason = monitor.should_allow_signal("test_strategy")
        assert allowed

    def test_reduced_strategy_allows_signal_with_scale(self):
        """REDUCED 策略允许信号但仓位缩减"""
        from app.risk.strategy_health_monitor import (
            StrategyHealthMonitor, StrategyStatus,
        )
        monitor = StrategyHealthMonitor()
        monitor._strategy_states["test_strategy"] = StrategyStatus.REDUCED
        allowed, reason = monitor.should_allow_signal("test_strategy")
        assert allowed  # REDUCED 不阻止, 只缩仓
        assert monitor.get_position_scale("test_strategy") == 0.5

    def test_suspended_strategy_blocks_when_flag_on(self):
        """SUSPENDED + ic_hard_blocking=true → 阻止信号"""
        from app.risk.strategy_health_monitor import (
            StrategyHealthMonitor, StrategyStatus,
        )
        monitor = StrategyHealthMonitor()
        monitor._strategy_states["test_strategy"] = StrategyStatus.SUSPENDED

        with patch("app.core.feature_flags.FeatureFlags.is_enabled", return_value=True):
            allowed, reason = monitor.should_allow_signal("test_strategy")
            assert not allowed

    def test_suspended_strategy_warns_when_flag_off(self):
        """SUSPENDED + ic_hard_blocking=false → 放行但警告"""
        from app.risk.strategy_health_monitor import (
            StrategyHealthMonitor, StrategyStatus,
        )
        monitor = StrategyHealthMonitor()
        monitor._strategy_states["test_strategy"] = StrategyStatus.SUSPENDED

        with patch("app.core.feature_flags.FeatureFlags.is_enabled", return_value=False):
            allowed, reason = monitor.should_allow_signal("test_strategy")
            assert allowed
            assert reason == "warn_mode"

    def test_new_strategy_forced_active(self):
        """新策略 (<30 信号) 强制 ACTIVE"""
        from app.risk.strategy_health_monitor import StrategyHealthMonitor
        monitor = StrategyHealthMonitor()

        with patch("app.analysis.signal_ic_tracker.get_ic_summary") as mock_ic:
            mock_ic.return_value = {
                "status": "ok",
                "by_strategy": {
                    "new_strat": {"ic": -0.10, "pval": 0.5, "n": 5},
                },
            }
            monitor.evaluate_all_strategies(force=True)
            status, _ = monitor.get_strategy_status("new_strat")
            from app.risk.strategy_health_monitor import StrategyStatus
            assert status == StrategyStatus.ACTIVE

    def test_at_least_one_active_safety(self):
        """安全约束: 至少保留 1 个 ACTIVE"""
        from app.risk.strategy_health_monitor import (
            StrategyHealthMonitor, StrategyStatus,
        )
        monitor = StrategyHealthMonitor()

        with patch("app.analysis.signal_ic_tracker.get_ic_summary") as mock_ic:
            mock_ic.return_value = {
                "status": "ok",
                "by_strategy": {
                    "strat_a": {"ic": -0.20, "pval": 0.9, "n": 50},
                    "strat_b": {"ic": -0.15, "pval": 0.8, "n": 50},
                },
            }
            monitor.evaluate_all_strategies(force=True)
            statuses = monitor.get_all_statuses()
            active_count = sum(1 for s in statuses.values() if s == "active")
            assert active_count >= 1, f"At least 1 ACTIVE required, got {statuses}"


# ═══════════════════════════════════════════════════════════════════
#  7. Signal Quality Scorer 集成测试
# ═══════════════════════════════════════════════════════════════════

class TestSignalQualityIntegration:
    """SignalQualityScorer 5 维评分 + 汇合/矛盾"""

    def test_high_confluence_bonus(self):
        """所有 5 维 >0.6 → 最大汇合奖励"""
        from app.strategy.signal_quality_scorer import SignalQualityScorer

        ctx = MagicMock()
        ctx.symbol = "US.AAPL"
        ctx.price = 200
        ctx.ma5 = 198
        ctx.ma20 = 195
        ctx.ma50 = 190
        ctx.trend = "UP"
        ctx.spy_price = 550
        ctx.spy_ma200 = 500
        ctx.rsi = 45
        ctx.macd = 0.5
        ctx.macd_signal = 0.3
        ctx.slope_5m = 0.5
        ctx.volume_ratio = 2.5
        ctx.vwap_deviation = 0.01
        ctx.volume = 1000000
        ctx.avg_volume = 500000
        ctx.order_flow_imbalance = 0.4
        ctx.ticker_buy_sell_ratio = 1.2
        ctx.metadata = {"large_order_ratio": 0.6}
        ctx.vix = 15
        ctx.market_regime = "TREND_UP"

        result = SignalQualityScorer.score(ctx, "LONG")
        assert result.confidence > 0.6
        assert result.confluence_bonus >= 0.10  # 4+ dims agreeing

    def test_contradiction_penalty(self):
        """多维 <0.3 → 矛盾惩罚"""
        from app.strategy.signal_quality_scorer import SignalQualityScorer

        ctx = MagicMock()
        ctx.symbol = "US.BAD"
        ctx.price = 50
        ctx.ma5 = 55    # 价格在 MA5 下方
        ctx.ma20 = 60   # 空头排列
        ctx.ma50 = 65
        ctx.trend = "DOWN"
        ctx.spy_price = 550
        ctx.spy_ma200 = 500
        ctx.rsi = 75    # 超买 (对 LONG 不利)
        ctx.macd = -0.1
        ctx.macd_signal = 0.1
        ctx.slope_5m = -1.0
        ctx.volume_ratio = 0.3  # 缩量
        ctx.vwap_deviation = -0.02
        ctx.volume = 100000
        ctx.avg_volume = 500000
        ctx.order_flow_imbalance = -0.4
        ctx.ticker_buy_sell_ratio = 0.7
        ctx.metadata = {}
        ctx.vix = 15
        ctx.market_regime = "TREND_UP"

        result = SignalQualityScorer.score(ctx, "LONG")
        assert result.contradiction_penalty < 0  # 矛盾惩罚
        assert result.confidence < 0.4  # 低质量 LONG 信号

    def test_blend_with_strategy_confidence(self):
        """strategy_confidence 存在时 60/40 混合"""
        from app.strategy.signal_quality_scorer import SignalQualityScorer

        ctx = MagicMock()
        ctx.symbol = "US.TEST"
        ctx.price = 100
        ctx.ma5 = 100
        ctx.ma20 = 100
        ctx.ma50 = 100
        ctx.trend = ""
        ctx.spy_price = 0
        ctx.spy_ma200 = 0
        ctx.rsi = 50
        ctx.macd = 0
        ctx.macd_signal = 0
        ctx.slope_5m = 0
        ctx.volume_ratio = 1.0
        ctx.vwap_deviation = None
        ctx.volume = 0
        ctx.avg_volume = 0
        ctx.order_flow_imbalance = None
        ctx.ticker_buy_sell_ratio = None
        ctx.metadata = {}
        ctx.vix = 20
        ctx.market_regime = ""

        result = SignalQualityScorer.score(ctx, "LONG", strategy_confidence=0.80)
        # 混合: 60% * 0.80 + 40% * quality_score
        assert result.detail.get("blended") is True
        assert 0.3 < result.confidence < 0.9
