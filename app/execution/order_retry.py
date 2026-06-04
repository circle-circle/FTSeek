"""
订单重试机制

P1 修复 (2026-04-17):
- 按错误类型分类重试策略
- 可重试错误：指数退避重试
- 不可重试错误：立即失败 + 通知
- 需人工介入错误：告警 + 暂停

High #2 修复 (2026-06-02):
- 扩展错误关键字到中英文双语，覆盖富途SDK返回的中文错误信息
- 增加富途错误码分类（网络层 / 业务层）
- 调整分类优先级：MANUAL_INTERVENE > NON_RETRYABLE > RETRYABLE > 默认
- 未分类错误默认为可重试（保守策略）+ 警告日志便于补全规则
- 保留最大重试次数与指数退避保护
"""
from typing import Dict, Optional, Set, Tuple, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import re
import time

logger = logging.getLogger(__name__)


class ErrorCategory(Enum):
    """错误分类"""
    RETRYABLE = "retryable"           # 可重试：网络超时、连接中断、API限流
    NON_RETRYABLE = "non_retryable"   # 不可重试：余额不足、标的停牌、市场已关
    MANUAL_INTERVENE = "manual"       # 需人工介入：账户异常、权限不足


# 错误关键词分类映射（中英双语，匹配按 MANUAL > NON_RETRYABLE > RETRYABLE 优先级判定）
ERROR_KEYWORDS: Dict[ErrorCategory, Set[str]] = {
    # 需人工介入：永久性、权限/账户层面问题
    ErrorCategory.MANUAL_INTERVENE: {
        # 英文
        "account locked", "account suspended", "permission",
        "unauthorized", "forbidden", "403", "margin call",
        "restricted", "blocked", "frozen",
        # 中文（富途常见）
        "账户异常", "账户冻结", "账户被冻结", "账户锁定",
        "权限不足", "无权限", "未授权",
        "保证金不足", "强制平仓",
    },
    # 不可重试：业务层永久错误
    ErrorCategory.NON_RETRYABLE: {
        # 英文
        "insufficient", "balance", "funds", "not enough",
        "suspended", "halted", "delisted",
        "market closed", "market is closed", "not trading",
        "invalid symbol", "invalid order", "invalid",
        "rejected", "expired",
        # 中文（富途常见）
        "余额不足", "资金不足", "可用资金不足", "购买力不足",
        "停牌", "已停牌", "已退市", "市场已关闭", "非交易时段",
        "标的不存在", "证券不存在", "股票不存在",
        "订单无效", "委托无效", "参数无效", "无效",
        "已过期", "订单已撤销", "订单已成交",
        "拒绝", "被拒绝", "禁止",
    },
    # 可重试：瞬时错误、网络/限流/服务暂不可用
    ErrorCategory.RETRYABLE: {
        # 英文
        "timeout", "timed out", "connection refused", "connection reset",
        "connection", "network", "rate limit", "too many requests", "429",
        "temporary", "temporarily", "service unavailable", "unavailable",
        "503", "502", "504", "retry",
        "open_d", "opend", "disconnected",
        "eagain", "econnreset", "etimedout", "econnrefused",
        # 中文（富途常见）
        "网络", "网络异常", "网络错误", "连接异常", "连接中断", "连接断开",
        "超时", "请求超时", "响应超时",
        "暂时", "暂时不可用", "稍后", "稍后再试", "重试",
        "服务繁忙", "系统繁忙", "请求频繁", "请求过于频繁", "限流",
        "未连接", "连接已关闭", "断开",
    },
}


# 富途错误码分类（基于 ft_common.RET_* 与常见接口错误码）
# 网络层 / 瞬时类 → 可重试；业务层 → 不可重试。
RETRYABLE_ERROR_CODES: Set[int] = {
    -2,    # 网络异常
    -3,    # 连接断开
    -4,    # 请求超时
    -10,   # OpenD 未连接
    -11,   # OpenD 连接已断开
    -12,   # 服务繁忙
    -13,   # 请求过于频繁（限流）
    429,
    502, 503, 504,
}

NON_RETRYABLE_ERROR_CODES: Set[int] = {
    1,     # 参数无效
    2,     # 订单不存在
    3,     # 余额不足
    4,     # 标的停牌
    5,     # 市场已关闭
    6,     # 订单已成交/已撤销
    7,     # 订单已过期
}

MANUAL_ERROR_CODES: Set[int] = {
    401,   # 未授权
    403,   # 禁止访问
    -100,  # 账户异常
    -101,  # 权限不足
}


def _classify_by_code(error_code: Optional[int]) -> Optional[ErrorCategory]:
    """根据数值错误码分类，未知码返回 None 由调用方走默认策略。"""
    if error_code is None:
        return None
    if error_code in MANUAL_ERROR_CODES:
        return ErrorCategory.MANUAL_INTERVENE
    if error_code in NON_RETRYABLE_ERROR_CODES:
        return ErrorCategory.NON_RETRYABLE
    if error_code in RETRYABLE_ERROR_CODES:
        return ErrorCategory.RETRYABLE
    return None


# 关键字优先级顺序：人工介入 > 不可重试 > 可重试
_CATEGORY_PRIORITY = (
    ErrorCategory.MANUAL_INTERVENE,
    ErrorCategory.NON_RETRYABLE,
    ErrorCategory.RETRYABLE,
)


def _match_keyword(msg_lower: str, msg_raw: str, keyword: str) -> bool:
    """中英文关键字匹配：英文走 lower 后匹配，中文走原文匹配。"""
    if not keyword:
        return False
    # 含中文字符的关键字直接在原文里查找（避免 lower() 在 ASCII 之外的副作用）
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in msg_raw
    return keyword.lower() in msg_lower


def classify_error(
    error_msg: str,
    error_code: Optional[int] = None,
) -> ErrorCategory:
    """根据错误消息 / 错误码进行分类。

    优先级：
    1. 错误码命中（若提供且在已知码表内）
    2. 关键字按 MANUAL > NON_RETRYABLE > RETRYABLE 顺序匹配
    3. 默认 → RETRYABLE（保守策略，至少尝试一次），并记录告警日志
    """
    # 1) 错误码优先
    by_code = _classify_by_code(error_code)
    if by_code is not None:
        return by_code

    msg_raw = error_msg or ""
    msg_lower = msg_raw.lower()

    # 2) 关键字匹配（按优先级）
    for category in _CATEGORY_PRIORITY:
        for kw in ERROR_KEYWORDS[category]:
            if _match_keyword(msg_lower, msg_raw, kw):
                return category

    # 3) 默认可重试 + 告警日志，方便后续补充规则
    logger.warning(
        "[重试][未分类错误] 默认按可重试处理: msg=%r code=%r",
        msg_raw, error_code,
    )
    return ErrorCategory.RETRYABLE


@dataclass
class RetryState:
    """重试状态"""
    attempts: int = 0
    max_attempts: int = 3
    last_attempt: datetime = field(default_factory=datetime.now)
    error_category: Optional[ErrorCategory] = None
    last_error: str = ""
    last_error_code: Optional[int] = None
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

    High #2 修复 (2026-06-02):
    - classify_error 支持中英双语 + 富途错误码
    - 未知错误默认可重试，但仍受 max_attempts 上限保护
    """

    # 指数退避基础延迟和倍数
    BASE_DELAY = 5.0
    BACKOFF_MULTIPLIER = 3.0  # 5s -> 15s -> 45s
    MAX_DELAY = 120.0  # 最大延迟上限

    # 全局兜底上限：即便外部传入 max_attempts 过大，也不会失控
    HARD_MAX_ATTEMPTS = 5

    def __init__(self, max_attempts: int = 3, delay_seconds: float = 5.0):
        # 重试上限保护：[1, HARD_MAX_ATTEMPTS]
        self.max_attempts = max(1, min(int(max_attempts), self.HARD_MAX_ATTEMPTS))
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

        # 超过最大重试次数（保底硬上限）
        if state.attempts >= state.max_attempts:
            return False, f"已达最大重试次数 ({state.max_attempts}), 已尝试 {state.attempts} 次"

        # 未到退避等待时间
        wait_until = state.last_attempt.timestamp() + state.backoff_seconds
        if time.time() < wait_until:
            remaining = wait_until - time.time()
            return False, f"退避等待中，剩余 {remaining:.0f}s"

        return True, ""

    def record_attempt(
        self,
        order_id: str,
        error_msg: str = "",
        success: bool = True,
        error_code: Optional[int] = None,
    ):
        """
        记录重试尝试

        Args:
            order_id: 订单 ID
            error_msg: 错误消息（失败时）
            success: 是否成功
            error_code: 富途/HTTP 错误码（可选），用于精确分类
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
        state.last_error_code = error_code
        state.error_category = classify_error(error_msg, error_code=error_code)

        # 根据分类处理
        if state.error_category == ErrorCategory.NON_RETRYABLE:
            logger.error(
                f"[重试] {order_id} 不可重试错误: {error_msg} (code={error_code})，不再重试"
            )
            return

        if state.error_category == ErrorCategory.MANUAL_INTERVENE:
            state.is_blocked = True
            state.block_reason = f"需人工介入: {error_msg}"
            logger.critical(
                f"[重试] {order_id} 需人工介入: {error_msg} (code={error_code})，已阻塞"
            )
            return

        # 可重试错误：指数退避
        old_delay = state.backoff_seconds
        state.backoff_seconds = min(
            old_delay * self.BACKOFF_MULTIPLIER,
            self.MAX_DELAY,
        )
        logger.warning(
            f"[重试] {order_id} 第 {state.attempts} 次失败: {error_msg} "
            f"(code={error_code})，下次重试延迟 {state.backoff_seconds:.0f}s"
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
            "last_error_code": state.last_error_code,
            "backoff_seconds": state.backoff_seconds,
            "is_blocked": state.is_blocked,
            "block_reason": state.block_reason,
            "status": "blocked" if state.is_blocked else "retrying" if state.attempts < state.max_attempts else "exhausted",
        }

    def reset(self, order_id: str):
        """重置重试状态"""
        if order_id in self.states:
            del self.states[order_id]
