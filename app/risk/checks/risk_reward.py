#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RiskRewardCheck — 盈亏比预检

职责:
  检查预期回报是否大于风险 (R:R >= min_rr)

依赖:
  - dynamic_exit_manager (读取止损/止盈参数)
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.risk_reward")


class RiskRewardCheck(RiskCheck):
    """盈亏比预检"""

    @property
    def name(self) -> str:
        return "risk_reward"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            from config.config_center import get_config_center
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem

            # 从 risk_limits SSOT 读取阈值 (2026-05-11 P0 修复，2026-05-19 对齐 YAML 1.25)
            min_rr = get_config_center().get('risk_limits.min_rr', 1.25)

            stop_pct = abs(_dem.current_params.get('stop_loss', -0.025))   # P1-SSOT: fallback 对齐 YAML (-0.025)
            take_pct = abs(_dem.current_params.get('take_profit', 0.08))   # P1-SSOT: fallback 对齐 YAML (0.08)
            if stop_pct > 0:
                rr_ratio = take_pct / stop_pct
                if rr_ratio < min_rr:
                    return CheckResult(
                        passed=False,
                        check_name=self.name,
                        reason=f'R:R={rr_ratio:.2f}:1 < {min_rr}:1 (止盈={take_pct:.1%}/止损={stop_pct:.1%})',
                        reject_code="risk_reward",
                    )
        except Exception as e:
            # 2026-05-19: 盈亏比异常必须记录，防止静默跳过 R:R 检查
            logger.warning(f"[盈亏比] 检查失败，跳过：{e}")

        return CheckResult(passed=True, check_name=self.name)
