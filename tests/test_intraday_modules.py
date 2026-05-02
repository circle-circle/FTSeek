"""
Intraday 模块测试

测试范围：
1. IntradayPositionManager - 持仓管理（止损/止盈）
2. SignalProcessor - 信号处理
3. WorkerPool - Worker 线程池
4. MarketSubscriber - 行情订阅
5. IntradayCoordinator - 协调器集成
"""

import pytest
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime


# ============================================================
# 1. IntradayPositionManager 测试
# ============================================================

class TestIntradayPositionManager:
    """盘中持仓管理器测试"""
    
    @pytest.fixture
    def mocks(self):
        """创建模拟依赖"""
        position_manager = MagicMock()
        trade_executor = MagicMock()
        notifier = MagicMock()
        
        # 模拟持仓
        position = MagicMock()
        position.code = "HK.00700"
        position.entry_price = 500.0
        position.quantity = 100
        position.action = "BUY"
        position.stop_loss = 485.0
        position.take_profit = 550.0
        
        position_manager.get_position.return_value = position
        
        return {
            "position_manager": position_manager,
            "trade_executor": trade_executor,
            "notifier": notifier,
            "position": position,
        }
    
    def test_stop_loss_trigger(self, mocks):
        """测试止损触发"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 当前价格低于止损价
        current_price = 480.0
        
        # Mock 配置
        with patch('app.intraday.position_manager.cfg') as mock_cfg:
            mock_cfg.get.return_value = 0.03  # 3% 止损
            
            result = pm.check_and_execute_stop_loss("HK.00700", current_price)
            
            # 验证止损被触发
            assert result == True
            mocks["trade_executor"].close_position.assert_called_once()
    
    def test_stop_loss_not_triggered(self, mocks):
        """测试止损未触发"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 当前价格高于止损价
        current_price = 490.0
        
        with patch('app.intraday.position_manager.cfg') as mock_cfg:
            mock_cfg.get.return_value = 0.03
            
            result = pm.check_and_execute_stop_loss("HK.00700", current_price)
            
            # 验证止损未触发
            assert result == False
            mocks["trade_executor"].close_position.assert_not_called()
    
    def test_take_profit_trigger(self, mocks):
        """测试止盈触发"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 当前价格高于止盈价
        current_price = 560.0
        
        with patch('app.intraday.position_manager.cfg') as mock_cfg:
            mock_cfg.get.return_value = 0.1  # 10% 止盈
            
            result = pm.check_and_execute_take_profit("HK.00700", current_price)
            
            # 验证止盈被触发
            assert result == True
            mocks["trade_executor"].close_position.assert_called_once()
    
    def test_round_to_lot(self, mocks):
        """测试整手数量计算"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 150 股，每手 100 股 → 200 股
        assert pm._round_to_lot(150, 100) == 200
        
        # 200 股，每手 100 股 → 200 股
        assert pm._round_to_lot(200, 100) == 200
        
        # 50 股，每手 100 股 → 100 股
        assert pm._round_to_lot(50, 100) == 100
    
    def test_stop_loss_cooldown(self, mocks):
        """测试止损冷却"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 设置冷却
        pm._stop_loss_cooldown["HK.00700"] = time.time()
        
        with patch('app.intraday.position_manager.cfg') as mock_cfg:
            mock_cfg.get.return_value = 0.03
            
            result = pm.check_and_execute_stop_loss("HK.00700", 480.0)
            
            # 冷却期间不应触发
            assert result == False
    
    def test_cleanup_code_state(self, mocks):
        """测试状态清理"""
        from app.intraday.position_manager import IntradayPositionManager
        
        pm = IntradayPositionManager(
            position_manager=mocks["position_manager"],
            trade_executor=mocks["trade_executor"],
        )
        
        # 设置一些状态
        pm._stop_loss_cooldown["HK.00700"] = time.time()
        pm._stop_loss_fail_count["HK.00700"] = 2
        pm._trailing_stop_cache["HK.00700"] = 490.0
        
        # 清理
        pm._cleanup_code_state("HK.00700")
        
        # 验证已清理
        assert "HK.00700" not in pm._stop_loss_cooldown
        assert "HK.00700" not in pm._stop_loss_fail_count
        assert "HK.00700" not in pm._trailing_stop_cache


# ============================================================
# 2. SignalProcessor 测试
# ============================================================

class TestSignalProcessor:
    """信号处理器测试"""
    
    @pytest.fixture
    def consensus_engine(self):
        """创建模拟共识引擎"""
        return MagicMock()
    
    def test_submit_signal(self, consensus_engine):
        """测试信号提交"""
        from app.intraday.signal_processor import SignalProcessor
        
        sp = SignalProcessor(consensus_engine)
        
        signal = MagicMock()
        signal.code = "HK.00700"
        signal.strategy = "test"
        signal.action.value = "BUY"
        
        sp.submit_signal(signal)
        
        consensus_engine.submit.assert_called_once_with(signal)
    
    def test_mtf_trend_update(self, consensus_engine):
        """测试多周期趋势更新"""
        from app.intraday.signal_processor import SignalProcessor
        
        sp = SignalProcessor(consensus_engine)
        
        # 提供足够的 K 线数据
        for i in range(20):
            kline = {"close": 500 + i, "high": 505 + i, "low": 495 + i}
            sp.update_mtf_trend("HK.00700", "K_1M", kline)
        
        # 验证趋势已更新
        trends = sp.get_mtf_trends("HK.00700")
        assert "K_1M" in trends
    
    def test_mtf_alignment(self, consensus_engine):
        """测试多周期对齐度"""
        from app.intraday.signal_processor import SignalProcessor
        
        sp = SignalProcessor(consensus_engine)
        
        # 设置趋势
        sp._mtf_trends["HK.00700"] = {
            "K_1M": "up",
            "K_5M": "up",
            "K_15M": "up",
            "K_30M": "up",
            "K_60M": "up",
            "K_DAY": "up",
        }
        
        alignment = sp.get_mtf_alignment("HK.00700")
        
        assert alignment["aligned"] == True
        assert alignment["direction"] == "up"
        assert alignment["score"] == 1.0


# ============================================================
# 3. WorkerPool 测试
# ============================================================

class TestWorkerPool:
    """Worker 线程池测试"""
    
    def test_start_stop(self):
        """测试启动停止"""
        from app.intraday.worker_pool import WorkerPool
        
        pool = WorkerPool()
        pool.start()
        
        assert pool.is_running == True
        
        pool.stop()
        assert pool.is_running == False
    
    def test_push_data(self):
        """测试数据入队"""
        from app.intraday.worker_pool import WorkerPool
        
        pool = WorkerPool()
        pool.start()
        
        # 推入数据
        assert pool.push_quote({"code": "HK.00700", "price": 500}) == True
        assert pool.push_kline({"code": "HK.00700", "close": 500}) == True
        
        pool.stop()
    
    def test_health_check(self):
        """测试健康检查"""
        from app.intraday.worker_pool import WorkerPool
        
        pool = WorkerPool()
        pool.start()
        
        health = pool.check_health()
        
        assert health["running"] == True
        assert health["quote_worker_alive"] == True
        assert health["kline_worker_alive"] == True
        
        pool.stop()
    
    def test_queue_full(self):
        """测试队列满"""
        from app.intraday.worker_pool import WorkerPool
        
        # 创建小队列
        pool = WorkerPool(quote_maxsize=1, kline_maxsize=1)
        pool.start()
        
        # 填满队列
        pool.push_quote({"data": 1})
        
        # 再次推入应失败
        assert pool.push_quote({"data": 2}) == False
        
        pool.stop()


# ============================================================
# 4. IntradayCoordinator 测试
# ============================================================

class TestIntradayCoordinator:
    """盘中协调器测试"""
    
    @pytest.fixture
    def dependencies(self):
        """创建模拟依赖"""
        return {
            "position_manager": MagicMock(),
            "trade_executor": MagicMock(),
            "consensus_engine": MagicMock(),
            "notifier": MagicMock(),
        }
    
    def test_coordinator_creation(self, dependencies):
        """测试协调器创建"""
        from app.intraday.coordinator import IntradayCoordinator
        
        coordinator = IntradayCoordinator(**dependencies)
        
        assert coordinator.is_running == False
        assert coordinator._pm is dependencies["position_manager"]
        assert coordinator._te is dependencies["trade_executor"]
    
    def test_ensure_modules(self, dependencies):
        """测试模块延迟初始化"""
        from app.intraday.coordinator import IntradayCoordinator
        
        coordinator = IntradayCoordinator(**dependencies)
        coordinator._ensure_modules()
        
        assert coordinator._market_subscriber is not None
        assert coordinator._signal_processor is not None
        assert coordinator._position_manager is not None
        assert coordinator._worker_pool is not None
    
    def test_health_status(self, dependencies):
        """测试健康状态"""
        from app.intraday.coordinator import IntradayCoordinator
        
        coordinator = IntradayCoordinator(**dependencies)
        
        status = coordinator.get_health_status()
        
        assert status["running"] == False
        assert "worker_pool" in status or status == {"running": False, "battle_plan": None}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
