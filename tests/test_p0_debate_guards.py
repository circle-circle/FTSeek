"""
P0 辩论止血守卫测试 — 基于309笔实盘数据的紧急修复

覆盖:
1. DynamicRiskExit: 期权禁止 (direction + symbol pattern)
2. DynamicRiskExit: 黑名单扩展 (9只持续亏损股)
3. DynamicRiskExit: 连续亏损暂停 (从0改为3)
4. PreTradeChecker: P0_GUARD 注入验证

版本: 1.0 | 2026-05-23
"""
import pytest
from unittest.mock import MagicMock, patch


class TestOptionBan:
    """P0-1: 期权交易禁止 (0%WR, -$23,635)"""

    def test_ban_option_direction(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, reason = exit_mod.check_entry_allowed(
            "US.SPY", direction="SHORT_PROXY_OPTION"
        )
        assert allowed is False
        assert "BANNED" in reason
        assert "期权" in reason

    def test_ban_option_symbol_pattern(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        # 期权风格 symbol: US.SNDK260605P1370000
        allowed, reason = exit_mod.check_entry_allowed(
            "US.SNDK260605P1370000"
        )
        assert allowed is False
        assert "BANNED" in reason
        assert "期权" in reason

    def test_allow_normal_stock(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, reason = exit_mod.check_entry_allowed("US.AAPL")
        assert allowed is True

    def test_allow_short_direction(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, reason = exit_mod.check_entry_allowed(
            "US.AAPL", direction="SHORT"
        )
        assert allowed is True

    def test_ban_put_option_symbol(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.TSLA260603P410000")
        assert allowed is False

    def test_ban_call_option_symbol(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        # 虽然没在实盘中出现, 但同样应该被禁
        allowed, _ = exit_mod.check_entry_allowed("US.AAPL260620C150000")
        assert allowed is False


class TestBlacklist:
    """P0-3: 持续亏损股票黑名单"""

    def test_qcom_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, reason = exit_mod.check_entry_allowed("US.QCOM")
        assert allowed is False
        assert "blacklist" in reason.lower()

    def test_tme_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.TME")
        assert allowed is False

    def test_nvda_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.NVDA")
        assert allowed is False

    def test_meta_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.META")
        assert allowed is False

    def test_googl_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.GOOGL")
        assert allowed is False

    def test_qqq_blocked(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.QQQ")
        assert allowed is False

    def test_orcl_allowed(self):
        """ORCL 是最大盈利股, 不应被禁"""
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.ORCL")
        assert allowed is True

    def test_arm_allowed(self):
        """ARM 有正收益, 不应被禁"""
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.ARM")
        assert allowed is True

    def test_rivn_allowed(self):
        """RIVN 有正收益, 不应被禁"""
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        allowed, _ = exit_mod.check_entry_allowed("US.RIVN")
        assert allowed is True


class TestConsecutiveLossPause:
    """P0: 连续3亏暂停 (从0改为3)"""

    def test_three_losses_triggers_pause(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        exit_mod.record_trade_result("US.A", -100)
        exit_mod.record_trade_result("US.B", -100)
        exit_mod.record_trade_result("US.C", -100)
        allowed, reason = exit_mod.check_entry_allowed("US.D")
        assert allowed is False
        assert "PAUSED" in reason

    def test_two_losses_no_pause(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        exit_mod.record_trade_result("US.A", -100)
        exit_mod.record_trade_result("US.B", -100)
        allowed, reason = exit_mod.check_entry_allowed("US.C")
        assert allowed is True

    def test_win_resets_streak(self):
        from app.exit.dynamic_risk import DynamicRiskExit
        exit_mod = DynamicRiskExit()
        exit_mod.record_trade_result("US.A", -100)
        exit_mod.record_trade_result("US.B", -100)
        exit_mod.record_trade_result("US.C", +200)  # 重置
        exit_mod.record_trade_result("US.D", -100)
        exit_mod.record_trade_result("US.E", -100)
        allowed, _ = exit_mod.check_entry_allowed("US.F")
        assert allowed is True  # 只有2连亏


class TestPreTradeP0Guard:
    """验证 PreTradeChecker 的 P0 守卫注入"""

    def _make_signal(self, symbol="US.AAPL", direction="LONG", strength=99, confidence=0.8):
        signal = MagicMock()
        signal.symbol = symbol
        signal.direction = direction
        signal.strength = strength
        signal.confidence = confidence
        signal.metadata = {}
        signal.is_expired.return_value = False
        return signal

    @patch('app.risk.pre_trade_check.market_can_trade', return_value=True)
    def test_option_direction_blocked(self, mock_market):
        from app.risk.pre_trade_check import PreTradeChecker
        checker = PreTradeChecker()
        signal = self._make_signal(direction="SHORT_PROXY_OPTION")
        result = checker.check(signal)
        assert result.allowed is False
        assert "P0_OPTION_BANNED" in result.reason

    @patch('app.risk.pre_trade_check.market_can_trade', return_value=True)
    def test_option_symbol_blocked(self, mock_market):
        from app.risk.pre_trade_check import PreTradeChecker
        checker = PreTradeChecker()
        signal = self._make_signal(symbol="US.SNDK260605P1370000")
        result = checker.check(signal)
        assert result.allowed is False
        assert "P0_OPTION_BANNED" in result.reason

    @patch('app.risk.pre_trade_check.market_can_trade', return_value=True)
    def test_blacklist_blocked(self, mock_market):
        from app.risk.pre_trade_check import PreTradeChecker
        checker = PreTradeChecker()
        signal = self._make_signal(symbol="US.QCOM")
        result = checker.check(signal)
        assert result.allowed is False
        assert "P0_BLACKLISTED" in result.reason

    @patch('app.risk.pre_trade_check.market_can_trade', return_value=True)
    @patch('app.risk.pre_trade_check.get_pdt_guard')
    def test_normal_stock_passes_p0(self, mock_pdt, mock_market):
        from app.risk.pre_trade_check import PreTradeChecker
        # Mock PDT guard
        pdt_instance = MagicMock()
        pdt_instance.check_trade_permission.return_value = (True, "OK", "NORMAL")
        mock_pdt.return_value = pdt_instance

        checker = PreTradeChecker()
        signal = self._make_signal(symbol="US.ORCL", direction="SHORT", strength=10)
        result = checker.check(signal)
        assert "P0_GUARD" in result.checks_passed
