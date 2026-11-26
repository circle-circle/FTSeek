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


class TestRegimeReadCaliber:
    @pytest.fixture(autouse=True)
    def isolated(self, monkeypatch):
        import app.playbook.config as config
        MarketRegimeGate._assess_cache = None
        monkeypatch.setattr(config, "get_playbook_config", lambda: Mock(get=lambda *args: True))
        yield
        MarketRegimeGate._assess_cache = None

    def test_actual_router_basis_and_proxy_receipt(self, monkeypatch):
        from datetime import datetime, timedelta
        from app.data.market_data_router import MarketDataRouter
        from app.data.vix_source import VIX_READ_RECEIPT
        monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window", lambda: True)
        router = MarketDataRouter.__new__(MarketDataRouter)
        router.futu = Mock()
        router.futu.get_quote.side_effect = lambda symbol: {
            "price": 550.0 if symbol == "US.SPY" else 24.0,
            "update_time": "2026-11-25 10:00:00",
        }
        router._spy_ma200_cache = 520.0
        router._spy_ma20_cache = 545.0
        router._spy_ma50_cache = 540.0
        router._spy_ma200_cache_time = datetime.now()
        router._spy_ma200_cache_ttl = timedelta(minutes=30)
        av = Mock()
        router.av = av
        ctx = MarketRegimeGate(router, av).assess(publish_cache=False)
        assert ctx.spy_price_basis == "futu_last_price"
        assert ctx.spy_quote_time == "2026-11-25 10:00:00"
        assert ctx.vix == 20.4
        assert ctx.vix_observation["value"] == 20.4
        assert ctx.vix_observation["source"] == "vixy_proxy_live"
        assert ctx.vix_observation["unit"] == "vix_points_estimated"
        assert ctx.vix_observation["is_proxy"] is True
        assert ctx.vix_observation["as_of"] is None
        assert ctx.vix_observation["quote_time"] == "2026-11-25 10:00:00"
        assert ctx.trend_score == 5 and ctx.volatility_score == pytest.approx(0.4)
        assert ctx.score == 5.4 and ctx.regime == MarketRegime.RISK_ON
        av.get_spy_context.assert_not_called()
        av.get_vix_or_none.assert_not_called()
        assert VIX_READ_RECEIPT.get() is None
        assert MarketRegimeGate._assess_cache is None

    def test_av_ma_fallback_does_not_relabel_router_price(self, mock_router, mock_av):
        mock_router.get_spy_context.return_value.update(
            spy_price_basis="futu_last_price", spy_quote_time="raw-provider-time")
        mock_av.get_spy_context.return_value.update(spy_price_basis="other")
        ctx = MarketRegimeGate(mock_router, mock_av).assess()
        assert ctx.spy_price_basis == "futu_last_price"
        assert ctx.spy_quote_time == "raw-provider-time"
        assert ctx.vix_observation["source"] == "unknown"
        assert ctx.vix_observation["is_proxy"] is None
        assert ctx.vix_observation["unit"] == "unknown"
        assert ctx.vix_observation["quote_time"] is None

    @pytest.mark.parametrize("vix", [14.9, 15, 20, 20.01, 21.99, 22, 29.99, 30])
    @pytest.mark.parametrize("up", [False, True])
    def test_components_preserve_original_policy(self, mock_router, mock_av, vix, up):
        from app.playbook.market_regime import score_to_regime_value, vix_score
        mock_router.get_spy_context.return_value = {
            "spy_price": 550 if up else 490, "spy_ma20": 540,
            "spy_ma50": 530 if up else 560, "spy_ma200": 520,
        }
        mock_router.get_vix_or_none.return_value = vix
        ctx = MarketRegimeGate(mock_router, mock_av).assess()
        points = vix_score(vix, smooth_20_22=True)
        assert ctx.trend_score == (5 if up else -5)
        assert ctx.volatility_score == points
        assert ctx.score == round(ctx.trend_score + points, 4)
        assert ctx.regime.value == score_to_regime_value(ctx.score)
        assert (ctx.top_n, ctx.position_multiplier, ctx.min_strength) == {
            "risk_on": (10, 1.0, 50), "cautious": (8, 0.5, 60),
            "risk_off": (3, 0.3, 70), "no_trade": (0, 0.0, 999),
        }[ctx.regime.value]

    def test_missing_is_not_a_twenty_point_observation(self, monkeypatch, mock_router, mock_av):
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        mock_router.get_spy_context.return_value = {
            "spy_price": 490, "spy_ma20": 540, "spy_ma50": 560, "spy_ma200": 520,
        }
        mock_router.get_vix_or_none.return_value = None
        mock_av.get_vix_or_none.return_value = None
        ctx = MarketRegimeGate(mock_router, mock_av).assess()
        assert ctx.vix == 20.0 and ctx.vix_uncertain is True
        assert ctx.vix_observation["value"] is None
        assert ctx.vix_observation["source"] == "unavailable"
        assert ctx.trend_score == -5 and ctx.volatility_score is None
        assert ctx.score == -5 and ctx.regime == MarketRegime.RISK_OFF
        assert mock_router.get_vix_or_none.call_count == 2
        assert mock_av.get_vix_or_none.call_count == 2

    def test_cache_retains_assessment_time(self, mock_router, mock_av):
        gate = MarketRegimeGate(mock_router, mock_av)
        first = gate.assess()
        second = gate.assess(publish_cache=False)
        assert second is first
        assert second.assessed_at == first.assessed_at
        assert second.vix_observation["read_at"] == first.vix_observation["read_at"]
        mock_router.get_vix_or_none.assert_called_once()

    def test_receipt_resets_on_exception_and_nested_reads(self):
        from types import SimpleNamespace
        from app.data.vix_source import VIX_READ_RECEIPT, record_vix_read
        from app.playbook.market_regime import _read_vix_with_receipt
        outer = {}
        token = VIX_READ_RECEIPT.set(outer)
        try:
            def failed():
                record_vix_read(19, "fred", as_of="2026-11-20")
                raise RuntimeError("read failed")
            with pytest.raises(RuntimeError, match="read failed"):
                _read_vix_with_receipt(SimpleNamespace(get_vix_or_none=failed))
            assert VIX_READ_RECEIPT.get() is outer and outer == {}
            value, receipt = _read_vix_with_receipt(SimpleNamespace(get_vix_or_none=lambda: 18))
            assert value == 18 and receipt["source"] == "unknown"
            assert VIX_READ_RECEIPT.get() is outer
        finally:
            VIX_READ_RECEIPT.reset(token)

    def test_concurrent_receipts_do_not_cross_sources(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from types import SimpleNamespace
        from app.data.vix_source import record_vix_read
        from app.playbook.market_regime import _read_vix_with_receipt
        barrier = Barrier(2)
        def read(value, source):
            def fetch():
                record_vix_read(value, source, as_of="2026-11-20")
                barrier.wait(timeout=2)
                return value
            return _read_vix_with_receipt(SimpleNamespace(get_vix_or_none=fetch))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(read, 18, "fred")
            b = pool.submit(read, 21, "vixy_proxy_live")
            assert a.result()[1]["source"] == "fred"
            assert b.result()[1]["source"] == "vixy_proxy_live"

    @pytest.mark.parametrize("phase,source", [
        ("TRADING", "finnhub_vixy"), ("TRADING", "fred"),
        ("WEEKEND", "fred"), ("WEEKEND", "finnhub_vixy"),
    ])
    def test_av_cache_preserves_original_observation(self, monkeypatch, phase, source):
        from datetime import datetime
        from app.core.scheduler_triggers import MarketPhase
        from app.data.alphavantage_provider import AlphaVantageProvider
        from app.playbook.market_regime import _read_vix_with_receipt
        monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: MarketPhase[phase])
        av = AlphaVantageProvider.__new__(AlphaVantageProvider)
        slot = "vix" if source == "finnhub_vixy" else "vix_fred"
        av.cache = {slot: {"vix": 18.5, "source": source, "as_of": "2026-11-20",
                           "quote_time": 100 if source == "finnhub_vixy" else None}}
        av.cache_time = {slot: datetime.now()}
        monkeypatch.setattr(av, "_fetch_live_vix", Mock(return_value=None))
        monkeypatch.setattr(av, "_fetch_fred_vix", Mock(return_value=None))
        value, receipt = _read_vix_with_receipt(av)
        assert value == 18.5
        assert receipt["source"] == ("finnhub_vixy_proxy" if source == "finnhub_vixy" else "fred")
        assert receipt["as_of"] == "2026-11-20"
        assert receipt["unit"] == ("vix_points_estimated" if source == "finnhub_vixy" else "vix_points")
        assert receipt["quote_time_basis"] == "provider_raw"
        assert receipt["quote_time"] == ("100" if source == "finnhub_vixy" else None)
        assert (av._fetch_live_vix.call_count + av._fetch_fred_vix.call_count) <= 1

    @pytest.mark.parametrize("publish", [True, False])
    @pytest.mark.parametrize("source", ["fred", "finnhub_vixy"])
    def test_av_actual_fetch_receipt_without_cache_publication(self, monkeypatch, source, publish):
        from types import SimpleNamespace
        from app.core.scheduler_triggers import MarketPhase
        from app.data.alphavantage_provider import AlphaVantageProvider
        from app.data.quote_cache_policy import DATA_CACHE_PUBLICATION_ALLOWED
        from app.playbook.market_regime import _read_vix_with_receipt
        monkeypatch.setenv("FINNHUB_API_KEY", "test-only")
        monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: (
            MarketPhase.TRADING if source == "finnhub_vixy" else MarketPhase.WEEKEND))
        response = SimpleNamespace(status_code=200, text="DATE,VIXCLS\n2026-11-20,18.7\n",
                                   raise_for_status=lambda: None, json=lambda: {"c": 22, "t": 1795188600})
        http = Mock(return_value=response)
        monkeypatch.setattr("requests.get", http)
        av = AlphaVantageProvider.__new__(AlphaVantageProvider)
        av.cache, av.cache_time = {}, {}
        token = DATA_CACHE_PUBLICATION_ALLOWED.set(publish)
        try:
            value, receipt = _read_vix_with_receipt(av)
        finally:
            DATA_CACHE_PUBLICATION_ALLOWED.reset(token)
        assert value == 18.7
        assert receipt["source"] == ("fred" if source == "fred" else "finnhub_vixy_proxy")
        assert receipt["as_of"] is not None
        assert receipt["quote_time"] == (None if source == "fred" else "1795188600")
        assert bool(av.cache) is publish and bool(av.cache_time) is publish
        http.assert_called_once()


class TestRegimeAuditCardinality:
    @pytest.mark.parametrize("n,verdict", [(0, "INSUFFICIENT"), (9, "INSUFFICIENT"),
                                           (10, "WARNING_CONST"), (11, "WARNING_CONST")])
    def test_threshold_and_missing_stages(self, n, verdict):
        from scripts.shadow_field_cardinality import regime_audit_cardinality
        rows = [{"regime_reads": {"preflight": {"trend_score": -5}}} for _ in range(n)]
        report = regime_audit_cardinality(rows)
        field = next(r for r in report["fields"] if r["stage"] == "preflight" and r["field"] == "trend_score")
        assert field["n"] == n and field["unique"] == (1 if n else 0)
        assert field["verdict"] == verdict and field["unit"] == "score_points"
        missing = next(r for r in report["fields"] if r["stage"] == "sizing" and r["field"] == "trend_score")
        assert missing["n"] == 0 and missing["missing"] == n
        assert report["record_only"] is True

    def test_variation_and_observation_units(self):
        from scripts.shadow_field_cardinality import regime_audit_cardinality
        rows = [{"regime_reads": {"sizing": {
            "trend_score": i % 2,
            "vix_observation": {"value": None, "unit": "unknown", "source": "unavailable"},
        }}} for i in range(10)]
        report = regime_audit_cardinality(rows)
        fields = {r["field"]: r for r in report["fields"] if r["stage"] == "sizing"}
        assert fields["trend_score"]["unique"] == 2 and fields["trend_score"]["verdict"] == "OK"
        assert fields["vix_observation.value"]["n"] == 0
        assert fields["vix_observation.value"]["unit"] == "vix_observation.unit"
        assert fields["vix_observation.quote_time"]["unit"] == "provider_raw"
        assert fields["vix_observation.read_at"]["unit"] == "utc_iso8601"

    def test_explicit_cli_is_read_only_and_warning_exits_zero(self, monkeypatch, tmp_path, capsys):
        import json
        from scripts import shadow_field_cardinality as audit
        path = tmp_path / "sizing.jsonl"
        row = {"regime_reads": {"preflight": {"regime": "risk_off"}}}
        path.write_text("\n".join(json.dumps(row) for _ in range(10)))
        collect = Mock(side_effect=AssertionError("legacy database collection must not run"))
        monkeypatch.setattr(audit, "collect", collect)
        monkeypatch.setattr("sys.argv", ["audit", "--regime-audit", str(path)])
        assert audit.main() == 0
        report = json.loads(capsys.readouterr().out)
        assert report["row_count"] == 10
        assert any(r["verdict"] == "WARNING_CONST" for r in report["fields"])
        collect.assert_not_called()

    def test_default_cli_keeps_legacy_collection(self, monkeypatch, capsys):
        import json
        from scripts import shadow_field_cardinality as audit
        collect = Mock(return_value=[])
        monkeypatch.setattr(audit, "collect", collect)
        monkeypatch.setattr("sys.argv", ["audit", "--days", "7", "--json"])
        assert audit.main() == 0
        assert json.loads(capsys.readouterr().out) == []
        collect.assert_called_once_with(7)
