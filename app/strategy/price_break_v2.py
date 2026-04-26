"""
价格突破策略 v2 - 动态阈值 (2026-04-11 优化)

问题:
- 原版本使用固定价格阈值，股价是动态的，阈值很快失效
- 需要人工频繁更新配置

优化方案:
1. 基于近期高/低点动态计算阈值 (ATR倍数)
2. 基于百分比偏移 (当前价 ± X%)
3. 基于技术指标 (布林带上下轨、Donchian通道)
4. 支持固定阈值 (兼容旧配置，但不推荐)

使用建议:
- 生产环境使用 dynamic 模式
- 固定阈值仅用于测试或特殊场景
"""

import logging
from typing import Optional, Dict, Any
from collections import defaultdict, deque
from .base import BaseStrategy, Signal, SignalAction
from app import config

logger = logging.getLogger("ftseek.strategy.price_break_v2")
logger.propagate = False


class PriceBreakStrategyV2(BaseStrategy):
    """价格突破策略 v2 - 动态阈值"""
    
    name = "price_break_v2"
    
    def __init__(self, cfg: dict = None):
        super().__init__(cfg or config.STRATEGIES.get("price_break_v2", {}))
        
        # 阈值模式: dynamic | percentage | bollinger | donchian | fixed
        self.mode = self.config.get("mode", "dynamic")
        
        # 动态阈值参数
        self.lookback_period = self.config.get("lookback_period", 20)  # 回看周期
        self.atr_multiplier = self.config.get("atr_multiplier", 2.0)   # ATR倍数
        
        # 百分比模式参数
        self.percentage_offset = self.config.get("percentage_offset", 0.05)  # ±5%
        
        # 价格缓冲区 (用于计算动态阈值)
        self._price_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.lookback_period)
        )
        self._atr_cache: Dict[str, float] = {}
        
        # 触发状态
        self._triggered: Dict[str, Dict[str, bool]] = defaultdict(
            lambda: {"upper": False, "lower": False}
        )
        
        # 固定阈值 (兼容旧配置)
        self.fixed_thresholds = self.config.get("thresholds", {})
        
        logger.info(
            f"价格突破策略V2已初始化 | "
            f"mode={self.mode} | "
            f"lookback={self.lookback_period} | "
            f"atr_multiplier={self.atr_multiplier}"
        )
    
    def _get_qty(self, code: str) -> int:
        """根据市场返回下单数量"""
        if code.startswith("US."):
            return config.DEFAULT_ORDER_QTY_US
        return config.DEFAULT_ORDER_QTY_HK
    
    def _get_currency(self, code: str) -> str:
        return "$" if code.startswith("US.") else "HK$"
    
    def _calculate_dynamic_thresholds(
        self, 
        code: str, 
        current_price: float
    ) -> Optional[Dict[str, float]]:
        """
        计算动态阈值
        
        Returns:
            {"upper": float, "lower": float} 或 None
        """
        if self.mode == "fixed":
            # 固定阈值模式 (兼容旧版)
            threshold = self.fixed_thresholds.get(code)
            if threshold:
                return {
                    "upper": threshold.get("upper"),
                    "lower": threshold.get("lower")
                }
            return None
        
        elif self.mode == "percentage":
            # 百分比偏移模式
            offset = current_price * self.percentage_offset
            return {
                "upper": current_price + offset,
                "lower": current_price - offset
            }
        
        elif self.mode in ("dynamic", "atr"):
            # ATR动态阈值模式
            history = self._price_history.get(code)
            if not history or len(history) < self.lookback_period:
                return None  # 数据不足
            
            # 计算近期高低价
            recent_high = max(history)
            recent_low = min(history)
            
            # 计算ATR (简化版)
            atr = self._calculate_atr(code, history)
            if atr is None:
                return None
            
            # 阈值 = 近期极值 ± ATR倍数
            return {
                "upper": recent_high + atr * self.atr_multiplier,
                "lower": recent_low - atr * self.atr_multiplier
            }
        
        elif self.mode == "donchian":
            # Donchian通道模式 (突破N日高/低点)
            history = self._price_history.get(code)
            if not history or len(history) < self.lookback_period:
                return None
            
            return {
                "upper": max(history),
                "lower": min(history)
            }
        
        else:
            logger.warning(f"未知的阈值模式: {self.mode}")
            return None
    
    def _calculate_atr(self, code: str, history: deque) -> Optional[float]:
        """计算ATR (Average True Range)"""
        if len(history) < 2:
            return None
        
        # 简化版ATR: 只使用收盘价差
        # 完整版需要 high, low, close
        true_ranges = []
        history_list = list(history)
        
        for i in range(1, len(history_list)):
            tr = abs(history_list[i] - history_list[i-1])
            true_ranges.append(tr)
        
        if not true_ranges:
            return None
        
        # ATR = 平均真实波幅
        atr = sum(true_ranges[-self.lookback_period:]) / min(
            len(true_ranges), 
            self.lookback_period
        )
        
        self._atr_cache[code] = atr
        return atr
    
    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        """
        处理行情数据
        
        Args:
            code: 股票代码
            quote: 行情数据 {"last_price": float, "high": float, "low": float, ...}
        
        Returns:
            Signal 或 None
        """
        if not self.enabled:
            return None
        
        last_price = quote.get("last_price", 0)
        if last_price <= 0:
            return None
        
        # 更新价格历史
        self._price_history[code].append(last_price)
        
        # 计算动态阈值
        thresholds = self._calculate_dynamic_thresholds(code, last_price)
        if thresholds is None:
            return None  # 数据不足，不产生信号
        
        upper = thresholds.get("upper")
        lower = thresholds.get("lower")
        
        if upper is None or lower is None:
            return None
        
        currency = self._get_currency(code)
        qty = self._get_qty(code)
        triggered = self._triggered[code]
        
        # 突破上限 -> BUY信号 (突破阻力位，可能继续上涨)
        if last_price >= upper and not triggered["upper"]:
            triggered["upper"] = True
            triggered["lower"] = False
            
            logger.info(
                f"[{code}] 突破动态上限 {upper:.2f}, "
                f"当前价 {last_price:.2f}, 模式={self.mode}"
            )
            
            return Signal(
                code=code,
                name=quote.get("name", code),
                action=SignalAction.BUY,
                price=last_price,
                quantity=qty,
                reason=(
                    f"价格 {currency}{last_price:.2f} 突破动态上限 "
                    f"{currency}{upper:.2f} (模式={self.mode})"
                ),
                strategy=self.name,
                expire_at=0,
                metadata={
                    "threshold_mode": self.mode,
                    "upper": upper,
                    "lower": lower,
                    "atr": self._atr_cache.get(code, 0)
                }
            )
        
        # 跌破下限 -> SELL信号 (跌破支撑位，可能继续下跌)
        if last_price <= lower and not triggered["lower"]:
            triggered["lower"] = True
            triggered["upper"] = False
            
            logger.info(
                f"[{code}] 跌破动态下限 {lower:.2f}, "
                f"当前价 {last_price:.2f}, 模式={self.mode}"
            )
            
            return Signal(
                code=code,
                name=quote.get("name", code),
                action=SignalAction.SELL,
                price=last_price,
                quantity=qty,
                reason=(
                    f"价格 {currency}{last_price:.2f} 跌破动态下限 "
                    f"{currency}{lower:.2f} (模式={self.mode})"
                ),
                strategy=self.name,
                expire_at=0,
                metadata={
                    "threshold_mode": self.mode,
                    "upper": upper,
                    "lower": lower,
                    "atr": self._atr_cache.get(code, 0)
                }
            )
        
        # 回到区间内，重置触发状态
        if lower < last_price < upper:
            triggered["upper"] = False
            triggered["lower"] = False
        
        return None
    
    def get_threshold_info(self, code: str) -> Dict[str, Any]:
        """获取当前阈值信息 (用于调试和监控)"""
        history = self._price_history.get(code, deque())
        current_price = history[-1] if history else 0
        
        thresholds = self._calculate_dynamic_thresholds(code, current_price)
        
        return {
            "code": code,
            "mode": self.mode,
            "current_price": current_price,
            "history_length": len(history),
            "thresholds": thresholds,
            "atr": self._atr_cache.get(code, 0),
            "triggered": self._triggered.get(code, {"upper": False, "lower": False})
        }
