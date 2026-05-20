"""
投票日志测试

测试投票日志记录器的功能
"""
import pytest
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.models import SignalScore, Direction, MarketRegime
from app.storage.vote_tables import VoteTables
from app.decision.vote_logger import VoteLogger, VoteRecord, DecisionRecord


@pytest.fixture
def temp_db():
    """创建临时数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def vote_tables(temp_db):
    """创建 VoteTables 实例"""
    return VoteTables(temp_db)


@pytest.fixture
def vote_logger(vote_tables):
    """创建 VoteLogger 实例"""
    return VoteLogger(vote_tables)


@pytest.fixture
def sample_signal():
    """创建示例信号"""
    return SignalScore(
        strategy_id="test_strategy",
        symbol="AAPL",
        direction=Direction.LONG,
        strength=4,
        confidence=0.85,
        expected_horizon=60,
        market_regime=MarketRegime.TREND_UP,
        risk_reward_estimate=2.5,
    )


class TestVoteTables:
    """测试 VoteTables 类"""
    
    def test_init_creates_tables(self, temp_db):
        """测试初始化创建表"""
        vote_tables = VoteTables(temp_db)
        
        # 验证表存在
        import sqlite3
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        
        assert "vote_logs" in tables
        assert "decision_logs" in tables
        conn.close()
    
    def test_record_vote(self, vote_tables):
        """测试记录投票"""
        vote_id = vote_tables.record_vote(
            trade_id="AAPL_20260328_120000_abc123",
            agent_id="strategy_1",
            vote_direction="LONG",
            vote_weight=0.25,
            vote_strength=4,
            vote_confidence=0.85,
            vote_reason="Strong bullish signal",
        )
        
        assert vote_id is not None
        assert vote_id > 0
    
    def test_record_decision(self, vote_tables):
        """测试记录决策"""
        decision_id = vote_tables.record_decision(
            trade_id="AAPL_20260328_120000_abc123",
            symbol="AAPL",
            final_decision="LONG",
            consensus_level=0.75,
            total_weight=1.0,
            long_weight=0.75,
            short_weight=0.15,
            contributing_agents=5,
            decision_confidence=0.80,
            market_regime="BULL",
        )
        
        assert decision_id is not None
        assert decision_id > 0
    
    def test_query_votes_by_trade(self, vote_tables):
        """测试按交易 ID 查询投票"""
        # 插入测试数据
        vote_tables.record_vote(
            trade_id="TEST_TRADE_1",
            agent_id="agent_1",
            vote_direction="LONG",
            vote_weight=0.25,
        )
        vote_tables.record_vote(
            trade_id="TEST_TRADE_1",
            agent_id="agent_2",
            vote_direction="SHORT",
            vote_weight=0.20,
        )
        vote_tables.record_vote(
            trade_id="TEST_TRADE_2",
            agent_id="agent_1",
            vote_direction="LONG",
            vote_weight=0.25,
        )
        
        # 查询
        votes = vote_tables.query_votes_by_trade("TEST_TRADE_1")
        
        assert len(votes) == 2
        assert votes[0]["agent_id"] == "agent_1"
        assert votes[1]["agent_id"] == "agent_2"
    
    def test_query_votes_by_agent(self, vote_tables):
        """测试按 Agent 查询投票"""
        # 插入测试数据
        vote_tables.record_vote(
            trade_id="trade_1",
            agent_id="test_agent",
            vote_direction="LONG",
            vote_weight=0.25,
        )
        vote_tables.record_vote(
            trade_id="trade_2",
            agent_id="test_agent",
            vote_direction="SHORT",
            vote_weight=0.20,
        )
        vote_tables.record_vote(
            trade_id="trade_3",
            agent_id="other_agent",
            vote_direction="LONG",
            vote_weight=0.15,
        )
        
        # 查询
        votes = vote_tables.query_votes_by_agent("test_agent", limit=10)
        
        assert len(votes) == 2
        assert all(v["agent_id"] == "test_agent" for v in votes)
    
    def test_query_votes_by_time_range(self, vote_tables):
        """测试按时间范围查询投票"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 插入测试数据
        vote_tables.record_vote(
            trade_id="trade_1",
            agent_id="agent_1",
            vote_direction="LONG",
            vote_weight=0.25,
        )
        
        # 查询
        votes = vote_tables.query_votes_by_time_range(start, end, limit=10)
        
        assert len(votes) >= 1
    
    def test_query_decisions_by_trade(self, vote_tables):
        """测试按交易 ID 查询决策"""
        # 插入测试数据
        vote_tables.record_decision(
            trade_id="TEST_TRADE_1",
            symbol="AAPL",
            final_decision="LONG",
            consensus_level=0.75,
            total_weight=1.0,
            long_weight=0.75,
            short_weight=0.15,
            contributing_agents=5,
        )
        
        # 查询
        decision = vote_tables.query_decisions_by_trade("TEST_TRADE_1")
        
        assert decision is not None
        assert decision["symbol"] == "AAPL"
        assert decision["final_decision"] == "LONG"
    
    def test_get_decision_statistics(self, vote_tables):
        """测试获取决策统计"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 插入测试数据
        vote_tables.record_decision(
            trade_id="trade_1",
            symbol="AAPL",
            final_decision="LONG",
            consensus_level=0.75,
            total_weight=1.0,
            long_weight=0.75,
            short_weight=0.15,
            contributing_agents=5,
            decision_confidence=0.80,
        )
        vote_tables.record_decision(
            trade_id="trade_2",
            symbol="GOOG",
            final_decision="SHORT",
            consensus_level=0.65,
            total_weight=1.0,
            long_weight=0.20,
            short_weight=0.65,
            contributing_agents=4,
            decision_confidence=0.70,
        )
        
        # 查询统计
        stats = vote_tables.get_decision_statistics(start, end)
        
        assert stats["total"] == 2
        assert stats["long_count"] == 1
        assert stats["short_count"] == 1
        assert stats["avg_consensus"] > 0


class TestVoteLogger:
    """测试 VoteLogger 类"""
    
    def test_generate_trade_id(self, vote_logger):
        """测试生成交易 ID"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        assert trade_id.startswith("AAPL_")
        assert len(trade_id) > 15  # 包含时间戳和 UUID
    
    def test_record_agent_vote(self, vote_logger, sample_signal):
        """测试记录 Agent 投票"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        vote = vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="test_strategy",
            signal=sample_signal,
            weight=0.25,
            reason="Test vote",
        )
        
        assert vote.trade_id == trade_id
        assert vote.agent_id == "test_strategy"
        assert vote.vote_direction == "LONG"
        assert vote.vote_weight == 0.25
        assert vote.vote_strength == 4
        assert vote.vote_confidence == 0.85
    
    def test_record_decision(self, vote_logger, sample_signal):
        """测试记录决策"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        # 先记录投票
        vote = vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="test_strategy",
            signal=sample_signal,
            weight=0.25,
        )
        
        # 记录决策
        decision = vote_logger.record_decision(
            trade_id=trade_id,
            symbol="AAPL",
            final_decision=Direction.LONG,
            consensus_level=0.75,
            total_weight=1.0,
            long_weight=0.75,
            short_weight=0.15,
            votes=[vote],
            decision_confidence=0.80,
            market_regime=MarketRegime.TREND_UP,
        )
        
        assert decision.trade_id == trade_id
        assert decision.symbol == "AAPL"
        assert decision.final_decision == "LONG"
        assert decision.consensus_level == 0.75
        assert decision.contributing_agents == 1
    
    def test_get_votes_for_trade(self, vote_logger, sample_signal):
        """测试获取交易的投票"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        # 记录投票
        vote1 = vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="strategy_1",
            signal=sample_signal,
            weight=0.25,
        )
        vote2 = vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="strategy_2",
            signal=sample_signal,
            weight=0.20,
        )
        
        # 查询
        votes = vote_logger.get_votes_for_trade(trade_id)
        
        assert len(votes) == 2
    
    def test_get_decision_for_trade(self, vote_logger, sample_signal):
        """测试获取交易的决策"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        # 记录投票和决策
        vote = vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="test_strategy",
            signal=sample_signal,
            weight=0.25,
        )
        
        decision = vote_logger.record_decision(
            trade_id=trade_id,
            symbol="AAPL",
            final_decision=Direction.LONG,
            consensus_level=0.75,
            total_weight=1.0,
            long_weight=0.75,
            short_weight=0.15,
            votes=[vote],
        )
        
        # 查询
        retrieved = vote_logger.get_decision_for_trade(trade_id)
        
        assert retrieved is not None
        assert retrieved.final_decision == "LONG"
    
    def test_get_votes_by_agent(self, vote_logger, sample_signal):
        """测试按 Agent 查询投票"""
        # 记录多个投票
        for i in range(5):
            trade_id = vote_logger.generate_trade_id("AAPL")
            vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id="test_agent",
                signal=sample_signal,
                weight=0.25,
            )
        
        # 查询
        votes = vote_logger.get_votes_by_agent("test_agent", limit=10)
        
        assert len(votes) == 5
        assert all(v.agent_id == "test_agent" for v in votes)
    
    def test_get_vote_summary(self, vote_logger, sample_signal):
        """测试获取投票摘要"""
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        # 记录投票
        vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="strategy_1",
            signal=sample_signal,
            weight=0.25,
        )
        vote_logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="strategy_2",
            signal=sample_signal,
            weight=0.20,
        )
        
        # 记录决策
        vote_logger.record_decision(
            trade_id=trade_id,
            symbol="AAPL",
            final_decision=Direction.LONG,
            consensus_level=0.75,
            total_weight=0.45,
            long_weight=0.45,
            short_weight=0.0,
            votes=[vote_logger.get_votes_for_trade(trade_id)],
        )
        
        # 获取摘要
        summary = vote_logger.get_vote_summary(trade_id)
        
        assert summary["trade_id"] == trade_id
        assert summary["total_votes"] == 2
        assert summary["decision"] == "LONG"
        assert summary["consensus_level"] == 0.75
    
    def test_vote_without_persistence(self, temp_db):
        """测试无持久化时的投票记录"""
        # 创建不带 vote_tables 的 logger
        logger = VoteLogger(vote_tables=None)
        
        trade_id = logger.generate_trade_id("AAPL")
        signal = SignalScore(
            strategy_id="test",
            symbol="AAPL",
            direction=Direction.LONG,
            strength=4,
            confidence=0.85,
            expected_horizon=60,
            market_regime=MarketRegime.TREND_UP,
            risk_reward_estimate=2.5,
        )
        
        # 记录投票（应该只缓存，不报错）
        vote = logger.record_agent_vote(
            trade_id=trade_id,
            agent_id="test",
            signal=signal,
            weight=0.25,
        )
        
        assert vote is not None
        assert vote.vote_direction == "LONG"


class TestVoteLoggerIntegration:
    """投票日志集成测试"""
    
    def test_full_voting_workflow(self, vote_logger, sample_signal):
        """测试完整投票流程"""
        # 1. 生成交易 ID
        trade_id = vote_logger.generate_trade_id("AAPL")
        
        # 2. 多个 Agent 投票
        signals = [
            SignalScore(
                strategy_id=f"strategy_{i}",
                symbol="AAPL",
                direction=Direction.LONG,
                strength=4,
                confidence=0.85,
                expected_horizon=60,
                market_regime=MarketRegime.TREND_UP,
                risk_reward_estimate=2.5,
            )
            for i in range(5)
        ]
        
        votes = []
        for i, signal in enumerate(signals):
            vote = vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id=f"strategy_{i}",
                signal=signal,
                weight=0.20,
            )
            votes.append(vote)
        
        # 3. 记录决策
        decision = vote_logger.record_decision(
            trade_id=trade_id,
            symbol="AAPL",
            final_decision=Direction.LONG,
            consensus_level=0.85,
            total_weight=1.0,
            long_weight=0.85,
            short_weight=0.0,
            votes=votes,
            decision_confidence=0.85,
            market_regime=MarketRegime.TREND_UP,
        )
        
        # 4. 验证记录
        retrieved_votes = vote_logger.get_votes_for_trade(trade_id)
        retrieved_decision = vote_logger.get_decision_for_trade(trade_id)
        
        assert len(retrieved_votes) == 5
        assert retrieved_decision is not None
        assert retrieved_decision.final_decision == "LONG"
        assert retrieved_decision.consensus_level == 0.85
    
    def test_query_by_time_range(self, vote_logger, sample_signal):
        """测试时间范围查询"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 记录多个交易
        for i in range(3):
            trade_id = vote_logger.generate_trade_id("AAPL")
            vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id="test_agent",
                signal=sample_signal,
                weight=0.25,
            )
            vote_logger.record_decision(
                trade_id=trade_id,
                symbol="AAPL",
                final_decision=Direction.LONG,
                consensus_level=0.75,
                total_weight=1.0,
                long_weight=0.75,
                short_weight=0.15,
                votes=[vote_logger.get_votes_for_trade(trade_id)],
            )
        
        # 查询
        votes = vote_logger.get_votes_by_time_range(start, end)
        decisions = vote_logger.get_decisions_by_time_range(start, end)
        
        assert len(votes) >= 3
        assert len(decisions) >= 3
    
    def test_decision_statistics(self, vote_logger, sample_signal):
        """测试决策统计"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 记录不同方向的决策
        for i in range(3):
            trade_id = vote_logger.generate_trade_id("AAPL")
            vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id="test_agent",
                signal=sample_signal,
                weight=0.25,
            )
            vote_logger.record_decision(
                trade_id=trade_id,
                symbol="AAPL",
                final_decision=Direction.LONG,
                consensus_level=0.75,
                total_weight=1.0,
                long_weight=0.75,
                short_weight=0.15,
                votes=[vote_logger.get_votes_for_trade(trade_id)],
            )
        
        for i in range(2):
            trade_id = vote_logger.generate_trade_id("GOOG")
            vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id="test_agent",
                signal=SignalScore(
                    strategy_id="test",
                    symbol="GOOG",
                    direction=Direction.SHORT,
                    strength=4,
                    confidence=0.80,
                    expected_horizon=60,
                    market_regime=MarketRegime.TREND_DOWN,
                    risk_reward_estimate=2.0,
                ),
                weight=0.25,
            )
            vote_logger.record_decision(
                trade_id=trade_id,
                symbol="GOOG",
                final_decision=Direction.SHORT,
                consensus_level=0.70,
                total_weight=1.0,
                long_weight=0.15,
                short_weight=0.70,
                votes=[vote_logger.get_votes_for_trade(trade_id)],
            )
        
        # 获取统计
        stats = vote_logger.get_decision_statistics(start, end)
        
        assert stats["total"] == 5
        assert stats["long_count"] == 3
        assert stats["short_count"] == 2
        assert stats["avg_consensus"] > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app/storage/vote_tables", "--cov=app/decision/vote_logger", "--cov-report=term-missing"])
