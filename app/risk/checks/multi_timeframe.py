#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MultiTimeframeCheck — 多周期趋势过滤

职责:
  检查多周期趋势是否对齐，根据对齐度调整置信度

依赖:
  - IMarketDataProvider 或 subscriber._get_multi_timeframe_signal
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.multi_timeframe")


class MultiTimeframeCheck(RiskCheck):
    """多周期趋势过滤"""

    @property
    def name(self) -> str:
        return "multi_timeframe"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        market_data = deps.get("market_data")

        if market_data is None:
            return CheckResult(passed=True, check_name=self.name)

        action_str = ctx.action.value if hasattr(ctx.action, 'value') else str(ctx.action)

        mtf_confirmed, mtf_boost, mtf_reason = market_data.get_mtf_alignment(ctx.code, action_str)
        # 同步 MTF 对齐分数至共识引擎缓存
        try:
            buy_score, sell_score = market_data.get_mtf_scores(ctx.code)
            market_data.update_mtf_alignment(ctx.code, buy_score, sell_score)
        except Exception as _mtf_e:
            logger.debug(f"[MTF 缓存同步] {ctx.code} 失败：{_mtf_e}")

        if not mtf_confirmed:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"多周期过滤拒绝：{mtf_reason}",
                reject_code="mtf_rejected",
            )

        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={
                "mtf_confirmed": True,
                "mtf_boost": mtf_boost,
                "mtf_reason": mtf_reason,
            },
        )
