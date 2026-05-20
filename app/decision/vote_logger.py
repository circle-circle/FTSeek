"""
投票日志记录器

提供投票和决策的详细日志记录功能
"""
import logging
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path
from dataclasses import dataclass, asdict

from app.core.models import SignalScore, Direction, MarketRegime

logger = logging.getLogger(__name__)


@dataclass
class VoteRecord:
    """投票记录数据类"""
    trade_id: str
    agent_id: str
    vote_direction: str  # LONG / SHORT / ABSTAIN
    vote_weight: float
    vote_strength: Optional[int] = None
    vote_confidence: Optional[float] = None
    vote_reason: Optional[str] = None
    timestamp: Optional[str] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


@dataclass
class DecisionRecord:
    """决策记录数据类"""
    trade_id: str
    symbol: str
    final_decision: str  # LONG / SHORT / NO_CONSENSUS
    consensus_level: float
    total_weight: float
    long_weight: float
    short_weight: float
    contributing_agents: int
    decision_confidence: Optional[float] = None
    market_regime: Optional[str] = None
    timestamp: Optional[str] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


class VoteLogger:
    """
    投票日志记录器
    
    功能:
    1. 记录每个 Agent 的投票详情
    2. 记录投票权重和最终决策
    3. 记录决策置信度
    4. 提供投票日志查询接口
    """
    
    def __init__(self, vote_tables=None):
        """
        初始化投票日志记录器
        
        Args:
            vote_tables: VoteTables 实例，用于持久化存储
        """
        self.vote_tables = vote_tables
        self._vote_cache: Dict[str, List[VoteRecord]] = {}
        self._decision_cache: Dict[str, DecisionRecord] = {}
        
        logger.info("VoteLogger 初始化完成")
    
    def generate_trade_id(self, symbol: str) -> str:
        """
        生成交易 ID
        
        Args:
            symbol: 股票代码
        
        Returns:
            交易 ID (格式：SYMBOL_YYYYMMDD_HHMMSS_UUID)
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        return f"{symbol}_{timestamp}_{unique_id}"
    
    def record_agent_vote(
        self,
        trade_id: str,
        agent_id: str,
        signal: SignalScore,
        weight: float,
        reason: Optional[str] = None,
    ) -> VoteRecord:
        """
        记录 Agent 投票
        
        Args:
            trade_id: 交易 ID
            agent_id: Agent ID
            signal: 信号对象
            weight: 投票权重
            reason: 投票理由
        
        Returns:
            投票记录
        """
        # 转换方向
        vote_direction = signal.direction.value
        if signal.strength < 2:  # 低强度视为弃权
            vote_direction = "ABSTAIN"
        
        vote = VoteRecord(
            trade_id=trade_id,
            agent_id=agent_id,
            vote_direction=vote_direction,
            vote_weight=weight,
            vote_strength=signal.strength,
            vote_confidence=signal.confidence,
            vote_reason=reason,
        )
        
        # 缓存
        if trade_id not in self._vote_cache:
            self._vote_cache[trade_id] = []
        self._vote_cache[trade_id].append(vote)
        
        # 持久化
        if self.vote_tables:
            try:
                self.vote_tables.record_vote(
                    trade_id=trade_id,
                    agent_id=agent_id,
                    vote_direction=vote_direction,
                    vote_weight=weight,
                    vote_strength=signal.strength,
                    vote_confidence=signal.confidence,
                    vote_reason=reason,
                )
            except Exception as e:
                logger.error(f"持久化投票记录失败：{e}")
        
        logger.debug(
            f"投票记录：trade_id={trade_id} agent={agent_id} "
            f"direction={vote_direction} weight={weight} "
            f"strength={signal.strength} confidence={signal.confidence}"
        )
        
        return vote
    
    def record_decision(
        self,
        trade_id: str,
        symbol: str,
        final_decision: Direction,
        consensus_level: float,
        total_weight: float,
        long_weight: float,
        short_weight: float,
        votes: List[VoteRecord],
        decision_confidence: Optional[float] = None,
        market_regime: Optional[MarketRegime] = None,
    ) -> DecisionRecord:
        """
        记录最终决策
        
        Args:
            trade_id: 交易 ID
            symbol: 股票代码
            final_decision: 最终决策方向
            consensus_level: 共识水平 (0-1)
            total_weight: 总权重
            long_weight: 多头权重
            short_weight: 空头权重
            votes: 所有投票记录
            decision_confidence: 决策置信度
            market_regime: 市场体制
        
        Returns:
            决策记录
        """
        decision = DecisionRecord(
            trade_id=trade_id,
            symbol=symbol,
            final_decision=final_decision.value if final_decision else "NO_CONSENSUS",
            consensus_level=consensus_level,
            total_weight=total_weight,
            long_weight=long_weight,
            short_weight=short_weight,
            contributing_agents=len(votes),
            decision_confidence=decision_confidence,
            market_regime=market_regime.value if market_regime else None,
        )
        
        # 缓存
        self._decision_cache[trade_id] = decision
        
        # 持久化
        if self.vote_tables:
            try:
                self.vote_tables.record_decision(
                    trade_id=trade_id,
                    symbol=symbol,
                    final_decision=decision.final_decision,
                    consensus_level=consensus_level,
                    total_weight=total_weight,
                    long_weight=long_weight,
                    short_weight=short_weight,
                    contributing_agents=len(votes),
                    decision_confidence=decision_confidence,
                    market_regime=market_regime.value if market_regime else None,
                )
            except Exception as e:
                logger.error(f"持久化决策记录失败：{e}")
        
        logger.info(
            f"决策记录：trade_id={trade_id} symbol={symbol} "
            f"decision={decision.final_decision} consensus={consensus_level:.2f} "
            f"confidence={decision_confidence}"
        )
        
        return decision
    
    def get_votes_for_trade(self, trade_id: str) -> List[VoteRecord]:
        """
        获取交易的投票记录
        
        Args:
            trade_id: 交易 ID
        
        Returns:
            投票记录列表
        """
        # 先查缓存
        if trade_id in self._vote_cache:
            return self._vote_cache[trade_id]
        
        # 再查数据库
        if self.vote_tables:
            try:
                vote_data = self.vote_tables.query_votes_by_trade(trade_id)
                return [
                    VoteRecord(
                        trade_id=v['trade_id'],
                        agent_id=v['agent_id'],
                        vote_direction=v['vote_direction'],
                        vote_weight=v['vote_weight'],
                        vote_strength=v.get('vote_strength'),
                        vote_confidence=v.get('vote_confidence'),
                        vote_reason=v.get('vote_reason'),
                        timestamp=v['created_at'],
                    )
                    for v in vote_data
                ]
            except Exception as e:
                logger.error(f"查询投票记录失败：{e}")
        
        return []
    
    def get_decision_for_trade(self, trade_id: str) -> Optional[DecisionRecord]:
        """
        获取交易的决策记录
        
        Args:
            trade_id: 交易 ID
        
        Returns:
            决策记录或 None
        """
        # 先查缓存
        if trade_id in self._decision_cache:
            return self._decision_cache[trade_id]
        
        # 再查数据库
        if self.vote_tables:
            try:
                decision_data = self.vote_tables.query_decisions_by_trade(trade_id)
                if decision_data:
                    return DecisionRecord(
                        trade_id=decision_data['trade_id'],
                        symbol=decision_data['symbol'],
                        final_decision=decision_data['final_decision'],
                        consensus_level=decision_data['consensus_level'],
                        total_weight=decision_data['total_weight'],
                        long_weight=decision_data['long_weight'],
                        short_weight=decision_data['short_weight'],
                        contributing_agents=decision_data['contributing_agents'],
                        decision_confidence=decision_data.get('decision_confidence'),
                        market_regime=decision_data.get('market_regime'),
                        timestamp=decision_data['created_at'],
                    )
            except Exception as e:
                logger.error(f"查询决策记录失败：{e}")
        
        return None
    
    def get_votes_by_agent(self, agent_id: str, limit: int = 100) -> List[VoteRecord]:
        """
        按 Agent 查询投票
        
        Args:
            agent_id: Agent ID
            limit: 返回记录数限制
        
        Returns:
            投票记录列表
        """
        if self.vote_tables:
            try:
                vote_data = self.vote_tables.query_votes_by_agent(agent_id, limit)
                return [
                    VoteRecord(
                        trade_id=v['trade_id'],
                        agent_id=v['agent_id'],
                        vote_direction=v['vote_direction'],
                        vote_weight=v['vote_weight'],
                        vote_strength=v.get('vote_strength'),
                        vote_confidence=v.get('vote_confidence'),
                        vote_reason=v.get('vote_reason'),
                        timestamp=v['created_at'],
                    )
                    for v in vote_data
                ]
            except Exception as e:
                logger.error(f"查询 Agent 投票失败：{e}")
        
        return []
    
    def get_votes_by_time_range(
        self, 
        start_time: str, 
        end_time: str,
        limit: int = 1000
    ) -> List[VoteRecord]:
        """
        按时间范围查询投票
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            limit: 返回记录数限制
        
        Returns:
            投票记录列表
        """
        if self.vote_tables:
            try:
                vote_data = self.vote_tables.query_votes_by_time_range(
                    start_time, end_time, limit
                )
                return [
                    VoteRecord(
                        trade_id=v['trade_id'],
                        agent_id=v['agent_id'],
                        vote_direction=v['vote_direction'],
                        vote_weight=v['vote_weight'],
                        vote_strength=v.get('vote_strength'),
                        vote_confidence=v.get('vote_confidence'),
                        vote_reason=v.get('vote_reason'),
                        timestamp=v['created_at'],
                    )
                    for v in vote_data
                ]
            except Exception as e:
                logger.error(f"查询时间范围投票失败：{e}")
        
        return []
    
    def get_decisions_by_time_range(
        self,
        start_time: str,
        end_time: str,
        limit: int = 1000
    ) -> List[DecisionRecord]:
        """
        按时间范围查询决策
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            limit: 返回记录数限制
        
        Returns:
            决策记录列表
        """
        if self.vote_tables:
            try:
                decision_data = self.vote_tables.query_decisions_by_time_range(
                    start_time, end_time, limit
                )
                return [
                    DecisionRecord(
                        trade_id=d['trade_id'],
                        symbol=d['symbol'],
                        final_decision=d['final_decision'],
                        consensus_level=d['consensus_level'],
                        total_weight=d['total_weight'],
                        long_weight=d['long_weight'],
                        short_weight=d['short_weight'],
                        contributing_agents=d['contributing_agents'],
                        decision_confidence=d.get('decision_confidence'),
                        market_regime=d.get('market_regime'),
                        timestamp=d['created_at'],
                    )
                    for d in decision_data
                ]
            except Exception as e:
                logger.error(f"查询时间范围决策失败：{e}")
        
        return []
    
    def get_decision_statistics(self, start_time: str, end_time: str) -> Dict[str, Any]:
        """
        获取决策统计
        
        Args:
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
        
        Returns:
            统计数据字典
        """
        if self.vote_tables:
            try:
                return self.vote_tables.get_decision_statistics(start_time, end_time)
            except Exception as e:
                logger.error(f"查询决策统计失败：{e}")
        
        return {}
    
    def get_vote_summary(self, trade_id: str) -> Dict[str, Any]:
        """
        获取投票摘要
        
        Args:
            trade_id: 交易 ID
        
        Returns:
            投票摘要字典
        """
        votes = self.get_votes_for_trade(trade_id)
        decision = self.get_decision_for_trade(trade_id)
        
        summary = {
            "trade_id": trade_id,
            "total_votes": len(votes),
            "votes_by_direction": {},
            "total_weight": 0,
            "decision": None,
            "consensus_level": 0,
        }
        
        # 统计投票分布
        for vote in votes:
            direction = vote.vote_direction
            if direction not in summary["votes_by_direction"]:
                summary["votes_by_direction"][direction] = {
                    "count": 0,
                    "total_weight": 0,
                }
            summary["votes_by_direction"][direction]["count"] += 1
            summary["votes_by_direction"][direction]["total_weight"] += vote.vote_weight
            summary["total_weight"] += vote.vote_weight
        
        # 添加决策信息
        if decision:
            summary["decision"] = decision.final_decision
            summary["consensus_level"] = decision.consensus_level
            summary["decision_confidence"] = decision.decision_confidence
        
        return summary
