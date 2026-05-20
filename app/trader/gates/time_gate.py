#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时段门禁 (Time Gate)

禁止在特定时段后开新仓。

根因:
- 历史数据 40% 持平交易因 14:30+ 开仓 → 15:45 盘末退出强平
- 港股半日市下午休市 (09:30-12:00)

配置:
    strategy.time_gate:
      enabled: true
      hk_latest_open: "14:30"

行为:
- 实盘: 14:30 后硬拦截
- 模拟盘:
  - 14:30-15:30: 告警但放行 (数据收集)
  - 15:30+: 硬拦截 (盘末退出前不足 15min)
  - 半日市 12:00 后：硬拦截

版本历史:
  2026-05-19: 从 executor.py 提取
"""

from datetime import datetime, date as _date
from typing import Tuple

import pytz

from app.trader.gates.base_gate import GateInterface
from app.strategy.base import Signal


class TimeGate(GateInterface):
    """
    时段门禁 — 禁止在特定时段后开新仓

    依赖 context:
        - config: 配置 (提供 TIME_GATE)
        - logger: 日志记录器
        - failure_tracker: 失败追踪器
        - is_sim: 是否模拟盘
    """

    def name(self) -> str:
        return "TimeGate"

    def is_enabled(self, context: dict) -> bool:
        config = context.get('config')
        if config is None:
            return True  # 默认启用
        tg_cfg = getattr(config, 'TIME_GATE', {'enabled': True})
        return tg_cfg.get('enabled', True)

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        检查时段是否允许开仓

        Returns:
            (True, ""): 通过
            (False, "原因"): 拦截
        """
        config = context.get('config')
        logger = context.get('logger')
        failure_tracker = context.get('failure_tracker')
        is_sim = context.get('is_sim', True)

        if not self.is_enabled(context):
            return True, ""

        # 读取配置
        tg_cfg = getattr(config, 'TIME_GATE', {'enabled': True, 'hk_latest_open': '14:30'})
        latest_str = tg_cfg.get('hk_latest_open', '14:30')
        latest_h, latest_m = map(int, latest_str.split(':'))
        latest_min = latest_h * 60 + latest_m

        # 当前港股时间
        hk_now = datetime.now(pytz.timezone('Asia/Hong_Kong'))
        hk_minutes = hk_now.hour * 60 + hk_now.minute

        # ── 半日市下午拦截 ──
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            cal = get_holiday_calendar()
            hk_today = _date(hk_now.year, hk_now.month, hk_now.day)
            if cal.is_hk_half_day(hk_today) and hk_minutes >= 12 * 60:
                msg = f"半日市下午休市: {hk_now.strftime('%Y-%m-%d')} 12:00 后禁止开仓"
                if logger:
                    logger.info(f"[半日市门禁] {signal.code} {msg}")
                if failure_tracker:
                    failure_tracker.record_failure(signal.code, 'half_day_closed', msg)
                return False, msg
        except Exception as _hd_err:
            if logger:
                logger.debug(f"[半日市门禁] 检查跳过: {_hd_err}")

        # ── 时段检查 ──
        if hk_minutes >= latest_min:
            # 14:30 之后
            if is_sim and hk_minutes < 15 * 60 + 30:
                # 模拟盘 14:30-15:30: 告警但放行，收集数据
                msg = f"时段门禁告警 (影子): {hk_now.strftime('%H:%M')} ≥ {latest_str}，模拟盘放行 (数据收集)"
                if logger:
                    logger.info(f"[模拟门禁·影子] {signal.code} {msg}")
                signal.extra = signal.extra or {}
                signal.extra['gate_warning'] = 'time_near_close'
                return True, ""  # 告警但放行

            elif hk_minutes >= 15 * 60 + 30:
                # 15:30+ 硬拦截：距离盘末退出仅 15min，不值得
                msg = f"时段门禁: 港股 {hk_now.strftime('%H:%M')} ≥ 15:30，禁止开新仓 (盘末退出前不足 15min)"
                if logger:
                    logger.info(f"[时段门禁] {signal.code} {msg}")
                if failure_tracker:
                    failure_tracker.record_failure(signal.code, 'time_gate', msg)
                return False, msg

            else:
                # 实盘: 硬拦截
                msg = f"时段门禁: 港股 {hk_now.strftime('%H:%M')} ≥ {latest_str}，禁止开新仓 (防盘末退出强平)"
                if logger:
                    logger.info(f"[时段门禁] {signal.code} {msg}")
                if failure_tracker:
                    failure_tracker.record_failure(signal.code, 'time_gate', msg)
                return False, msg

        # 通过：时段允许
        return True, ""
