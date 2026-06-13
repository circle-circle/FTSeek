"""熔断器系统 - 三级熔断

StrategyCircuitBreaker: 策略级连续亏损熔断
SymbolCircuitBreaker: 品种级波动率冻结
SystemCircuitBreaker: 系统级数据源健康度

2026-06-14: 删除 TradingFrequencyLimiter (零生产消费者)
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, List
from dataclasses import dataclass

from app.utils.timezone import now_et

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    consecutive_losses: int = 0
    last_loss_time: Optional[datetime] = None
    triggered_at: Optional[datetime] = None
    pause_until: Optional[datetime] = None



class StrategyCircuitBreaker:
    """策略级熔断器"""
    
    def __init__(self, consecutive_losses_threshold: int = 3, pause_duration_seconds: int = 1800):  # SSOT: YAML=3
        self.threshold = consecutive_losses_threshold
        self.pause_duration = timedelta(seconds=pause_duration_seconds)
        self.states: Dict[str, CircuitBreakerState] = {}
    
    def record_loss(self, strategy_id: str):
        """记录亏损"""
        if strategy_id not in self.states:
            self.states[strategy_id] = CircuitBreakerState()
        
        state = self.states[strategy_id]
        state.consecutive_losses += 1
        state.last_loss_time = now_et()
        
        # 检查是否触发熔断
        if state.consecutive_losses >= self.threshold:
            state.triggered_at = now_et()
            state.pause_until = now_et() + self.pause_duration
    
    def is_triggered(self, strategy_id: str) -> bool:
        """检查是否触发熔断"""
        if strategy_id not in self.states:
            return False
        
        state = self.states[strategy_id]
        
        if not state.triggered_at:
            return False
        
        # 检查是否已过暂停期
        if state.pause_until and now_et() > state.pause_until:
            # 重置状态
            state.consecutive_losses = 0
            state.triggered_at = None
            state.pause_until = None
            return False
        
        return True
    
    def get_remaining_pause(self, strategy_id: str) -> int:
        """获取剩余暂停时间 (秒)"""
        if strategy_id not in self.states:
            return 0
        
        state = self.states[strategy_id]
        if not state.pause_until:
            return 0
        
        remaining = (state.pause_until - now_et()).total_seconds()
        return max(0, int(remaining))
    
    def reset(self, strategy_id: str):
        """重置策略状态"""
        if strategy_id in self.states:
            self.states[strategy_id] = CircuitBreakerState()


class SymbolCircuitBreaker:
    """品种级熔断器"""
    
    def __init__(self, volatility_threshold: float = 0.05):
        self.volatility_threshold = volatility_threshold
        self.frozen_symbols: Dict[str, datetime] = {}
    
    def check_volatility(self, symbol: str, volatility: float) -> bool:
        """检查波动率，返回是否可交易"""
        if volatility > self.volatility_threshold:
            self.frozen_symbols[symbol] = now_et()
            return False
        return True
    
    def is_frozen(self, symbol: str) -> bool:
        """检查品种是否被冻结"""
        return symbol in self.frozen_symbols


class DataSourceHealth:
    """单个数据源健康状态"""
    def __init__(self, name: str, weight: float = 1.0):
        self.name = name
        self.weight = weight  # 数据源权重（主源权重更高）
        self.last_update: Optional[datetime] = None
        self.consecutive_failures = 0
        self.is_healthy = True

    def record_success(self):
        self.last_update = now_et()
        self.consecutive_failures = 0
        self.is_healthy = True

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.is_healthy = False

    def score(self) -> float:
        """数据源健康评分 0-100"""
        if not self.is_healthy:
            return 0.0
        if self.consecutive_failures == 0:
            return 100.0
        # 1 次失败=70, 2 次失败=40
        return max(0.0, 100.0 - self.consecutive_failures * 30.0)


class SystemCircuitBreaker:
    """系统级熔断器

    P0 修复 (2026-04-17):
    - 引入数据源健康度评分（0-100）
    - 仅当评分 >= 60 时才重置熔断器
    - 区分"降级模式"和"完全恢复"
    """

    # 状态常量
    STATE_NORMAL = "NORMAL"
    STATE_DEGRADED = "DEGRADED"    # 降级模式：部分源可用，限制交易规模
    STATE_TRIGGERED = "TRIGGERED"  # 熔断：所有源不可用

    HEALTH_THRESHOLD_FULL = 60     # 完全恢复阈值
    HEALTH_THRESHOLD_DEGRADED = 20  # 降级模式阈值

    def __init__(self, market_data_timeout_seconds: int = 60):
        self.timeout = timedelta(seconds=market_data_timeout_seconds)
        self.last_market_data_time: Optional[datetime] = None
        self.triggered = False
        self.state = self.STATE_NORMAL

        # 数据源健康跟踪
        self.data_sources: Dict[str, DataSourceHealth] = {}
        self.state_history: List[Dict[str, Any]] = []

    def register_source(self, name: str, weight: float = 1.0):
        """注册一个数据源"""
        self.data_sources[name] = DataSourceHealth(name, weight)
        logger.info(f"[熔断器] 注册数据源: {name} (weight={weight})")

    def update_market_data_status(self, is_alive: bool, source: str = "default"):
        """
        更新行情状态（兼容旧接口）
        新代码应使用 report_source_status()
        """
        if source not in self.data_sources:
            self.register_source(source)

        ds = self.data_sources[source]
        if is_alive:
            ds.record_success()
        else:
            ds.record_failure()

        self._recompute_state()

    def report_source_status(self, source: str, success: bool):
        """报告某个数据源的采集结果"""
        if source not in self.data_sources:
            self.register_source(source)

        ds = self.data_sources[source]
        if success:
            ds.record_success()
        else:
            ds.record_failure()

        self._recompute_state()

    def _recompute_state(self):
        """根据所有数据源状态重新计算系统熔断状态"""
        if not self.data_sources:
            self.state = self.STATE_NORMAL
            return

        # 计算加权健康评分
        total_weight = 0.0
        weighted_score = 0.0
        for ds in self.data_sources.values():
            total_weight += ds.weight
            weighted_score += ds.score() * ds.weight

        if total_weight > 0:
            health_score = weighted_score / total_weight
        else:
            health_score = 0.0

        old_state = self.state

        if health_score >= self.HEALTH_THRESHOLD_FULL:
            self.state = self.STATE_NORMAL
            self.triggered = False
        elif health_score >= self.HEALTH_THRESHOLD_DEGRADED:
            self.state = self.STATE_DEGRADED
            self.triggered = False
        else:
            self.state = self.STATE_TRIGGERED
            self.triggered = True

        # 状态变更时记录
        if old_state != self.state:
            self._record_state_change(old_state, self.state, health_score)

    def _record_state_change(self, old_state: str, new_state: str, health_score: float):
        """记录熔断器状态变更"""
        record = {
            "timestamp": now_et().isoformat(),
            "old_state": old_state,
            "new_state": new_state,
            "health_score": round(health_score, 1),
            "source_scores": {
                name: round(ds.score(), 1)
                for name, ds in self.data_sources.items()
            },
        }
        self.state_history.append(record)
        logger.warning(
            f"[熔断器] 状态变更: {old_state} -> {new_state} "
            f"(健康评分={health_score:.1f})"
        )

    def is_triggered(self) -> bool:
        """检查是否触发系统熔断"""
        return self.state == self.STATE_TRIGGERED

    def is_degraded(self) -> bool:
        """是否处于降级模式"""
        return self.state == self.STATE_DEGRADED

    def get_health_score(self) -> float:
        """获取当前加权健康评分"""
        if not self.data_sources:
            return 100.0
        total_weight = sum(ds.weight for ds in self.data_sources.values())
        if total_weight == 0:
            return 0.0
        weighted = sum(ds.score() * ds.weight for ds in self.data_sources.values())
        return weighted / total_weight

    def get_status(self) -> Dict[str, Any]:
        """获取熔断器完整状态"""
        return {
            "state": self.state,
            "triggered": self.triggered,
            "health_score": round(self.get_health_score(), 1),
            "sources": {
                name: {
                    "healthy": ds.is_healthy,
                    "score": round(ds.score(), 1),
                    "consecutive_failures": ds.consecutive_failures,
                    "last_update": ds.last_update.isoformat() if ds.last_update else None,
                }
                for name, ds in self.data_sources.items()
            },
            "recent_changes": self.state_history[-5:],
        }

