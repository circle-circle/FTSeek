"""
Regime Filter (市场环境过滤器) - Livermore 门控系统模块 #5

核心理念:
"永远不要与市场趋势作对。"
突破策略在震荡市或极度恐慌市中胜率极低。此模块作为"第一道总闸"。

功能:
1. VIX 恐慌检测 (VIX > 30 禁止突破交易)
2. 大盘趋势检测 (SPY < 50MA 视为空头，大幅降权)
"""

import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str = "RegimeFilter"
    passed: bool = True
    score: float = 1.0
    weight: float = 0.15
    reason: str = ""
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class RegimeFilter:
    """市场环境过滤器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        cfg = loader.get('strategy_enhancements.regime_filter', {})
        self.enabled = cfg.get('enabled', True)
        self.max_vix = cfg.get('max_vix_threshold', 30.0)
        self.spy_trend_ma = cfg.get('spy_trend_ma', 50)
        self.bear_penalty = cfg.get('spy_bear_market_penalty', 0.5)
        
        logger.info(
            f"[RegimeFilter] 已初始化 | VIX 上限={self.max_vix} | "
            f"SPY 趋势 MA={self.spy_trend_ma}"
        )

    def check(self, symbol: str, history: Dict, market_ctx: Dict) -> GateResult:
        """
        检查市场环境是否适合交易

        Args:
            symbol: 标的代码
            history: 历史数据 (本模块主要用不到个股历史，主要看宏观)
            market_ctx: 包含 'vix', 'spy_context' 等
        """
        if not self.enabled:
            return GateResult(name="RegimeFilter", passed=True, score=1.0, reason="Disabled")

        try:
            # 1. 获取 VIX
            vix = market_ctx.get('vix', 0.0)
            vix_observed = True
            # 12Data 返回的 VIX 有时是字符串或负数，需处理
            try:
                vix = float(vix)
            except (ValueError, TypeError):
                vix = float(market_ctx.get('vix_score_fallback', 20.0))
                vix_observed = False
            
            if vix > self.max_vix:
                return GateResult(
                    name="RegimeFilter", passed=False, score=0.0, 
                    reason=f"VIX ({vix:.1f}) > Threshold ({self.max_vix}), 极度恐慌，禁止突破"
                )

            # 2. 获取 SPY 趋势
            spy_ctx = market_ctx.get('spy_context', {})
            spy_price = spy_ctx.get('price', 0.0)
            # DEB-20260903 #51: 按配置 spy_trend_ma 读取对应 MA 键 (原硬编码 ma50 使配置无效),
            # 键不存在时回退 ma50 保持兼容
            spy_ma = spy_ctx.get(f'ma{self.spy_trend_ma}', 0.0) or spy_ctx.get('ma50', 0.0)
            
            # 简单逻辑：如果没有提供 MA，我们检查 price vs prev_close 的粗略趋势
            # 更好的方式是使用 TwelveData 获取 SPY 历史，但为了性能，这里假设 market_ctx 包含趋势状态
            # 如果外部未提供 MA，我们给一个中性分
            if spy_ma > 0 and spy_price < spy_ma:
                score = self.bear_penalty
                reason = f"SPY ({spy_price:.2f}) < MA{self.spy_trend_ma} ({spy_ma:.2f}), 空头市场"
            else:
                # VIX 较低时，分数随 VIX 降低而升高
                # VIX 20 -> 1.0, VIX 30 -> 0.0 (线性插值)
                # DEB-20260903 #51: 加上界钓位 min(1.0,...) — VIX<15 时原式 score>1.0
                # 违反 GateResult score 0-1 约定 (:24)
                score = min(1.0, max(0.5, 1.0 - (vix - 15.0) / 30.0))
                reason = (f"市场环境正常 (VIX={vix:.1f})" if vix_observed else
                          f"VIX 不可用；沿用既有回退评分输入={vix:.1f}（非观测值）")

            return GateResult(
                name="RegimeFilter", passed=True, score=score, reason=reason,
                metadata={"vix_observed": vix_observed},
            )

        except Exception as e:
            logger.warning(f"[RegimeFilter] 检查异常: {e}")
            return GateResult(name="RegimeFilter", passed=True, score=0.5, reason=f"Error: {e}")
