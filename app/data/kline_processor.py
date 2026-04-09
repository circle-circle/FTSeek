"""
K 线数据处理模块
负责 K 线缓冲、ADX/ATR 计算、多周期趋势分析
"""
import logging
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

from app.strategy.indicators import IndicatorBuffer, adx as calc_adx, rsi as calc_rsi

logger = logging.getLogger("ftseek.data.kline_processor")


class KlineProcessor:
    """K 线数据处理器"""
    
    # ADX 计算周期
    ADX_PERIOD = 14
    
    # 多周期配置
    MULTI_TIMEFRAMES = [
        "K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"
    ]
    
    # 各周期 K 线缓冲区大小
    BUFFER_SIZES = {
        "K_1M": 200,
        "K_3M": 100,
        "K_5M": 100,
        "K_15M": 100,
        "K_30M": 100,
        "K_60M": 100,
        "K_DAY": 100,
    }
    
    # 各周期所需最小数据量
    MIN_DATA_LENGTHS = {
        "K_1M": 15,
        "K_3M": 15,
        "K_5M": 18,
        "K_15M": 25,
        "K_30M": 38,
        "K_60M": 38,
        "K_DAY": 55,
    }
    
    # 多周期权重配置 (用于对齐分数计算)
    PERIOD_WEIGHTS = {
        "K_DAY": 0.30,
        "K_60M": 0.20,
        "K_30M": 0.15,
        "K_15M": 0.12,
        "K_5M":  0.10,
        "K_3M":  0.08,
        "K_1M":  0.05,
    }
    
    # 多周期趋势权重 (用于动态止损参数计算)
    TREND_WEIGHTS = {
        "K_DAY": 0.35,
        "K_60M": 0.25,
        "K_30M": 0.15,
        "K_15M": 0.10,
        "K_5M":  0.07,
        "K_3M":  0.05,
        "K_1M":  0.03,
    }
    
    def __init__(self):
        """初始化 K 线处理器"""
        # K 线缓冲：为每个 code 维护滑动窗口
        self._kline_buffers: Dict[str, IndicatorBuffer] = defaultdict(
            lambda: IndicatorBuffer(maxlen=200)
        )
        
        # 多周期 K 线缓存
        self._kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] = defaultdict(
            lambda: {
                period: IndicatorBuffer(maxlen=self.BUFFER_SIZES.get(period, 100))
                for period in self.MULTI_TIMEFRAMES
            }
        )
        
        # 多周期趋势缓存
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(
            lambda: {period: "neutral" for period in self.MULTI_TIMEFRAMES}
        )
        
        # ADX 缓存
        self._adx_cache: Dict[str, float] = {}
    
    def push_kline(self, code: str, kline: dict, k_type: str = "K_1M") -> None:
        """
        推送 K 线数据到缓冲区
        
        Args:
            code: 股票代码
            kline: K 线数据字典
            k_type: K 线周期
        """
        # 更新主缓冲区 (1M)
        if k_type == "K_1M":
            self._kline_buffers[code].push_kline(kline)
        
        # 更新对应周期缓冲区
        if k_type in self._kline_buffers_by_period[code]:
            self._kline_buffers_by_period[code][k_type].push_kline(kline)
    
    def calculate_adx(self, code: str) -> Optional[float]:
        """
        计算指定标的的 ADX 值
        
        Args:
            code: 股票代码
        
        Returns:
            ADX 值，数据不足时返回 None
        """
        buf = self._kline_buffers[code]
        
        if buf.size < self.ADX_PERIOD * 2 + 5:
            return None
        
        adx_val = calc_adx(
            list(buf.high),
            list(buf.low),
            list(buf.close),
            self.ADX_PERIOD
        )
        
        if adx_val is not None:
            self._adx_cache[code] = adx_val
        
        return adx_val
    
    def update_adx(self, code: str, kline: dict) -> Optional[float]:
        """
        更新 ADX 值 (每根 K 线到达时调用)
        
        Args:
            code: 股票代码
            kline: K 线数据
        
        Returns:
            ADX 值，数据不足时返回 None
        """
        self.push_kline(code, kline, "K_1M")
        return self.calculate_adx(code)
    
    def calculate_atr_percent(self, code: str, period: int = 14) -> float:
        """
        计算 ATR 波动率百分比
        
        Args:
            code: 股票代码
            period: ATR 计算周期
        
        Returns:
            ATR 百分比 (0.003 ~ 0.08)
        """
        buf = self._kline_buffers[code]
        
        if buf.size < period + 1:
            return 0.02  # 默认 2%
        
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        
        # 计算 True Range
        tr_list = []
        n = min(period, len(highs) - 1)
        
        for i in range(-n, 0):
            prev_close = closes[i - 1] if (len(closes) + i - 1) >= 0 else closes[0]
            tr = max(highs[i], prev_close) - min(lows[i], prev_close)
            if tr > 0:
                tr_list.append(tr)
        
        if not tr_list:
            return 0.02
        
        current_price = closes[-1]
        if current_price <= 0:
            return 0.02
        
        atr = sum(tr_list) / len(tr_list)
        atr_pct = atr / current_price
        
        # 限制合理范围
        return max(0.003, min(atr_pct, 0.08))
    
    def calculate_atr_from_period(self, code: str, k_type: str, period: int = 14) -> float:
        """
        从指定周期 K 线计算 ATR 百分比
        
        Args:
            code: 股票代码
            k_type: K 线周期 (K_15M/K_30M/K_60M/K_DAY)
            period: ATR 计算周期
        
        Returns:
            ATR 百分比
        """
        try:
            buf = self._kline_buffers_by_period[code].get(k_type)
            if buf is None or buf.size < period + 1:
                return self.calculate_atr_percent(code)
            
            closes = list(buf.close)
            highs = list(buf.high)
            lows = list(buf.low)
            
            tr_list = []
            n = min(period, len(highs) - 1)
            
            for i in range(-n, 0):
                prev_close = closes[i - 1] if (len(closes) + i - 1) >= 0 else closes[0]
                tr = max(highs[i], prev_close) - min(lows[i], prev_close)
                if tr > 0:
                    tr_list.append(tr)
            
            if not tr_list:
                return self.calculate_atr_percent(code)
            
            current_price = closes[-1]
            if current_price <= 0:
                return 0.02
            
            atr_val = sum(tr_list) / len(tr_list)
            atr_pct = atr_val / current_price
            
            # 各周期 ATR 上下界
            bounds = {
                "K_1M":  (0.001, 0.04),
                "K_3M":  (0.002, 0.05),
                "K_5M":  (0.002, 0.05),
                "K_15M": (0.003, 0.06),
                "K_30M": (0.004, 0.08),
                "K_60M": (0.005, 0.10),
                "K_DAY": (0.008, 0.15),
            }
            
            lo, hi = bounds.get(k_type, (0.003, 0.10))
            return max(lo, min(atr_pct, hi))
        
        except Exception as e:
            logger.debug(f"[多周期 ATR] {code} {k_type} 计算失败：{e}")
            return self.calculate_atr_percent(code)
    
    def calculate_atr(self, code: str, preferred_k_type: str = "K_60M") -> float:
        """
        计算 ATR 绝对值
        
        Args:
            code: 股票代码
            preferred_k_type: 首选 K 线周期
        
        Returns:
            ATR 绝对值
        """
        buf = self._kline_buffers[code]
        if buf.size < 5:
            return 0.0
        
        current_price = buf.close[-1]
        atr_pct = self.calculate_atr_from_period(code, preferred_k_type)
        return current_price * atr_pct
    
    def update_multi_timeframe_trend(self, code: str, k_type: str, kline: dict) -> Optional[str]:
        """
        更新多周期趋势状态
        
        Args:
            code: 股票代码
            k_type: K 线周期
            kline: K 线数据
        
        Returns:
            趋势方向 ("up"/"down"/"neutral")
        """
        try:
            buf = self._kline_buffers_by_period[code][k_type]
            buf.push_kline(kline)
            
            closes = list(buf.close)
            highs = list(buf.high)
            lows = list(buf.low)
            
            min_len = self.MIN_DATA_LENGTHS.get(k_type, 20)
            if len(closes) < min_len:
                return self._multi_timeframe_trends[code][k_type]
            
            current_price = closes[-1]
            rsi_val = calc_rsi(closes, period=14)
            
            # 根据周期计算趋势
            trend = self._calculate_trend_for_period(
                k_type, closes, highs, lows, current_price, rsi_val
            )
            
            old_trend = self._multi_timeframe_trends[code][k_type]
            self._multi_timeframe_trends[code][k_type] = trend
            
            if trend != old_trend:
                rsi_str = f"RSI={rsi_val:.1f}" if rsi_val else "RSI=N/A"
                logger.info(f"[多周期趋势] {code} {k_type}: {old_trend} → {trend} ({rsi_str})")
            
            return trend
        
        except Exception as e:
            logger.debug(f"[多周期趋势] {code} {k_type} 更新失败：{e}")
            return self._multi_timeframe_trends[code][k_type]
    
    def _calculate_trend_for_period(
        self,
        k_type: str,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        current_price: float,
        rsi_val: Optional[float]
    ) -> str:
        """
        根据周期计算趋势方向
        
        Args:
            k_type: K 线周期
            closes: 收盘价列表
            highs: 最高价列表
            lows: 最低价列表
            current_price: 当前价格
            rsi_val: RSI 值
        
        Returns:
            趋势方向 ("up"/"down"/"neutral")
        """
        rsi_up_bias = (rsi_val or 50) > 50
        rsi_down_bias = (rsi_val or 50) < 50
        
        if k_type in ("K_1M", "K_3M"):
            # 超短周期：MA5/MA10
            ma_fast = sum(closes[-5:]) / 5
            ma_slow = sum(closes[-10:]) / 10
            rsi_up_bias = (rsi_val or 50) > 51
            rsi_down_bias = (rsi_val or 50) < 49
            
        elif k_type == "K_5M":
            # 短周期：MA8/MA13
            ma_fast = sum(closes[-8:]) / 8
            ma_slow = sum(closes[-13:]) / 13
            rsi_up_bias = (rsi_val or 50) > 51
            rsi_down_bias = (rsi_val or 50) < 49
            
        elif k_type == "K_15M":
            # 中短周期：MA8/MA21
            ma_fast = sum(closes[-8:]) / 8
            ma_slow = sum(closes[-21:]) / 21
            
        elif k_type in ("K_30M", "K_60M"):
            # 中/中长周期：MA13/MA34
            ma_fast = sum(closes[-13:]) / 13
            ma_slow = sum(closes[-34:]) / 34
            rsi_up_bias = (rsi_val or 50) > 48
            rsi_down_bias = (rsi_val or 50) < 52
            
        else:  # K_DAY
            # 日线：MA20/MA50
            ma20 = sum(closes[-20:]) / 20
            ma50 = sum(closes[-50:]) / 50
            rsi_up_bias = (rsi_val or 50) > 45
            rsi_down_bias = (rsi_val or 50) < 55
            
            if current_price > ma20 and ma20 > ma50 and rsi_up_bias:
                return "up"
            elif current_price < ma20 and ma20 < ma50 and rsi_down_bias:
                return "down"
            elif current_price > ma20:
                return "up" if rsi_up_bias else "neutral"
            elif current_price < ma20:
                return "down" if rsi_down_bias else "neutral"
            else:
                return "neutral"
        
        # 通用判断逻辑
        if ma_fast > ma_slow and current_price > ma_fast and rsi_up_bias:
            return "up"
        elif ma_fast < ma_slow and current_price < ma_fast and rsi_down_bias:
            return "down"
        else:
            return "neutral"
    
    def get_multi_timeframe_trend(self, code: str, k_type: str) -> str:
        """
        获取指定周期的趋势状态
        
        Args:
            code: 股票代码
            k_type: K 线周期
        
        Returns:
            趋势方向 ("up"/"down"/"neutral")
        """
        return self._multi_timeframe_trends[code].get(k_type, "neutral")
    
    def get_all_trends(self, code: str) -> Dict[str, str]:
        """
        获取标的所有周期的趋势状态
        
        Args:
            code: 股票代码
        
        Returns:
            趋势字典 {周期：趋势}
        """
        return dict(self._multi_timeframe_trends[code])
    
    def calculate_mtf_alignment_score(self, code: str, action: str) -> Tuple[float, str]:
        """
        计算多周期对齐分数
        
        Args:
            code: 股票代码
            action: 交易方向 ("BUY"/"SELL")
        
        Returns:
            (分数，详情字符串)
        """
        trends = self._multi_timeframe_trends[code]
        is_buy = (action == "BUY")
        target_trend = "up" if is_buy else "down"
        opposite_trend = "down" if is_buy else "up"
        
        aligned_weight = 0.0
        conflict_weight = 0.0
        detail_parts = []
        
        for period, weight in self.PERIOD_WEIGHTS.items():
            t = trends.get(period, "neutral")
            if t == target_trend:
                aligned_weight += weight
                detail_parts.append(f"{period}↑")
            elif t == opposite_trend:
                conflict_weight += weight
                detail_parts.append(f"{period}↓")
            else:
                detail_parts.append(f"{period}~")
        
        # 最终对齐分
        raw_score = aligned_weight - conflict_weight * 0.5
        mtf_score = max(0.0, min(1.0, raw_score))
        detail = " | ".join(detail_parts)
        
        return mtf_score, detail
    
    def get_mtf_signal_confirmation(
        self,
        code: str,
        action: str
    ) -> Tuple[bool, float, str]:
        """
        获取多周期趋势确认结果
        
        Args:
            code: 股票代码
            action: 交易方向 ("BUY"/"SELL")
        
        Returns:
            (是否确认，置信度调整，原因)
        """
        trends = self._multi_timeframe_trends[code]
        
        day_trend = trends.get("K_DAY", "neutral")
        h1_trend = trends.get("K_60M", "neutral")
        m5_trend = trends.get("K_5M", "neutral")
        m3_trend = trends.get("K_3M", "neutral")
        
        is_buy = action == "BUY"
        
        # 日线 +60M 双重过滤
        if day_trend != "neutral" and h1_trend != "neutral":
            both_against = (
                (is_buy and day_trend == "down" and h1_trend == "down") or
                (not is_buy and day_trend == "up" and h1_trend == "up")
            )
            if both_against:
                return False, 0.0, f"日线 +60M 双重逆势 (日={day_trend}, 60M={h1_trend})"
        
        # 超短线双重逆势拦截
        short_both_against = (
            (is_buy and m5_trend == "down" and m3_trend == "down") or
            (not is_buy and m5_trend == "up" and m3_trend == "up")
        )
        if short_both_against:
            return False, 0.0, f"超短线 3M+5M 双重逆势 (3M={m3_trend}, 5M={m5_trend})"
        
        # 仅日线反向
        single_against = False
        if day_trend != "neutral":
            single_against = (is_buy and day_trend == "down") or (not is_buy and day_trend == "up")
        
        # 计算对齐分
        mtf_score, detail = self.calculate_mtf_alignment_score(code, action)
        
        # 分档映射
        if single_against:
            boost = -0.08 + mtf_score * 0.05
        elif mtf_score >= 0.70:
            boost = 0.20
        elif mtf_score >= 0.55:
            boost = 0.12
        elif mtf_score >= 0.40:
            boost = 0.05
        elif mtf_score >= 0.20:
            boost = 0.0
        else:
            boost = -0.08
        
        up_count = sum(1 for t in trends.values() if t == "up")
        down_count = sum(1 for t in trends.values() if t == "down")
        reason = f"MTF 对齐={mtf_score:.2f}({detail}) 上{up_count}/下{down_count}/5"
        
        return True, round(boost, 3), reason
    
    def get_dynamic_stop_params(self, code: str) -> dict:
        """
        根据多周期趋势动态调整止损止盈参数
        
        Args:
            code: 股票代码
        
        Returns:
            止损止盈参数字典
        """
        trends = self._multi_timeframe_trends[code]
        score_map = {"up": 1, "down": -1, "neutral": 0}
        
        # 七周期加权
        combined = sum(
            score_map.get(trends.get(k, "neutral"), 0) * w
            for k, w in self.TREND_WEIGHTS.items()
        )
        
        if combined >= 0.55:  # 强上升
            return {
                "stop_loss_pct": -0.06,
                "take_profit_pct": 0.08,
                "breakeven_trigger": 0.025,
                "trail_atr_mult": 2.8,
                "regime": "strong_up",
            }
        elif combined >= 0.20:  # 弱上升
            return {
                "stop_loss_pct": -0.045,
                "take_profit_pct": 0.06,
                "breakeven_trigger": 0.020,
                "trail_atr_mult": 2.3,
                "regime": "weak_up",
            }
        elif combined <= -0.55:  # 强下跌
            return {
                "stop_loss_pct": -0.03,
                "take_profit_pct": 0.045,
                "breakeven_trigger": 0.018,
                "trail_atr_mult": 1.4,
                "regime": "strong_down",
            }
        elif combined <= -0.20:  # 弱下跌
            return {
                "stop_loss_pct": -0.04,
                "take_profit_pct": 0.05,
                "breakeven_trigger": 0.020,
                "trail_atr_mult": 1.7,
                "regime": "weak_down",
            }
        else:  # 震荡/中性
            return {
                "stop_loss_pct": -0.035,
                "take_profit_pct": 0.05,
                "breakeven_trigger": 0.018,
                "trail_atr_mult": 1.6,
                "regime": "neutral",
            }
    
    def get_adx(self, code: str) -> Optional[float]:
        """
        获取缓存的 ADX 值
        
        Args:
            code: 股票代码
        
        Returns:
            ADX 值
        """
        return self._adx_cache.get(code)
    
    def clear_code(self, code: str) -> None:
        """
        清除指定标的所有缓存数据
        
        Args:
            code: 股票代码
        """
        self._kline_buffers.pop(code, None)
        self._kline_buffers_by_period.pop(code, None)
        self._multi_timeframe_trends.pop(code, None)
        self._adx_cache.pop(code, None)


# 单例实例
_kline_processor = None


def get_kline_processor() -> KlineProcessor:
    """
    获取 K 线处理器单例
    
    Returns:
        KlineProcessor 实例
    """
    global _kline_processor
    if _kline_processor is None:
        _kline_processor = KlineProcessor()
    return _kline_processor
