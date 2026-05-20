#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁接口 (Gate Interface)

所有交易前门禁必须实现此接口。

设计原则:
- 单一职责: 每个门禁只检查一个条件
- 可测试性: 门禁可独立 mock 依赖进行测试
- 可组合性: 多个门禁可链式调用

使用示例:
    class TimeGate(GateInterface):
        def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
            if not self._is_valid_time():
                return False, "时段门禁：禁止开新仓"
            return True, ""

    # executor.py 中使用:
    for gate in self._gates:
        passed, reason = gate.check(signal, context)
        if not passed:
            return TradeResult(success=False, message=reason)
"""

from abc import ABC, abstractmethod
from typing import Tuple

from app.strategy.base import Signal


class GateInterface(ABC):
    """
    门禁接口 — 交易前检查抽象

    所有门禁必须实现 check() 方法，返回 (通过，原因)
    """

    @abstractmethod
    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        执行门禁检查

        Args:
            signal: 交易信号
            context: 上下文 (包含 executor 注入的依赖)
                - position_manager: 持仓管理器
                - config: 配置
                - circuit_breaker: 熔断器
                - logger: 日志记录器

        Returns:
            Tuple[bool, str]: (是否通过，原因说明)
                - (True, ""): 通过
                - (False, "原因"): 拦截，返回原因
        """
        pass

    def name(self) -> str:
        """门禁名称 (用于日志和监控)"""
        return self.__class__.__name__

    def is_enabled(self, context: dict) -> bool:
        """
        门禁是否启用 (可配置)

        Args:
            context: 上下文

        Returns:
            bool: 是否启用
        """
        return True
