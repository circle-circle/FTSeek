#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
价格动量门控 (Price Momentum Gate) - FTSeek-US P0

职责:
在 LLM 发出 BUY/SELL 信号后、实际下单前，检查价格动量是否与信号方向一致。
防止 LLM 说 BUY 但价格正在暴跌时仍然下单。

规则:
1. 1 分钟 K 线方向与信号方向一致
2. 成交量不低于 20 日均量的 50%
3. 价格在 5 分钟内波动不超过 3%
4. 如果门控失败，记录原因并拒绝下单
"""

import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class PriceMomentumGate:
    """
    价格动量门控 -- 防止逆势下单。
    
    用法:
        gate = PriceMomentumGate(data_source)
        passed, reason = gate.check("US.AAPL", "LONG")
        if not passed:
            logger.warning(f"门控拒绝: {reason}")
            return  # 不下单
    """
    
    # 门控参数
    MOMENTUM_LOOKBACK_MINUTES = 1       # 动量检查窗口 (分钟)
    VOLUME_THRESHOLD_RATIO = 0.5        # 成交量阈值 (相对 20 日均量)
    MAX_PRICE_CHANGE_PCT = 0.03         # 最大价格波动 (5 分钟内 3%)
    PRICE_CHANGE_WINDOW_MINUTES = 5     # 价格波动检查窗口
    
    def __init__(self, data_source=None):
        """
        Args:
            data_source: 行情数据源 (需提供 get_klines, get_avg_volume 方法)
        """
        self.data_source = data_source
        self._reject_count = {}  # 记录每个标的被拒绝次数
    
    def check(self, symbol: str, direction: str) -> Tuple[bool, str]:
        """
        检查价格动量是否与信号方向一致。
        
        Args:
            symbol: 股票代码 (如 "US.AAPL")
            direction: 信号方向 ("LONG" 或 "SHORT")
            
        Returns:
            (passed, reason): 是否通过门控，及原因
        """
        if self.data_source is None:
            # 无数据源时放行 (兼容模式)
            logger.debug(f"[动量门控] {symbol} 无数据源，放行 (兼容模式)")
            return True, "no_data_source"
        
        try:
            # 检查 1: 1 分钟 K 线方向
            kline_passed, kline_reason = self._check_kline_momentum(symbol, direction)
            if not kline_passed:
                return False, kline_reason
            
            # 检查 2: 成交量
            volume_passed, volume_reason = self._check_volume(symbol)
            if not volume_passed:
                return False, volume_reason
            
            # 检查 3: 价格波动
            volatility_passed, vol_reason = self._check_price_volatility(symbol)
            if not volatility_passed:
                return False, vol_reason
            
            # 全部通过
            self._reject_count.pop(symbol, None)  # 清除拒绝计数
            return True, "all_checks_passed"
            
        except Exception as e:
            logger.warning(f"[动量门控] {symbol} 检查异常: {e}，放行 (保守)")
            return True, f"check_error_pass_safe: {e}"
    
    def _check_kline_momentum(self, symbol: str, direction: str) -> Tuple[bool, str]:
        """
        检查 1 分钟 K 线方向是否与信号一致。
        """
        try:
            klines = self.data_source.get_klines(
                symbol,
                interval="1m",
                count=3  # 最近 3 根 1 分钟 K 线
            )
            
            if not klines or len(klines) < 2:
                logger.debug(f"[动量门控] {symbol} K 线数据不足，放行")
                return True, "insufficient_klines"
            
            # 比较最近 2 根 K 线的收盘价
            latest_close = klines[-1].get('close', 0)
            prev_close = klines[-2].get('close', 0)
            
            if prev_close <= 0:
                return True, "invalid_prev_close"
            
            change_pct = (latest_close - prev_close) / prev_close
            
            # LONG 信号: 价格应上涨或持平
            if direction == "LONG" and change_pct < -0.005:  # 下跌超过 0.5%
                self._record_reject(symbol, "kline_momentum_down")
                return False, (
                    f"价格动量向下 (1m K 线变化 {change_pct:.2%})，"
                    f"与 LONG 信号冲突"
                )
            
            # SHORT 信号: 价格应下跌或持平
            if direction == "SHORT" and change_pct > 0.005:  # 上涨超过 0.5%
                self._record_reject(symbol, "kline_momentum_up")
                return False, (
                    f"价格动量向上 (1m K 线变化 {change_pct:.2%})，"
                    f"与 SHORT 信号冲突"
                )
            
            return True, f"kline_momentum_ok (变化 {change_pct:.2%})"
            
        except Exception as e:
            logger.debug(f"[动量门控] {symbol} K 线检查异常: {e}")
            return True, f"kline_check_error: {e}"
    
    def _check_volume(self, symbol: str) -> Tuple[bool, str]:
        """
        检查成交量是否足够。
        """
        try:
            avg_volume = self.data_source.get_avg_volume(symbol, period=20)
            current_volume = self.data_source.get_current_volume(symbol)
            
            if avg_volume <= 0 or current_volume <= 0:
                return True, "volume_data_unavailable"
            
            volume_ratio = current_volume / avg_volume
            
            if volume_ratio < self.VOLUME_THRESHOLD_RATIO:
                return False, (
                    f"成交量不足 (当前 {current_volume:.0f} vs 20 日均 {avg_volume:.0f}, "
                    f"比率 {volume_ratio:.2f} < {self.VOLUME_THRESHOLD_RATIO})"
                )
            
            return True, f"volume_ok (比率 {volume_ratio:.2f})"
            
        except Exception as e:
            logger.debug(f"[动量门控] {symbol} 成交量检查异常: {e}")
            return True, f"volume_check_error: {e}"
    
    def _check_price_volatility(self, symbol: str) -> Tuple[bool, str]:
        """
        检查 5 分钟内价格波动是否超过阈值。
        """
        try:
            klines = self.data_source.get_klines(
                symbol,
                interval="1m",
                count=self.PRICE_CHANGE_WINDOW_MINUTES
            )
            
            if not klines or len(klines) < 2:
                return True, "insufficient_klines_for_volatility"
            
            oldest_close = klines[0].get('close', 0)
            latest_close = klines[-1].get('close', 0)
            
            if oldest_close <= 0:
                return True, "invalid_oldest_close"
            
            change_pct = abs(latest_close - oldest_close) / oldest_close
            
            if change_pct > self.MAX_PRICE_CHANGE_PCT:
                return False, (
                    f"价格波动过大 ({change_pct:.2%} > {self.MAX_PRICE_CHANGE_PCT:.2%} "
                    f"在 {self.PRICE_CHANGE_WINDOW_MINUTES} 分钟内)"
                )
            
            return True, f"volatility_ok (波动 {change_pct:.2%})"
            
        except Exception as e:
            logger.debug(f"[动量门控] {symbol} 波动检查异常: {e}")
            return True, f"volatility_check_error: {e}"
    
    def _record_reject(self, symbol: str, reason: str):
        """记录门控拒绝"""
        if symbol not in self._reject_count:
            self._reject_count[symbol] = []
        self._reject_count[symbol].append({
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        })
        # 保留最近 10 条
        self._reject_count[symbol] = self._reject_count[symbol][-10:]
        
        logger.warning(f"[动量门控拒绝] {symbol}: {reason}")
    
    def get_reject_history(self, symbol: str) -> list:
        """获取某个标的的拒绝历史"""
        return self._reject_count.get(symbol, [])
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total_rejects = sum(len(v) for v in self._reject_count.values())
        return {
            "total_symbols_tracked": len(self._reject_count),
            "total_rejects": total_rejects,
            "recent_rejects": {
                k: v[-1] for k, v in self._reject_count.items() if v
            },
        }


# 全局单例
_price_momentum_gate: Optional[PriceMomentumGate] = None


def get_price_momentum_gate(data_source=None) -> PriceMomentumGate:
    """获取或创建价格动量门控"""
    global _price_momentum_gate
    if _price_momentum_gate is None:
        _price_momentum_gate = PriceMomentumGate(data_source)
    elif data_source is not None:
        _price_momentum_gate.data_source = data_source
    return _price_momentum_gate


def init_price_momentum_gate(data_source) -> PriceMomentumGate:
    """初始化价格动量门控"""
    global _price_momentum_gate
    _price_momentum_gate = PriceMomentumGate(data_source)
    return _price_momentum_gate
