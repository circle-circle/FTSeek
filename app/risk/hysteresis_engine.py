#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滞回引擎 (Hysteresis Engine) - 防止风控状态高频跳变

职责:
1. 为 FutuMarginMonitor 提供状态记忆和滞回缓冲区。
2. 状态降级（恶化）立即生效，状态恢复需要跨越更高的滞回阈值。
3. 内置 ObservationWindow：从高风险恢复时需要连续 N 次确认。
4. 记录所有状态转换日志，支持审计追踪。

设计原则:
- 降级无延迟: SAFE -> CAUTION 在触及阈值时立即触发
- 恢复需确认: CAUTION -> SAFE 需越过滞回线 + 观察窗口确认
- 防抖动: 在滞回带内 (如 1.45-1.55) 不改变状态
"""

import time
import logging
from enum import Enum
from typing import Optional, Tuple, List
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.risk.hysteresis")


class MarginStatus(Enum):
    SAFE = "SAFE"               # 安全: 正常交易
    CAUTION = "CAUTION"         # 谨慎: 降低仓位，收紧止损
    WARNING = "WARNING"         # 警戒: 禁止新开仓，准备减仓
    MARGIN_CALL = "CALL"        # 追保: 强制减仓
    LIQUIDATION = "DANGER"      # 强平: 市价全平
    DATA_ERROR = "ERROR"        # 数据异常: 暂停交易
    TRANSITIONING = "TRANSITIONING"  # 恢复中: 观察窗口期内


@dataclass
class HysteresisThreshold:
    """
    滞回阈值定义
    
    degrade_to: 状态恶化的触发阈值 (ratio < this -> degrade)
    recover_to: 状态恢复的滞回阈值 (ratio > this -> eligible for recovery)
    observation_count: 恢复需要连续确认次数
    """
    degrade_to: float
    recover_to: float
    observation_count: int = 3


@dataclass
class StateTransition:
    """状态转换记录"""
    from_state: str
    to_state: str
    margin_ratio: float
    timestamp: float
    reason: str = ""


# 默认滞回阈值配置
# 设计: 恢复阈值 = 恶化阈值 + 0.10 (10% 缓冲带)
DEFAULT_HYSTERESIS = {
    # SAFE -> CAUTION: ratio < 1.50 立即降级
    # CAUTION -> SAFE: ratio > 1.60 才可恢复，需 3 次确认
    MarginStatus.SAFE: HysteresisThreshold(
        degrade_to=1.50, recover_to=1.60, observation_count=3
    ),
    # CAUTION -> WARNING: ratio < 1.30 立即降级
    # WARNING -> CAUTION: ratio > 1.40 才可恢复，需 5 次确认
    MarginStatus.CAUTION: HysteresisThreshold(
        degrade_to=1.30, recover_to=1.40, observation_count=5
    ),
    # WARNING -> MARGIN_CALL: ratio < 1.15 立即降级
    # MARGIN_CALL -> WARNING: ratio > 1.25 才可恢复，需 5 次确认
    MarginStatus.WARNING: HysteresisThreshold(
        degrade_to=1.15, recover_to=1.25, observation_count=5
    ),
    # MARGIN_CALL -> LIQUIDATION: ratio < 1.05 立即降级
    # LIQUIDATION -> MARGIN_CALL: ratio > 1.15 才可恢复，需 10 次确认
    MarginStatus.MARGIN_CALL: HysteresisThreshold(
        degrade_to=1.05, recover_to=1.15, observation_count=10
    ),
    # LIQUIDATION is the bottom state -- degrade_to irrelevant
    MarginStatus.LIQUIDATION: HysteresisThreshold(
        degrade_to=0.0, recover_to=1.15, observation_count=10
    ),
}

# 状态优先级（数字越大越危险）
STATUS_SEVERITY = {
    MarginStatus.SAFE: 0,
    MarginStatus.CAUTION: 1,
    MarginStatus.WARNING: 2,
    MarginStatus.MARGIN_CALL: 3,
    MarginStatus.LIQUIDATION: 4,
    MarginStatus.DATA_ERROR: 5,
    MarginStatus.TRANSITIONING: -1,  # 特殊状态
}


class HysteresisEngine:
    """
    滞回引擎 -- 为状态机提供滞回和观察窗口能力
    
    用法:
        engine = HysteresisEngine()
        status = engine.evaluate(raw_status="CAUTION", margin_ratio=1.55)
        # 如果之前是 SAFE，且 ratio=1.55 > 1.50 但 < 1.60，保持 SAFE
    """
    
    def __init__(
        self,
        hysteresis_config: Optional[dict] = None,
        initial_status: MarginStatus = MarginStatus.SAFE,
        min_ratio: float = 1.0,
    ):
        """
        Args:
            hysteresis_config: 自定义滞回配置 (覆盖 DEFAULT_HYSTERESIS)
            initial_status: 初始状态
            min_ratio: 最低可接受的 margin_ratio (低于此值直接 LIQUIDATION)
        """
        self.config = hysteresis_config or DEFAULT_HYSTERESIS
        self.current_status = initial_status
        self.current_ratio = 0.0
        
        # 观察窗口状态
        self._obs_target_status: Optional[MarginStatus] = None  # 观察窗目标状态
        self._obs_pass_count: int = 0                           # 连续通过次数
        self._obs_required: int = 0                             # 需要的确认次数
        self._obs_started_at: float = 0.0                       # 观察开始时间
        
        # 恢复冷却 -- 防止观察窗口失败后立即重新进入
        self._recovery_cooldown_until: float = 0.0
        self._recovery_cooldown_seconds: float = 5.0  # 失败后 5s 内不重新进入
        
        # 转换历史
        self.transition_history: List[StateTransition] = []
        self._max_history = 100
        
        # 状态稳定时间
        self._status_since: float = time.time()
        
        # 统计
        self.total_evaluations = 0
        self.transitions_blocked = 0  # 被滞回阻止的切换次数
        
        logger.info(
            f"滞回引擎已启动 | 初始状态={initial_status.value} | "
            f"滞回缓冲=0.10 (可配置)"
        )
    
    def evaluate(
        self, 
        raw_status: MarginStatus, 
        margin_ratio: float,
        liquidity_buffer: float = 999.0,
    ) -> Tuple[MarginStatus, dict]:
        """
        评估当前状态，应用滞回和观察窗口逻辑。
        
        Args:
            raw_status: 原始计算状态（来自 FutuMarginMonitor 阈值判断）
            margin_ratio: 当前保证金比率
            liquidity_buffer: 流动性缓冲
            
        Returns:
            (最终状态, 元数据字典)
        """
        self.total_evaluations += 1
        self.current_ratio = margin_ratio
        
        meta = {
            "raw_status": raw_status.value,
            "margin_ratio": margin_ratio,
            "liquidity_buffer": liquidity_buffer,
            "hysteresis_active": False,
            "observation_window": False,
        }
        
        # 0. 极低比率直接强平（安全底线）
        if margin_ratio < self._min_safe_ratio():
            return self._transition_to(MarginStatus.LIQUIDATION, 
                                       reason="极低保证金比率", meta=meta)
        
        # 1. 数据错误 -- 立即生效，不受滞回限制
        if raw_status == MarginStatus.DATA_ERROR:
            return self._transition_to(MarginStatus.DATA_ERROR,
                                       reason="数据异常", meta=meta)
        
        # 2. 检查观察窗口（从高风险恢复中）
        if self.current_status == MarginStatus.TRANSITIONING:
            return self._evaluate_observation_window(raw_status, margin_ratio, meta)
        
        # 3. 状态恶化（降级）-- 立即生效，无滞回
        if self._is_degradation(self.current_status, raw_status):
            return self._transition_to(raw_status, 
                                       reason=f"保证金比率降至 {margin_ratio:.2f}", 
                                       meta=meta)
        
        # 4. 状态持平或改善 -- 应用滞回判断
        # 检查恢复冷却（观察窗口失败后不立即重新进入）
        if time.time() < self._recovery_cooldown_until:
            if raw_status != self.current_status:
                self.transitions_blocked += 1
                meta["hysteresis_active"] = True
                meta["reason"] = (
                    f"恢复冷却中: ratio={margin_ratio:.2f}, "
                    f"保持 {self.current_status.value}"
                )
            meta["status"] = self.current_status.value
            return self.current_status, meta
        
        threshold = self.config.get(self.current_status)
        if threshold and margin_ratio >= threshold.recover_to:
            # 达到恢复阈值，进入观察窗口
            # 计算目标状态：根据当前 ratio 确定恢复到的状态
            target = self._determine_fallback(margin_ratio)
            if STATUS_SEVERITY.get(target, 0) < STATUS_SEVERITY.get(self.current_status, 0):
                return self._start_observation_window(target, margin_ratio, meta)
        
        # 5. 在滞回带内 -- 保持当前状态
        if raw_status != self.current_status:
            self.transitions_blocked += 1
            meta["hysteresis_active"] = True
            meta["reason"] = (
                f"滞回保护: ratio={margin_ratio:.2f} 在 "
                f"[{threshold.degrade_to:.2f}, {threshold.recover_to:.2f}] 带内，"
                f"保持 {self.current_status.value}"
            )
        
        meta["status"] = self.current_status.value
        return self.current_status, meta
    
    def _is_degradation(self, current: MarginStatus, new: MarginStatus) -> bool:
        """判断是否是从当前状态向更危险方向恶化"""
        if new == MarginStatus.DATA_ERROR:
            return True  # 数据错误始终视为恶化
        return STATUS_SEVERITY.get(new, 0) > STATUS_SEVERITY.get(current, 0)
    
    def _start_observation_window(
        self, 
        raw_status: MarginStatus, 
        margin_ratio: float,
        meta: dict,
    ) -> Tuple[MarginStatus, dict]:
        """
        启动观察窗口 -- 状态有改善但需要确认。
        """
        threshold = self.config.get(self.current_status)
        if not threshold:
            # 没有配置，直接切换
            return self._transition_to(raw_status, reason="无滞回配置", meta=meta)
        
        # 目标状态是 raw_status（改善后的状态）
        self._obs_target_status = raw_status
        self._obs_required = threshold.observation_count
        self._obs_pass_count = 1  # 当前这次算第一次
        self._obs_started_at = time.time()
        
        self.current_status = MarginStatus.TRANSITIONING
        
        meta["observation_window"] = True
        meta["obs_target"] = raw_status.value
        meta["obs_progress"] = f"1/{self._obs_required}"
        
        logger.info(
            f"[观察窗口] 启动: {self.current_status.value} -> {raw_status.value} | "
            f"ratio={margin_ratio:.2f} | 需要连续 {self._obs_required} 次确认"
        )
        
        return self.current_status, meta
    
    def _evaluate_observation_window(
        self,
        raw_status: MarginStatus,
        margin_ratio: float,
        meta: dict,
    ) -> Tuple[MarginStatus, dict]:
        """
        评估观察窗口进度。
        """
        if self._obs_target_status is None:
            # 异常情况，直接回退
            self._reset_observation()
            return self.current_status, meta
        
        # 检查是否仍然满足恢复条件
        threshold = self.config.get(self._obs_target_status)
        if threshold and margin_ratio >= threshold.recover_to:
            self._obs_pass_count += 1
            meta["observation_window"] = True
            meta["obs_target"] = self._obs_target_status.value
            meta["obs_progress"] = f"{self._obs_pass_count}/{self._obs_required}"
            
            if self._obs_pass_count >= self._obs_required:
                # 观察窗口通过，完成状态转换
                elapsed = time.time() - self._obs_started_at
                new_status = self._obs_target_status
                self._reset_observation()
                
                logger.info(
                    f"[观察窗口] 通过: 转换至 {new_status.value} | "
                    f"耗时 {elapsed:.0f}s, ratio={margin_ratio:.2f}"
                )
                return self._transition_to(new_status, 
                                           reason=f"观察窗口通过 ({self._obs_required}次确认)", 
                                           meta=meta)
            
            logger.debug(
                f"[观察窗口] 进度: {self._obs_pass_count}/{self._obs_required} | "
                f"ratio={margin_ratio:.2f}"
            )
        else:
            # 恢复条件不再满足，观察窗口失败，退回原状态
            fallback_status = self._determine_fallback(margin_ratio)
            
            # 先重置观察窗口（清除目标状态等）
            self._reset_observation()
            
            # 再设置恢复冷却 -- 防止立即重新进入观察窗口
            # 注意：必须在 _reset_observation 之后设置，否则会被清零
            self._recovery_cooldown_until = time.time() + self._recovery_cooldown_seconds
            
            logger.warning(
                f"[观察窗口] 失败: ratio={margin_ratio:.2f} 跌破恢复线，"
                f"回退到 {fallback_status.value}"
            )
            return self._transition_to(fallback_status,
                                       reason="观察窗口失败，回退", meta=meta)
        
        return self.current_status, meta
    
    def _determine_fallback(self, margin_ratio: float) -> MarginStatus:
        """
        当观察窗口失败时，根据当前 ratio 确定回退状态。
        保持保守原则 -- 如果不确定，选择更安全的状态。
        注意: 使用 > 严格比较，边界值归入更危险的等级。
        """
        if margin_ratio > 1.50:
            return MarginStatus.SAFE
        elif margin_ratio > 1.30:
            return MarginStatus.CAUTION
        elif margin_ratio > 1.15:
            return MarginStatus.WARNING
        elif margin_ratio > 1.05:
            return MarginStatus.MARGIN_CALL
        else:
            return MarginStatus.LIQUIDATION
    
    def _reset_observation(self):
        """重置观察窗口"""
        self._obs_target_status = None
        self._obs_pass_count = 0
        self._obs_required = 0
        self._obs_started_at = 0.0
        self._recovery_cooldown_until = 0.0
    
    def _transition_to(
        self, 
        status: MarginStatus, 
        reason: str = "", 
        meta: dict = None,
    ) -> Tuple[MarginStatus, dict]:
        """执行状态转换并记录"""
        old_status = self.current_status
        self.current_status = status
        self._status_since = time.time()
        
        # 记录转换历史
        transition = StateTransition(
            from_state=old_status.value,
            to_state=status.value,
            margin_ratio=self.current_ratio,
            timestamp=time.time(),
            reason=reason,
        )
        self.transition_history.append(transition)
        if len(self.transition_history) > self._max_history:
            self.transition_history = self.transition_history[-self._max_history:]
        
        # 记录转换日志
        if old_status != status:
            severity = "升级" if self._is_degradation(old_status, status) else "恢复"
            logger.info(
                f"[状态转换] {severity}: {old_status.value} -> {status.value} | "
                f"ratio={self.current_ratio:.2f} | {reason}"
            )
        
        if meta:
            meta["status"] = status.value
        return status, meta or {}
    
    def _min_safe_ratio(self) -> float:
        """最低安全比率硬底线"""
        return 1.02  # 低于 1.02 直接 LIQUIDATION，无滞回
    
    # ── 查询接口 ─────────────────────────────────────────────
    
    def get_status(self) -> MarginStatus:
        """获取当前状态"""
        return self.current_status
    
    def get_status_duration(self) -> float:
        """获取当前状态持续时长（秒）"""
        return time.time() - self._status_since
    
    def get_observation_progress(self) -> Optional[str]:
        """获取观察窗口进度"""
        if self.current_status == MarginStatus.TRANSITIONING:
            return f"{self._obs_pass_count}/{self._obs_required}"
        return None
    
    def get_transitions(self, limit: int = 10) -> List[StateTransition]:
        """获取最近的转换历史"""
        return self.transition_history[-limit:]
    
    def get_stats(self) -> dict:
        """获取引擎统计"""
        return {
            "current_status": self.current_status.value,
            "current_ratio": self.current_ratio,
            "status_duration_s": self.get_status_duration(),
            "total_evaluations": self.total_evaluations,
            "transitions_blocked": self.transitions_blocked,
            "total_transitions": len(self.transition_history),
            "observation_window": self.get_observation_progress(),
        }
    
    def reset(self, status: MarginStatus = MarginStatus.SAFE):
        """重置引擎状态"""
        self.current_status = status
        self.current_ratio = 0.0
        self._status_since = time.time()
        self._reset_observation()
        logger.info(f"滞回引擎已重置: {status.value}")
    
    def update_config(self, hysteresis_config: dict):
        """
        运行时更新滞回配置
        
        Args:
            hysteresis_config: {MarginStatus: HysteresisThreshold}
        """
        self.config = hysteresis_config
        logger.info("滞回配置已更新")
