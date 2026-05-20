"""
趋势过滤器 (Trend Filter) - V2 增强版

功能:
1. 判断当前市场趋势 (上涨/下跌/震荡)
2. 防止逆势交易
3. 多周期趋势确认 (日K + 60分钟K)
4. 提供趋势信号给策略引擎

使用:
- 在信号生成前调用 should_trade() 检查
- 下跌趋势中禁止买入
- 上涨趋势中禁止做空
- 支持多周期确认，提高判断准确性

更新日志:
- 2026-03-21: V2 增强版，添加多周期确认
"""

import logging
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.trend_filter")
logger.propagate = False  # 防止日志重复


class Trend(Enum):
    """趋势类型"""
    STRONG_UP = "strong_up"      # 强上涨
    WEAK_UP = "weak_up"          # 弱上涨
    RANGE = "range"              # 震荡
    WEAK_DOWN = "weak_down"      # 弱下跌
    STRONG_DOWN = "strong_down"  # 强下跌


@dataclass
class TrendInfo:
    """趋势信息"""
    trend: Trend
    ma5: float
    ma20: float
    price: float
    adx: Optional[float] = None
    allow_buy: bool = True
    allow_sell: bool = True
    # V2 新增
    intraday_trend: Optional[Trend] = None
    confirmed: bool = False
    reason: str = ""
    # 2026-03-25 V3 新增: 多周期趋势确认
    k60m_trend: Optional[Trend] = None  # 60分钟线趋势缓存
    multi_timeframe_confirmed: bool = True  # 多周期确认状态
    trend_score: float = 0.0  # 趋势强度评分 (-1 to 1)
    # 2026-03-25 V4 新增: 超短线趋势确认
    k3m_trend: Optional[Trend] = None  # 3分钟线趋势缓存
    k5m_trend: Optional[Trend] = None  # 5分钟线趋势缓存
    short_term_confirmed: bool = True  # 短周期确认状态
    # 2026-05-17 新增: 门禁告警 (模拟盘 shadow 模式)
    gate_warnings: Optional[list] = None  # 门禁告警列表，不拦截但记录


class TrendFilter:
    """趋势过滤器 V2"""
    
    def __init__(self, config: dict = None):
        """
        初始化
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.ma_period_short = self.config.get('ma_period_short', 5)
        self.ma_period_long = self.config.get('ma_period_long', 10)  # Critic 审查：10 日反应更快
        self.ma_period_intraday = self.config.get('ma_period_intraday', 10)
        self.prevent_buy_in_downtrend = self.config.get('prevent_buy_in_downtrend', True)
        self.prevent_short_in_uptrend = self.config.get('prevent_short_in_uptrend', True)
        self.min_adx_for_trend = self.config.get('min_adx_for_trend', 20)
        self.adx_range_gate = self.config.get('adx_range_gate', 18)  # P0 (2026-05-16): 范围市场门禁
        self.use_multi_timeframe = self.config.get('use_multi_timeframe', True)
        self.require_both_timeframes = self.config.get('require_both_timeframes', True)
        
        # 模拟盘 shadow 模式 (2026-05-17): 门禁告警但不拦截，用于数据收集
        self._shadow_mode = self.config.get('shadow_mode', False)
        
        # 缓存
        self._cache = {}
        # 2026-03-25 V3 新增: 多周期趋势缓存
        self._k60m_trends: Dict[str, Trend] = {}  # 60分钟线趋势缓存
        # 2026-03-25 V4 新增: 超短线趋势缓存
        self._k3m_trends: Dict[str, Trend] = {}  # 3分钟线趋势缓存
        self._k5m_trends: Dict[str, Trend] = {}  # 5分钟线趋势缓存
        
        logger.info(f"趋势过滤器V2初始化 | MA短={self.ma_period_short} | MA长={self.ma_period_long} | 多周期={self.use_multi_timeframe}")
    
    def update_k60m_trend(self, code: str, k60m_trend: Trend):
        """
        更新60分钟线趋势 (从外部data_subscriber调用)
        
        Args:
            code: 股票代码
            k60m_trend: 60分钟线趋势
        """
        self._k60m_trends[code] = k60m_trend
        logger.debug(f"[多周期趋势] {code} 60分钟线趋势更新: {k60m_trend.value}")
    
    def update_k3m_trend(self, code: str, k3m_trend: Trend):
        """
        更新3分钟线趋势 (从外部data_subscriber调用)
        
        Args:
            code: 股票代码
            k3m_trend: 3分钟线趋势
        """
        self._k3m_trends[code] = k3m_trend
        logger.debug(f"[多周期趋势] {code} 3分钟线趋势更新: {k3m_trend.value}")
    
    def update_k5m_trend(self, code: str, k5m_trend: Trend):
        """
        更新5分钟线趋势 (从外部data_subscriber调用)
        
        Args:
            code: 股票代码
            k5m_trend: 5分钟线趋势
        """
        self._k5m_trends[code] = k5m_trend
        logger.debug(f"[多周期趋势] {code} 5分钟线趋势更新: {k5m_trend.value}")
    
    def get_k60m_trend(self, code: str) -> Optional[Trend]:
        """获取60分钟线趋势"""
        return self._k60m_trends.get(code)
    
    def get_k3m_trend(self, code: str) -> Optional[Trend]:
        """获取3分钟线趋势"""
        return self._k3m_trends.get(code)
    
    def get_k5m_trend(self, code: str) -> Optional[Trend]:
        """获取5分钟线趋势"""
        return self._k5m_trends.get(code)
    
    def _calculate_ma(self, data: list, period: int) -> float:
        """计算移动平均线"""
        if len(data) < period:
            return 0
        return sum(data[-period:]) / period
    
    def _determine_trend(self, price: float, ma_short: float, ma_long: float) -> Trend:
        """
        判断趋势方向
        
        Args:
            price: 当前价格
            ma_short: 短期均线
            ma_long: 长期均线
            
        Returns:
            Trend: 趋势类型
        """
        if ma_long == 0:
            return Trend.RANGE
        
        # 上涨趋势: 价格 > 短期MA > 长期MA
        if price > ma_short > ma_long:
            slope = (ma_short - ma_long) / ma_long
            if slope > 0.015:  # 1.5%斜率
                return Trend.STRONG_UP
            return Trend.WEAK_UP
        
        # 下跌趋势: 价格 < 短期MA < 长期MA
        if price < ma_short < ma_long:
            slope = (ma_long - ma_short) / ma_long
            if slope > 0.015:
                return Trend.STRONG_DOWN
            return Trend.WEAK_DOWN
        
        return Trend.RANGE

    def _compute_adx(self, highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
        """
        计算 ADX (Average Directional Index) — 趋势强度指标
        ADX < 18: 震荡市（无趋势），应拒绝 BB/RSI 假信号
        ADX 18-25: 弱趋势
        ADX > 25: 强趋势

        Args:
            highs: 最高价列表
            lows: 最低价列表
            closes: 收盘价列表
            period: ADX 周期 (默认 14)

        Returns:
            ADX 值 (0-100)，或 None 如果数据不足
        """
        if len(highs) < period + 1 or len(lows) < period + 1:
            return None

        # 计算 +DM, -DM, TR
        plus_dm = []
        minus_dm = []
        true_ranges = []

        for i in range(1, len(highs)):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            true_ranges.append(tr)

            if high_diff > low_diff and high_diff > 0:
                plus_dm.append(high_diff)
            else:
                plus_dm.append(0)

            if low_diff > high_diff and low_diff > 0:
                minus_dm.append(low_diff)
            else:
                minus_dm.append(0)

        # 初始 ATR, +DI, -DI (使用 period 的简单平均)
        atr = sum(true_ranges[:period]) / period
        plus_di = 100 * sum(plus_dm[:period]) / atr if atr > 0 else 0
        minus_di = 100 * sum(minus_dm[:period]) / atr if atr > 0 else 0

        # 计算初始 DX
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_values = [dx]

        # 平滑计算后续值 (Wilder's smoothing)
        for i in range(period, len(true_ranges)):
            atr = (atr * (period - 1) + true_ranges[i]) / period
            plus_di_val = (plus_dm[i] if i < len(plus_dm) else 0)
            minus_di_val = (minus_dm[i] if i < len(minus_dm) else 0)
            plus_di = (plus_di * (period - 1) + plus_di_val) / period
            minus_di = (minus_di * (period - 1) + minus_di_val) / period

            di_sum = plus_di + minus_di
            dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
            dx_values.append(dx)

        # ADX = DX 的 period 期平均
        if len(dx_values) < period:
            return None

        adx = sum(dx_values[-period:]) / period
        return round(adx, 1)

    def analyze_trend(self, closes: list, highs: list = None, lows: list = None, 
                      intraday_closes: list = None, adx: float = None, code: str = None) -> TrendInfo:
        """
        分析趋势 (支持多周期 + ADX增强)
        
        Args:
            closes: 日K收盘价列表
            highs: 最高价列表 (可选)
            lows: 最低价列表 (可选)
            intraday_closes: 日内K线收盘价列表 (可选，用于多周期确认)
            adx: ADX值 (可选，用于增强趋势判断)
            
        Returns:
            TrendInfo: 趋势信息
        """
        if len(closes) < self.ma_period_long:
            logger.warning(f"数据不足: 需要{self.ma_period_long}个，实际{len(closes)}个")
            return TrendInfo(
                trend=Trend.RANGE, 
                ma5=0, 
                ma20=0, 
                price=closes[-1] if closes else 0,
                reason="数据不足，默认震荡"
            )
        
        # 计算日K均线
        ma5 = self._calculate_ma(closes, self.ma_period_short)
        ma20 = self._calculate_ma(closes, self.ma_period_long)
        price = closes[-1]
        
        # 判断日K趋势
        daily_trend = self._determine_trend(price, ma5, ma20)
        
        # 2026-03-25 新增: ADX增强趋势方向判断
        # 当ADX > 30时，表示强趋势，需要更严格的方向判断
        if adx is not None and adx > 30:
            # 价格位置判断
            price_below_ma5 = price < ma5
            price_below_ma20 = price < ma20
            price_above_ma5 = price > ma5
            price_above_ma20 = price > ma20
            
            # 强下跌趋势: 价格 < MA5 < MA20
            if price_below_ma5 and ma5 < ma20:
                daily_trend = Trend.STRONG_DOWN
                logger.info(f"[ADX增强] ADX={adx:.1f} 价格{price:.2f}<MA5{ma5:.2f}<MA20{ma20:.2f} → 强下跌")
            # 强上涨趋势: 价格 > MA5 > MA20
            elif price_above_ma5 and ma5 > ma20:
                daily_trend = Trend.STRONG_UP
                logger.info(f"[ADX增强] ADX={adx:.1f} 价格{price:.2f}>MA5{ma5:.2f}>MA20{ma20:.2f} → 强上涨")
            # 价格在MA之间，但ADX高 - 可能是趋势转换
            elif adx > 50:
                # 极端ADX值，趋势可能反转
                if price_below_ma20:
                    daily_trend = Trend.WEAK_DOWN
                    logger.warning(f"[ADX预警] ADX={adx:.1f}极高，价格{price:.2f}<MA20{ma20:.2f} → 偏弱下跌")
                elif price_above_ma20:
                    daily_trend = Trend.WEAK_UP
                    logger.warning(f"[ADX预警] ADX={adx:.1f}极高，价格{price:.2f}>MA20{ma20:.2f} → 偏弱上涨")
        
        # 多周期确认
        intraday_trend = None
        confirmed = True
        reason_parts = [f"日K:{daily_trend.value}"]
        
        # ── P0 范围市场门禁 (2026-05-16): ADX < 18 禁止买入 ──
        # 根因: BB+RSI 在震荡市产生大量假信号，历史胜率仅 42%
        # ADX < 18 表示无趋势，此时 BB/RSI 的超买超卖信号不可靠
        # 修复 (2026-05-17): reason_parts 必须在使用前定义 + 模拟盘 shadow 模式
        adx_below_gate = False
        if adx is not None and adx < self.adx_range_gate:
            adx_below_gate = True
            logger.info(f"[范围市场门禁] ADX={adx:.1f} < {self.adx_range_gate}，标记为震荡市")
        
        if self.use_multi_timeframe and intraday_closes and len(intraday_closes) >= self.ma_period_intraday:
            # 计算日内均线
            intraday_ma5 = self._calculate_ma(intraday_closes, 5)
            intraday_ma10 = self._calculate_ma(intraday_closes, self.ma_period_intraday)
            intraday_price = intraday_closes[-1]
            
            intraday_trend = self._determine_trend(intraday_price, intraday_ma5, intraday_ma10)
            reason_parts.append(f"日内:{intraday_trend.value}")
            
            # 检查趋势一致性
            if self.require_both_timeframes:
                daily_down = daily_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                daily_up = daily_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                intraday_down = intraday_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                intraday_up = intraday_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                
                # 下跌趋势确认: 日K和日内K都显示下跌
                if daily_down and intraday_down:
                    confirmed = True
                    reason_parts.append("✓双重确认下跌")
                # 上涨趋势确认: 日K和日内K都显示上涨
                elif daily_up and intraday_up:
                    confirmed = True
                    reason_parts.append("✓双重确认上涨")
                # 趋势不一致
                elif (daily_down and intraday_up) or (daily_up and intraday_down):
                    confirmed = False
                    reason_parts.append("⚠趋势不一致，保持观望")
                else:
                    confirmed = True  # 震荡市场允许交易
        else:
            # 单周期模式
            if self.use_multi_timeframe:
                reason_parts.append("日内数据不足")
        
        # 计算允许的操作
        allow_buy = True
        allow_sell = True
        gate_warnings = []  # 模拟盘 shadow 模式: 记录门禁告警但不拦截

        # P0 (2026-05-16): ADX < 18 震荡市禁止买入
        # 阈值从 config.ADX_RANGE_GATE 读取，可通过 unified_config.yaml 调整
        # 修复 (2026-05-17): 使用 adx_below_gate 标志，支持模拟盘 shadow 模式
        adx_threshold = getattr(self, 'adx_range_gate', 18)
        if adx_below_gate:
            gate_warnings.append(f"ADX={adx:.1f}<{adx_threshold} 震荡市")
            reason_parts.append(f"⚠ADX={adx:.1f}<18 震荡市风险")
            # 模拟盘: shadow 模式仅告警不拦截
            if getattr(self, '_shadow_mode', False):
                logger.info(f"[模拟门禁·影子] {code} ADX={adx:.1f} < {adx_threshold} 告警但放行 (数据收集)")
            else:
                allow_buy = False
                logger.info(f"[范围市场门禁] ADX={adx:.1f} < {adx_threshold}，禁止买入 (震荡市假信号)")
        
        # 下跌趋势中禁止买入 (需要确认)
        # 模拟盘 shadow 模式: 仅记录告警，不硬拦截
        if self.prevent_buy_in_downtrend:
            if daily_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]:
                if not self.require_both_timeframes or (intraday_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN, None]):
                    if getattr(self, '_shadow_mode', False):
                        gate_warnings.append(f"下跌趋势({daily_trend.value})")
                        reason_parts.append(f"⚠下跌趋势({daily_trend.value})")
                        logger.info(f"[模拟门禁·影子] {code} 下跌趋势 {daily_trend.value} 告警但放行 (数据收集)")
                    else:
                        allow_buy = False
                        reason_parts.append("🚫禁止买入")
        
        # 上涨趋势中禁止做空 (需要确认)
        if self.prevent_short_in_uptrend:
            if daily_trend in [Trend.WEAK_UP, Trend.STRONG_UP]:
                if not self.require_both_timeframes or (intraday_trend in [Trend.WEAK_UP, Trend.STRONG_UP, None]):
                    allow_sell = False
                    reason_parts.append("🚫禁止做空")
        
        # 2026-03-25 V3 新增: 多周期趋势确认
        k60m_trend = None
        multi_timeframe_confirmed = True
        trend_score = 0.0
        
        # 计算趋势强度评分
        if daily_trend in [Trend.STRONG_UP]:
            trend_score = 1.0
        elif daily_trend in [Trend.WEAK_UP]:
            trend_score = 0.5
        elif daily_trend in [Trend.STRONG_DOWN]:
            trend_score = -1.0
        elif daily_trend in [Trend.WEAK_DOWN]:
            trend_score = -0.5
        else:
            trend_score = 0.0
        
        # 获取60分钟线趋势并验证
        if hasattr(self, '_k60m_trends'):
            k60m_trend = self._k60m_trends.get(code) if 'code' in dir() else None
            if k60m_trend:
                reason_parts.append(f"K60:{k60m_trend.value}")
                
                # 多周期趋势一致性检查
                daily_up = daily_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                daily_down = daily_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                k60m_up = k60m_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                k60m_down = k60m_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                
                # 日线和60分钟线趋势一致
                if (daily_up and k60m_up) or (daily_down and k60m_down):
                    multi_timeframe_confirmed = True
                    reason_parts.append("✓多周期确认")
                    # 加强趋势评分
                    if daily_up and k60m_up:
                        trend_score = min(1.0, trend_score + 0.3)
                    elif daily_down and k60m_down:
                        trend_score = max(-1.0, trend_score - 0.3)
                # 日线和60分钟线趋势冲突
                elif (daily_up and k60m_down) or (daily_down and k60m_up):
                    multi_timeframe_confirmed = False
                    reason_parts.append("⚠多周期冲突")
                    # 减弱趋势评分
                    trend_score = trend_score * 0.5
        
        # 2026-03-25 V4 新增: 超短线趋势确认
        k3m_trend = None
        k5m_trend = None
        short_term_confirmed = True
        
        if hasattr(self, '_k3m_trends') and hasattr(self, '_k5m_trends') and code:
            k3m_trend = self._k3m_trends.get(code)
            k5m_trend = self._k5m_trends.get(code)
            
            if k3m_trend and k5m_trend:
                # 短周期趋势一致性检查
                k3m_up = k3m_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                k3m_down = k3m_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                k5m_up = k5m_trend in [Trend.WEAK_UP, Trend.STRONG_UP]
                k5m_down = k5m_trend in [Trend.WEAK_DOWN, Trend.STRONG_DOWN]
                
                # 添加短周期趋势到原因
                reason_parts.append(f"K3:{k3m_trend.value}")
                reason_parts.append(f"K5:{k5m_trend.value}")
                
                # 短周期趋势冲突检测
                if (k3m_up and k5m_down) or (k3m_down and k5m_up):
                    short_term_confirmed = False
                    reason_parts.append("⚠短周期冲突")
                elif (k3m_up and k5m_up) or (k3m_down and k5m_down):
                    short_term_confirmed = True
                    reason_parts.append("✓短周期一致")
        
        reason = " | ".join(reason_parts)
        logger.info(f"[趋势分析] 价格:{price:.2f} MA5:{ma5:.2f} MA20:{ma20:.2f} | {reason}")
        
        return TrendInfo(
            trend=daily_trend,
            ma5=ma5,
            ma20=ma20,
            price=price,
            adx=adx,
            allow_buy=allow_buy,
            allow_sell=allow_sell,
            intraday_trend=intraday_trend,
            confirmed=confirmed,
            reason=reason,
            k60m_trend=k60m_trend,
            multi_timeframe_confirmed=multi_timeframe_confirmed,
            trend_score=trend_score,
            k3m_trend=k3m_trend,
            k5m_trend=k5m_trend,
            short_term_confirmed=short_term_confirmed,
            gate_warnings=gate_warnings if gate_warnings else None
        )
    
    def should_trade(self, action: str, trend_info: TrendInfo, rsi: Optional[float] = None, confidence: Optional[float] = None) -> Tuple[bool, str]:
        """
        检查是否应该交易
        
        Args:
            action: 交易方向 ('BUY' / 'SELL')
            trend_info: 趋势信息
            
        Returns:
            (是否允许, 原因)
        """
        trend = trend_info.trend
        adx_info = f" ADX={trend_info.adx:.1f}" if trend_info.adx else ""
        
        if action.upper() == 'BUY':
            if not trend_info.allow_buy:
                reason = f"趋势过滤: {trend.value} 趋势中禁止买入{adx_info} | {trend_info.reason}"
                logger.warning(reason)
                return False, reason
            return True, f"趋势允许买入 ({trend.value}){adx_info} | {trend_info.reason}"
        
        elif action.upper() == 'SELL':
            if not trend_info.allow_sell:
                reason = f"趋势过滤: {trend.value} 趋势中禁止做空{adx_info} | {trend_info.reason}"
                logger.warning(reason)
                return False, reason
            return True, f"趋势允许卖出 ({trend.value}){adx_info} | {trend_info.reason}"
        
        return True, "未知操作，默认允许"


# 全局实例
_trend_filter: Optional[TrendFilter] = None


def get_trend_filter(config: dict = None) -> TrendFilter:
    """获取趋势过滤器实例"""
    global _trend_filter
    if _trend_filter is None:
        # 模拟盘 shadow 模式 (2026-05-17): 门禁告警但不拦截
        if config is None:
            try:
                from config.config_center import get_config_center as _gcc_tf
                _cc_tf = _gcc_tf()
                config = _cc_tf.get('strategy.trend_filter', {})
                # 注入模拟盘 shadow 模式
                _trade_env = _cc_tf.get('base.trade_env', 'paper')
                if _trade_env in ('paper', 'simulation'):
                    config['shadow_mode'] = True
            except Exception:
                config = {}
        _trend_filter = TrendFilter(config)
    return _trend_filter


def reset_trend_filter():
    """重置趋势过滤器实例 (用于重新加载配置)"""
    global _trend_filter
    _trend_filter = None
    logger.info("趋势过滤器已重置")