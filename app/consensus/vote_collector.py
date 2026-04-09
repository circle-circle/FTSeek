"""票数收集器 - 收集各策略信号"""
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.consensus.votes")

@dataclass
class Vote:
    """一票：策略 + 方向"""
    strategy: str
    action: SignalAction
    price: float
    reason: str
    timestamp: float = field(default_factory=lambda: __import__('time').time())

class VoteCollector:
    """票数收集器"""
    
    def __init__(self, window_seconds: int = 60):
        self.window = window_seconds
        self.votes: Dict[str, List[Vote]] = {}
    
    def add_vote(self, code: str, vote: Vote):
        """添加一票"""
        if code not in self.votes:
            self.votes[code] = []
        
        # 清理过期票数
        self._cleanup(code)
        
        # 添加新票
        self.votes[code].append(vote)
        logger.debug(f"[{code}] 收到 {vote.strategy} 信号：{vote.action.name}")
    
    def get_votes(self, code: str) -> List[Vote]:
        """获取某标的所有票数"""
        self._cleanup(code)
        return self.votes.get(code, [])
    
    def _cleanup(self, code: str):
        """清理过期票数"""
        import time
        if code in self.votes:
            now = time.time()
            self.votes[code] = [
                v for v in self.votes[code]
                if now - v.timestamp < self.window
            ]
