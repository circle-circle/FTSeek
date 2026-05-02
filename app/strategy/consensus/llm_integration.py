"""
LLM 集成模块 — 市场研判 + 权重自适应 + 审计日志

从 consensus_engine.py 拆出：
- init_llm_integration() — LLM 注入
- _record_llm_audit() — LLM 审计记录
- _update_llm_weight() — LLM 权重自适应
- get_llm_accuracy_stats() — LLM 准确率统计
"""

import logging
import time
import threading
from typing import Optional, Dict, List, Any
from datetime import datetime

from app.core.config_loader import cfg

logger = logging.getLogger("ftseek.consensus.llm")


class LLMIntegration:
    """
    LLM 集成管理器
    
    职责：
    1. 管理 LLM 市场分析师注入
    2. LLM 审计日志记录
    3. LLM 权重自适应 (基于历史准确率)
    4. LLM 准确率统计
    """
    
    def __init__(self, engine_ref=None):
        """
        Args:
            engine_ref: 对 ConsensusEngine 的引用 (用于获取 votes)
        """
        self._engine_ref = engine_ref
        self._llm_analyst = None
        self._llm_consensus_weight = cfg.get("strategy.ai.weight", 0.15)
        self._llm_weight_config = {}
        self._audit_log: List[Dict] = []
        self._outcome_log: List[Dict] = []
        self._lock = threading.Lock()
        
        # 准确率追踪
        self._llm_predictions: Dict[str, Dict] = {}
    
    def inject_llm(self, llm_analyst, weight: float = None,
                   adaptive_config: Dict = None):
        """
        注入 LLM 市场分析师
        
        Args:
            llm_analyst: LLM 市场分析师实例
            weight: LLM 权重 (默认从配置读取)
            adaptive_config: 权重自适应配置
        """
        self._llm_analyst = llm_analyst
        if weight:
            self._llm_consensus_weight = weight
        
        self._llm_weight_config = {
            "base_weight": self._llm_consensus_weight,
            "min_accuracy": cfg.get("strategy.ai.min_accuracy", 0.5),
            "disable_threshold": cfg.get("strategy.ai.disable_threshold", 0.45),
            "signal_boost": cfg.get("strategy.ai.signal_boost", 1.1),
        }
        
        if adaptive_config:
            self._llm_weight_config.update(adaptive_config)
        
        logger.info(f"LLM 分析师已注入 (weight={self._llm_consensus_weight})")
    
    def record_audit(self, code: str, llm_prediction: str,
                     consensus_action: str, confidence: float,
                     llm_reason: str = ""):
        """
        记录 LLM 审计
        
        Args:
            code: 股票代码
            llm_prediction: LLM 预测方向
            consensus_action: 共识动作
            confidence: 置信度
            llm_reason: LLM 推理过程
        """
        entry = {
            "code": code,
            "llm_prediction": llm_prediction,
            "consensus_action": consensus_action,
            "confidence": confidence,
            "llm_reason": llm_reason,
            "timestamp": datetime.now().isoformat(),
        }
        
        with self._lock:
            self._audit_log.append(entry)
            # 保留最近 1000 条
            if len(self._audit_log) > 1000:
                self._audit_log = self._audit_log[-1000:]
    
    def record_outcome(self, code: str, action: str,
                       pnl: float, holding_hours: float):
        """
        记录交易结果 (用于 LLM 准确率追踪)
        
        Args:
            code: 股票代码
            action: 交易方向
            pnl: 盈亏
            holding_hours: 持仓时长
        """
        entry = {
            "code": code,
            "action": action,
            "pnl": pnl,
            "holding_hours": holding_hours,
            "is_win": pnl > 0,
            "timestamp": datetime.now().isoformat(),
        }
        
        with self._lock:
            self._outcome_log.append(entry)
            if len(self._outcome_log) > 5000:
                self._outcome_log = self._outcome_log[-5000:]
        
        # 更新 LLM 权重
        self._update_llm_weight()
    
    def _update_llm_weight(self):
        """基于历史准确率更新 LLM 权重"""
        stats = self.get_accuracy_stats()
        accuracy = stats.get("accuracy", 0.5)
        
        min_acc = self._llm_weight_config.get("min_accuracy", 0.5)
        disable_threshold = self._llm_weight_config.get("disable_threshold", 0.45)
        base_weight = self._llm_weight_config.get("base_weight", 0.15)
        
        if accuracy < disable_threshold:
            # 准确率太低，禁用 LLM
            self._llm_consensus_weight = 0.0
            logger.warning(f"LLM 准确率 {accuracy:.1%} 低于阈值，已禁用")
        elif accuracy < min_acc:
            # 准确率低，降低权重
            self._llm_consensus_weight = base_weight * 0.5
        elif accuracy > 0.7:
            # 准确率高，提高权重
            self._llm_consensus_weight = base_weight * 1.5
        else:
            self._llm_consensus_weight = base_weight
    
    def get_accuracy_stats(self, scenario: str = None,
                           limit: int = 100) -> Dict[str, Any]:
        """
        获取 LLM 准确率统计
        
        Args:
            scenario: 场景过滤 (可选)
            limit: 最近 N 条记录
        
        Returns:
            {accuracy, total_trades, win_rate, avg_pnl, ...}
        """
        with self._lock:
            recent = self._outcome_log[-limit:]
        
        if not recent:
            return {"accuracy": 0.5, "total_trades": 0}
        
        wins = sum(1 for r in recent if r.get("is_win", False))
        total = len(recent)
        avg_pnl = sum(r.get("pnl", 0) for r in recent) / total if total > 0 else 0
        
        return {
            "accuracy": wins / total if total > 0 else 0.5,
            "total_trades": total,
            "win_count": wins,
            "loss_count": total - wins,
            "win_rate": wins / total if total > 0 else 0.0,
            "avg_pnl": avg_pnl,
            "llm_weight": self._llm_consensus_weight,
        }
    
    def get_audit_log(self, code: str = None, limit: int = 50) -> List[Dict]:
        """获取审计日志"""
        with self._lock:
            log = list(self._audit_log)
        
        if code:
            log = [e for e in log if e.get("code") == code]
        
        return log[-limit:]
    
    @property
    def llm_weight(self) -> float:
        return self._llm_consensus_weight
    
    @property
    def has_llm(self) -> bool:
        return self._llm_analyst is not None
    
    @property
    def llm_analyst(self):
        return self._llm_analyst
