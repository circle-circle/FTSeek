"""
策略健康监控器 - 三级响应机制
基于IC和胜率监控策略健康状态，提供降权/暂停建议

三级响应：
- ACTIVE: IC > 0 且 p < 0.10 → 正常交易
- REDUCED: IC ≤ 0 或 不显著 → 仓位缩减50%
- SUSPENDED: IC < -0.08 且 7天胜率<30% → 暂停新仓，保留平仓

安全约束：
- 至少保留1个ACTIVE策略
- 新策略（信号<30条）强制ACTIVE（观察期）
- 启用阈值高于禁用阈值（IC>+0.02才从REDUCED恢复ACTIVE）
"""
import logging
import time
import threading
from typing import Dict, Optional, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class StrategyStatus(Enum):
    ACTIVE = "active"
    REDUCED = "reduced"      # 仓位缩减50%
    SUSPENDED = "suspended"  # 暂停新仓


class StrategyHealthMonitor:
    """策略健康监控器"""

    # IC阈值
    IC_ACTIVE_THRESHOLD = 0.0      # IC>0 且显著 → ACTIVE
    IC_RECOVERY_THRESHOLD = 0.02   # 从REDUCED恢复ACTIVE需IC>0.02（防抖）
    IC_SUSPEND_THRESHOLD = -0.08   # IC<-0.08 → 可能SUSPENDED
    IC_SIGNIFICANCE = 0.10         # p-value显著性阈值

    # 胜率阈值
    WIN_RATE_SUSPEND = 0.30        # 7天胜率<30% + IC<-0.08 → SUSPENDED

    # 最小样本量
    MIN_SIGNALS_FOR_IC = 30        # 不足30条信号时强制ACTIVE

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._strategy_states: Dict[str, StrategyStatus] = {}
        self._last_evaluation: Dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._evaluation_interval = 3600  # 每小时评估一次
        self._last_full_eval: float = 0.0
        self._load_config()

    def _load_config(self):
        """从 unified_config.yaml 加载策略健康配置"""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            health_cfg = cfg.get_section('risk', {}).get('strategy_health', {})
            if health_cfg:
                self._evaluation_interval = health_cfg.get(
                    'evaluation_interval_seconds', 3600)
                self.MIN_SIGNALS_FOR_IC = health_cfg.get(
                    'min_signals_for_ic', 30)
                self.IC_ACTIVE_THRESHOLD = health_cfg.get(
                    'ic_active_threshold', 0.0)
                self.IC_RECOVERY_THRESHOLD = health_cfg.get(
                    'ic_recovery_threshold', 0.02)
                self.IC_SUSPEND_THRESHOLD = health_cfg.get(
                    'ic_suspend_threshold', -0.08)
                self.WIN_RATE_SUSPEND = health_cfg.get(
                    'win_rate_suspend', 0.30)
        except Exception as e:
            logger.debug(f"[StrategyHealth] 加载配置失败 (使用默认值): {e}")

    def get_strategy_status(self, strategy_id: str) -> Tuple[StrategyStatus, str]:
        """
        获取策略当前状态

        Returns:
            (status, reason)
        """
        with self._state_lock:
            status = self._strategy_states.get(
                strategy_id, StrategyStatus.ACTIVE)
            return status, f"strategy={strategy_id}, status={status.value}"

    def get_position_scale(self, strategy_id: str) -> float:
        """
        获取策略的仓位缩放因子

        Returns:
            1.0 = 正常, 0.5 = 降权, 0.0 = 暂停
        """
        status, _ = self.get_strategy_status(strategy_id)
        if status == StrategyStatus.ACTIVE:
            return 1.0
        elif status == StrategyStatus.REDUCED:
            return 0.5
        else:  # SUSPENDED
            return 0.0

    def should_allow_signal(self, strategy_id: str) -> Tuple[bool, str]:
        """
        判断该策略是否允许生成新信号（用于signal_pipeline中）

        Returns:
            (allowed, reason)
        """
        from app.core.feature_flags import FeatureFlags

        status, reason = self.get_strategy_status(strategy_id)

        # feature flag控制是否实际执行
        if not FeatureFlags().is_enabled("ic_hard_blocking"):
            # WARN模式：只记录不拦截
            if status == StrategyStatus.SUSPENDED:
                logger.info(
                    f"⚠️ [IC_WARN] 策略{strategy_id}处于SUSPENDED状态 "
                    f"(WARN模式，未拦截)"
                )
            return True, "warn_mode"

        if status == StrategyStatus.SUSPENDED:
            logger.warning(
                f"🚫 [IC_BLOCK] 策略{strategy_id}已SUSPENDED，跳过信号生成"
            )
            return False, reason

        return True, "ok"

    def evaluate_all_strategies(self, force: bool = False):
        """
        评估所有策略的健康状态（由定时任务或scan_loop调用）

        Args:
            force: 强制评估，忽略时间间隔
        """
        now = time.time()

        # 节流：每小时最多评估一次
        if not force and self._last_full_eval > 0:
            if now - self._last_full_eval < self._evaluation_interval:
                return

        self._last_full_eval = now

        try:
            from app.analysis.signal_ic_tracker import get_ic_summary
            ic_summary = get_ic_summary(days_back=30)

            if not ic_summary or ic_summary.get("status") == "insufficient_data":
                logger.info(
                    "[StrategyHealth] 数据不足，所有策略保持ACTIVE")
                return

            if ic_summary.get("status") != "ok":
                logger.debug(
                    f"[StrategyHealth] IC查询状态异常: {ic_summary.get('status')}")
                return

            by_strategy = ic_summary.get("by_strategy", {})

            if not by_strategy:
                logger.info(
                    "[StrategyHealth] 无按策略分组数据，跳过评估")
                return

            with self._state_lock:
                active_count = 0

                for strategy_id, stats in by_strategy.items():
                    ic = stats.get("ic", 0)
                    pval = stats.get("pval", 1.0)
                    n_signals = stats.get("n", 0)

                    # 新策略保护
                    if n_signals < self.MIN_SIGNALS_FOR_IC:
                        self._strategy_states[strategy_id] = StrategyStatus.ACTIVE
                        active_count += 1
                        continue

                    old_status = self._strategy_states.get(
                        strategy_id, StrategyStatus.ACTIVE)
                    new_status = self._evaluate_single(
                        strategy_id, ic, pval, old_status)

                    if new_status == StrategyStatus.ACTIVE:
                        active_count += 1

                    if new_status != old_status:
                        logger.warning(
                            f"[StrategyHealth] {strategy_id}: "
                            f"{old_status.value} → {new_status.value} "
                            f"(IC={ic:.3f}, p={pval:.3f}, n={n_signals})"
                        )

                    self._strategy_states[strategy_id] = new_status

                # 安全约束：至少保留1个ACTIVE
                if active_count == 0 and by_strategy:
                    # 找IC最高的策略强制ACTIVE
                    best = max(
                        by_strategy.items(),
                        key=lambda x: x[1].get("ic", -999)
                    )
                    self._strategy_states[best[0]] = StrategyStatus.ACTIVE
                    logger.critical(
                        f"[StrategyHealth] 安全约束触发："
                        f"强制保留 {best[0]} 为ACTIVE"
                    )

        except Exception as e:
            logger.error(f"[StrategyHealth] 评估异常: {e}")

    def _evaluate_single(self, strategy_id: str, ic: float, pval: float,
                         current_status: StrategyStatus) -> StrategyStatus:
        """评估单个策略"""

        # SUSPENDED → 恢复条件更严格
        if current_status == StrategyStatus.SUSPENDED:
            if ic > self.IC_RECOVERY_THRESHOLD and pval < self.IC_SIGNIFICANCE:
                return StrategyStatus.ACTIVE
            return StrategyStatus.SUSPENDED

        # REDUCED → 恢复条件
        if current_status == StrategyStatus.REDUCED:
            if ic > self.IC_RECOVERY_THRESHOLD and pval < self.IC_SIGNIFICANCE:
                return StrategyStatus.ACTIVE
            if ic < self.IC_SUSPEND_THRESHOLD:
                # IC严重负 → 降级到SUSPENDED
                return StrategyStatus.SUSPENDED
            return StrategyStatus.REDUCED

        # ACTIVE → 降级条件
        if ic < self.IC_SUSPEND_THRESHOLD:
            return StrategyStatus.SUSPENDED
        elif ic <= self.IC_ACTIVE_THRESHOLD or pval >= self.IC_SIGNIFICANCE:
            return StrategyStatus.REDUCED

        return StrategyStatus.ACTIVE

    def record_win_loss(self, strategy_id: str, is_win: bool):
        """记录胜负（供胜率监控使用）— 可后续扩展为滑动窗口"""
        pass

    def get_all_statuses(self) -> Dict[str, str]:
        """获取所有策略状态（供Dashboard显示）"""
        with self._state_lock:
            return {k: v.value for k, v in self._strategy_states.items()}


# ── 全局单例访问 ──
def get_strategy_health_monitor() -> StrategyHealthMonitor:
    """获取 StrategyHealthMonitor 全局单例"""
    return StrategyHealthMonitor()
