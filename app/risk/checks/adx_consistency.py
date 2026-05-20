#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ADXConsistencyCheck — ADX-趋势一致性防守检查

职责:
  检查 ADX 极高但市场状态不明确的情况 (避免趋势方向不明时开仓)

依赖:
  - ctx.consensus_result.adx_value, ctx.consensus_result.market_regime
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.adx_consistency")


class ADXConsistencyCheck(RiskCheck):
    """ADX-趋势一致性防守检查"""

    @property
    def name(self) -> str:
        return "adx_consistency"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        _adx_val = getattr(ctx.consensus_result, 'adx_value', 0) or 0
        _regime_str = getattr(ctx.consensus_result, 'market_regime', '') or ''
        _regime_unclear = _regime_str in ('range_bound', 'high_volatility', 'neutral', '')

        if _adx_val >= 50 and _regime_unclear:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"ADX={_adx_val:.1f} 极高但市场状态={_regime_str!r}，趋势方向不明",
                reject_code="adx_inconsistent",
            )

        return CheckResult(passed=True, check_name=self.name)
