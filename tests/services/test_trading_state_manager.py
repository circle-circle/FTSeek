#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingStateManager 单元测试

测试状态管理器的所有核心功能。

测试覆盖率:
- 单例模式
- 冷却状态管理
- 峰值状态管理
- 快照状态管理
- 锁状态管理
- 统一清理
- 持久化/恢复
"""

import sys
import os
import json
import tempfile
import time
import threading
import unittest

sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.services.trading_state_manager import TradingStateManager, get_trading_state_manager


class TestTradingStateManagerSingleton(unittest.TestCase):
    """单例模式测试"""

    def test_singleton_same_instance(self):
        """单例：多次获取同一实例"""
        mgr1 = get_trading_state_manager()
        mgr2 = get_trading_state_manager()
        self.assertIs(mgr1, mgr2)

    def test_singleton_different_threads(self):
        """单例：多线程环境下同一实例"""
        results = []

        def get_instance():
            mgr = get_trading_state_manager()
            results.append(id(mgr))

        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程应该获取同一实例
        self.assertEqual(len(set(results)), 1)


class TestCooldownStates(unittest.TestCase):
    """冷却状态测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        # 清理状态
        self.mgr._cooldown.clear()
        self.mgr._stop_loss_cooldown.clear()
        self.mgr._soft_stop_warnings.clear()

    def test_cooldown_not_in_period(self):
        """冷却期：不在冷却期"""
        self.assertFalse(self.mgr.is_in_cooldown("HK.00700", cooldown_seconds=120))

    def test_cooldown_in_period(self):
        """冷却期：在冷却期内"""
        self.mgr.update_cooldown("HK.00700")
        self.assertTrue(self.mgr.is_in_cooldown("HK.00700", cooldown_seconds=120))

    def test_cooldown_expired(self):
        """冷却期：已过期"""
        self.mgr._cooldown["HK.00700"] = time.time() - 150
        self.assertFalse(self.mgr.is_in_cooldown("HK.00700", cooldown_seconds=120))

    def test_stop_loss_cooldown(self):
        """止损冷却"""
        self.assertFalse(self.mgr.is_in_stop_loss_cooldown("HK.00700"))
        self.mgr.update_stop_loss_cooldown("HK.00700")
        self.assertTrue(self.mgr.is_in_stop_loss_cooldown("HK.00700"))

    def test_soft_stop_warning_cooldown(self):
        """软止损预警冷却"""
        self.assertTrue(self.mgr.is_soft_stop_warning_cooled("HK.00700"))
        self.mgr.update_soft_stop_warning("HK.00700")
        self.assertFalse(self.mgr.is_soft_stop_warning_cooled("HK.00700"))


class TestPeakStates(unittest.TestCase):
    """峰值状态测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        self.mgr._peak_price_cache.clear()
        self.mgr._trailing_stop_cache.clear()
        self.mgr._trailing_exit_time.clear()

    def test_peak_price_update(self):
        """峰值价格更新"""
        self.mgr.update_peak_price("HK.00700", 350.0)
        self.assertEqual(self.mgr.get_peak_price("HK.00700"), 350.0)

        # 新价格更高
        self.mgr.update_peak_price("HK.00700", 360.0)
        self.assertEqual(self.mgr.get_peak_price("HK.00700"), 360.0)

        # 新价格更低，不更新
        self.mgr.update_peak_price("HK.00700", 340.0)
        self.assertEqual(self.mgr.get_peak_price("HK.00700"), 360.0)

    def test_peak_price_reset(self):
        """峰值价格重置"""
        self.mgr.update_peak_price("HK.00700", 350.0)
        self.mgr.reset_peak_price("HK.00700")
        self.assertEqual(self.mgr.get_peak_price("HK.00700"), 0)

    def test_trailing_stop_update(self):
        """追踪止损价更新"""
        self.mgr.update_trailing_stop("HK.00700", 345.0)
        self.assertEqual(self.mgr.get_trailing_stop("HK.00700"), 345.0)

    def test_trailing_cooldown(self):
        """追踪止盈冷却"""
        self.assertFalse(self.mgr.is_in_trailing_cooldown("HK.00700"))
        self.mgr.update_trailing_exit_time("HK.00700")
        self.assertTrue(self.mgr.is_in_trailing_cooldown("HK.00700"))


class TestSnapshotStates(unittest.TestCase):
    """快照状态测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        self.mgr._flash_snapshots.clear()

    def test_flash_snapshot_update(self):
        """Flash 快照更新"""
        self.mgr.update_flash_snapshot("HK.00700", 350.0)
        snapshot = self.mgr.get_flash_snapshot("HK.00700")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot[0], 350.0)

    def test_flash_snapshot_clear(self):
        """Flash 快照清除"""
        self.mgr.update_flash_snapshot("HK.00700", 350.0)
        self.mgr.clear_flash_snapshot("HK.00700")
        self.assertIsNone(self.mgr.get_flash_snapshot("HK.00700"))


class TestLockStates(unittest.TestCase):
    """锁状态测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        self.mgr._trade_locks.clear()
        self.mgr._open_position_ts.clear()

    def test_trade_lock(self):
        """交易锁"""
        lock1 = self.mgr.get_trade_lock("HK.00700")
        lock2 = self.mgr.get_trade_lock("HK.00700")
        self.assertIs(lock1, lock2)

        # 不同 code 不同锁
        lock3 = self.mgr.get_trade_lock("HK.09988")
        self.assertIsNot(lock1, lock3)

    def test_open_position_atomicity(self):
        """开仓原子性检查"""
        # 第一次允许
        self.assertTrue(self.mgr.check_open_position_atomicity("HK.00700", 350.0))

        # 同一秒内拒绝
        self.assertFalse(self.mgr.check_open_position_atomicity("HK.00700", 350.0))

        # 不同 code 允许
        self.assertTrue(self.mgr.check_open_position_atomicity("HK.09988", 350.0))


class TestCacheStates(unittest.TestCase):
    """缓存状态测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        self.mgr._last_quote_time = 0

    def test_last_quote_time(self):
        """最后行情时间"""
        self.mgr.update_last_quote_time()
        self.assertGreater(self.mgr.get_last_quote_time(), 0)

    def test_quote_stale(self):
        """行情过期检查"""
        self.mgr.update_last_quote_time(time.time() - 15)
        self.assertTrue(self.mgr.is_quote_stale(max_age_seconds=10))

        self.mgr.update_last_quote_time(time.time())
        self.assertFalse(self.mgr.is_quote_stale(max_age_seconds=10))

    def test_trend_update(self):
        """趋势更新"""
        self.mgr.update_trend("HK.00700", "K_1M", "up")
        self.assertEqual(self.mgr.get_trend("HK.00700", "K_1M"), "up")

    def test_trend_updated_mark(self):
        """趋势更新标记"""
        self.assertFalse(self.mgr.is_trend_updated("HK.00700", "K_1M"))
        self.mgr.mark_trend_updated("HK.00700", "K_1M")
        self.assertTrue(self.mgr.is_trend_updated("HK.00700", "K_1M"))


class TestCleanup(unittest.TestCase):
    """统一清理测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()

    def test_cleanup_expired_cooldown(self):
        """清理过期冷却状态"""
        # 添加过期状态
        self.mgr._cooldown["HK.00700"] = time.time() - 100000  # 超过 24 小时
        self.mgr._cooldown["HK.09988"] = time.time()  # 未过期

        self.mgr.cleanup_expired()

        self.assertNotIn("HK.00700", self.mgr._cooldown)
        self.assertIn("HK.09988", self.mgr._cooldown)

    def test_cleanup_expired_flash(self):
        """清理过期 Flash 快照"""
        self.mgr._flash_snapshots["HK.00700"] = (350.0, time.time() - 400)  # 超过 5 分钟
        self.mgr._flash_snapshots["HK.09988"] = (350.0, time.time())  # 未过期

        self.mgr.cleanup_expired()

        self.assertNotIn("HK.00700", self.mgr._flash_snapshots)
        self.assertIn("HK.09988", self.mgr._flash_snapshots)


class TestPersistence(unittest.TestCase):
    """持久化/恢复测试"""

    def setUp(self):
        self.mgr = get_trading_state_manager()
        self.mgr._cooldown.clear()
        self.mgr._peak_price_cache.clear()
        self.temp_file = None

    def tearDown(self):
        if self.temp_file and os.path.exists(self.temp_file):
            os.remove(self.temp_file)

    def test_persist_and_restore(self):
        """持久化和恢复"""
        # 设置状态
        self.mgr.update_cooldown("HK.00700")
        self.mgr.update_peak_price("HK.00700", 350.0)
        self.mgr.update_last_quote_time(1234567890)

        # 持久化
        self.temp_file = tempfile.mktemp(suffix='.json')
        self.assertTrue(self.mgr.persist(self.temp_file))

        # 验证文件存在
        self.assertTrue(os.path.exists(self.temp_file))

        # 清理状态
        self.mgr._cooldown.clear()
        self.mgr._peak_price_cache.clear()
        self.mgr._last_quote_time = 0

        # 恢复
        self.assertTrue(self.mgr.restore(self.temp_file))

        # 验证状态恢复
        self.assertIn("HK.00700", self.mgr._cooldown)
        self.assertEqual(self.mgr.get_peak_price("HK.00700"), 350.0)
        self.assertEqual(self.mgr._last_quote_time, 1234567890)

    def test_restore_missing_file(self):
        """恢复不存在的文件"""
        self.temp_file = "/tmp/nonexistent_state.json"
        self.assertFalse(self.mgr.restore(self.temp_file))

    def test_state_summary(self):
        """状态摘要"""
        self.mgr.update_cooldown("HK.00700")
        self.mgr.update_peak_price("HK.00700", 350.0)

        summary = self.mgr.get_state_summary()

        self.assertIn('cooldown_count', summary)
        self.assertIn('peak_price_count', summary)
        self.assertEqual(summary['cooldown_count'], 1)
        self.assertEqual(summary['peak_price_count'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
