"""交易模式回归测试 - 验证模拟/实盘切换安全性"""
import pytest
import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestTradingMode:
    """测试 TradingMode 管理器"""

    def test_default_is_simulate(self):
        """默认应为模拟模式"""
        # 重置单例
        from app.core import trading_mode
        trading_mode.TradingMode._instance = None

        os.environ['TRADE_ENV'] = '1'
        tm = trading_mode.TradingMode()
        assert tm.is_simulate
        assert not tm.is_live
        assert tm.mode_label == '模拟盘'

        # 清理
        trading_mode.TradingMode._instance = None

    def test_risk_multiplier_simulate(self):
        """模拟盘风控倍数应为1.0"""
        from app.core import trading_mode
        trading_mode.TradingMode._instance = None

        os.environ['TRADE_ENV'] = '1'
        tm = trading_mode.TradingMode()
        assert tm.get_risk_multiplier() == 1.0

        trading_mode.TradingMode._instance = None

    def test_conservative_mode_restricts(self):
        """保守模式应限制仓位"""
        from app.core import trading_mode
        trading_mode.TradingMode._instance = None

        os.environ['TRADE_ENV'] = '0'
        os.environ['LIVE_CONSERVATIVE'] = '1'
        tm = trading_mode.TradingMode()

        assert tm.is_live
        assert tm.is_conservative
        assert tm.get_risk_multiplier() == 0.5
        assert tm.get_max_position_size(10000) == 5000

        # 清理
        os.environ['TRADE_ENV'] = '1'
        os.environ.pop('LIVE_CONSERVATIVE', None)
        trading_mode.TradingMode._instance = None


class TestLiveReadinessCheck:
    """测试实盘就绪检查"""

    def test_checker_handles_missing_db(self):
        """数据库不存在时应安全失败"""
        from scripts.live_readiness_check import LiveReadinessChecker
        checker = LiveReadinessChecker(db_path='/tmp/nonexistent_test.db')
        results = checker.check_all()
        assert not results.get('all_passed', True)
