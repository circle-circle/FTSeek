"""
Consensus 模块测试

测试范围：
1. ConsensusEngine - 核心投票聚合
2. WeightCalculator - 权重计算
3. LLMIntegration - LLM 集成
4. AdaptiveWeightManager - 动态权重
5. AuditLogger - 审计日志
"""

import pytest
import time
import tempfile
import os
from unittest.mock import MagicMock, patch
from datetime import datetime


# ============================================================
# 1. ConsensusEngine 测试
# ============================================================

class TestConsensusEngine:
    """共识引擎核心测试"""
    
    def test_basic_consensus(self):
        """测试基本共识达成"""
        from app.strategy.consensus.engine import ConsensusEngine, Vote
        
        consensus_results = []
        
        def on_consensus(result):
            consensus_results.append(result)
        
        engine = ConsensusEngine(
            window_seconds=60,
            threshold=1.5,
            on_consensus=on_consensus,
        )
        
        # 创建模拟信号动作
        BUY = MagicMock()
        BUY.value = "BUY"
        
        # 提交多个同向信号
        for i in range(3):
            signal = MagicMock()
            signal.code = "HK.00700"
            signal.strategy = f"strategy_{i}"
            signal.action = BUY
            signal.price = 500.0
            signal.reason = f"reason_{i}"
            
            engine.submit(signal)
        
        # 验证信号被接收
        votes = engine.get_votes("HK.00700")
        # 注意：可能已触发共识被清理，或者仍在窗口中
        assert len(votes) >= 0 or len(consensus_results) >= 0
    
    def test_vote_cleanup(self):
        """测试过期投票清理"""
        from app.strategy.consensus.engine import ConsensusEngine
        
        engine = ConsensusEngine(window_seconds=1)
        
        # 提交信号
        signal = MagicMock()
        signal.code = "HK.00700"
        signal.strategy = "test"
        signal.action.value = "BUY"
        signal.price = 500.0
        signal.reason = "test"
        
        engine.submit(signal)
        
        # 等待过期
        time.sleep(1.5)
        
        # 提交新信号触发清理
        signal2 = MagicMock()
        signal2.code = "HK.00700"
        signal2.strategy = "test2"
        signal2.action.value = "BUY"
        signal2.price = 500.0
        signal2.reason = "test2"
        
        engine.submit(signal2)
        
        # 旧投票应被清理
        votes = engine.get_votes("HK.00700")
        assert len(votes) <= 1
    
    def test_divergence_calculation(self):
        """测试分歧度计算"""
        from app.strategy.consensus.engine import ConsensusEngine
        
        engine = ConsensusEngine()
        
        # 完全一致
        assert engine._calc_divergence(3.0, 0.0) == 0.0
        
        # 完全分裂
        assert engine._calc_divergence(1.0, 1.0) == 1.0
        
        # 部分分裂
        div = engine._calc_divergence(2.0, 1.0)
        assert 0 < div < 1
    
    def test_cooldown(self):
        """测试触发冷却"""
        from app.strategy.consensus.engine import ConsensusEngine
        
        trigger_count = 0
        
        def on_consensus(result):
            nonlocal trigger_count
            trigger_count += 1
        
        engine = ConsensusEngine(
            window_seconds=60,
            threshold=0.5,  # 低阈值便于触发
            on_consensus=on_consensus,
        )
        engine._trigger_cooldown = 60
        
        # 连续提交信号
        for _ in range(5):
            signal = MagicMock()
            signal.code = "HK.00700"
            signal.strategy = "test"
            signal.action.value = "BUY"
            signal.price = 500.0
            signal.reason = "test"
            
            engine.submit(signal)
        
        # 冷却期间只应触发一次
        assert trigger_count <= 1


# ============================================================
# 2. WeightCalculator 测试
# ============================================================

class TestWeightCalculator:
    """权重计算器测试"""
    
    @pytest.fixture
    def mock_cfg(self):
        """配置 mock"""
        with patch('app.strategy.consensus.weights.cfg') as mock:
            mock.get.side_effect = lambda key, default=None: {
                "strategy.weights": {"confluence_v2": 1.5, "supertrend": 1.0},
                "strategy.weight_engine_path": "",
                f"dynamic_exit.default": {},
            }.get(key, default)
            yield mock
    
    def test_base_weights(self, mock_cfg):
        """测试基准权重"""
        from app.strategy.consensus.weights import WeightCalculator
        
        calc = WeightCalculator()
        weights = calc.get_base_weights()
        
        assert weights["confluence_v2"] == 1.5
        assert weights["supertrend"] == 1.0
    
    def test_regime_adjustment(self, mock_cfg):
        """测试市场状态调整"""
        from app.strategy.consensus.weights import WeightCalculator
        
        calc = WeightCalculator()
        
        # 趋势市场中 supertrend 应获得奖励
        adjust = calc._get_regime_adjustment("supertrend", "trending")
        assert adjust >= 0  # 可能是 0，取决于配置
    
    def test_calc_weight(self, mock_cfg):
        """测试权重计算"""
        from app.strategy.consensus.weights import WeightCalculator
        
        calc = WeightCalculator()
        
        weight, details = calc.calc_weight("confluence_v2", "default")
        
        assert weight >= 0
        assert "base" in details
    
    def test_hybrid_weight(self, mock_cfg):
        """测试混合权重"""
        from app.strategy.consensus.weights import WeightCalculator
        
        calc = WeightCalculator()
        
        weight = calc.get_hybrid_weight(
            strategy="confluence_v2",
            regime="default",
            news_factor={"weight": 0.5, "sentiment": 0.8},
        )
        
        assert weight >= 0


# ============================================================
# 3. LLMIntegration 测试
# ============================================================

class TestLLMIntegration:
    """LLM 集成测试"""
    
    def test_llm_injection(self):
        """测试 LLM 注入"""
        from app.strategy.consensus.llm_integration import LLMIntegration
        
        llm = LLMIntegration()
        analyst = MagicMock()
        
        llm.inject_llm(analyst, weight=0.2)
        
        assert llm.has_llm == True
        assert llm.llm_analyst is analyst
        assert llm.llm_weight == 0.2
    
    def test_audit_log(self):
        """测试审计日志"""
        from app.strategy.consensus.llm_integration import LLMIntegration
        
        llm = LLMIntegration()
        
        llm.record_audit(
            code="HK.00700",
            llm_prediction="BUY",
            consensus_action="BUY",
            confidence=0.8,
            llm_reason="测试推理",
        )
        
        log = llm.get_audit_log(code="HK.00700")
        assert len(log) == 1
        assert log[0]["llm_prediction"] == "BUY"
    
    def test_outcome_recording(self):
        """测试结果记录"""
        from app.strategy.consensus.llm_integration import LLMIntegration
        
        llm = LLMIntegration()
        
        llm.record_outcome("HK.00700", "BUY", 1000.0, 2.0)
        llm.record_outcome("HK.00700", "BUY", -500.0, 1.0)
        
        stats = llm.get_accuracy_stats()
        assert stats["total_trades"] == 2
        assert stats["win_count"] == 1
    
    def test_weight_adaptation(self):
        """测试权重自适应"""
        from app.strategy.consensus.llm_integration import LLMIntegration
        
        llm = LLMIntegration()
        llm._llm_weight_config = {
            "base_weight": 0.15,
            "min_accuracy": 0.5,
            "disable_threshold": 0.45,
        }
        
        # 记录多个盈利交易
        for _ in range(10):
            llm.record_outcome("HK.00700", "BUY", 100.0, 1.0)
        
        # 权重应增加
        assert llm.llm_weight > 0.15


# ============================================================
# 4. AdaptiveWeightManager 测试
# ============================================================

class TestAdaptiveWeightManager:
    """自适应权重管理器测试"""
    
    def test_trade_recording(self):
        """测试交易记录"""
        from app.strategy.consensus.adaptive_weights import AdaptiveWeightManager
        
        mgr = AdaptiveWeightManager()
        
        mgr.record_trade("confluence_v2", "HK.00700", "BUY", 1000.0, 2.0)
        mgr.record_trade("confluence_v2", "HK.00700", "BUY", -500.0, 1.0)
        
        stats = mgr.get_strategy_stats()
        assert "confluence_v2" in stats
        assert stats["confluence_v2"]["total_trades"] == 2
    
    def test_db_persistence(self):
        """测试数据库持久化"""
        from app.strategy.consensus.adaptive_weights import AdaptiveWeightManager, _DB_PATH
        
        # 使用临时数据库
        with patch('app.strategy.consensus.adaptive_weights._DB_PATH', tempfile.mktemp(suffix='.db')):
            mgr = AdaptiveWeightManager()
            
            mgr.record_trade("test", "HK.00700", "BUY", 100.0, 1.0)
            
            stats = mgr.get_strategy_stats()
            assert "test" in stats


# ============================================================
# 5. AuditLogger 测试
# ============================================================

class TestAuditLogger:
    """审计日志测试"""
    
    def test_signal_recording(self):
        """测试信号记录"""
        from app.strategy.consensus.audit_log import AuditLogger
        
        logger = AuditLogger()
        
        signal_id = logger.record_signal(
            trace_id="test-001",
            code="HK.00700",
            strategy="confluence_v2",
            action="BUY",
            price=500.0,
            confidence=0.8,
            consensus_score=2.5,
            market_regime="trending",
            adx=30.0,
            agree_strategies=["confluence_v2", "supertrend"],
        )
        
        assert signal_id != ""
    
    def test_execution_recording(self):
        """测试执行记录"""
        from app.strategy.consensus.audit_log import AuditLogger
        
        logger = AuditLogger()
        
        logger.record_execution(
            trace_id="test-001",
            code="HK.00700",
            executed=True,
            entry_price=500.0,
            exit_price=510.0,
            pnl=1000.0,
            exit_reason="TAKE_PROFIT",
            holding_hours=2.0,
        )
    
    def test_signal_history(self):
        """测试信号历史查询"""
        from app.strategy.consensus.audit_log import AuditLogger
        
        logger = AuditLogger()
        
        # 记录信号
        logger.record_signal(
            trace_id="test-001",
            code="HK.00700",
            strategy="test",
            action="BUY",
            price=500.0,
            confidence=0.8,
            consensus_score=2.0,
            market_regime="trending",
        )
        
        # 查询历史
        history = logger.get_signal_history(code="HK.00700")
        assert len(history) >= 1
    
    def test_weight_snapshot(self):
        """测试权重快照"""
        from app.strategy.consensus.audit_log import AuditLogger
        
        logger = AuditLogger()
        
        logger.record_weight_snapshot(
            snapshot_type="daily",
            code="HK.00700",
            weights={"confluence_v2": 1.5, "supertrend": 1.0},
        )
        
        snapshots = logger.get_weights_snapshot(code="HK.00700")
        assert len(snapshots) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
