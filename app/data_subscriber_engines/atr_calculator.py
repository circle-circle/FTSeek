"""
ATRCalculator — ATR 波动率计算引擎

职责:
- 计算 ATR 波动率百分比 (用于港股适配)
- 支持多周期 ATR 计算 (1M/3M/5M/15M/30M/60M/日线)
- 提供动态止损所需的 ATR 数据

从 data_subscriber.py 提取，降低耦合，提升可测试性

依赖注入:
- kline_buffers: K 线数据缓冲区 (1 分钟)
- kline_buffers_by_period: 多周期 K 线缓冲区
"""
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class ATRCalculator:
    """ATR 波动率计算引擎 — Wilder 风格 True Range 均值"""

    def __init__(self, kline_buffers: Dict, kline_buffers_by_period: Dict):
        """
        初始化 ATR 计算引擎
        
        Args:
            kline_buffers: 1 分钟 K 线缓冲区 {code: KlineBuffer}
            kline_buffers_by_period: 多周期 K 线缓冲区 {code: {k_type: KlineBuffer}}
        """
        self._kline_buffers = kline_buffers
        self._kline_buffers_by_period = kline_buffers_by_period
        logger.info(f"[ATRCalculator] 初始化完成：buffers={len(kline_buffers)}, multi_period={len(kline_buffers_by_period)}")

    def get_atr_percent(self, code: str) -> float:
        """
        计算 ATR 波动率百分比 (用于港股适配)
        
        使用 Wilder 风格的 True Range 均值计算，避免用整体价格区间导致 ATR 估算过小。
        True Range = max(High, PrevClose) - min(Low, PrevClose)
        ATR = 14 根 K 线的 TR 均值 / 当前价
        
        Returns:
            float: ATR 百分比 (0.003 ~ 0.08)，默认 2%
        """
        buf = self._kline_buffers.get(code)
        if buf is None or buf.size < 15:
            return 0.02  # 默认 2%
        
        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        
        # 取最近 14 根 K 线计算 True Range
        tr_list = []
        n = min(14, len(highs))
        for i in range(-n, 0):
            prev_close = closes[i - 1] if abs(i - 1) <= len(closes) else closes[0]
            tr = max(highs[i], prev_close) - min(lows[i], prev_close)
            if tr > 0:
                tr_list.append(tr)
        
        if not tr_list:
            return 0.02
        
        current_price = closes[-1]
        atr = sum(tr_list) / len(tr_list)
        atr_pct = atr / current_price if current_price > 0 else 0.02
        # 设置合理上下界（0.3% ~ 8%），避免极端值
        return max(0.003, min(atr_pct, 0.08))

    def get_atr_from_period(self, code: str, k_type: str, period: int = 14) -> float:
        """
        从指定周期 K 线缓冲区计算 ATR 百分比 (2026-03-25 新增)

        通过多周期 K 线（15M/30M/60M/日线）获取更稳定的 ATR，用于止损宽度自适应。

        Args:
            code:   股票代码
            k_type: K 线周期 "K_15M" / "K_30M" / "K_60M" / "K_DAY"
            period: ATR 计算周期，默认 14

        Returns:
            float: ATR 百分比 (0.003 ~ 0.15)
        """
        try:
            code_buffers = self._kline_buffers_by_period.get(code, {})
            buf = code_buffers.get(k_type)
            if buf is None or buf.size < period + 1:
                return self.get_atr_percent(code)  # fallback 到 1M

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
                return self.get_atr_percent(code)

            current_price = closes[-1]
            if current_price <= 0:
                return 0.02
            atr_val = sum(tr_list) / len(tr_list)
            atr_pct = atr_val / current_price
            
            # 各周期 ATR 上下界有所不同（2026-03-25 补全 1M/3M/5M）
            bounds = {
                "K_1M":  (0.001, 0.04),   # 1 分钟：波动极小
                "K_3M":  (0.002, 0.05),   # 3 分钟
                "K_5M":  (0.002, 0.05),   # 5 分钟
                "K_15M": (0.003, 0.06),
                "K_30M": (0.004, 0.08),
                "K_60M": (0.005, 0.10),
                "K_DAY": (0.008, 0.15),
            }
            lo, hi = bounds.get(k_type, (0.003, 0.10))
            return max(lo, min(atr_pct, hi))
        except Exception as e:
            logger.debug(f"[多周期 ATR] {code} {k_type} 计算失败：{e}")
            return self.get_atr_percent(code)

    def get_preferred_atr_percent(self, code: str) -> float:
        """
        根据动态止损配置选择最优 ATR 周期计算 ATR 百分比 (2026-03-25 新增)

        优先级：dynamic_exit_manager 中配置的 preferred_kline → 60M → 30M → 1M fallback
        """
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            preferred = _dem.current_params.get('atr_stop', {}).get('preferred_kline', 'K_60M')
            return self.get_atr_from_period(code, preferred)
        except Exception:
            return self.get_atr_from_period(code, 'K_60M')

    def get_atr(self, code: str) -> float:
        """
        获取 ATR 绝对值（用于止损宽度计算）
        
        Returns:
            float: ATR 绝对值 (价格单位)
        """
        try:
            buf = self._kline_buffers.get(code)
            if buf is None or buf.size < 15:
                return 0.0
            
            closes = list(buf.close)
            highs = list(buf.high)
            lows = list(buf.low)
            
            tr_list = []
            n = min(14, len(highs))
            for i in range(-n, 0):
                prev_close = closes[i - 1] if abs(i - 1) <= len(closes) else closes[0]
                tr = max(highs[i], prev_close) - min(lows[i], prev_close)
                if tr > 0:
                    tr_list.append(tr)
            
            if not tr_list:
                return 0.0
            
            return sum(tr_list) / len(tr_list)
        except Exception:
            return 0.0
