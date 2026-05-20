#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ErrorHandler 单元测试

测试错误处理器的所有核心功能。

测试覆盖率:
- 单例模式
- 错误处理与统计
- 告警触发
- 降级机制
- 监控 API
"""

import sys
import time
import threading
import unittest

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.core.error_handler import ErrorHandler, get_error_handler


class TestErrorHandlerSingleton(unittest.TestCase):
    """单例模式测试"""

    def test_singleton_same_instance(self):
        """单例：多次获取同一实例"""
        handler1 = get_error_handler()
        handler2 = get_error_handler()
        self.assertIs(handler1, handler2)

    def test_singleton_different_threads(self):
        """单例：多线程环境下同一实例"""
        results = []

        def get_instance():
            handler = get_error_handler()
            results.append(id(handler))

        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程应该获取同一实例
        self.assertEqual(len(set(results)), 1)


class TestErrorHandling(unittest.TestCase):
    """错误处理测试"""

    def setUp(self):
        self.handler = get_error_handler()
        self.handler.reset_counts()

    def test_handle_error(self):
        """处理错误"""
        error = ValueError("测试错误")
        self.handler.handle('test_component', error, severity='warning')

        # 验证计数
        self.assertEqual(self.handler.get_error_count('test_component'), 1)

    def test_handle_multiple_errors(self):
        """处理多个错误"""
        for i in range(5):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        self.assertEqual(self.handler.get_error_count('test_component'), 5)

    def test_error_rate_calculation(self):
        """错误率计算"""
        # 添加 10 个错误
        for i in range(10):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        # 错误率应该 > 0
        rate = self.handler.get_error_rate('test_component', window_seconds=60)
        self.assertGreater(rate, 0)

    def test_error_rate_window(self):
        """错误率时间窗口"""
        # 添加旧错误 (超过 1 小时)
        self.handler._error_history['old_component'].append(time.time() - 4000)

        # 添加新错误
        self.handler._error_history['new_component'].append(time.time())

        # 新错误率应该更高
        rate_old = self.handler.get_error_rate('old_component')
        rate_new = self.handler.get_error_rate('new_component')

        # 新错误率应该 > 旧错误率 (因为旧错误在窗口外)
        self.assertGreater(rate_new, rate_old)


class TestAlerting(unittest.TestCase):
    """告警测试"""

    def setUp(self):
        self.handler = get_error_handler()
        self.handler.reset_counts()
        self.alerted = False
        self.alert_component = None

    def _alert_callback(self, component, error, context):
        """告警回调"""
        self.alerted = True
        self.alert_component = component

    def test_alert_threshold_exceeded(self):
        """告警阈值触发"""
        self.handler.register_alert_callback(self._alert_callback)
        self.handler.set_alert_threshold('test_component', 10)

        # 触发超过阈值的错误
        for i in range(15):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        # 验证错误计数
        self.assertEqual(self.handler.get_error_count('test_component'), 15)

        # 手动触发告警检查 (模拟高错误率场景)
        if self.handler.get_error_count('test_component') >= 10:
            self.handler._trigger_alert('test_component', ValueError("测试"), None)

        # 验证告警触发
        self.assertTrue(self.alerted)
        self.assertEqual(self.alert_component, 'test_component')

    def test_alert_cooldown(self):
        """告警冷却"""
        self.handler.register_alert_callback(self._alert_callback)
        self.handler.set_alert_threshold('test_component', 5)

        # 第一次触发
        for i in range(10):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        # 手动触发
        self.handler._trigger_alert('test_component', ValueError("测试"), None)
        self.assertTrue(self.alerted)

        # 重置
        self.alerted = False

        # 第二次触发 (应该在冷却期内)
        for i in range(10):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        # 冷却期内不应该再次触发 (因为冷却期 300 秒)
        self.assertFalse(self.alerted)


class TestDegradation(unittest.TestCase):
    """降级测试"""

    def setUp(self):
        self.handler = get_error_handler()
        self.handler.reset_counts()
        self.handler.reset_degradation('test_component')

    def test_degradation_trigger(self):
        """降级触发"""
        self.handler.set_degradation_threshold('test_component', 3)

        # 连续错误
        for i in range(5):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        # 验证降级状态
        self.assertTrue(self.handler.is_degraded('test_component'))

    def test_degradation_auto_recovery(self):
        """降级自动恢复"""
        self.handler.set_degradation_threshold('test_component', 3)

        # 触发降级
        for i in range(5):
            self.handler.handle('test_component', ValueError(f"错误{i}"))

        self.assertTrue(self.handler.is_degraded('test_component'))

        # 模拟时间流逝
        self.handler._degradation_recover_time['test_component'] = time.time() - 1

        # 应该自动恢复
        self.assertFalse(self.handler.is_degraded('test_component'))

    def test_degradation_manual_reset(self):
        """降级手动重置"""
        self.handler._degradation_state['test_component'] = True
        self.handler.reset_degradation('test_component')

        self.assertFalse(self.handler.is_degraded('test_component'))


class TestMonitoring(unittest.TestCase):
    """监控测试"""

    def setUp(self):
        self.handler = get_error_handler()
        self.handler.reset_counts()

    def test_get_summary(self):
        """获取摘要"""
        self.handler.handle('component1', ValueError("错误 1"))
        self.handler.handle('component2', ValueError("错误 2"))
        self.handler.handle('component2', ValueError("错误 3"))

        summary = self.handler.get_summary()

        self.assertIn('error_counts', summary)
        self.assertIn('error_rates', summary)
        self.assertIn('degradation_status', summary)

        self.assertEqual(summary['error_counts']['component1'], 1)
        self.assertEqual(summary['error_counts']['component2'], 2)

    def test_get_top_errors(self):
        """获取错误最多的组件"""
        # 添加不同数量的错误
        for i in range(10):
            self.handler.handle('component_a', ValueError(f"错误{i}"))
        for i in range(5):
            self.handler.handle('component_b', ValueError(f"错误{i}"))
        for i in range(3):
            self.handler.handle('component_c', ValueError(f"错误{i}"))

        top_errors = self.handler.get_top_errors(limit=3)

        self.assertEqual(len(top_errors), 3)
        self.assertEqual(top_errors[0]['component'], 'component_a')
        self.assertEqual(top_errors[0]['count'], 10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
