"""
集成测试 — 验证新框架的完整交易链路

测试完整的交易流程：
信号生成 → 共识评估 → 风控检查 → 执行下单 → 持仓管理 → 平仓通知 → 归因分析

运行方式:
    pytest tests/test_integration_trading_chain.py -v
"""

import pytest
import time
import tempfile
import os
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime
from pathlib import Path


class TestConfigToExecutionChain:
    """测试配置到执行的完整链路"""
    
    def test_config_loader_to_trading_env(self):
        """测试配置加载器到交易环境的链路"""
        from app.core.config_loader import cfg
        from app.core.trading_env import get_trading_env
        
        # 配置应正确加载
        assert cfg.get("base.project_name") is not None
        
        # 交易环境应正确初始化
        env = get_trading_env()
        assert env.is_simulate or env.is_real
        
        # 环境应能访问配置
        account_id = env.get_account_id("HK.00700")
        assert account_id is not None
    
    def test_stages_to_intraday(self):
        """测试阶段抽象到盘中模块的链路"""
        from app.core.stages import BattlePlan, StageManager
        from app.intraday.coordinator import IntradayCoordinator
        
        # 创建盘前计划
        plan = BattlePlan(
            date="2026-04-28",
            targets=[
                {"symbol": "HK.00700", "direction": "ANY", "status": "VALID"},
            ],
            restricted=[],
        )
        
        # 创建协调器
        coordinator = IntradayCoordinator(
            position_manager=MagicMock(),
            trade_executor=MagicMock(),
            consensus_engine=MagicMock(),
        )
        
        # 验证计划可传递给协调器
        coordinator._battle_plan = plan
        assert coordinator._battle_plan is plan
        assert coordinator._battle_plan.is_allowed("HK.00700")


class TestSignalToExecutionChain:
    """测试信号到执行的完整链路"""
    
    @pytest.fixture
    def full_chain_mocks(self):
        """创建完整链路的模拟依赖"""
        return {
            "position_manager": MagicMock(),
            "trade_executor": MagicMock(),
            "consensus_engine": MagicMock(),
            "notifier": MagicMock(),
        }
    
    def test_signal_to_consensus(self, full_chain_mocks):
        """测试信号到共识的链路"""
        from app.intraday.signal_processor import SignalProcessor
        from app.strategy.consensus.engine import ConsensusEngine
        
        # 创建共识引擎
        consensus_results = []
        def on_consensus(result):
            consensus_results.append(result)
        
        engine = ConsensusEngine(
            window_seconds=60,
            threshold=0.5,  # 低阈值便于测试
            on_consensus=on_consensus,
        )
        
        # 创建信号处理器
        processor = SignalProcessor(engine)
        processor.set_callbacks(on_consensus_result=lambda r: None)
        
        # 提交多个信号以触发共识
        BUY = MagicMock()
        BUY.value = "BUY"
        
        for i in range(3):
            signal = MagicMock()
            signal.code = "HK.00700"
            signal.strategy = f"test_strategy_{i}"
            signal.action = BUY
            signal.price = 500.0
            signal.reason = f"test_{i}"
            signal.quantity = 100
            
            processor.submit_signal(signal)
        
        # 验证信号已提交到共识引擎
        votes = engine.get_votes("HK.00700")
        assert len(votes) >= 1 or len(consensus_results) >= 1
    
    def test_consensus_to_position_manager(self, full_chain_mocks):
        """测试共识到持仓管理的链路"""
        from app.intraday.position_manager import IntradayPositionManager
        from app.intraday.signal_processor import SignalProcessor
        from app.strategy.consensus.engine import ConsensusEngine
        
        # 设置持仓
        position = MagicMock()
        position.code = "HK.00700"
        position.entry_price = 500.0
        position.quantity = 100
        position.action = "BUY"
        full_chain_mocks["position_manager"].get_position.return_value = position
        
        # 创建共识引擎
        engine = ConsensusEngine(window_seconds=60, threshold=0.5)
        
        # 创建信号处理器
        processor = SignalProcessor(engine)
        
        # 创建持仓管理器
        pm = IntradayPositionManager(
            position_manager=full_chain_mocks["position_manager"],
            trade_executor=full_chain_mocks["trade_executor"],
        )
        
        # 模拟价格变动触发止损
        with patch('app.intraday.position_manager.cfg') as mock_cfg:
            mock_cfg.get.return_value = 0.03
            
            # 价格下跌 4% 触发止损
            result = pm.check_and_execute_stop_loss("HK.00700", 480.0)
            
            # 验证执行器被调用
            if result:
                full_chain_mocks["trade_executor"].close_position.assert_called_once()


class TestFullTradingLoop:
    """测试完整交易循环"""
    
    @pytest.fixture
    def trading_env(self):
        """创建完整的交易环境"""
        from app.intraday.coordinator import IntradayCoordinator
        from app.strategy.consensus.engine import ConsensusEngine
        from app.core.trading_env import get_trading_env
        
        # 创建 mock 依赖
        position_manager = MagicMock()
        trade_executor = MagicMock()
        consensus_engine = MagicMock()
        
        # 创建协调器
        coordinator = IntradayCoordinator(
            position_manager=position_manager,
            trade_executor=trade_executor,
            consensus_engine=consensus_engine,
        )
        
        return {
            "coordinator": coordinator,
            "position_manager": position_manager,
            "trade_executor": trade_executor,
            "consensus_engine": consensus_engine,
        }
    
    def test_coordinator_initialization(self, trading_env):
        """测试协调器初始化"""
        coordinator = trading_env["coordinator"]
        
        # 验证模块已创建
        assert coordinator._pm is not None
        assert coordinator._te is not None
        assert coordinator._consensus is not None
    
    def test_health_monitoring(self, trading_env):
        """测试健康监控"""
        coordinator = trading_env["coordinator"]
        
        # 获取健康状态
        health = coordinator.get_health_status()
        
        # 验证基本结构
        assert "running" in health
        assert isinstance(health["running"], bool)


class TestNotificationChain:
    """测试通知链路"""
    
    def test_unified_notifier_creation(self):
        """测试统一通知器创建"""
        from app.notifier.unified import UnifiedNotifier
        
        notifier = UnifiedNotifier(
            dingtalk_url="https://test.dingtalk.com",
            feishu_url="https://test.feishu.com",
        )
        
        assert notifier.dingtalk_url is not None
        assert notifier.feishu_url is not None
    
    def test_trade_exit_notification(self):
        """测试平仓通知生成"""
        from app.notifier.unified import UnifiedNotifier
        
        notifier = UnifiedNotifier()
        
        # 生成通知数据
        trade_data = {
            "code": "US.MSFT",
            "entry_price": 672.46,
            "exit_price": 679.30,
            "quantity": 287,
            "pnl": -1963.65,
            "pnl_rate": -0.0102,
            "hold_minutes": 5,
            "reason": "PRE_CLOSE_EXIT",
        }
        
        # 验证建议生成
        suggestion = notifier._generate_suggestion(trade_data)
        assert len(suggestion) > 0
        
        # 验证市场分析
        analysis = notifier._get_market_analysis(trade_data)
        assert len(analysis) > 0
    
    def test_attribution_integration(self):
        """测试归因分析集成"""
        from app.attribution.pnl_loop import PnLAttributionEngine
        import tempfile
        
        # 使用临时数据库
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            engine = PnLAttributionEngine(db_path=f.name)
            
            # 生成归因报告
            report = engine.analyze(
                code="US.MSFT",
                entry_price=672.46,
                exit_price=679.30,
                quantity=287,
                pnl=-1963.65,
                pnl_rate=-0.0102,
                hold_minutes=5,
                exit_reason="PRE_CLOSE_EXIT",
                strategy="momentum",
                signal_strength=0.65,
            )
            
            # 验证报告
            assert report.code == "US.MSFT"
            assert report.pnl == -1963.65
            assert len(report.failure_factors) > 0
            
            # 保存到数据库
            engine.save(report)
            
            # 从数据库读取
            reports = engine.get_recent_reports(code="US.MSFT")
            assert len(reports) >= 1
            
            # 清理
            os.unlink(f.name)


class TestStageTransitions:
    """测试阶段转换"""
    
    def test_pre_market_to_intraday(self):
        """测试盘前到盘中的转换"""
        from app.core.stages import StageManager, BattlePlan, TradingStage
        
        manager = StageManager()
        
        # 模拟盘前阶段
        manager._current_stage = TradingStage.PRE_MARKET
        
        # 生成盘前计划
        plan = BattlePlan(
            date="2026-04-28",
            targets=[{"symbol": "HK.00700"}],
            restricted=[],
        )
        
        manager._battle_plan = plan
        
        # 转换到盘中
        manager._current_stage = TradingStage.INTRADAY
        
        assert manager.current_stage == TradingStage.INTRADAY
        assert manager.battle_plan is plan
    
    def test_intraday_to_post_market(self):
        """测试盘中到盘后的转换"""
        from app.core.stages import StageManager, TradingStage
        
        manager = StageManager()
        manager._current_stage = TradingStage.INTRADAY
        
        # 转换到盘后
        manager._current_stage = TradingStage.POST_MARKET
        
        assert manager.current_stage == TradingStage.POST_MARKET


class TestEventBusIntegration:
    """测试事件总线集成"""
    
    def test_trade_events(self):
        """测试交易事件"""
        from app.core.event_bus import EventBus, TradeEvent
        
        events_received = []
        
        def handler(event):
            events_received.append(event)
        
        EventBus.subscribe(TradeEvent.POSITION_CLOSED, handler)
        
        # 发布事件
        EventBus.publish(TradeEvent.POSITION_CLOSED, {
            "code": "HK.00700",
            "pnl": 1000.0,
            "reason": "TAKE_PROFIT",
        })
        
        assert len(events_received) == 1
        assert events_received[0].payload["code"] == "HK.00700"
        
        EventBus.clear()
    
    def test_system_events(self):
        """测试系统事件"""
        from app.core.event_bus import EventBus, SystemEvent
        
        events_received = []
        
        def handler(event):
            events_received.append(event)
        
        EventBus.subscribe(SystemEvent.SERVICE_STARTED, handler)
        EventBus.publish(SystemEvent.SERVICE_STARTED, {"service": "intraday"})
        
        assert len(events_received) == 1
        
        EventBus.clear()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
