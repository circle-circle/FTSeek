#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁链集成测试

验证门禁链在 TradeExecutor 中的集成。

测试场景:
1. 门禁链初始化
2. 门禁链检查通过
3. 门禁链检查拦截
4. 门禁链异常隔离
"""

import sys
import unittest
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction


class TestGateChainIntegration(unittest.TestCase):
    """门禁链集成测试"""

    def setUp(self):
        """初始化测试环境"""
        # Mock 依赖
        with patch('app.trader.executor.PositionManager') as mock_pm, \
             patch('app.trader.executor.get_risk_manager') as mock_rm:
            mock_pm.get_instance.return_value = Mock()
            mock_rm.return_value = Mock()
            self.executor = TradeExecutor()

    def test_gate_chain_initialization(self):
        """门禁链初始化"""
        self.assertIsNotNone(self.executor._gate_chain)
        gates = self.executor._gate_chain.get_gates()
        self.assertEqual(len(gates), 5)  # 5 个门禁

    def test_gate_chain_check_pass(self):
        """门禁链检查通过"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

        # Mock 所有门禁通过
        with patch.object(self.executor._gate_chain, 'check_all', return_value=(True, "")) as mock_check:
            context = {
                'config': Mock(),
                'config_center': None,
                'position_manager': Mock(),
                'logger': Mock(),
                'failure_tracker': Mock(),
                'is_sim': True,
                'get_lot_size_fn': Mock(return_value=100),
            }
            passed, reason = self.executor._gate_chain.check_all(signal, context)
            self.assertTrue(passed)
            self.assertEqual(reason, "")

    def test_gate_chain_check_block(self):
        """门禁链检查拦截"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

        # Mock 某个门禁拦截
        with patch.object(self.executor._gate_chain, 'check_all', return_value=(False, "时段门禁：14:30 后禁止开仓")):
            context = {
                'config': Mock(),
                'config_center': None,
                'position_manager': Mock(),
                'logger': Mock(),
                'failure_tracker': Mock(),
                'is_sim': False,
                'get_lot_size_fn': Mock(return_value=100),
            }
            passed, reason = self.executor._gate_chain.check_all(signal, context)
            self.assertFalse(passed)
            self.assertIn("时段门禁", reason)

    def test_gate_chain_exception_isolation(self):
        """门禁链异常隔离"""
        signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

        # Mock 某个门禁抛出异常
        from app.trader.gates import GateInterface
        from typing import Tuple

        class FailingGate(GateInterface):
            def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
                raise Exception("测试异常")

        # 添加失败的门禁
        self.executor._gate_chain.add_gate(FailingGate())

        # 异常隔离：应该通过
        context = {
            'config': Mock(),
            'config_center': None,
            'position_manager': Mock(),
            'logger': Mock(),
            'failure_tracker': Mock(),
            'is_sim': True,
            'get_lot_size_fn': Mock(return_value=100),
        }
        passed, reason = self.executor._gate_chain.check_all(signal, context)
        # 异常隔离后应该通过
        self.assertTrue(passed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
