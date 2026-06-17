"""投票性能追踪器 - 追踪模型表现并持续优化

⚠️ 已废弃 (P2.1 / RFC-060, 2026-06-17):
    本类是早期内存实现, 已被 app/analysis/voting_accuracy_tracker.py 取代:
      - VotingAccuracyTracker 持久化 (SQLite), 不会被重启清空
      - 已在 voting_coordinator.record_prediction + order_executor.update_outcome 闭环
      - 产出 get_model_benchmark (per-model 准确率/方向)
    P2.1 已将 VotingAccuracyTracker 准确率接进 voting_aggregator 权重 (闭环通电).
    本文件保留仅为兼容; tracker 全局实例不再被消费. 待 P3 清理时删除.
"""
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from collections import defaultdict

logger = logging.getLogger("ftseek_us.voting.performance")


class VotingPerformanceTracker:
    """投票性能追踪器"""
    
    def __init__(self, max_history: int = 100):
        self.max_history = max_history
        self.vote_results: Dict[str, Dict[str, Any]] = {}
        self.model_history: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"correct": 0, "incorrect": 0, "total": 0}
        )
        self.strategy_history: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"correct": 0, "incorrect": 0, "total": 0}
        )
    
    def record_vote_result(self, vote_id: str, result: Dict[str, Any]):
        """记录投票结果"""
        self.vote_results[vote_id] = {
            "model_scores": result.get("model_scores", []),
            "final_decision": result.get("direction", "NEUTRAL"),
            "final_strength": result.get("final_strength", 50),
            "consensus_level": result.get("consensus_level", 0),
            "timestamp": result.get("timestamp", datetime.now().timestamp()),
            "symbol": result.get("symbol") or "UNKNOWN",
            "outcome": None,  # 待更新
            "pnl": 0.0,       # 待更新
        }
        
        logger.info(f"记录投票结果：{vote_id} {result.get('symbol')} {result.get('direction')}")
    
    def update_outcome(self, vote_id: str, outcome: Dict[str, Any]):
        """根据实际结果更新表现"""
        if vote_id not in self.vote_results:
            logger.warning(f"投票结果不存在：{vote_id}")
            return
        
        result = self.vote_results[vote_id]
        result["outcome"] = outcome.get("direction")
        result["pnl"] = outcome.get("pnl", 0)
        result["exit_time"] = datetime.now().isoformat()
        
        predicted_dir = result["final_decision"]
        actual_dir = outcome.get("direction")
        
        # 更新模型表现
        if actual_dir and predicted_dir != "NEUTRAL":
            for model_score in result["model_scores"]:
                model_id = model_score.get("model_id", "unknown")
                model_dir = model_score.get("direction", "NEUTRAL")
                
                self.model_history[model_id]["total"] += 1
                
                if model_dir == actual_dir:
                    self.model_history[model_id]["correct"] += 1
                else:
                    self.model_history[model_id]["incorrect"] += 1
            
            # 更新策略表现
            strategy_id = outcome.get("strategy_id", "consensus")
            self.strategy_history[strategy_id]["total"] += 1
            
            if predicted_dir == actual_dir:
                self.strategy_history[strategy_id]["correct"] += 1
            else:
                self.strategy_history[strategy_id]["incorrect"] += 1
        
        # 清理旧记录
        if len(self.vote_results) > self.max_history:
            oldest_id = min(self.vote_results.keys(),
                          key=lambda k: self.vote_results[k]["timestamp"])
            del self.vote_results[oldest_id]
        
        logger.info(f"更新投票结果：{vote_id} 结果={actual_dir} 盈亏={outcome.get('pnl', 0):.2f}")
    
    def get_model_win_rates(self) -> Dict[str, float]:
        """获取各模型胜率"""
        win_rates = {}
        
        for model_id, history in self.model_history.items():
            total = history["total"]
            if total > 0:
                win_rates[model_id] = history["correct"] / total
            else:
                win_rates[model_id] = 0.5
        
        return win_rates
    
    def get_strategy_win_rates(self) -> Dict[str, float]:
        """获取各策略胜率"""
        win_rates = {}
        
        for strategy_id, history in self.strategy_history.items():
            total = history["total"]
            if total > 0:
                win_rates[strategy_id] = history["correct"] / total
            else:
                win_rates[strategy_id] = 0.5
        
        return win_rates
    
    def get_model_weights_adjustment(self) -> Dict[str, float]:
        """根据表现获取权重调整系数"""
        win_rates = self.get_model_win_rates()
        adjustments = {}
        
        for model_id, win_rate in win_rates.items():
            if win_rate > 0.6:
                adjustments[model_id] = 1.2  # 表现好提高 20%
            elif win_rate > 0.5:
                adjustments[model_id] = 1.0  # 表现一般不变
            elif win_rate > 0.4:
                adjustments[model_id] = 0.9  # 表现差降低 10%
            else:
                adjustments[model_id] = 0.8  # 表现很差降低 20%
        
        return adjustments
    
    def get_performance_report(self) -> Dict[str, Any]:
        """获取性能报告"""
        return {
            "total_votes": len(self.vote_results),
            "model_win_rates": self.get_model_win_rates(),
            "strategy_win_rates": self.get_strategy_win_rates(),
            "model_weights_adjustment": self.get_model_weights_adjustment(),
            "recent_votes": list(self.vote_results.values())[-10:],
        }


# 全局实例
tracker = VotingPerformanceTracker()
