"""
趋势过滤 — 防止在强趋势日交易

逻辑:
  SPY 当日涨跌 > 3% → 暂停 scalping (趋势太强，均值回归失效)
  VIX > 35 → 暂停 scalping (波动太大)

原理: scalping 是均值回归策略，在趋势日会连续止损。
      强趋势 = 单边行情 = 均值回归的天敌。
"""

import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class TrendFilter:
    """
    大盘趋势过滤
    
    过滤条件:
      1. SPY 当日涨跌超过阈值 → 暂停
      2. VIX 超过阈值 → 暂停
    
    数据源:
      SPY 和 VIX 数据通过 MarketDataRouter 获取
    """

    def __init__(
        self,
        spy_threshold: float = 0.03,     # SPY 涨跌 3% 阈值
        vix_threshold: float = 35.0,      # VIX 35 阈值
    ):
        self.spy_threshold = spy_threshold
        self.vix_threshold = vix_threshold

        # 缓存 (避免频繁 API 调用)
        self._cached_spy_change: Optional[float] = None
        self._cached_vix: Optional[float] = None
        self._cache_time = None

    def is_trend_too_strong(self, spy_change_pct: float) -> bool:
        """
        检查 SPY 趋势是否太强
        
        Args:
            spy_change_pct: SPY 当日涨跌幅 (如 0.03 = +3%)
        
        Returns:
            True 如果趋势太强 (应暂停 scalping)
        """
        return abs(spy_change_pct) > self.spy_threshold

    def is_vix_too_high(self, vix: float) -> bool:
        """
        检查 VIX 是否太高
        
        Args:
            vix: 当前 VIX 值
        
        Returns:
            True 如果 VIX 太高 (应暂停 scalping)
        """
        return vix > self.vix_threshold

    def should_block(
        self,
        spy_change_pct: float,
        vix: Optional[float] = None,
    ) -> Optional[str]:
        """
        综合检查是否应阻止 scalping 交易
        
        Args:
            spy_change_pct: SPY 当日涨跌幅
            vix: 当前 VIX 值 (可选)
        
        Returns:
            阻止原因字符串，或 None 如果允许交易
        """
        # SPY 趋势检查
        if self.is_trend_too_strong(spy_change_pct):
            direction = "上涨" if spy_change_pct > 0 else "下跌"
            reason = (
                f"SPY 当日{direction} {spy_change_pct:.1%}，"
                f"趋势太强 (阈值: {self.spy_threshold:.1%})，"
                f"均值回归策略暂停"
            )
            logger.warning(f"[TrendFilter] 🚫 {reason}")
            return reason

        # VIX 检查
        if vix is not None and self.is_vix_too_high(vix):
            reason = (
                f"VIX = {vix:.1f} > {self.vix_threshold:.0f}，"
                f"波动太大，均值回归策略暂停"
            )
            logger.warning(f"[TrendFilter] 🚫 {reason}")
            return reason

        return None

    def update_config(
        self,
        spy_threshold: Optional[float] = None,
        vix_threshold: Optional[float] = None,
    ):
        """更新配置"""
        if spy_threshold is not None:
            self.spy_threshold = spy_threshold
        if vix_threshold is not None:
            self.vix_threshold = vix_threshold
        logger.info("[TrendFilter] 配置已更新")

    def get_state(self) -> Dict:
        """获取当前状态"""
        return {
            "spy_threshold": self.spy_threshold,
            "vix_threshold": self.vix_threshold,
        }


def get_trend_filter(**kwargs) -> TrendFilter:
    """获取趋势过滤器实例"""
    return TrendFilter(**kwargs)
