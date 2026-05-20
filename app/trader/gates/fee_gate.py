#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
费用门禁 (Fee Gate)

防止小仓位交易被费用侵蚀。

根因:
- 港股费用 ~140bps
- HK$3K 交易：费用占比 1.4% → 实际 R:R 从 1.43:1 降至 0.4:1
- HK$8K+ 交易：费用占比 <0.6% → R:R 可接受

配置:
    base.simulation_mode.min_trade_value: 8000

行为:
- 实盘：交易金额 < min_trade_value → 硬拦截
- 模拟盘：告警但放行 (数据收集)

版本历史:
  2026-05-19: 从 executor.py 提取
"""

from typing import Tuple

from app.trader.gates.base_gate import GateInterface
from app.strategy.base import Signal


class FeeGate(GateInterface):
    """
    费用门禁 — 防止小仓位交易被费用侵蚀

    依赖 context:
        - config_center: 配置中心
        - logger: 日志记录器
        - failure_tracker: 失败追踪器
        - is_sim: 是否模拟盘
        - get_lot_size_fn: 获取一手股数函数
    """

    def name(self) -> str:
        return "FeeGate"

    def is_enabled(self, context: dict) -> bool:
        return True  # 始终启用

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        检查交易金额是否足够 (费用不侵蚀 R:R)

        Returns:
            (True, ""): 通过
            (False, "原因"): 拦截
        """
        config_center = context.get('config_center')
        logger = context.get('logger')
        failure_tracker = context.get('failure_tracker')
        is_sim = context.get('is_sim', True)
        get_lot_size_fn = context.get('get_lot_size_fn')

        if config_center is None:
            return True, ""

        # 读取最小交易金额配置
        try:
            min_tv = config_center.get('base.simulation_mode.min_trade_value', 8000)
        except Exception:
            return True, ""

        # 预估交易金额
        if signal.price <= 0 or get_lot_size_fn is None:
            return True, ""

        try:
            lot_size = get_lot_size_fn(signal.code)
            est_value = signal.price * lot_size
        except Exception:
            return True, ""

        if 0 < est_value < min_tv:
            if is_sim:
                # 模拟盘：告警但放行 (数据收集)
                msg = f"预估金额 HK${est_value:,.0f} < HK${min_tv:,}，费用侵蚀严重但模拟盘放行"
                if logger:
                    logger.info(f"[模拟门禁·费用] {signal.code} {msg}")
                signal.extra = signal.extra or {}
                signal.extra['fee_warning'] = f'trade_value_below_min ({est_value:.0f}<{min_tv})'
                return True, ""  # 告警但放行
            else:
                # 实盘：硬拦截
                msg = f"交易金额过小: HK${est_value:,.0f} < HK${min_tv:,}，费用侵蚀严重"
                if logger:
                    logger.info(f"[费用门禁] {signal.code} {msg}")
                if failure_tracker:
                    failure_tracker.record_failure(signal.code, 'min_trade_value', msg)
                return False, msg

        # 通过：金额足够
        return True, ""
