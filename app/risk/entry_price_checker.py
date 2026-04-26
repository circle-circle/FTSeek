"""
入场价格检查器 (Entry Price Checker)

功能:
1. 检查入场价格是否合理
2. 避免在当日高点买入
3. 避免在当日低点卖出
4. 防止追高杀跌

使用:
- 在开仓前调用 check_entry_price() 检查
- 返回 (是否允许, 原因)

更新日志:
- 2026-03-25: 初始版本
"""

import logging
from typing import Tuple, Optional, Dict
from dataclasses import dataclass
from datetime import datetime, date

logger = logging.getLogger("ftseek.entry_price_checker")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class PriceCheckResult:
    """价格检查结果"""
    allowed: bool
    reason: str
    current_price: float
    day_high: float
    day_low: float
    day_open: float
    position_in_range: float  # 当前价格在日内高低点中的位置 (0-1)
    warning: Optional[str] = None


class EntryPriceChecker:
    """入场价格检查器"""
    
    def __init__(self, config: dict = None):
        """
        初始化
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        
        # 高点买入禁止阈值: 价格在日内高点 0.5% 内禁止买入 (2026-04-17: 2%→0.5%, 上涨行情中2%拦截所有突破信号)
        self.high_buy_threshold = self.config.get('high_buy_threshold', 0.005)
        # 低点卖出禁止阈值: 价格在日内低点 2% 内禁止卖出 (2026-04-15: 1%→2%)
        self.low_sell_threshold = self.config.get('low_sell_threshold', 0.02)
        # 警告阈值: 价格在日内高点 3% 内发出警告 (2026-04-15: 2%→3%)
        self.high_buy_warning = self.config.get('high_buy_warning', 0.03)
        # 警告阈值: 价格在日内低点 3% 内发出警告 (2026-04-15: 2%→3%)
        self.low_sell_warning = self.config.get('low_sell_warning', 0.03)
        # 追高阈值: 价格涨幅超过此值时警告 (相对于开盘价)
        self.chase_threshold = self.config.get('chase_threshold', 0.03)
        # 杀跌阈值: 价格跌幅超过此值时警告 (相对于开盘价)
        self.panic_threshold = self.config.get('panic_threshold', 0.03)
        # 2026-04-08 新增：ADX 极端值阈值 - ADX 超过此值表示趋势极端，谨慎交易
        self.adx_extreme_threshold = self.config.get('adx_extreme_threshold', 60)
        # 2026-04-08 新增：极端追高禁止阈值
        self.chase_reject_threshold = self.config.get('chase_reject_threshold', 0.05)

        # ADX 缓存：{code: adx_value}
        self._adx_cache: Dict[str, float] = {}
        
        # 日内数据缓存: {code: {"high": x, "low": y, "open": z, "date": date}}
        self._daily_data: Dict[str, dict] = {}
        
        logger.info(
            f"入场价格检查器初始化 | 高点买入阈值={self.high_buy_threshold:.1%} | "
            f"追高警告阈值={self.chase_threshold:.1%} | 追高禁止阈值={self.chase_reject_threshold:.1%}"
        )
    
    def update_daily_data(self, code: str, high: float, low: float, open_price: float, current_date: str = None):
        """
        更新日内数据
        
        Args:
            code: 股票代码
            high: 当日最高价
            low: 当日最低价
            open_price: 开盘价
            current_date: 当前日期 (YYYY-MM-DD)
        """
        if current_date is None:
            current_date = date.today().isoformat()
        
        self._daily_data[code] = {
            "high": high,
            "low": low,
            "open": open_price,
            "date": current_date
        }
    
    def get_daily_data(self, code: str) -> Optional[dict]:
        """获取日内数据"""
        return self._daily_data.get(code)
    
    def check_entry_price(
        self,
        code: str,
        action: str,
        current_price: float,
        day_high: float = None,
        day_low: float = None,
        day_open: float = None
    ) -> PriceCheckResult:
        """
        检查入场价格是否合理
        
        Args:
            code: 股票代码
            action: 交易方向 ('BUY' / 'SELL')
            current_price: 当前价格
            day_high: 当日最高价 (可选，如果不提供则使用缓存)
            day_low: 当日最低价 (可选)
            day_open: 开盘价 (可选)
            
        Returns:
            PriceCheckResult: 检查结果
        """
        # 获取或更新日内数据
        if day_high is not None:
            self.update_daily_data(code, day_high, day_low or current_price, day_open or current_price)
        
        daily_data = self.get_daily_data(code)
        if not daily_data:
            logger.warning(f"[价格检查] {code} 缺少日内数据，跳过检查")
            return PriceCheckResult(
                allowed=True,
                reason="缺少日内数据，默认允许",
                current_price=current_price,
                day_high=current_price,
                day_low=current_price,
                day_open=current_price
            )
        
        high = daily_data["high"]
        low = daily_data["low"]
        open_price = daily_data["open"]
        
        # 计算当前价格在日内范围的位置 (0=低点, 1=高点)
        price_range = high - low
        if price_range > 0:
            position_in_range = (current_price - low) / price_range
        else:
            position_in_range = 0.5  # 无波动时默认中间
        
        # 计算涨跌幅 (相对于开盘价)
        change_pct = (current_price - open_price) / open_price if open_price > 0 else 0
        
        action_upper = action.upper()
        
        # ==================== 买入检查 ====================
        if action_upper == 'BUY':
            # 检查1: 价格接近当日高点
            distance_to_high = (high - current_price) / high if high > 0 else 0
            
            if distance_to_high <= self.high_buy_threshold:
                # 价格在日内高点 1% 内，禁止买入
                reason = (
                    f"价格检查: 价格 {current_price:.2f} 接近日内高点 {high:.2f} "
                    f"(距离 {distance_to_high:.2%} < {self.high_buy_threshold:.1%})，禁止追高买入"
                )
                logger.warning(f"[价格检查] {code} {reason}")
                return PriceCheckResult(
                    allowed=False,
                    reason=reason,
                    current_price=current_price,
                    day_high=high,
                    day_low=low,
                    day_open=open_price,
                    position_in_range=position_in_range
                )
            
            # 检查2: 价格涨幅过大 (追高警告)
            if change_pct > self.chase_threshold:
                warning = (
                    f"价格检查: 价格已涨 {change_pct:.2%}，可能追高，建议谨慎"
                )
                logger.warning(f"[价格检查] {code} {warning}")
                # 只发警告，不禁止
                return PriceCheckResult(
                    allowed=True,
                    reason=f"价格在合理范围 (位置: {position_in_range:.0%})",
                    current_price=current_price,
                    day_high=high,
                    day_low=low,
                    day_open=open_price,
                    position_in_range=position_in_range,
                    warning=warning
                )
            
            # 检查3: 价格接近高点警告区
            if distance_to_high <= self.high_buy_warning:
                warning = (
                    f"价格检查: 价格距日内高点仅 {distance_to_high:.2%}，注意追高风险"
                )
                logger.info(f"[价格检查] {code} {warning}")
                return PriceCheckResult(
                    allowed=True,
                    reason=f"价格在合理范围 (位置: {position_in_range:.0%})",
                    current_price=current_price,
                    day_high=high,
                    day_low=low,
                    day_open=open_price,
                    position_in_range=position_in_range,
                    warning=warning
                )
        
        # ==================== 卖出检查 ====================
        elif action_upper == 'SELL':
            # 检查1: 价格接近当日低点
            distance_to_low = (current_price - low) / current_price if current_price > 0 else 0
            
            if distance_to_low <= self.low_sell_threshold:
                # 价格在日内低点 1% 内，禁止卖出
                reason = (
                    f"价格检查: 价格 {current_price:.2f} 接近日内低点 {low:.2f} "
                    f"(距离 {distance_to_low:.2%} < {self.low_sell_threshold:.1%})，禁止杀跌卖出"
                )
                logger.warning(f"[价格检查] {code} {reason}")
                return PriceCheckResult(
                    allowed=False,
                    reason=reason,
                    current_price=current_price,
                    day_high=high,
                    day_low=low,
                    day_open=open_price,
                    position_in_range=position_in_range
                )
            
            # 检查2: 价格跌幅过大 (杀跌警告)
            if change_pct < -self.panic_threshold:
                warning = (
                    f"价格检查: 价格已跌 {abs(change_pct):.2%}，可能杀跌，建议谨慎"
                )
                logger.warning(f"[价格检查] {code} {warning}")
                return PriceCheckResult(
                    allowed=True,
                    reason=f"价格在合理范围 (位置: {position_in_range:.0%})",
                    current_price=current_price,
                    day_high=high,
                    day_low=low,
                    day_open=open_price,
                    position_in_range=position_in_range,
                    warning=warning
                )
        
        # 价格在合理范围
        return PriceCheckResult(
            allowed=True,
            reason=f"价格在合理范围 (日内位置: {position_in_range:.0%}, 涨跌: {change_pct:+.2%})",
            current_price=current_price,
            day_high=high,
            day_low=low,
            day_open=open_price,
            position_in_range=position_in_range
        )


# 全局实例
_entry_price_checker: Optional[EntryPriceChecker] = None


def get_entry_price_checker(config: dict = None) -> EntryPriceChecker:
    """获取入场价格检查器实例"""
    global _entry_price_checker
    if _entry_price_checker is None:
        _entry_price_checker = EntryPriceChecker(config)
    return _entry_price_checker


def reset_entry_price_checker():
    """重置入场价格检查器实例"""
    global _entry_price_checker
    _entry_price_checker = None
    logger.info("入场价格检查器已重置")