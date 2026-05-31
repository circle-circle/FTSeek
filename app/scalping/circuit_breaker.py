"""
连续亏损熔断 — Scalping 专属熔断器

逻辑:
  连续亏损 N 次 → 触发熔断 → 冷却 M 分钟 → 恢复

参数:
  max_consecutive_losses: 最大连续亏损次数 (默认 3)
  cooldown_minutes: 熔断冷却时间 (默认 60)

线程安全: 使用 threading.Lock
"""

import threading
import logging
from datetime import datetime, timedelta
from app.utils.timezone import now_et
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ScalpingCircuitBreaker:
    """
    Scalping 连续亏损熔断器
    
    状态机:
      NORMAL → 连续亏损 → COOLDOWN → 冷却结束 → NORMAL
    
    职责:
      1. 跟踪连续亏损次数
      2. 触发熔断时阻止新开仓
      3. 冷却结束后自动恢复
    """

    STATE_NORMAL = "NORMAL"
    STATE_COOLDOWN = "COOLDOWN"

    def __init__(
        self,
        max_consecutive_losses: int = 3,
        cooldown_minutes: int = 60,
    ):
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_minutes = cooldown_minutes

        self._consecutive_losses = 0
        self._state = self.STATE_NORMAL
        self._cooldown_until: Optional[datetime] = None
        self._lock = threading.Lock()

        logger.info(
            f"[CircuitBreaker] 初始化 | "
            f"最大连续亏损: {max_consecutive_losses} | "
            f"冷却时间: {cooldown_minutes} 分钟"
        )

    # C.2 修复 (2026-05-19): 噪声波动不应计入连续亏损.
    # 5/18 实测: LCID gross_pnl=0.00% (摩擦 -0.09%) 计为 sl_hit, 3 笔 → 60min 熔断,
    # 全天触发 3 次熔断 = 浪费 180min 真实交易窗口.
    # 阈值: 仅 gross_pnl < NOISE_LOSS_THRESHOLD 才计连续亏损 (默认 -0.1%).
    NOISE_LOSS_THRESHOLD = -0.001  # gross_pnl_pct ≥ -0.1% 视为噪声, 不计 loss

    def record_loss(self, gross_pnl_pct: Optional[float] = None):
        """记录一次亏损.

        Args:
            gross_pnl_pct: 毛盈亏百分比 (含摩擦前). None 时按以前逻辑兼容旧调用方.
                         非 None 且 ≥ -0.1% 时视为噪声, 不递增 consecutive_losses.
        """
        with self._lock:
            # C.2: 噪声过滤 (仅当显式传入 gross_pnl)
            if gross_pnl_pct is not None and gross_pnl_pct >= self.NOISE_LOSS_THRESHOLD:
                logger.debug(
                    f"[CircuitBreaker·噪声] gross_pnl={gross_pnl_pct*100:.2f}% "
                    f"≥ {self.NOISE_LOSS_THRESHOLD*100:.1f}% 视为噪声, 不计连续亏损 "
                    f"(consecutive={self._consecutive_losses}, 不变)"
                )
                return

            self._consecutive_losses += 1

            if self._consecutive_losses >= self.max_consecutive_losses:
                self._trigger_cooldown()

    def record_win(self):
        """记录一次盈利 (重置连续亏损计数)"""
        with self._lock:
            self._consecutive_losses = 0

            # 如果在冷却中，不自动恢复 (需要等冷却结束)
            # 但如果连续盈利，可以提前恢复
            if self._state == self.STATE_COOLDOWN:
                self._state = self.STATE_NORMAL
                self._cooldown_until = None
                logger.info(
                    f"[CircuitBreaker] ✅ 提前恢复 (连续盈利后) | "
                    f"之前连续亏损: {self.max_consecutive_losses}"
                )

    def _trigger_cooldown(self):
        """触发冷却 (内部方法，需持有锁)"""
        if self._state == self.STATE_COOLDOWN:
            return  # 已在冷却中

        self._state = self.STATE_COOLDOWN
        self._cooldown_until = now_et() + timedelta(
            minutes=self.cooldown_minutes
        )

        logger.critical(
            f"[CircuitBreaker] 🚨 熔断触发! | "
            f"连续亏损 {self._consecutive_losses} 次 | "
            f"冷却 {self.cooldown_minutes} 分钟 | "
            f"恢复时间: {self._cooldown_until.strftime('%H:%M:%S')}"
        )

    def is_blocked(self) -> bool:
        """
        检查是否被熔断阻止
        
        Returns:
            True 如果当前不能开仓
        """
        with self._lock:
            if self._state == self.STATE_NORMAL:
                return False

            if self._state == self.STATE_COOLDOWN:
                if self._cooldown_until and now_et() >= self._cooldown_until:
                    # 冷却结束，恢复
                    self._state = self.STATE_NORMAL
                    self._cooldown_until = None
                    self._consecutive_losses = 0
                    logger.info("[CircuitBreaker] ✅ 冷却结束，恢复正常")
                    return False
                else:
                    remaining = (
                        (self._cooldown_until - now_et()).total_seconds() / 60
                    )
                    logger.debug(
                        f"[CircuitBreaker] 熔断中，剩余 {remaining:.0f} 分钟"
                    )
                    return True

            return False

    def get_state(self) -> Dict:
        """获取当前状态"""
        with self._lock:
            remaining_minutes = 0
            if (
                self._state == self.STATE_COOLDOWN
                and self._cooldown_until
            ):
                remaining = (
                    self._cooldown_until - now_et()
                ).total_seconds() / 60
                remaining_minutes = max(0, remaining)

            return {
                "state": self._state,
                "consecutive_losses": self._consecutive_losses,
                "max_consecutive_losses": self.max_consecutive_losses,
                "cooldown_until": (
                    self._cooldown_until.isoformat() if self._cooldown_until else None
                ),
                "remaining_minutes": remaining_minutes,
                "is_blocked": self._state == self.STATE_COOLDOWN
                and (
                    not self._cooldown_until
                    or now_et() < self._cooldown_until
                ),
            }

    def reset(self):
        """手动重置 (用于调试或强制恢复)"""
        with self._lock:
            self._consecutive_losses = 0
            self._state = self.STATE_NORMAL
            self._cooldown_until = None
            logger.info("[CircuitBreaker] 手动重置")

    def update_config(self, max_consecutive_losses: Optional[int] = None, cooldown_minutes: Optional[int] = None):
        """更新配置"""
        with self._lock:
            if max_consecutive_losses is not None:
                self.max_consecutive_losses = max_consecutive_losses
            if cooldown_minutes is not None:
                self.cooldown_minutes = cooldown_minutes
            logger.info("[CircuitBreaker] 配置已更新")


def get_circuit_breaker(**kwargs) -> ScalpingCircuitBreaker:
    """获取熔断器实例"""
    return ScalpingCircuitBreaker(**kwargs)
