#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁链 (Gate Chain)

管理多个门禁的链式检查。

设计原则:
- 门禁可配置启用/禁用
- 门禁可按优先级排序
- 单个门禁失败不影响其他门禁 (异常隔离)

使用示例:
    from app.trader.gates import GateChain, TimeGate, FeeGate, MaxPositionGate

    chain = GateChain()
    chain.add_gate(TimeGate())
    chain.add_gate(FeeGate())
    chain.add_gate(MaxPositionGate())

    # 检查所有门禁
    passed, reason = chain.check_all(signal, context)
    if not passed:
        return TradeResult(success=False, message=reason)

版本历史:
  2026-05-19: 创建
"""

from typing import List, Tuple, Optional
import logging

from app.strategy.base import Signal
from app.trader.gates.base_gate import GateInterface

logger = logging.getLogger(__name__)


class GateChain:
    """
    门禁链 — 管理多个门禁的链式检查

    特点:
    - 异常隔离: 单个门禁失败不影响其他门禁
    - 可配置: 每个门禁可独立启用/禁用
    - 可追溯: 记录每个门禁的检查结果
    """

    def __init__(self):
        self._gates: List[GateInterface] = []

    def add_gate(self, gate: GateInterface, priority: Optional[int] = None):
        """
        添加门禁

        Args:
            gate: 门禁实例
            priority: 优先级 (越小越先执行，默认按添加顺序)
        """
        if priority is not None:
            # 按优先级插入
            self._gates.insert(priority, gate)
        else:
            self._gates.append(gate)
        logger.debug(f"[门禁链] 添加门禁：{gate.name()}")

    def remove_gate(self, gate_name: str) -> bool:
        """
        移除门禁

        Args:
            gate_name: 门禁名称

        Returns:
            bool: 是否移除成功
        """
        for i, gate in enumerate(self._gates):
            if gate.name() == gate_name:
                self._gates.pop(i)
                logger.info(f"[门禁链] 移除门禁：{gate_name}")
                return True
        return False

    def check_all(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        执行所有门禁检查

        Args:
            signal: 交易信号
            context: 上下文 (包含门禁需要的依赖)

        Returns:
            Tuple[bool, str]: (是否全部通过，原因)
                - (True, ""): 全部通过
                - (False, "原因"): 某个门禁拦截
        """
        for gate in self._gates:
            try:
                # 检查门禁是否启用
                if not gate.is_enabled(context):
                    logger.debug(f"[门禁链] {gate.name()} 已禁用，跳过")
                    continue

                # 执行检查
                passed, reason = gate.check(signal, context)

                if not passed:
                    logger.info(f"[门禁链] {gate.name()} 拦截：{reason}")
                    return False, reason

            except Exception as e:
                # 异常隔离：单个门禁失败不影响其他门禁
                logger.warning(f"[门禁链] {gate.name()} 检查异常：{e}，降级放行")
                continue

        # 所有门禁通过
        return True, ""

    def get_gates(self) -> List[GateInterface]:
        """获取所有门禁"""
        return self._gates.copy()

    def clear(self):
        """清空所有门禁"""
        self._gates.clear()
        logger.info("[门禁链] 已清空所有门禁")
