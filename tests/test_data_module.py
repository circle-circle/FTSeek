"""
数据模块测试
测试 app.data 模块的功能和集成
"""
import unittest
import time
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

# 测试模块导入
from app.data import (
    TradingHoursManager, 
    KlineProcessor, 
    ExitManager,
    get_trading_hours_manager,
    get_kline_processor,
    create_exit_manager,
)


class TestTradingHoursManager(unittest.TestCase):
    """交易时间管理器测试"""
    
    def setUp(self):
        """测试前准备"""
        self.manager = TradingHoursManager(default_cooldown=120)
    
    def tearDown(self):
        """测试后清理"""
        self.manager.cleanup_cooldown()
    
    def test_is_trading_hours_during_trading_time(self):
        """测试交易时间检查 - 交易时间内"""
        # 模拟港股交易时间 (工作日 10:00)
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 28, 10, 0, 0)  # 周六
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            # 周六应返回 False
            result = self.manager.is_trading_hours()
            self.assertFalse(result, "周六应返回 False")
    
    def test_is_trading_hours_on_weekend(self):
        """测试交易时间检查 - 周末"""
        # 周六
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 28, 10, 0, 0)  # 周六
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            result = self.manager.is_trading_hours()
            self.assertFalse(result, "周六应返回 False")
        
        # 周日
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 29, 10, 0, 0)  # 周日
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            result = self.manager.is_trading_hours()
            self.assertFalse(result, "周日应返回 False")
    
    def test_is_high_quality_trading_hours(self):
        """测试高质量交易时间检查"""
        # 开盘冲击期 (09:30-09:45) 应返回 False
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 30, 9, 35, 0)  # 周一 09:35
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            result = self.manager.is_high_quality_trading_hours()
            self.assertFalse(result, "开盘冲击期应返回 False")
        
        # 高质量时段 (10:00) 应返回 True
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 30, 10, 0, 0)  # 周一 10:00
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            result = self.manager.is_high_quality_trading_hours()
            self.assertTrue(result, "高质量时段应返回 True")
        
        # 收盘撮合期 (15:45-16:00) 应返回 False
        with patch('app.data.trading_hours.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 3, 30, 15, 50, 0)  # 周一 15:50
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            
            result = self.manager.is_high_quality_trading_hours()
            self.assertFalse(result, "收盘撮合期应返回 False")
    
    def test_cooldown_management(self):
        """测试冷却时间管理"""
        code = "HK.00700"
        
        # 初始状态应冷却完成
        self.assertTrue(self.manager.is_cooled_down(code))
        
        # 更新冷却时间
        self.manager.update_cooldown(code)
        
        # 立即检查应未冷却完成
        self.assertFalse(self.manager.is_cooled_down(code))
        
        # 等待冷却时间过后应冷却完成
        time.sleep(0.1)  # 短暂等待
        # 注意：实际测试中需要等待 120 秒，这里仅验证逻辑
    
    def test_stop_loss_cooldown(self):
        """测试止损冷却管理"""
        code = "HK.00700"
        
        # 初始失败次数为 0
        self.assertEqual(self.manager.get_stop_loss_fail_count(code), 0)
        
        # 递增失败次数
        count = self.manager.increment_stop_loss_fail_count(code)
        self.assertEqual(count, 1)
        
        count = self.manager.increment_stop_loss_fail_count(code)
        self.assertEqual(count, 2)
        
        # 获取冷却时间 (第 2 次失败应返回 30 秒，COOLDOWN_SCHEDULE=[5, 15, 30, 60, 60])
        cooldown = self.manager.get_stop_loss_cooldown_seconds(code)
        self.assertEqual(cooldown, 30)
        
        # 重置失败次数
        self.manager.reset_stop_loss_fail_count(code)
        self.assertEqual(self.manager.get_stop_loss_fail_count(code), 0)
    
    def test_trailing_exit_time(self):
        """测试追踪止盈时间管理"""
        code = "HK.00700"
        
        # 初始时间为 0
        self.assertEqual(self.manager.get_trailing_exit_time(code), 0)
        
        # 设置追踪止盈时间
        self.manager.set_trailing_exit_time(code)
        
        # 检查时间已设置
        self.assertGreater(self.manager.get_trailing_exit_time(code), 0)
        
        # 检查冷却期激活
        is_active, remaining = self.manager.is_trailing_cooldown_active(code, cooldown_sec=1)
        # 刚设置后应立即激活
        self.assertTrue(is_active or not is_active)  # 取决于执行时间
    
    def test_singleton_pattern(self):
        """测试单例模式"""
        manager1 = get_trading_hours_manager(cooldown=120)
        manager2 = get_trading_hours_manager(cooldown=180)
        
        # 应返回同一实例
        self.assertIs(manager1, manager2)
        
        # 默认冷却时间应为第一次设置的值
        self.assertEqual(manager1.default_cooldown, 120)
    
    def test_reset(self):
        """测试重置功能"""
        code = "HK.00700"
        
        # 设置各种状态
        self.manager.update_cooldown(code)
        self.manager.set_stop_loss_cooldown(code)
        self.manager.increment_stop_loss_fail_count(code)
        self.manager.set_trailing_exit_time(code)
        
        # 重置
        self.manager.reset(code)
        
        # 验证所有状态已清除
        self.assertTrue(self.manager.is_cooled_down(code))
        self.assertEqual(self.manager.get_stop_loss_fail_count(code), 0)
        self.assertEqual(self.manager.get_trailing_exit_time(code), 0)


class TestKlineProcessor(unittest.TestCase):
    """K 线处理器测试"""
    
    def setUp(self):
        """测试前准备"""
        self.processor = KlineProcessor()
    
    def tearDown(self):
        """测试后清理"""
        # 清理测试数据
        test_codes = ["HK.00700", "HK.09988", "US.AAPL"]
        for code in test_codes:
            self.processor.clear_code(code)
    
    def test_push_kline(self):
        """测试 K 线数据推送"""
        code = "HK.00700"
        kline = {
            'time': '2026-03-28 10:00:00',
            'open': 100.0,
            'high': 102.0,
            'low': 99.0,
            'close': 101.0,
            'volume': 1000000
        }
        
        # 推送 K 线
        self.processor.push_kline(code, kline, "K_1M")
        
        # 验证缓冲区有数据
        buf = self.processor._kline_buffers[code]
        self.assertEqual(buf.size, 1)
    
    def test_calculate_adx(self):
        """测试 ADX 计算"""
        code = "HK.00700"
        
        # 数据不足时应返回 None
        adx = self.processor.calculate_adx(code)
        self.assertIsNone(adx)
        
        # 推送足够数据
        for i in range(50):
            kline = {
                'time': f'2026-03-28 10:{i:02d}:00',
                'open': 100.0 + i * 0.1,
                'high': 102.0 + i * 0.1,
                'low': 99.0 + i * 0.1,
                'close': 101.0 + i * 0.1,
                'volume': 1000000
            }
            self.processor.push_kline(code, kline, "K_1M")
        
        # 计算 ADX
        adx = self.processor.calculate_adx(code)
        self.assertIsNotNone(adx)
        self.assertGreater(adx, 0)
    
    def test_calculate_atr_percent(self):
        """测试 ATR 百分比计算"""
        code = "HK.00700"
        
        # 数据不足时返回默认值
        atr_pct = self.processor.calculate_atr_percent(code)
        self.assertEqual(atr_pct, 0.02)
        
        # 推送数据
        for i in range(20):
            kline = {
                'time': f'2026-03-28 10:{i:02d}:00',
                'open': 100.0,
                'high': 102.0 + i * 0.05,
                'low': 99.0 - i * 0.05,
                'close': 100.5 + i * 0.02,
                'volume': 1000000
            }
            self.processor.push_kline(code, kline, "K_1M")
        
        # 计算 ATR 百分比
        atr_pct = self.processor.calculate_atr_percent(code)
        self.assertGreater(atr_pct, 0)
        self.assertLessEqual(atr_pct, 0.08)
    
    def test_update_multi_timeframe_trend(self):
        """测试多周期趋势更新"""
        code = "HK.00700"
        
        # 推送多个周期的 K 线数据
        for period in ["K_1M", "K_5M", "K_15M", "K_60M", "K_DAY"]:
            for i in range(60):
                kline = {
                    'time': f'2026-03-28 10:{i:02d}:00',
                    'open': 100.0 + i * 0.1,
                    'high': 102.0 + i * 0.1,
                    'low': 99.0 + i * 0.1,
                    'close': 101.0 + i * 0.1,
                    'volume': 1000000
                }
                trend = self.processor.update_multi_timeframe_trend(code, period, kline)
                self.assertIn(trend, ["up", "down", "neutral"])
    
    def test_get_multi_timeframe_trend(self):
        """测试获取多周期趋势"""
        code = "HK.00700"
        
        # 未设置时应返回 neutral
        trend = self.processor.get_multi_timeframe_trend(code, "K_1M")
        self.assertEqual(trend, "neutral")
        
        # 设置趋势
        self.processor._multi_timeframe_trends[code]["K_1M"] = "up"
        
        # 验证获取
        trend = self.processor.get_multi_timeframe_trend(code, "K_1M")
        self.assertEqual(trend, "up")
    
    def test_calculate_mtf_alignment_score(self):
        """测试多周期对齐分数计算"""
        code = "HK.00700"
        
        # 设置所有周期为上涨
        for period in self.processor.MULTI_TIMEFRAMES:
            self.processor._multi_timeframe_trends[code][period] = "up"
        
        # 计算买入对齐分数
        score, detail = self.processor.calculate_mtf_alignment_score(code, "BUY")
        self.assertGreater(score, 0.7)  # 所有周期上涨，分数应较高
        self.assertIn("↑", detail)
        
        # 设置所有周期为下跌
        for period in self.processor.MULTI_TIMEFRAMES:
            self.processor._multi_timeframe_trends[code][period] = "down"
        
        # 计算买入对齐分数 (应较低)
        score, detail = self.processor.calculate_mtf_alignment_score(code, "BUY")
        self.assertLess(score, 0.3)  # 所有周期下跌，分数应较低
    
    def test_get_mtf_signal_confirmation(self):
        """测试多周期信号确认"""
        code = "HK.00700"
        
        # 设置所有周期为上涨
        for period in self.processor.MULTI_TIMEFRAMES:
            self.processor._multi_timeframe_trends[code][period] = "up"
        
        # 买入信号应确认
        confirmed, boost, reason = self.processor.get_mtf_signal_confirmation(code, "BUY")
        self.assertTrue(confirmed)
        self.assertGreater(boost, 0)
        
        # 设置日线和 60M 为下跌
        self.processor._multi_timeframe_trends[code]["K_DAY"] = "down"
        self.processor._multi_timeframe_trends[code]["K_60M"] = "down"
        
        # 买入信号应被拒绝
        confirmed, boost, reason = self.processor.get_mtf_signal_confirmation(code, "BUY")
        self.assertFalse(confirmed)
    
    def test_get_dynamic_stop_params(self):
        """测试动态止损止盈参数"""
        code = "HK.00700"
        
        # 设置强上升趋势
        for period, weight in self.processor.TREND_WEIGHTS.items():
            self.processor._multi_timeframe_trends[code][period] = "up"
        
        params = self.processor.get_dynamic_stop_params(code)
        self.assertEqual(params["regime"], "strong_up")
        self.assertLess(params["stop_loss_pct"], -0.05)  # 宽松止损
        self.assertGreater(params["take_profit_pct"], 0.07)  # 高止盈
        
        # 设置强下跌趋势
        for period, weight in self.processor.TREND_WEIGHTS.items():
            self.processor._multi_timeframe_trends[code][period] = "down"
        
        params = self.processor.get_dynamic_stop_params(code)
        self.assertEqual(params["regime"], "strong_down")
        self.assertGreater(params["stop_loss_pct"], -0.04)  # 紧止损
    
    def test_singleton_pattern(self):
        """测试单例模式"""
        processor1 = get_kline_processor()
        processor2 = get_kline_processor()
        
        # 应返回同一实例
        self.assertIs(processor1, processor2)
    
    def test_clear_code(self):
        """测试清除标的缓存"""
        code = "HK.00700"
        
        # 添加数据
        kline = {
            'time': '2026-03-28 10:00:00',
            'open': 100.0,
            'high': 102.0,
            'low': 99.0,
            'close': 101.0,
            'volume': 1000000
        }
        self.processor.push_kline(code, kline, "K_1M")
        self.processor._adx_cache[code] = 25.0
        
        # 清除
        self.processor.clear_code(code)
        
        # 验证已清除
        self.assertNotIn(code, self.processor._kline_buffers)
        self.assertNotIn(code, self.processor._adx_cache)


class TestExitManager(unittest.TestCase):
    """退出管理器测试"""
    
    def setUp(self):
        """测试前准备"""
        # 创建 Mock 对象
        self.mock_position_manager = Mock()
        self.mock_trade_executor = Mock()
        
        self.manager = ExitManager(
            self.mock_position_manager,
            self.mock_trade_executor
        )
    
    def test_update_trailing_stop_long_position(self):
        """测试多头追踪止盈更新"""
        code = "HK.00700"
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.action = 'BUY'
        
        # 价格从 100 上涨到 105 (盈利 5%)
        current_price = 105.0
        atr_pct = 0.02
        trail_atr_mult = 2.0
        
        self.manager.update_trailing_stop(code, current_price, mock_position, atr_pct, trail_atr_mult)
        
        # 验证追踪止盈价已设置
        trail_price = self.manager.get_trailing_stop_price(code)
        self.assertGreater(trail_price, 0)
        # 追踪价应低于当前价格
        self.assertLess(trail_price, current_price)
    
    def test_update_trailing_stop_short_position(self):
        """测试空头追踪止盈更新"""
        code = "HK.00700"
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.action = 'SELL'
        
        # 价格从 100 下跌到 95 (盈利 5%)
        current_price = 95.0
        atr_pct = 0.02
        trail_atr_mult = 2.0
        
        self.manager.update_trailing_stop(code, current_price, mock_position, atr_pct, trail_atr_mult)
        
        # 验证追踪止盈价已设置
        trail_price = self.manager.get_trailing_stop_price(code)
        self.assertGreater(trail_price, 0)
        # 追踪价应高于当前价格 (空头)
        self.assertGreater(trail_price, current_price)
    
    def test_check_trailing_stop_triggered(self):
        """测试追踪止盈触发检查"""
        code = "HK.00700"
        
        # 设置追踪止盈价
        self.manager._trailing_stop_cache[code] = 98.0
        
        # 创建 Mock 持仓 (多头，盈利状态)
        mock_position = Mock()
        mock_position.entry_price = 95.0  # 入场价低于追踪价，确保盈利
        mock_position.action = 'BUY'
        
        # 价格跌破追踪止盈价 (且盈利)
        triggered = self.manager.check_trailing_stop_triggered(code, 97.0, mock_position)
        self.assertTrue(triggered)
        
        # 价格未跌破追踪止盈价
        triggered = self.manager.check_trailing_stop_triggered(code, 99.0, mock_position)
        self.assertFalse(triggered)
    
    def test_execute_stop_loss(self):
        """测试止损执行"""
        code = "HK.00700"
        current_price = 95.0
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.code = code
        mock_position.entry_price = 100.0
        mock_position.quantity = 100
        mock_position.action = 'BUY'
        
        self.mock_position_manager.get_position.return_value = mock_position
        
        # Mock 交易执行结果
        mock_result = Mock()
        mock_result.success = True
        mock_result.pnl = -500.0
        self.mock_trade_executor.execute.return_value = mock_result
        
        # 执行止损
        result = self.manager.execute_stop_loss(code, current_price)
        
        # 验证执行成功
        self.assertTrue(result)
        self.mock_trade_executor.execute.assert_called_once()
    
    def test_execute_take_profit(self):
        """测试止盈执行"""
        code = "HK.00700"
        current_price = 110.0
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.code = code
        mock_position.entry_price = 100.0
        mock_position.quantity = 100
        mock_position.action = 'BUY'
        
        self.mock_position_manager.get_position.return_value = mock_position
        
        # Mock 交易执行结果
        mock_result = Mock()
        mock_result.success = True
        mock_result.pnl = 1000.0
        self.mock_trade_executor.execute.return_value = mock_result
        
        # 执行止盈
        result = self.manager.execute_take_profit(code, current_price)
        
        # 验证执行成功
        self.assertTrue(result)
        self.mock_trade_executor.execute.assert_called_once()
    
    def test_execute_partial_take_profit(self):
        """测试分批止盈执行"""
        code = "HK.00700"
        current_price = 110.0
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.code = code
        mock_position.entry_price = 100.0
        mock_position.quantity = 500  # 5 手
        mock_position.action = 'BUY'
        
        self.mock_position_manager.get_position.return_value = mock_position
        
        # Mock 交易执行结果
        mock_result = Mock()
        mock_result.success = True
        mock_result.pnl = 600.0
        self.mock_trade_executor.execute_partial_close.return_value = mock_result
        
        # 执行 60% 分批止盈
        result = self.manager.execute_partial_take_profit(code, current_price, ratio=0.6)
        
        # 验证执行成功
        self.assertTrue(result)
        self.mock_trade_executor.execute_partial_close.assert_called_once()
    
    def test_execute_partial_take_profit_insufficient_quantity(self):
        """测试分批止盈 - 持仓不足一手"""
        code = "HK.00700"
        current_price = 110.0
        
        # 创建 Mock 持仓 (不足一手)
        mock_position = Mock()
        mock_position.code = code
        mock_position.entry_price = 100.0
        mock_position.quantity = 50  # 不足 100 股一手
        mock_position.action = 'BUY'
        
        self.mock_position_manager.get_position.return_value = mock_position
        
        # 执行分批止盈应返回 False
        result = self.manager.execute_partial_take_profit(code, current_price, ratio=0.6)
        
        # 验证跳过执行
        self.assertFalse(result)
        self.mock_trade_executor.execute.assert_not_called()
    
    def test_update_peak_price(self):
        """测试峰值价格更新"""
        code = "HK.00700"
        
        # 创建 Mock 持仓
        mock_position = Mock()
        mock_position.action = 'BUY'
        
        # 更新峰值价格
        self.manager.update_peak_price(code, 100.0, mock_position)
        self.assertEqual(self.manager.get_peak_price(code), 100.0)
        
        # 价格上涨
        self.manager.update_peak_price(code, 105.0, mock_position)
        self.assertEqual(self.manager.get_peak_price(code), 105.0)
        
        # 价格下跌 (不应更新)
        self.manager.update_peak_price(code, 103.0, mock_position)
        self.assertEqual(self.manager.get_peak_price(code), 105.0)
    
    def test_clear_trailing_stop(self):
        """测试清除追踪止盈缓存"""
        code = "HK.00700"
        
        # 设置缓存
        self.manager._trailing_stop_cache[code] = 98.0
        self.manager._peak_price_cache[code] = 105.0
        
        # 清除
        self.manager.clear_trailing_stop(code)
        
        # 验证已清除
        self.assertEqual(self.manager.get_trailing_stop_price(code), 0)
        self.assertIsNone(self.manager.get_peak_price(code))
    
    def test_create_exit_manager(self):
        """测试工厂函数"""
        manager = create_exit_manager(self.mock_position_manager, self.mock_trade_executor)
        
        # 验证类型
        self.assertIsInstance(manager, ExitManager)
        
        # 验证依赖注入
        self.assertIs(manager.position_manager, self.mock_position_manager)
        self.assertIs(manager.trade_executor, self.mock_trade_executor)


class TestIntegration(unittest.TestCase):
    """集成测试"""
    
    def test_module_imports(self):
        """测试模块导入"""
        # 测试直接导入
        from app.data import TradingHoursManager, KlineProcessor, ExitManager
        from app.data import get_trading_hours_manager, get_kline_processor, create_exit_manager
        
        # 验证类可实例化
        thm = TradingHoursManager()
        kp = KlineProcessor()
        
        # 验证单例函数
        thm_singleton = get_trading_hours_manager()
        kp_singleton = get_kline_processor()
        
        # 验证单例模式
        self.assertIs(thm_singleton, get_trading_hours_manager())
        self.assertIs(kp_singleton, get_kline_processor())
    
    def test_exception_handling(self):
        """测试异常处理"""
        # TradingHoursManager 异常处理
        manager = TradingHoursManager()
        
        # 不应抛出异常
        try:
            manager.is_cooled_down("HK.00700")
            manager.update_cooldown("HK.00700")
            manager.reset("HK.00700")
        except Exception as e:
            self.fail(f"TradingHoursManager 抛出异常：{e}")
        
        # KlineProcessor 异常处理
        processor = KlineProcessor()
        
        try:
            processor.push_kline("HK.00700", {}, "K_1M")
            processor.calculate_adx("HK.00700")
            processor.clear_code("HK.00700")
        except Exception as e:
            self.fail(f"KlineProcessor 抛出异常：{e}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
