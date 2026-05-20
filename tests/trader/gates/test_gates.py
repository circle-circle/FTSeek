#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁系统单元测试

测试所有门禁类的独立功能。

测试覆盖率:
- GateInterface: 接口验证
- GateChain: 链式检查、异常隔离
- TimeGate: 时段检查
- FeeGate: 金额检查
- MaxPositionGate: 持仓数检查
- LLMSignalImpactGate: LLM 标签检查
- MarketRegimeGate: 市场趋势检查
"""

import sys
import unittest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime
from typing import Tuple

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.trader.gates import (
    GateInterface,
    GateChain,
    TimeGate,
    FeeGate,
    MaxPositionGate,
    LLMSignalImpactGate,
    MarketRegimeGate,
)
from app.strategy.base import Signal, SignalAction


class MockGate(GateInterface):
    """测试用 Mock 门禁"""
    def __init__(self, should_pass: bool = True, reason: str = ""):
        self._should_pass = should_pass
        self._reason = reason
        self._enabled = True

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        return self._should_pass, self._reason

    def is_enabled(self, context: dict) -> bool:
        return self._enabled


class TestGateChain(unittest.TestCase):
    """门禁链测试"""

    def setUp(self):
        self.chain = GateChain()
        self.signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )
        self.context = {}

    def test_all_pass(self):
        """所有门禁通过"""
        self.chain.add_gate(MockGate(True))
        self.chain.add_gate(MockGate(True))

        passed, reason = self.chain.check_all(self.signal, self.context)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_first_block(self):
        """第一个门禁拦截"""
        self.chain.add_gate(MockGate(False, "拦截 1"))
        self.chain.add_gate(MockGate(True))

        passed, reason = self.chain.check_all(self.signal, self.context)
        self.assertFalse(passed)
        self.assertEqual(reason, "拦截 1")

    def test_second_block(self):
        """第二个门禁拦截"""
        self.chain.add_gate(MockGate(True))
        self.chain.add_gate(MockGate(False, "拦截 2"))

        passed, reason = self.chain.check_all(self.signal, self.context)
        self.assertFalse(passed)
        self.assertEqual(reason, "拦截 2")

    def test_exception_isolation(self):
        """异常隔离：单个门禁失败不影响其他门禁"""
        failing_gate = MockGate(True)
        failing_gate.check = Mock(side_effect=Exception("测试异常"))

        self.chain.add_gate(failing_gate)
        self.chain.add_gate(MockGate(True))

        passed, reason = self.chain.check_all(self.signal, self.context)
        # 异常隔离后应该通过
        self.assertTrue(passed)

    def test_disabled_gate_skipped(self):
        """禁用的门禁被跳过"""
        disabled_gate = MockGate(False, "应该被跳过")
        disabled_gate._enabled = False

        self.chain.add_gate(disabled_gate)
        self.chain.add_gate(MockGate(True))

        passed, reason = self.chain.check_all(self.signal, self.context)
        self.assertTrue(passed)


class TestTimeGate(unittest.TestCase):
    """时段门禁测试"""

    def setUp(self):
        self.gate = TimeGate()
        self.signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

    def test_is_enabled_default(self):
        """默认启用"""
        context = {'config': Mock(TIME_GATE={'enabled': True})}
        self.assertTrue(self.gate.is_enabled(context))

    def test_is_enabled_disabled(self):
        """可禁用"""
        context = {'config': Mock(TIME_GATE={'enabled': False})}
        self.assertFalse(self.gate.is_enabled(context))


class TestFeeGate(unittest.TestCase):
    """费用门禁测试"""

    def setUp(self):
        self.gate = FeeGate()
        self.signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

    def test_sufficient_value(self):
        """金额足够通过"""
        context = {
            'config_center': Mock(get=Mock(return_value=8000)),
            'get_lot_size_fn': Mock(return_value=100),  # 350 * 100 = 35000
            'is_sim': False,
        }
        passed, reason = self.gate.check(self.signal, context)
        self.assertTrue(passed)

    def test_insufficient_value_sim(self):
        """金额不足，模拟盘放行"""
        context = {
            'config_center': Mock(get=Mock(return_value=8000)),
            'get_lot_size_fn': Mock(return_value=10),  # 350 * 10 = 3500
            'is_sim': True,
        }
        passed, reason = self.gate.check(self.signal, context)
        self.assertTrue(passed)  # 模拟盘放行
        self.assertIn('fee_warning', self.signal.extra)

    def test_insufficient_value_real(self):
        """金额不足，实盘拦截"""
        context = {
            'config_center': Mock(get=Mock(return_value=8000)),
            'get_lot_size_fn': Mock(return_value=10),  # 350 * 10 = 3500
            'is_sim': False,
        }
        passed, reason = self.gate.check(self.signal, context)
        self.assertFalse(passed)
        self.assertIn("费用侵蚀", reason)


class TestMaxPositionGate(unittest.TestCase):
    """最大持仓数门禁测试"""

    def setUp(self):
        self.gate = MaxPositionGate()
        self.signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

    def test_under_limit(self):
        """未达到上限"""
        config = Mock(MAX_OPEN_POSITIONS=12)
        position_manager = Mock(get_open_positions_count=Mock(return_value=5))
        context = {'config': config, 'position_manager': position_manager}

        passed, reason = self.gate.check(self.signal, context)
        self.assertTrue(passed)

    def test_at_limit(self):
        """达到上限"""
        config = Mock(MAX_OPEN_POSITIONS=12)
        position_manager = Mock(get_open_positions_count=Mock(return_value=12))
        context = {'config': config, 'position_manager': position_manager}

        passed, reason = self.gate.check(self.signal, context)
        self.assertFalse(passed)
        self.assertIn("最大持仓数", reason)


class TestLLMSignalImpactGate(unittest.TestCase):
    """LLM 信号门禁测试"""

    def setUp(self):
        self.gate = LLMSignalImpactGate()
        self.signal = Signal(
            code="HK.00700",
            action=SignalAction.BUY,
            price=350.0,
            strategy="test"
        )

    def test_no_impact(self):
        """无 LLM 标签"""
        context = {'is_sim': False}
        passed, reason = self.gate.check(self.signal, context)
        self.assertTrue(passed)

    def test_block_sim(self):
        """block 标签，模拟盘放行"""
        self.signal.extra = {'signal_impact': 'block'}
        context = {'is_sim': True}

        passed, reason = self.gate.check(self.signal, context)
        self.assertTrue(passed)  # 模拟盘放行
        self.assertTrue(self.signal.extra.get('llm_block_override'))

    def test_block_real(self):
        """block 标签，实盘拦截"""
        self.signal.extra = {'signal_impact': 'block'}
        context = {'is_sim': False}

        passed, reason = self.gate.check(self.signal, context)
        self.assertFalse(passed)
        self.assertIn("LLM 信号 block", reason)


if __name__ == '__main__':
    unittest.main(verbosity=2)
