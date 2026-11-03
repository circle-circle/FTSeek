"""
MarketRegimeGate 单元测试
"""
import pytest
from unittest.mock import Mock, patch
from app.playbook.market_regime import (
    MarketRegimeGate,
    MarketRegime,
    RegimeContext,
)


@pytest.fixture
def mock_router():
    router = Mock()
    router.get_spy_context.return_value = {
        "spy_price": 550.0,
        "spy_ma200": 520.0,
        "spx_change_percent": 0.5,
    }
    router.get_vix_or_none.return_value = 18.0
    return router


@pytest.fixture
def mock_av():
    av = Mock()
    av.get_spy_context.return_value = {
        "spy_price": 550.0,
        "spy_ma20": 545.0,
        "spy_ma50": 540.0,
        "spx_change_percent": 0.5,
    }
    av.get_vix_or_none.return_value = 18.0
    return av


class TestMarketRegimeGate:
    """测试 MarketRegimeGate"""

    def test_risk_on_high_score(self, mock_router, mock_av):
        """测试 RISK_ON: SPY > MA20 > MA50 > MA200, VIX 低"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma20": 550.0,
            "spy_ma50": 540.0,
            "spx_change_percent": 0.8,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.8,
        }
        mock_router.get_vix_or_none.return_value = 14.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        assert ctx.regime == MarketRegime.RISK_ON
        assert ctx.top_n == 10
        assert ctx.position_multiplier == 1.0
        assert ctx.min_strength == 50
        # score: +2 (SPY>MA20) +2 (MA20>MA50) +1 (SPY>MA200) +3 (VIX<15) = 8
        assert ctx.score == 8

    def test_risk_on_normal_vix(self, mock_router, mock_av):
        """测试 RISK_ON: 趋势向上 + VIX 正常 (15-20)"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 555.0,
            "spy_ma20": 550.0,
            "spy_ma50": 545.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 555.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_vix_or_none.return_value = 17.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        assert ctx.regime == MarketRegime.RISK_ON
        # score: +2 +2 +1 +1 = 6
        assert ctx.score == 6

    def test_cautious_mixed_signals(self, mock_router, mock_av):
        """测试 CAUTIOUS: 混合信号"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 545.0,
            "spy_ma20": 550.0,  # SPY < MA20
            "spy_ma50": 540.0,  # MA20 > MA50
            "spx_change_percent": -0.3,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 545.0,
            "spy_ma200": 520.0,  # SPY > MA200
            "spx_change_percent": -0.3,
        }
        mock_router.get_vix_or_none.return_value = 18.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        assert ctx.regime == MarketRegime.CAUTIOUS
        assert ctx.top_n == 8  # 2026-07-21 P1: cautious top_n 5→8 (market_regime.py 同步)
        assert ctx.position_multiplier == 0.5
        assert ctx.min_strength == 60
        # score: -2 (SPY<MA20) +2 (MA20>MA50) +1 (SPY>MA200) +1 (VIX 15-20) = 2
        assert ctx.score == 2

    def test_risk_off_high_vix(self, mock_router, mock_av):
        """测试 NO_TRADE: 高 VIX + 趋势向下 (score=-7 < -5)"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 500.0,
            "spy_ma20": 520.0,
            "spy_ma50": 530.0,
            "spx_change_percent": -2.0,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 500.0,
            "spy_ma200": 520.0,
            "spx_change_percent": -2.0,
        }
        mock_router.get_vix_or_none.return_value = 25.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        # score: -2 (SPY<MA20) -2 (MA20<MA50) -1 (SPY<MA200) -2 (VIX 20-30) = -7
        assert ctx.regime == MarketRegime.NO_TRADE
        assert ctx.score == -7

    def test_risk_off_moderate_bearish(self, mock_router, mock_av):
        """测试 RISK_OFF: 中等看跌"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 510.0,
            "spy_ma20": 520.0,  # SPY < MA20
            "spy_ma50": 525.0,  # MA20 < MA50
            "spx_change_percent": -1.0,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 510.0,
            "spy_ma200": 500.0,  # SPY > MA200
            "spx_change_percent": -1.0,
        }
        mock_router.get_vix_or_none.return_value = 22.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        assert ctx.regime == MarketRegime.RISK_OFF
        # score: -2 (SPY<MA20) -2 (MA20<MA50) +1 (SPY>MA200) -2 (VIX 20-30) = -5
        assert ctx.score == -5

    def test_no_trade_extreme_vix(self, mock_router, mock_av):
        """测试 NO_TRADE: 极端 VIX"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 450.0,
            "spy_ma20": 500.0,
            "spy_ma50": 520.0,
            "spx_change_percent": -5.0,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 450.0,
            "spy_ma200": 500.0,
            "spx_change_percent": -5.0,
        }
        mock_router.get_vix_or_none.return_value = 35.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        assert ctx.regime == MarketRegime.NO_TRADE
        assert ctx.top_n == 0
        assert ctx.position_multiplier == 0.0
        # score: -2 -2 -1 -5 = -10
        assert ctx.score == -10

    def test_fallback_when_router_fails(self, mock_av):
        """测试 Router 失败时使用 AlphaVantage 降级"""
        mock_router = Mock()
        mock_router.get_spy_context.side_effect = Exception("Router failed")
        mock_router.get_vix_or_none.side_effect = Exception("Router failed")

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        # 应该使用 AV 的数据
        assert ctx.spy_price == 550.0
        assert ctx.vix == 18.0

    def test_default_values_when_all_fail(self):
        """测试所有数据源失败时使用兜底值 + vix_uncertain 标记 (RFC-060)"""
        mock_router = Mock()
        mock_router.get_spy_context.return_value = {}
        mock_router.get_vix_or_none.return_value = None

        mock_av = Mock()
        mock_av.get_spy_context.return_value = {}
        mock_av.get_vix_or_none.return_value = None

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        # RFC-060: VIX 全源失败 → 兜底 20.0 + vix_uncertain, 跳过 VIX 评分
        # 无趋势数据, score=0 → CAUTIOUS
        assert ctx.regime == MarketRegime.CAUTIOUS
        assert ctx.vix == 20.0
        assert ctx.vix_uncertain is True

    def test_to_dict(self, mock_router, mock_av):
        """测试 to_dict() 方法"""
        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()
        d = ctx.to_dict()

        assert isinstance(d, dict)
        assert "regime" in d
        assert "vix" in d
        assert "spy_price" in d
        assert "top_n" in d
        assert "position_multiplier" in d

    def test_no_router_no_av(self):
        """测试无 Router 和 AV 时不崩溃 (密封: 阻断默认实例自动创建)"""
        # 2026-07-11 起构造器会自动创建共享 router/AV 实例; 测试密封为创建失败路径
        # 2026-11-03: AV 构造点已收敛到进程级单例 get_alpha_vantage_provider()
        #   (原每实例 new AlphaVantageProvider)。密封目标随之迁移到新缝 —— 守卫的语义
        #   ("两个默认实例都建不出来时不崩") 未变, 只换了打桩对象。
        with patch("app.playbook.market_regime.get_market_data_router",
                   side_effect=Exception("sealed")), \
             patch("app.playbook.market_regime.get_alpha_vantage_provider",
                   side_effect=Exception("sealed")):
            gate = MarketRegimeGate(None, None)
        ctx = gate.assess()

        # RFC-060: 无任何 VIX 源 → 兜底 20.0 + vix_uncertain
        assert ctx.regime == MarketRegime.CAUTIOUS
        assert ctx.vix == 20.0
        assert ctx.vix_uncertain is True

    def test_vix_boundary_15(self, mock_router, mock_av):
        """测试 VIX=15 边界"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 550.0,
            "spy_ma20": 545.0,
            "spy_ma50": 540.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 550.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_vix_or_none.return_value = 15.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        # VIX=15 → 15-20 range → +1
        # score: +2 +2 +1 +1 = 6
        assert ctx.score == 6

    def test_vix_boundary_20(self, mock_router, mock_av):
        """测试 VIX=20 边界"""
        mock_av.get_spy_context.return_value = {
            "spy_price": 550.0,
            "spy_ma20": 545.0,
            "spy_ma50": 540.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_spy_context.return_value = {
            "spy_price": 550.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.5,
        }
        mock_router.get_vix_or_none.return_value = 20.0

        gate = MarketRegimeGate(mock_router, mock_av)
        ctx = gate.assess()

        # VIX=20 → <=20 range → +1
        # score: +2 +2 +1 +1 = 6
        assert ctx.score == 6


class TestRegimeContext:
    """测试 RegimeContext"""

    def test_regime_context_creation(self):
        """测试 RegimeContext 创建"""
        ctx = RegimeContext(
            regime=MarketRegime.RISK_ON,
            vix=15.0,
            spy_price=550.0,
            spy_ma20=545.0,
            spy_ma50=540.0,
            spy_ma200=520.0,
            spy_change_pct=0.5,
            score=8,
            top_n=10,
            position_multiplier=1.0,
            min_strength=50,
        )

        assert ctx.regime == MarketRegime.RISK_ON
        assert ctx.vix == 15.0
        assert ctx.top_n == 10

    def test_market_regime_enum(self):
        """测试 MarketRegime 枚举"""
        assert MarketRegime.RISK_ON.value == "risk_on"
        assert MarketRegime.CAUTIOUS.value == "cautious"
        assert MarketRegime.RISK_OFF.value == "risk_off"
        assert MarketRegime.NO_TRADE.value == "no_trade"


class TestRouterMAFallback:
    """2026-08-10 回归: MA20/MA50 须优先取自 Router (与 MA200 同源 Futu 日K).

    缺陷: 消费端此前只从 Router 读 spy_ma200, MA20/50 全靠 AlphaVantage —
    AV 免费额度每日耗尽 → MA20/50=0.00 → 趋势分项归零, regime 静默降级
    CAUTIOUS (×0.5)。Router 自 2026-07-03 起已返回 spy_ma20/spy_ma50。
    """

    def setup_method(self):
        MarketRegimeGate._assess_cache = None  # 类级缓存, 跨用例隔离

    def test_router_ma20_ma50_used_when_av_dead(self):
        """AV 额度耗尽 (返回 {}) 时, Router 的 MA20/50 仍让趋势分项生效 → RISK_ON."""
        router = Mock()
        router.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma20": 550.0,
            "spy_ma50": 540.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.8,
        }
        router.get_vix_or_none.return_value = 14.0
        av = Mock()
        av.get_spy_context.return_value = {}   # 额度耗尽的典型返回
        av.get_vix_or_none.return_value = None

        ctx = MarketRegimeGate(router, av).assess()

        assert ctx.spy_ma20 == 550.0
        assert ctx.spy_ma50 == 540.0
        # +2 (SPY>MA20) +2 (MA20>MA50) +1 (SPY>MA200) +3 (VIX<15) = 8
        assert ctx.score == 8
        assert ctx.regime == MarketRegime.RISK_ON
        assert ctx.position_multiplier == 1.0

    def test_av_not_consulted_when_router_has_mas(self):
        """Router 已提供 MA20/50 时不再调 AV (省免费额度)."""
        router = Mock()
        router.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma20": 550.0,
            "spy_ma50": 540.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.8,
        }
        router.get_vix_or_none.return_value = 14.0
        av = Mock()
        av.get_vix_or_none.return_value = 14.0

        MarketRegimeGate(router, av).assess()

        av.get_spy_context.assert_not_called()

    def test_av_still_fallback_when_router_lacks_mas(self):
        """Router 缺 MA20/50 (如日K拉取失败) 时保留 AV fallback 语义."""
        router = Mock()
        router.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma200": 520.0,
            "spx_change_percent": 0.8,
        }
        router.get_vix_or_none.return_value = 14.0
        av = Mock()
        av.get_spy_context.return_value = {
            "spy_price": 560.0,
            "spy_ma20": 550.0,
            "spy_ma50": 540.0,
            "spx_change_percent": 0.8,
        }
        av.get_vix_or_none.return_value = 14.0

        ctx = MarketRegimeGate(router, av).assess()

        av.get_spy_context.assert_called_once()
        assert ctx.spy_ma20 == 550.0
        assert ctx.score == 8
