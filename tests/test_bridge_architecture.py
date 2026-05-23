"""
策略路由器 + 市场上下文 + 管线适配器 单元测试

覆盖:
1. MarketContextProvider: VIX分级, 资本水位, 趋势判断, 交易门禁
2. StrategyRouter: 配置解析, 链路切换, 安全降级
3. PipelineAdapter: 拦截逻辑, 格式转换
4. RiskGateway: 三层门禁合并

版本: 1.0 | 2026-05-23
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# ============================================================
#  MarketContext Tests
# ============================================================
class TestVixRegime:
    def test_euphoria(self):
        from app.harness.market_context import classify_vix_regime, VixRegime
        assert classify_vix_regime(8) == VixRegime.EUPHORIA
        assert classify_vix_regime(11.9) == VixRegime.EUPHORIA

    def test_normal(self):
        from app.harness.market_context import classify_vix_regime, VixRegime
        assert classify_vix_regime(12) == VixRegime.NORMAL
        assert classify_vix_regime(19.9) == VixRegime.NORMAL

    def test_caution(self):
        from app.harness.market_context import classify_vix_regime, VixRegime
        assert classify_vix_regime(20) == VixRegime.CAUTION
        assert classify_vix_regime(24.9) == VixRegime.CAUTION

    def test_fear(self):
        from app.harness.market_context import classify_vix_regime, VixRegime
        assert classify_vix_regime(30) == VixRegime.FEAR

    def test_crisis(self):
        from app.harness.market_context import classify_vix_regime, VixRegime
        assert classify_vix_regime(40) == VixRegime.CRISIS
        assert classify_vix_regime(80) == VixRegime.CRISIS


class TestCapitalLevel:
    def test_safe(self):
        from app.harness.market_context import classify_capital_level, CapitalLevel
        level, scale = classify_capital_level(-0.02)
        assert level == CapitalLevel.SAFE
        assert scale == 1.0

    def test_warning(self):
        from app.harness.market_context import classify_capital_level, CapitalLevel
        level, scale = classify_capital_level(-0.07)
        assert level == CapitalLevel.WARNING
        assert scale == 0.5

    def test_alert(self):
        from app.harness.market_context import classify_capital_level, CapitalLevel
        level, scale = classify_capital_level(-0.15)
        assert level == CapitalLevel.ALERT
        assert scale == 0.25

    def test_danger(self):
        from app.harness.market_context import classify_capital_level, CapitalLevel
        level, scale = classify_capital_level(-0.25)
        assert level == CapitalLevel.DANGER
        assert scale == 0.0

    def test_circuit_breaker(self):
        from app.harness.market_context import classify_capital_level, CapitalLevel
        level, scale = classify_capital_level(-0.40)
        assert level == CapitalLevel.CIRCUIT_BREAKER
        assert scale == 0.0


class TestMarketTrend:
    def test_bullish(self):
        from app.harness.market_context import classify_market_trend, MarketTrend
        assert classify_market_trend(550, 500) == MarketTrend.BULLISH

    def test_bearish(self):
        from app.harness.market_context import classify_market_trend, MarketTrend
        assert classify_market_trend(480, 500) == MarketTrend.BEARISH

    def test_neutral(self):
        from app.harness.market_context import classify_market_trend, MarketTrend
        assert classify_market_trend(505, 500) == MarketTrend.NEUTRAL


class TestMarketContext:
    def test_can_trade_normal(self):
        from app.harness.market_context import MarketContext, RiskState, CapitalLevel
        ctx = MarketContext(
            timestamp=datetime.now(),
            vix=15,
            risk_state=RiskState.NORMAL,
            capital_level=CapitalLevel.SAFE,
        )
        assert ctx.can_trade is True

    def test_cannot_trade_high_vix(self):
        from app.harness.market_context import MarketContext, RiskState, CapitalLevel
        ctx = MarketContext(
            timestamp=datetime.now(),
            vix=30,
            risk_state=RiskState.CAUTION,
            capital_level=CapitalLevel.SAFE,
        )
        assert ctx.can_trade is False

    def test_cannot_trade_halt(self):
        from app.harness.market_context import MarketContext, RiskState, CapitalLevel
        ctx = MarketContext(
            timestamp=datetime.now(),
            vix=15,
            risk_state=RiskState.HALT,
            capital_level=CapitalLevel.CIRCUIT_BREAKER,
        )
        assert ctx.can_trade is False

    def test_cannot_trade_emergency(self):
        from app.harness.market_context import MarketContext, RiskState, CapitalLevel
        ctx = MarketContext(
            timestamp=datetime.now(),
            vix=15,
            risk_state=RiskState.NORMAL,
            capital_level=CapitalLevel.SAFE,
            emergency_active=True,
            emergency_reason="flash crash",
        )
        assert ctx.can_trade is False

    def test_vix_allows_trading_none(self):
        from app.harness.market_context import MarketContext
        ctx = MarketContext(timestamp=datetime.now(), vix=None)
        assert ctx.vix_allows_trading is True  # 数据不可用时不阻断

    def test_to_dict(self):
        from app.harness.market_context import MarketContext
        ctx = MarketContext(timestamp=datetime.now(), vix=18)
        d = ctx.to_dict()
        assert "vix" in d
        assert d["vix"] == 18


class TestMarketContextProvider:
    def test_init(self):
        from app.harness.market_context import MarketContextProvider
        provider = MarketContextProvider()
        assert provider is not None

    def test_get_context_returns_market_context(self):
        from app.harness.market_context import MarketContextProvider, MarketContext
        provider = MarketContextProvider()
        ctx = provider.get_context()
        assert isinstance(ctx, MarketContext)

    def test_caching(self):
        from app.harness.market_context import MarketContextProvider
        provider = MarketContextProvider()
        ctx1 = provider.get_context()
        ctx2 = provider.get_context()
        assert ctx1 is ctx2  # 同一缓存实例

    def test_force_refresh(self):
        from app.harness.market_context import MarketContextProvider
        provider = MarketContextProvider()
        ctx1 = provider.get_context()
        ctx2 = provider.get_context(force_refresh=True)
        # 可能相同 (无外部数据源时), 但 timestamp 不同
        assert ctx2.timestamp >= ctx1.timestamp

    def test_check_entry_allowed_normal(self):
        from app.harness.market_context import MarketContextProvider
        provider = MarketContextProvider()
        allowed, reason = provider.check_entry_allowed("AAPL")
        # 无外部数据时应该允许
        assert isinstance(allowed, bool)


# ============================================================
#  StrategyRouter Tests
# ============================================================
class TestPipelineConfig:
    def test_default(self):
        from app.harness.strategy_router import PipelineConfig, PipelineMode
        cfg = PipelineConfig()
        assert cfg.master_switch == PipelineMode.LEGACY

    def test_from_dict(self):
        from app.harness.strategy_router import PipelineConfig, PipelineMode
        cfg = PipelineConfig.from_dict({
            "master_switch": "factor",
            "overrides": {"signal": "legacy", "exit": "factor"},
        })
        assert cfg.master_switch == PipelineMode.FACTOR
        assert cfg.signal_override == "legacy"
        assert cfg.exit_override == "factor"

    def test_resolve_follows_master(self):
        from app.harness.strategy_router import PipelineConfig, PipelineMode
        cfg = PipelineConfig(master_switch=PipelineMode.FACTOR)
        assert cfg.resolve("signal") == "factor"
        assert cfg.resolve("risk") == "factor"

    def test_resolve_override(self):
        from app.harness.strategy_router import PipelineConfig, PipelineMode
        cfg = PipelineConfig(
            master_switch=PipelineMode.FACTOR,
            signal_override="legacy",
        )
        assert cfg.resolve("signal") == "legacy"
        assert cfg.resolve("risk") == "factor"  # no override

    def test_hybrid_defaults(self):
        from app.harness.strategy_router import PipelineConfig, PipelineMode
        cfg = PipelineConfig(master_switch=PipelineMode.HYBRID)
        assert cfg.resolve("signal") == "factor"
        assert cfg.resolve("exit") == "legacy"
        assert cfg.resolve("sizing") == "factor"


class TestStrategyRouter:
    def test_init(self):
        from app.harness.strategy_router import StrategyRouter, PipelineConfig
        router = StrategyRouter(PipelineConfig())
        assert router is not None

    def test_get_status(self):
        from app.harness.strategy_router import StrategyRouter, PipelineConfig, PipelineMode
        cfg = PipelineConfig(master_switch=PipelineMode.SHADOW_ONLY)
        router = StrategyRouter(cfg)
        status = router.get_status()
        assert status["master_switch"] == "shadow_only"

    def test_signal_legacy_no_score(self):
        from app.harness.strategy_router import StrategyRouter, PipelineConfig, PipelineMode
        cfg = PipelineConfig(master_switch=PipelineMode.LEGACY)
        router = StrategyRouter(cfg)
        result = router.route_signal("AAPL", {})
        assert result.allowed is False
        assert "旧链路未产生信号" in result.reason

    def test_signal_legacy_with_score(self):
        from app.harness.strategy_router import StrategyRouter, PipelineConfig, PipelineMode
        cfg = PipelineConfig(master_switch=PipelineMode.LEGACY)
        router = StrategyRouter(cfg)
        mock_score = MagicMock()
        mock_score.direction = "LONG"
        mock_score.strength = 60
        mock_score.confidence = 0.7
        result = router.route_signal("AAPL", {}, signal_score=mock_score)
        assert result.allowed is True
        assert result.direction == "LONG"

    def test_audit_log(self):
        from app.harness.strategy_router import StrategyRouter, PipelineConfig
        router = StrategyRouter(PipelineConfig())
        router.route_signal("AAPL", {})
        log = router.get_audit_log()
        assert len(log) >= 1
        assert log[0]["symbol"] == "AAPL"


# ============================================================
#  RiskGateway Tests
# ============================================================
class TestRiskGateway:
    def test_init(self):
        from app.harness.risk_gateway import RiskGateway
        gw = RiskGateway()
        assert gw is not None

    def test_check_entry_no_data(self):
        from app.harness.risk_gateway import RiskGateway
        gw = RiskGateway()
        decision = gw.check_entry("AAPL")
        assert isinstance(decision.allowed, bool)

    def test_check_exit_no_trigger(self):
        from app.harness.risk_gateway import RiskGateway
        gw = RiskGateway()
        result = gw.check_exit(
            {"symbol": "AAPL", "direction": "LONG", "last_price": 150},
            {"price": 150, "atr": 2.0},
        )
        assert result is None  # 无退出触发

    def test_check_exit_flash_crash(self):
        from app.harness.risk_gateway import RiskGateway
        gw = RiskGateway()
        result = gw.check_exit(
            {"symbol": "AAPL", "direction": "LONG", "last_price": 150},
            {"price": 140, "atr": 2.0},  # 跌10, > 3×2=6
        )
        assert result is not None
        assert result.source == "emergency"
        assert "闪崩" in result.reason

    def test_audit_log(self):
        from app.harness.risk_gateway import RiskGateway
        gw = RiskGateway()
        gw.check_entry("AAPL")
        log = gw.get_audit_log()
        assert len(log) >= 1


# ============================================================
#  PipelineAdapter Tests
# ============================================================
class TestPipelineAdapter:
    def test_init_legacy_mode(self):
        from app.harness.pipeline_adapter import PipelineAdapter
        adapter = PipelineAdapter(scheduler=None)
        # 默认 legacy 模式, router 不初始化
        assert adapter.is_active is False

    def test_intercept_not_active(self):
        from app.harness.pipeline_adapter import PipelineAdapter
        adapter = PipelineAdapter(scheduler=None)
        result = adapter.intercept("AAPL")
        assert result.intercepted is False
        assert result.allowed is True

    def test_get_status(self):
        from app.harness.pipeline_adapter import PipelineAdapter
        adapter = PipelineAdapter(scheduler=None)
        status = adapter.get_status()
        assert "active" in status

    def test_enable_disable(self):
        from app.harness.pipeline_adapter import PipelineAdapter
        adapter = PipelineAdapter(scheduler=None)
        adapter.enable()
        assert adapter._enabled is True
        adapter.disable()
        assert adapter._enabled is False
