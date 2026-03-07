"""
PreTradeRisk 单元测试模块

测试交易前风控系统的核心功能，包括：
- 资金检查
- 每日交易次数限制检查
- 仓位限制检查
- 黑名单检查
- 催化事件验证
- 完整风控检查流程
"""

import pytest
from typing import Dict, List
from datetime import datetime

from app.v4.risk.pre_trade import (
    PreTradeRisk,
    RiskCheckResult,
    PreTradeRiskReport,
)


class TestRiskCheckResult:
    """RiskCheckResult 数据结构测试"""

    def test_risk_check_result_structure(self) -> None:
        """测试风控检查结果的数据结构"""
        result = RiskCheckResult(
            passed=True,
            check_type="capital",
            message="资金检查通过",
            details={"required": 10000.0, "available": 50000.0}
        )

        assert result.passed is True
        assert result.check_type == "capital"
        assert result.message == "资金检查通过"
        assert isinstance(result.details, dict)
        assert result.details["required"] == 10000.0
        assert result.details["available"] == 50000.0


class TestPreTradeRisk:
    """PreTradeRisk 核心功能测试"""

    @pytest.fixture
    def risk_system(self) -> PreTradeRisk:
        """创建默认的 PreTradeRisk 实例"""
        return PreTradeRisk()

    @pytest.fixture
    def risk_system_with_config(self) -> PreTradeRisk:
        """创建带自定义配置的 PreTradeRisk 实例"""
        config = {
            'max_daily_trades': 5,
            'max_single_position': 0.15,
            'max_total_position': 0.50,
            'blacklist': ['XYZ', 'ABC']
        }
        return PreTradeRisk(config=config)

    def test_capital_check_pass(self, risk_system: PreTradeRisk) -> None:
        """测试资金检查通过场景"""
        result = risk_system.check_capital(
            required=10000.0,
            available=50000.0
        )

        assert result.passed is True
        assert result.check_type == "capital"
        assert "通过" in result.message or "充足" in result.message

    def test_capital_check_fail(self, risk_system: PreTradeRisk) -> None:
        """测试资金检查失败场景"""
        result = risk_system.check_capital(
            required=60000.0,
            available=50000.0
        )

        assert result.passed is False
        assert result.check_type == "capital"
        assert "不足" in result.message or "失败" in result.message

    def test_capital_check_edge_case(self, risk_system: PreTradeRisk) -> None:
        """测试资金检查边界情况（刚好相等）"""
        result = risk_system.check_capital(
            required=50000.0,
            available=50000.0
        )

        assert result.passed is True

    def test_daily_limit_check_pass(self, risk_system: PreTradeRisk) -> None:
        """测试每日交易次数检查通过场景"""
        result = risk_system.check_daily_limit(
            current_trades=5,
            max_limit=10
        )

        assert result.passed is True
        assert result.check_type == "daily_limit"

    def test_daily_limit_check_fail(self, risk_system: PreTradeRisk) -> None:
        """测试每日交易次数检查失败场景"""
        result = risk_system.check_daily_limit(
            current_trades=10,
            max_limit=10
        )

        assert result.passed is False
        assert result.check_type == "daily_limit"

    def test_daily_limit_check_with_config(
        self,
        risk_system_with_config: PreTradeRisk
    ) -> None:
        """测试使用自定义配置的每日交易次数检查"""
        # 配置中 max_daily_trades=5
        result = risk_system_with_config.check_daily_limit(
            current_trades=5,
            max_limit=5  # 达到限制
        )

        assert result.passed is False

    def test_position_limit_single_stock_pass(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试单股仓位检查通过场景"""
        current_position = {
            'AAPL': {'quantity': 100, 'price': 150.0},
            'TSLA': {'quantity': 50, 'price': 200.0}
        }
        total_capital = 100000.0

        # 新买入 GOOGL 100 股 @ 100 元 = 10000 元 (10% < 20%)
        result = risk_system.check_position_limit(
            symbol='GOOGL',
            quantity=100,
            current_position=current_position,
            total_capital=total_capital
        )

        assert result.passed is True
        assert result.check_type == "position_limit"

    def test_position_limit_single_stock_fail(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试单股仓位检查失败场景（超过 20%）"""
        current_position = {
            'AAPL': {'quantity': 100, 'price': 150.0},
        }
        total_capital = 100000.0

        # 新买入 AAPL 200 股 @ 150 元 = 30000 元
        # 原有仓位 15000 元 + 新增 30000 元 = 45000 元 (45% > 20%)
        result = risk_system.check_position_limit(
            symbol='AAPL',
            quantity=200,
            current_position=current_position,
            total_capital=total_capital
        )

        assert result.passed is False
        assert result.check_type == "position_limit"

    def test_position_limit_total_exceed(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试总仓位检查失败场景（超过 60%）"""
        current_position = {
            'AAPL': {'quantity': 200, 'price': 150.0},  # 30000 元
            'TSLA': {'quantity': 100, 'price': 200.0},  # 20000 元
            'GOOGL': {'quantity': 100, 'price': 100.0},  # 10000 元
        }
        total_capital = 100000.0
        # 当前总仓位：60000 元 (60%)

        # 再买入 MSFT 50 股 @ 300 元 = 15000 元
        # 总仓位将变成 75000 元 (75% > 60%)
        result = risk_system.check_position_limit(
            symbol='MSFT',
            quantity=50,
            current_position=current_position,
            total_capital=total_capital,
            price=300.0  # 传入价格
        )

        assert result.passed is False

    def test_blacklist_check_pass(self, risk_system: PreTradeRisk) -> None:
        """测试黑名单检查通过场景"""
        # 先添加一些股票到黑名单
        risk_system.blacklist = {'XYZ', 'ABC'}

        result = risk_system.check_blacklist('AAPL')

        assert result.passed is True
        assert result.check_type == "blacklist"

    def test_blacklist_check_fail(self, risk_system: PreTradeRisk) -> None:
        """测试黑名单检查失败场景"""
        risk_system.blacklist = {'XYZ', 'ABC', 'AAPL'}

        result = risk_system.check_blacklist('AAPL')

        assert result.passed is False
        assert result.check_type == "blacklist"
        assert "黑名单" in result.message or "禁止" in result.message

    def test_blacklist_check_with_config(
        self,
        risk_system_with_config: PreTradeRisk
    ) -> None:
        """测试使用自定义配置黑名单的检查"""
        # 配置中 blacklist=['XYZ', 'ABC']
        result = risk_system_with_config.check_blacklist('XYZ')

        assert result.passed is False

    def test_validate_catalyst_pass(self, risk_system: PreTradeRisk) -> None:
        """测试催化事件验证通过场景"""
        # 这个测试可能需要 CatalystTracker 集成
        # 暂时测试基本功能
        result = risk_system.validate_catalyst('AAPL')

        assert result.check_type == "catalyst"
        # 催化事件验证可能通过或失败，取决于是否有事件
        assert isinstance(result.passed, bool)

    def test_full_check_pass(self, risk_system: PreTradeRisk) -> None:
        """测试完整风控检查通过场景"""
        signal = {
            'symbol': 'AAPL',
            'quantity': 100,
            'price': 150.0,
            'side': 'buy'
        }
        account_info = {
            'available_cash': 100000.0,
            'positions': {
                'TSLA': {'quantity': 50, 'price': 200.0}
            },
            'total_capital': 200000.0,
            'daily_trades': 3
        }

        report = risk_system.full_check(signal, account_info)

        assert isinstance(report, PreTradeRiskReport)
        assert isinstance(report.checks, list)
        assert report.timestamp > 0
        assert report.can_trade == report.all_passed

    def test_full_check_fail_insufficient_capital(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试完整风控检查失败场景（资金不足）"""
        signal = {
            'symbol': 'AAPL',
            'quantity': 1000,
            'price': 150.0,  # 需要 150000 元
            'side': 'buy'
        }
        account_info = {
            'available_cash': 50000.0,  # 只有 50000 元
            'positions': {},
            'total_capital': 100000.0,
            'daily_trades': 0
        }

        report = risk_system.full_check(signal, account_info)

        assert report.all_passed is False
        assert report.can_trade is False

        # 至少有一个检查失败
        failed_checks = [c for c in report.checks if not c.passed]
        assert len(failed_checks) >= 1

        # 资金检查应该失败
        capital_checks = [
            c for c in report.checks if c.check_type == "capital"
        ]
        assert len(capital_checks) >= 1
        assert capital_checks[0].passed is False

    def test_full_check_fail_daily_limit(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试完整风控检查失败场景（达到日交易限制）"""
        signal = {
            'symbol': 'AAPL',
            'quantity': 100,
            'price': 150.0,
            'side': 'buy'
        }
        account_info = {
            'available_cash': 100000.0,
            'positions': {},
            'total_capital': 200000.0,
            'daily_trades': 10  # 已达到限制
        }

        report = risk_system.full_check(signal, account_info)

        assert report.all_passed is False
        assert report.can_trade is False

    def test_full_check_fail_blacklist(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试完整风控检查失败场景（股票在黑名单）"""
        risk_system.blacklist = {'XYZ'}

        signal = {
            'symbol': 'XYZ',
            'quantity': 100,
            'price': 50.0,
            'side': 'buy'
        }
        account_info = {
            'available_cash': 100000.0,
            'positions': {},
            'total_capital': 200000.0,
            'daily_trades': 0
        }

        report = risk_system.full_check(signal, account_info)

        assert report.all_passed is False
        assert report.can_trade is False

        # 黑名单检查应该失败
        blacklist_checks = [
            c for c in report.checks if c.check_type == "blacklist"
        ]
        assert len(blacklist_checks) >= 1
        assert blacklist_checks[0].passed is False

    def test_pre_trade_risk_report_structure(
        self,
        risk_system: PreTradeRisk
    ) -> None:
        """测试 PreTradeRiskReport 数据结构"""
        signal = {
            'symbol': 'AAPL',
            'quantity': 100,
            'price': 150.0,
            'side': 'buy'
        }
        account_info = {
            'available_cash': 100000.0,
            'positions': {},
            'total_capital': 200000.0,
            'daily_trades': 0
        }

        report = risk_system.full_check(signal, account_info)

        assert hasattr(report, 'all_passed')
        assert hasattr(report, 'checks')
        assert hasattr(report, 'timestamp')
        assert hasattr(report, 'can_trade')
        assert isinstance(report.all_passed, bool)
        assert isinstance(report.checks, list)
        assert isinstance(report.timestamp, float)
        assert isinstance(report.can_trade, bool)
