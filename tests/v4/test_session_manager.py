"""
单元测试：MarketSessionManager (市场时段管理器)

测试场景:
1. 港股交易日 10:00 → 交易中
2. 港股交易日 12:30 → 午休 (非交易)
3. 港股交易日 20:00 → 收盘后
4. 美股交易日 22:00 → 交易中
5. 美股交易日 03:00 → 交易中 (跨日)
6. 美股交易日 10:00 → 收盘后
7. 周六任意时间 → 非交易
8. 周日任意时间 → 非交易
"""
import pytest
from datetime import datetime, timedelta
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.v4.core.session_manager import MarketSessionManager


class TestMarketSessionManager:
    """MarketSessionManager 单元测试"""
    
    def setup_method(self):
        """每个测试前的初始化"""
        self.manager = MarketSessionManager()
        self.hk_code = "0700.HK"
        self.us_code = "MU"
    
    # ==================== 测试 1: 港股交易日 10:00 → 交易中 ====================
    def test_hk_trading_morning(self):
        """测试场景 1: 港股交易日上午 10:00 应该在交易中"""
        # 周二 10:00 (交易日)
        timestamp = datetime(2026, 3, 10, 10, 0, 0)
        
        assert self.manager.is_trading_session(self.hk_code, timestamp) is True
        assert self.manager.get_session_type(self.hk_code, timestamp) == "REGULAR"
    
    # ==================== 测试 2: 港股交易日 12:30 → 午休 (非交易) ====================
    def test_hk_lunch_break(self):
        """测试场景 2: 港股交易日 12:30 午休时间应该非交易"""
        # 周二 12:30 (午休)
        timestamp = datetime(2026, 3, 10, 12, 30, 0)
        
        assert self.manager.is_trading_session(self.hk_code, timestamp) is False
        assert self.manager.get_session_type(self.hk_code, timestamp) == "CLOSED"
    
    # ==================== 测试 3: 港股交易日 20:00 → 收盘后 ====================
    def test_hk_after_hours(self):
        """测试场景 3: 港股交易日 20:00 收盘后应该非交易"""
        # 周二 20:00 (收盘后)
        timestamp = datetime(2026, 3, 10, 20, 0, 0)
        
        assert self.manager.is_trading_session(self.hk_code, timestamp) is False
        assert self.manager.get_session_type(self.hk_code, timestamp) == "CLOSED"
    
    # ==================== 测试 4: 美股交易日 22:00 → 交易中 ====================
    def test_us_trading_evening(self):
        """测试场景 4: 美股交易日 22:00 应该在交易中"""
        # 周二 22:00 (美股交易时间)
        timestamp = datetime(2026, 3, 10, 22, 0, 0)
        
        assert self.manager.is_trading_session(self.us_code, timestamp) is True
        assert self.manager.get_session_type(self.us_code, timestamp) == "REGULAR"
    
    # ==================== 测试 5: 美股交易日 03:00 → 交易中 (跨日) ====================
    def test_us_trading_early_morning(self):
        """测试场景 5: 美股交易日凌晨 03:00 应该在交易中 (跨日)"""
        # 周三 03:00 (属于周二开始的美股交易时段)
        timestamp = datetime(2026, 3, 11, 3, 0, 0)
        
        assert self.manager.is_trading_session(self.us_code, timestamp) is True
        assert self.manager.get_session_type(self.us_code, timestamp) == "REGULAR"
    
    # ==================== 测试 6: 美股交易日 10:00 → 收盘后 ====================
    def test_us_after_hours(self):
        """测试场景 6: 美股交易日 10:00 收盘后应该非交易"""
        # 周二 10:00 (美股收盘后)
        timestamp = datetime(2026, 3, 10, 10, 0, 0)
        
        assert self.manager.is_trading_session(self.us_code, timestamp) is False
        assert self.manager.get_session_type(self.us_code, timestamp) == "CLOSED"
    
    # ==================== 测试 7: 周六任意时间 → 非交易 ====================
    def test_saturday_non_trading(self):
        """测试场景 7: 周六任意时间应该非交易"""
        # 周六 10:00
        timestamp_saturday = datetime(2026, 3, 14, 10, 0, 0)
        
        assert self.manager.is_trading_session(self.hk_code, timestamp_saturday) is False
        assert self.manager.is_trading_session(self.us_code, timestamp_saturday) is False
        assert self.manager.get_session_type(self.hk_code, timestamp_saturday) == "CLOSED"
        assert self.manager.get_session_type(self.us_code, timestamp_saturday) == "CLOSED"
    
    # ==================== 测试 8: 周日任意时间 → 非交易 ====================
    def test_sunday_non_trading(self):
        """测试场景 8: 周日任意时间应该非交易"""
        # 周日 14:00
        timestamp_sunday = datetime(2026, 3, 15, 14, 0, 0)
        
        assert self.manager.is_trading_session(self.hk_code, timestamp_sunday) is False
        assert self.manager.is_trading_session(self.us_code, timestamp_sunday) is False
        assert self.manager.get_session_type(self.hk_code, timestamp_sunday) == "CLOSED"
        assert self.manager.get_session_type(self.us_code, timestamp_sunday) == "CLOSED"
    
    # ==================== 边界情况测试 ====================
    
    def test_hk_market_hours(self):
        """测试港股市场时间配置"""
        hours = self.manager.get_market_hours(self.hk_code)
        
        assert hours['timezone'] == 'Asia/Shanghai'
        assert hours['morning'] == '09:30-12:00'
        assert hours['afternoon'] == '13:00-16:00'
    
    def test_us_market_hours(self):
        """测试美股市场时间配置"""
        hours = self.manager.get_market_hours(self.us_code)
        
        assert hours['timezone'] == 'America/New_York'
        assert hours['regular'] == '21:30-04:00'
    
    def test_hk_open_boundary(self):
        """测试港股开盘边界：09:29 非交易，09:30 交易"""
        # 09:29 - 开盘前
        before_open = datetime(2026, 3, 10, 9, 29, 0)
        assert self.manager.is_trading_session(self.hk_code, before_open) is False
        
        # 09:30 - 刚好开盘
        at_open = datetime(2026, 3, 10, 9, 30, 0)
        assert self.manager.is_trading_session(self.hk_code, at_open) is True
    
    def test_hk_morning_close_boundary(self):
        """测试港股上午收盘边界：11:59 交易，12:00 收盘"""
        # 11:59 - 上午交易结束前
        before_close = datetime(2026, 3, 10, 11, 59, 0)
        assert self.manager.is_trading_session(self.hk_code, before_close) is True
        
        # 12:00 - 上午收盘
        at_close = datetime(2026, 3, 10, 12, 0, 0)
        assert self.manager.is_trading_session(self.hk_code, at_close) is False
    
    def test_hk_afternoon_open_boundary(self):
        """测试港股下午开盘边界：12:59 非交易，13:00 交易"""
        # 12:59 - 午休
        before_open = datetime(2026, 3, 10, 12, 59, 0)
        assert self.manager.is_trading_session(self.hk_code, before_open) is False
        
        # 13:00 - 下午开盘
        at_open = datetime(2026, 3, 10, 13, 0, 0)
        assert self.manager.is_trading_session(self.hk_code, at_open) is True
    
    def test_hk_afternoon_close_boundary(self):
        """测试港股下午收盘边界：15:59 交易，16:00 收盘"""
        # 15:59 - 收盘前
        before_close = datetime(2026, 3, 10, 15, 59, 0)
        assert self.manager.is_trading_session(self.hk_code, before_close) is True
        
        # 16:00 - 收盘
        at_close = datetime(2026, 3, 10, 16, 0, 0)
        assert self.manager.is_trading_session(self.hk_code, at_close) is False
    
    def test_us_session_start_boundary(self):
        """测试美股开盘边界：21:29 非交易，21:30 交易"""
        # 21:29 - 开盘前
        before_open = datetime(2026, 3, 10, 21, 29, 0)
        assert self.manager.is_trading_session(self.us_code, before_open) is False
        
        # 21:30 - 刚好开盘
        at_open = datetime(2026, 3, 10, 21, 30, 0)
        assert self.manager.is_trading_session(self.us_code, at_open) is True
    
    def test_us_session_end_boundary(self):
        """测试美股收盘边界：03:59 交易，04:00 收盘"""
        # 03:59 - 收盘前 (次日凌晨)
        before_close = datetime(2026, 3, 11, 3, 59, 0)
        assert self.manager.is_trading_session(self.us_code, before_close) is True
        
        # 04:00 - 收盘 (次日凌晨)
        at_close = datetime(2026, 3, 11, 4, 0, 0)
        assert self.manager.is_trading_session(self.us_code, at_close) is False
    
    def test_us_cross_midnight(self):
        """测试美股跨日时段：23:59 和 00:01 都应该交易"""
        # 23:59 (当天)
        before_midnight = datetime(2026, 3, 10, 23, 59, 0)
        assert self.manager.is_trading_session(self.us_code, before_midnight) is True
        
        # 00:01 (次日)
        after_midnight = datetime(2026, 3, 11, 0, 1, 0)
        assert self.manager.is_trading_session(self.us_code, after_midnight) is True
    
    def test_next_session_start_hk(self):
        """测试下一个交易时段开始时间 - 港股"""
        # 午休时间查询，应该返回下午开盘时间
        lunch_time = datetime(2026, 3, 10, 12, 30, 0)
        next_start = self.manager.next_session_start(self.hk_code, lunch_time)
        
        assert next_start.hour == 13
        assert next_start.minute == 0
    
    def test_next_session_start_us(self):
        """测试下一个交易时段开始时间 - 美股"""
        # 白天非交易时间查询，应该返回当晚开盘时间
        daytime = datetime(2026, 3, 10, 10, 0, 0)
        next_start = self.manager.next_session_start(self.us_code, daytime)
        
        assert next_start.hour == 21
        assert next_start.minute == 30
        # 应该是当天
        assert next_start.date() == daytime.date()
    
    def test_invalid_market_code(self):
        """测试无效市场代码"""
        timestamp = datetime(2026, 3, 10, 10, 0, 0)
        
        # 无效代码应该返回 False 或抛出异常
        with pytest.raises(ValueError):
            self.manager.is_trading_session("INVALID", timestamp)
    
    def test_current_time_default(self):
        """测试不传 timestamp 时使用当前时间"""
        # 不传 timestamp 应该使用当前时间
        # 这个测试主要用于验证 API 的默认参数行为
        result = self.manager.is_trading_session(self.hk_code)
        assert isinstance(result, bool)
