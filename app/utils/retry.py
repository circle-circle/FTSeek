"""
retry-with-backoff helper — Task #38 (2026-06-16)

为 K 线 / 实时行情 / 持仓 三类对 OpenD 的核心 fetch 方法提供统一的
重试装饰器，应对 OpenD 偶发 timeout / 短暂断连。

设计:
  - 默认 3 次重试，间隔 1s / 3s / 10s（指数退避，固定列表）
  - 重试期间记录 WARNING 日志（含函数名、第 N 次、间隔）
  - 全部失败后抛原异常，调用方按现有逻辑处理（不吞异常）
  - 可选 retry_on_none=True：返回 None 也视为失败并重试
    （允许把 "Futu 偶发返回空 DataFrame → 业务层 None" 当作可重试错误）
  - retryable_exceptions 默认捕获 Exception，调用方可窄化范围

用法:
    from app.utils.retry import retry_with_backoff

    @retry_with_backoff()
    def get_kline(...):
        ...

    @retry_with_backoff(delays=(0.5, 2, 5), retry_on_none=True)
    def get_quote(...):
        ...
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Callable, Optional, Sequence, Tuple, Type

logger = logging.getLogger("ftseek_us.retry")

# 默认退避序列 — Task #38 要求 1s / 3s / 10s
DEFAULT_DELAYS: Tuple[float, ...] = (1.0, 3.0, 10.0)


def retry_with_backoff(
    retries: int = 3,
    delays: Sequence[float] = DEFAULT_DELAYS,
    retry_on_none: bool = False,
    retryable_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    log_label: Optional[str] = None,
):
    """
    通用 retry-with-backoff 装饰器。

    Args:
        retries: 最大重试次数 (不含首次调用)；总尝试次数 = retries + 1。
        delays: 每次重试前的等待秒数序列；长度不足时用最后一项填充。
        retry_on_none: 是否把 None 返回也视为失败可重试。
        retryable_exceptions: 哪些异常类型可重试，其余直接抛出。
        log_label: 日志中显示的标签 (默认用被装饰函数 __qualname__)。

    Returns:
        装饰器；被装饰函数的签名/返回值不变；最后一次仍失败时:
          - 异常路径：抛出最后一次的异常
          - retry_on_none 路径：返回 None
    """
    if retries < 0:
        retries = 0
    if not delays:
        delays = (1.0,)

    def _decorator(func: Callable):
        label = log_label or getattr(func, "__qualname__", func.__name__)

        @functools.wraps(func)
        def _wrapper(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for attempt in range(retries + 1):
                try:
                    result = func(*args, **kwargs)
                    if retry_on_none and result is None and attempt < retries:
                        # 触发重试路径
                        wait = float(delays[min(attempt, len(delays) - 1)])
                        logger.warning(
                            "[retry] %s 返回 None，第 %d/%d 次重试，等待 %.1fs",
                            label, attempt + 1, retries, wait,
                        )
                        time.sleep(wait)
                        continue
                    if attempt > 0:
                        logger.info(
                            "[retry] %s 第 %d 次重试后成功", label, attempt
                        )
                    return result
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt >= retries:
                        # 重试用尽
                        logger.error(
                            "[retry] %s 重试 %d 次仍失败，抛出原异常: %s",
                            label, retries, e,
                        )
                        raise
                    wait = float(delays[min(attempt, len(delays) - 1)])
                    logger.warning(
                        "[retry] %s 第 %d/%d 次失败 (%s)，等待 %.1fs 后重试",
                        label, attempt + 1, retries, e, wait,
                    )
                    time.sleep(wait)

            # retry_on_none 路径用尽 — 返回最后一次结果 (None)
            return None

        # 暴露原函数便于单测/绕过
        _wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return _wrapper

    return _decorator


__all__ = ["retry_with_backoff", "DEFAULT_DELAYS"]
