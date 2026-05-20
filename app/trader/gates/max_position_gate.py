#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最大持仓数门禁 (Max Position Gate)

防止开仓超过最大持仓数限制。

配置:
    risk.max_open_positions: 12

行为:
- 当前持仓数 >= 最大持仓数 → 拦截

版本历史:
  2026-05-19: 从 executor.py 提取
"""

from typing import Tuple

from app.trader.gates.base_gate import GateInterface
from app.strategy.base import Signal


class MaxPositionGate(GateInterface):
    """
    最大持仓数门禁 — 防止开仓超过最大持仓数限制

    依赖 context:
        - config: 配置 (提供 MAX_OPEN_POSITIONS)
        - position_manager: 持仓管理器
        - logger: 日志记录器
        - failure_tracker: 失败追踪器
    """

    def name(self) -> str:
        return "MaxPositionGate"

    def is_enabled(self, context: dict) -> bool:
        return True  # 始终启用

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        检查是否达到最大持仓数

        Returns:
            (True, ""): 通过
            (False, "原因"): 拦截
        """
        config = context.get('config')
        position_manager = context.get('position_manager')
        logger = context.get('logger')
        failure_tracker = context.get('failure_tracker')

        if config is None or position_manager is None:
            return True, ""

        # 读取配置
        max_open_positions = getattr(config, 'MAX_OPEN_POSITIONS', 12)

        # 获取当前持仓数
        try:
            current_open_positions = position_manager.get_open_positions_count()
        except Exception as e:
            if logger:
                logger.warning(f"[最大持仓门禁] 获取持仓数失败: {e}，放行")
            return True, ""

        if current_open_positions >= max_open_positions:
            msg = f"已达最大持仓数 ({current_open_positions}/{max_open_positions})，拒绝开仓"
            if logger:
                logger.warning(f"[持仓限制] {signal.code} {msg}")
            if failure_tracker:
                failure_tracker.record_failure(signal.code, 'position_limit', msg)
            return False, msg

        # 通过：未达到最大持仓数
        return True, ""
