"""
ADX 趋势管理模块 (ADX Manager)
负责 ADX 计算、趋势强度判断、多周期趋势分析
"""
import logging
from typing import Dict, Optional, Tuple
from collections import defaultdict

from app.strategy.indicators import IndicatorBuffer, adx as calc_adx

logger = logging.getLogger("ftseek.data.adx_manager")
logger.propagate = False


class ADXManager:
    """ADX 趋势管理器"""

    # ADX 计算周期
    ADX_PERIOD = 14

    # ADX 阈值配置
    ADX_STRONG_TREND = 30  # 强趋势
    ADX_WEAK_TREND = 20    # 弱趋势
    ADX_HIGH_VOLATILITY = 50  # 极高波动

    def __init__(self, buffer_size: int = 200):
        """
        初始化 ADX 管理器

        Args:
            buffer_size: K 线缓冲区大小
        """
        # K 线缓冲：为每个 code 维护滑动窗口
        self._kline_buffers: Dict[str, IndicatorBuffer] = defaultdict(
            lambda: IndicatorBuffer(maxlen=buffer_size)
        )

        # ADX 缓存：code -> adx_value
        self._adx_cache: Dict[str, float] = {}

        logger.info(f"ADX 管理器已初始化 (周期={self.ADX_PERIOD})")

    def push_kline(self, code: str, kline: dict) -> None:
        """
        推送 K 线数据到缓冲区

        Args:
            code: 股票代码
            kline: K 线数据字典
        """
        self._kline_buffers[code].push_kline(kline)

    def get_adx(self, code: str) -> Optional[float]:
        """
        获取 ADX 值

        Args:
            code: 股票代码

        Returns:
            ADX 值，或 None (数据不足)
        """
        buf = self._kline_buffers[code]

        if buf.size < self.ADX_PERIOD * 2 + 5:
            return None

        # 检查缓存
        if code in self._adx_cache:
            return self._adx_cache[code]

        # 计算 ADX
        adx_val = calc_adx(
            list(buf.high), list(buf.low), list(buf.close), self.ADX_PERIOD
        )

        if adx_val is not None:
            self._adx_cache[code] = adx_val

        return adx_val

    def update_adx(self, code: str, kline: dict) -> Optional[float]:
        """
        更新 ADX 值（推送 K 线并计算）

        Args:
            code: 股票代码
            kline: K 线数据

        Returns:
            ADX 值，或 None (数据不足)
        """
        self.push_kline(code, kline)
        return self.get_adx(code)

    def get_trend_strength(self, code: str) -> str:
        """
        获取趋势强度

        Args:
            code: 股票代码

        Returns:
            "strong" | "weak" | "none"
        """
        adx = self.get_adx(code)

        if adx is None:
            return "none"

        if adx >= self.ADX_STRONG_TREND:
            return "strong"
        elif adx >= self.ADX_WEAK_TREND:
            return "weak"
        else:
            return "none"

    def is_high_volatility(self, code: str) -> bool:
        """
        检查是否处于高波动状态

        Args:
            code: 股票代码

        Returns:
            True: 高波动
            False: 正常波动
        """
        adx = self.get_adx(code)
        return adx is not None and adx >= self.ADX_HIGH_VOLATILITY

    def clear_cache(self, code: str) -> None:
        """
        清除 ADX 缓存

        Args:
            code: 股票代码
        """
        self._adx_cache.pop(code, None)
        logger.debug(f"[{code}] ADX 缓存已清理")


class MultiTimeframeTrendManager:
    """多周期趋势管理器"""

    # 支持的时间周期
    TIMEFRAMES = [
        "K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"
    ]

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

    # 多周期权重 (用于对齐分数计算)
    ALIGNMENT_WEIGHTS = {
        "K_DAY": 0.30,
        "K_60M": 0.20,
        "K_30M": 0.15,
        "K_15M": 0.12,
        "K_5M":  0.10,
        "K_3M":  0.08,
        "K_1M":  0.05,
    }

    # 多周期权重 (用于动态止损参数计算)
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
        """初始化多周期趋势管理器"""
        # 多周期 K 线缓存
        self._kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] = defaultdict(
            lambda: {
                period: IndicatorBuffer(maxlen=100)
                for period in self.TIMEFRAMES
            }
        )

        # 多周期趋势缓存
        self._trends: Dict[str, Dict[str, str]] = defaultdict(
            lambda: {period: "neutral" for period in self.TIMEFRAMES}
        )

        logger.info("多周期趋势管理器已初始化")

    def push_kline(self, code: str, kline: dict, k_type: str) -> None:
        """
        推送 K 线数据到对应周期的缓冲区

        Args:
            code: 股票代码
            kline: K 线数据
            k_type: K 线周期
        """
        if k_type not in self.TIMEFRAMES:
            return

        self._kline_buffers_by_period[code][k_type].push_kline(kline)

    def get_trend(self, code: str, k_type: str) -> str:
        """
        获取指定周期的趋势

        Args:
            code: 股票代码
            k_type: K 线周期

        Returns:
            "up" | "down" | "neutral"
        """
        return self._trends[code].get(k_type, "neutral")

    def set_trend(self, code: str, k_type: str, trend: str) -> None:
        """
        设置指定周期的趋势

        Args:
            code: 股票代码
            k_type: K 线周期
            trend: 趋势 ("up" | "down" | "neutral")
        """
        old_trend = self._trends[code][k_type]
        self._trends[code][k_type] = trend

        if trend != old_trend:
            logger.info(f"[多周期趋势] {code} {k_type}: {old_trend} → {trend}")

    def get_all_trends(self, code: str) -> Dict[str, str]:
        """获取所有周期的趋势"""
        return dict(self._trends[code])

    def get_alignment_score(self, code: str, action: str) -> Tuple[float, str]:
        """
        计算多周期对齐分数

        Args:
            code: 股票代码
            action: 交易方向 ("BUY" | "SELL")

        Returns:
            (score, detail)
            score: 0.0 ~ 1.0，越高表示多周期越一致
            detail: 详细描述字符串
        """
        trends = self._trends[code]
        target_trend = "up" if action == "BUY" else "down"

        aligned_weight = 0.0
        conflict_weight = 0.0
        detail_parts = []

        for period, weight in self.ALIGNMENT_WEIGHTS.items():
            t = trends.get(period, "neutral")
            if t == target_trend:
                aligned_weight += weight
                detail_parts.append(f"{period}↑")
            elif t == "down" if action == "BUY" else "up":
                conflict_weight += weight
                detail_parts.append(f"{period}↓")
            else:
                detail_parts.append(f"{period}~")

        # 最终对齐分：aligned - conflict×0.5
        raw_score = aligned_weight - conflict_weight * 0.5
        score = max(0.0, min(1.0, raw_score))
        detail = " | ".join(detail_parts)

        return score, detail

    def get_resonance_score(self, code: str, action: str) -> float:
        """
        计算多周期共振度

        共振度 = 同向周期权重和 / 总权重

        Args:
            code: 股票代码
            action: 交易方向 ("BUY" | "SELL")

        Returns:
            共振度 (0.0 ~ 1.0)
        """
        trends = self._trends[code]
        target_trend = "up" if action == "BUY" else "down"

        # 计算同向周期权重和
        aligned_weight = sum(
            w for k, w in self.TREND_WEIGHTS.items()
            if trends.get(k, "neutral") == target_trend
        )

        # 总权重（排除 neutral 周期）
        total_weight = sum(
            w for k, w in self.TREND_WEIGHTS.items()
            if trends.get(k, "neutral") != "neutral"
        )

        if total_weight == 0:
            return 0.5  # 全部 neutral，返回中性值

        return aligned_weight / total_weight

    def get_combined_trend_score(self, code: str) -> float:
        """
        计算综合趋势分数

        使用七周期加权计算：
        score = Σ(weight × trend_score)
        trend_score: up=1, neutral=0, down=-1

        Args:
            code: 股票代码

        Returns:
            综合分数 (-1.0 ~ 1.0)
        """
        trends = self._trends[code]
        score_map = {"up": 1, "down": -1, "neutral": 0}

        combined = sum(
            score_map.get(trends.get(k, "neutral"), 0) * w
            for k, w in self.TREND_WEIGHTS.items()
        )

        return combined

    def is_contra_trend(
        self,
        code: str,
        action: str,
        check_periods: Tuple[str, ...] = ("K_DAY", "K_60M")
    ) -> bool:
        """
        检查是否处于逆势状态

        Args:
            code: 股票代码
            action: 交易方向 ("BUY" | "SELL")
            check_periods: 需要检查的周期列表

        Returns:
            True: 所有指定周期都逆势
            False: 至少有一个周期不逆势
        """
        trends = self._trends[code]
        opposite_trend = "down" if action == "BUY" else "up"

        for period in check_periods:
            if trends.get(period, "neutral") != opposite_trend:
                return False

        return True


# 全局单例
_adx_manager: Optional[ADXManager] = None
_mtf_manager: Optional[MultiTimeframeTrendManager] = None


def get_adx_manager() -> ADXManager:
    """获取 ADX 管理器单例"""
    global _adx_manager
    if _adx_manager is None:
        _adx_manager = ADXManager()
    return _adx_manager


def get_mtf_manager() -> MultiTimeframeTrendManager:
    """获取多周期趋势管理器单例"""
    global _mtf_manager
    if _mtf_manager is None:
        _mtf_manager = MultiTimeframeTrendManager()
    return _mtf_manager
