"""
订单重试机制

P1 修复 (2026-04-17):
- 按错误类型分类重试策略
- 可重试错误：指数退避重试
- 不可重试错误：立即失败 + 通知
- 需人工介入错误：告警 + 暂停
"""
from typing import Dict, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import time

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """错误分类"""
    RETRYABLE = "retryable"           # 可重试：网络超时、连接中断、API限流
    NON_RETRYABLE = "non_retryable"   # 不可重试：余额不足、标的停牌、市场已关
    MANUAL_INTERVENE = "manual"       # 需人工介入：账户异常、权限不足


# 错误关键词分类映射
ERROR_KEYWORDS: Dict[ErrorCategory, Set[str]] = {
    ErrorCategory.RETRYABLE: {
        "timeout", "timed out", "connection refused", "connection reset",
        "network", "rate limit", "too many requests", "429",
        "temporary", "service unavailable", "503", "502", "504",
        "retry", "open_d", "disconnected",
    },
    ErrorCategory.NON_RETRYABLE: {
        "insufficient", "balance", "funds", "not enough",
        "suspended", "halted", "delisted", "停牌",
        "market closed", "market is closed", "not trading",
        "invalid symbol", "invalid order",
    },
    ErrorCategory.MANUAL_INTERVENE: {
        "account locked", "account suspended", "permission",
        "unauthorized", "forbidden", "403", "margin call",
        "restricted", "blocked", "frozen", "异常",
    },
}


def classify_error(error_msg: str) -> ErrorCategory:
    """根据错误消息分类"""
    msg_lower = error_msg.lower()
    for category, keywords in ERROR_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            return category
    # 默认视为可重试（保守策略）
    return ErrorCategory.RETRYABLE


@dataclass
class RetryState:
    """重试状态"""
    attempts: int = 0
    max_attempts: int = 3
    last_attempt: datetime = field(default_factory=datetime.now)
    error_category: Optional[ErrorCategory] = None
    last_error: str = ""
    backoff_seconds: float = 5.0
    is_blocked: bool = False
    block_reason: str = ""


class OrderRetryManager:
    """订单重试管理器

    P1 修复 (2026-04-17):
    - 按错误类型分类重试策略
    - 指数退避：5s → 15s → 45s
    - 不可重试错误立即终止
    - 人工介入错误标记阻塞
    """

    # 指数退避基础延迟和倍数
    BASE_DELAY = 5.0
    BACKOFF_MULTIPLIER = 3.0  # 5s -> 15s -> 45s
    MAX_DELAY = 120.0  # 最大延迟上限

    def __init__(self, max_attempts: int = 3, delay_seconds: float = 5.0):
        self.max_attempts = max_attempts
        self.base_delay = delay_seconds  # 指数退避基础延迟
        self.states: Dict[str, RetryState] = {}

    def should_retry(self, order_id: str, error_msg: str = "") -> Tuple[bool, str]:
        """
        是否应该重试

        Returns:
            (should_retry: bool, reason: str)
        """
        if order_id not in self.states:
            return True, ""

        state = self.states[order_id]

        # 已阻塞（需人工介入）
        if state.is_blocked:
            return False, f"已阻塞: {state.block_reason}"

        # 不可重试错误
        if state.error_category == ErrorCategory.NON_RETRYABLE:
            return False, f"不可重试错误: {state.last_error}"

        # 超过最大重试次数
        if state.attempts >= state.max_attempts:
            return False, f"已达最大重试次数 ({state.max_attempts}), 已尝试 {state.attempts} 次"

        # 未到退避等待时间
        wait_until = state.last_attempt.timestamp() + state.backoff_seconds
        if time.time() < wait_until:
            remaining = wait_until - time.time()
            return False, f"退避等待中，剩余 {remaining:.0f}s"

        return True, ""

    def record_attempt(self, order_id: str, error_msg: str = "", success: bool = True):
        """
        记录重试尝试

        Args:
            order_id: 订单 ID
            error_msg: 错误消息（失败时）
            success: 是否成功
        """
        if order_id not in self.states:
            self.states[order_id] = RetryState(
                max_attempts=self.max_attempts,
                backoff_seconds=self.base_delay,  # 使用实例的 base_delay
            )

        state = self.states[order_id]
        state.attempts += 1
        state.last_attempt = datetime.now()

        if success:
            logger.info(f"[重试] {order_id} 第 {state.attempts} 次尝试成功")
            return

        state.last_error = error_msg
        state.error_category = classify_error(error_msg)

        # 根据分类处理
        if state.error_category == ErrorCategory.NON_RETRYABLE:
            logger.error(
                f"[重试] {order_id} 不可重试错误: {error_msg}，不再重试"
            )
            return

        if state.error_category == ErrorCategory.MANUAL_INTERVENE:
            state.is_blocked = True
            state.block_reason = f"需人工介入: {error_msg}"
            logger.critical(
                f"[重试] {order_id} 需人工介入: {error_msg}，已阻塞"
            )
            return

        # 可重试错误：指数退避
        old_delay = state.backoff_seconds
        state.backoff_seconds = min(
            old_delay * self.BACKOFF_MULTIPLIER,
            self.MAX_DELAY,
        )
        logger.warning(
            f"[重试] {order_id} 第 {state.attempts} 次失败: {error_msg}，"
            f"下次重试延迟 {state.backoff_seconds:.0f}s"
        )

    def get_delay(self, order_id: str) -> float:
        """获取下次重试延迟（秒）"""
        if order_id not in self.states:
            return self.base_delay
        return self.states[order_id].backoff_seconds

    def get_attempts(self, order_id: str) -> int:
        """获取已重试次数"""
        if order_id not in self.states:
            return 0
        return self.states[order_id].attempts

    def get_status(self, order_id: str) -> Dict:
        """获取重试状态详情"""
        if order_id not in self.states:
            return {"attempts": 0, "status": "not_started"}

        state = self.states[order_id]
        return {
            "attempts": state.attempts,
            "max_attempts": state.max_attempts,
            "error_category": state.error_category.value if state.error_category else None,
            "last_error": state.last_error,
            "backoff_seconds": state.backoff_seconds,
            "is_blocked": state.is_blocked,
            "block_reason": state.block_reason,
            "status": "blocked" if state.is_blocked else "retrying" if state.attempts < state.max_attempts else "exhausted",
        }

    def reset(self, order_id: str):
        """重置重试状态"""
        if order_id in self.states:
            del self.states[order_id]
