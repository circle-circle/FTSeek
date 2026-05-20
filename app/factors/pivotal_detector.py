"""
Livermore 关键点检测器 (PivotalDetector) - 状态机增强版

核心理念:
"在价格突破关键点 (Pivotal Point) 之前，不要入场。"
"突破必须伴随着成交量的放大，否则可能是假突破。"
"45度角以上的急涨往往是透支，应观察而非入场。"

功能:
1. 状态机: WATCH (预突破) -> TRIGGER (确认突破) -> FAKEOUT (假突破/失效)
2. 突破速度 (Velocity): 计算突破斜率，拒绝过度透支的突破。
3. 量能确认: 结合 Volume Ratio 确认突破有效性。

配置来源: unified_config.yaml -> strategy_enhancements.pivotal_points
"""

import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str = "PivotalDetector"
    passed: bool = True
    score: float = 0.0
    weight: float = 0.30
    reason: str = ""
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class PivotalDetector:
    """Livermore 关键点检测器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        cfg = loader.get('strategy_enhancements.pivotal_points', {})
        self.enabled = cfg.get('enabled', True)
        self.lookback = cfg.get('lookback_period', 20)
        self.vol_trigger = cfg.get('volume_ratio_trigger', 1.5)
        self.buffer = cfg.get('breakout_buffer', 0.005)
        self.atr_period = cfg.get('atr_period', 14)
        self.max_velocity = cfg.get('velocity_slope_limit', 2.0)
        
        # 缓存失效状态，防止在假突破后立刻再次触发
        self._invalidated_pivots = {} 

        logger.info(
            f"[关键点检测] 已初始化 | "
            f"观察期={self.lookback} | "
            f"量能要求={self.vol_trigger}x | "
            f"缓冲={self.buffer*100:.1f}% | "
            f"最大斜率={self.max_velocity}x ATR"
        )

    def check(self, symbol: str, history: Dict) -> GateResult:
        """
        检查是否突破关键点 (State Machine + Velocity)

        Args:
            symbol: 股票代码
            history: 历史数据字典 (需包含 'high', 'low', 'close', 'volume')
                格式: {'high': [...], 'low': [...], 'close': [...], 'volume': [...]}
                注意: TwelveData 返回的是倒序 (最新在前)

        Returns:
            GateResult 检测结果
        """
        if not self.enabled:
            return GateResult(name="PivotalDetector", passed=True, score=0.5, reason="Disabled")

        try:
            closes = history.get('close', [])
            highs = history.get('high', [])
            lows = history.get('low', [])
            volumes = history.get('volume', [])

            if len(closes) < self.lookback + 5: # 需要足够数据
                return GateResult(name="PivotalDetector", passed=False, score=0.0, reason="Insufficient Data")

            # 1. 计算关键点位 (Lookback 周期内的最高价和最低价)
            # 注意: TwelveData 是倒序，所以 index 0 是最新。
            # 我们需要取过去 lookback 天的数据，即 index 1 到 lookback+1 (排除最新这根 K 线作为参考基准)
            # 或者包含最新这根？通常突破是指收盘价突破前 lookback 的高点。
            # 这里的逻辑：Current Close > Max(High of past 20 days excluding today's high if intraday)
            # 为简化，我们取 closes[1:21] 和 highs[1:21] 作为基准。
            
            if len(highs) < self.lookback + 1:
                return GateResult(name="PivotalDetector", passed=False, score=0.0, reason="Data Length Error")

            ref_highs = highs[1:self.lookback + 1]
            ref_closes = closes[1:self.lookback + 1]
            
            pivot_resistance = max(ref_highs)
            
            current_price = closes[0]
            prev_close = closes[1]
            
            # 2. 检查是否已经失效 (近期刚突破但跌回)
            if self._is_invalidated(symbol, current_price, pivot_resistance):
                return GateResult(name="PivotalDetector", passed=False, score=0.0, reason="Recent Fakeout Detected")

            # 3. 判断突破状态
            is_breakout = current_price > (pivot_resistance * (1 + self.buffer))
            
            # 4. 计算 ATR 和 速度
            atr = self._calculate_atr(highs, lows, closes)
            velocity = 0.0
            if atr > 0:
                # 速度 = 过去 3 天涨幅 / (3 * ATR)
                # TwelveData closes 是倒序: closes[0]=Today, closes[1]=Yesterday...
                price_3d_ago = closes[3] if len(closes) > 3 else closes[-1]
                move = abs(current_price - price_3d_ago)
                velocity = move / (3 * atr)

            # 5. 量能确认
            avg_vol = sum(volumes[1:21]) / 20 if len(volumes) > 20 else 1
            curr_vol = volumes[0]
            vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

            result = GateResult(name="PivotalDetector", passed=is_breakout, metadata={"resistance": pivot_resistance, "velocity": velocity, "vol_ratio": vol_ratio})

            if is_breakout:
                # 检查速度是否过快 (45度急涨)
                if velocity > self.max_velocity:
                    result.passed = False
                    result.reason = f"突破过快 (Velocity {velocity:.1f}x ATR)，存在透支风险，建议观望"
                    result.score = 0.4
                    logger.info(f"[关键点检测] {symbol} 突破过快，跳过")
                elif vol_ratio >= self.vol_trigger:
                    result.score = 1.0
                    result.reason = f"有效突破! Price={current_price:.2f} > Pivot={pivot_resistance:.2f}, Vol={vol_ratio:.1f}x"
                    logger.info(f"[关键点检测] {symbol} 确认有效突破! {result.reason}")
                    # 记录突破，开始监控是否失效
                    self._mark_breakout(symbol, pivot_resistance)
                else:
                    # 突破了但没量 -> 可能是假突破，降权
                    result.score = 0.6
                    result.passed = True # 允许通过，但分数低
                    result.reason = f"无量突破 (Vol={vol_ratio:.1f}x)，存疑，降权处理"
                    logger.info(f"[关键点检测] {symbol} 无量突破，降权")
            else:
                # 未突破
                result.score = 0.2
                # 检查是否接近关键点 (WATCH 状态)
                proximity = (current_price - pivot_resistance) / pivot_resistance
                if 0.0 < proximity < 0.01: # 距离 < 1%
                    result.score = 0.4
                    result.reason = f"接近关键点 (距离 {proximity*100:.1f}%)，处于 WATCH 状态"
                else:
                    result.reason = f"未突破关键点 (Price={current_price:.2f} < Pivot={pivot_resistance:.2f})"

            return result

        except Exception as e:
            logger.warning(f"[关键点检测] {symbol} 检测异常: {e}")
            return GateResult(name="PivotalDetector", passed=False, score=0.0, reason=f"Error: {e}")

    def _calculate_atr(self, highs, lows, closes, period=None) -> float:
        """计算 ATR"""
        p = period or self.atr_period
        if len(highs) < p + 1: return 0.0
        trs = []
        for i in range(1, p + 1):
            h = highs[i]
            l = lows[i]
            pc = closes[i] # 注意这里索引逻辑，假设是倒序
            # TwelveData: closes[0] is today. closes[1] is yesterday close.
            # TR = Max(H-L, abs(H-PC), abs(L-PC))
            # For index i (where i>0): H=highs[i], L=lows[i], PC=closes[i] is wrong.
            # PC for candle i is closes[i]. But for TR calculation of candle i, 
            # we need closes[i] as close of current candle, and closes[i-1] as prev close?
            # Wait, standard TR for candle i uses Close of candle i-1.
            # In our list: closes[0]=Close(Today), closes[1]=Close(Yesterday).
            # TR for Today (index 0) uses Close(Yesterday) = closes[1].
            # So for index j in loop 0..p-1:
            #   h=highs[j], l=lows[j], pc_prev=closes[j+1]
            # Let's correct this loop for TwelveData format (Reverse chronological)
            pass 
        
        # Correct ATR calculation for Reverse List
        trs = []
        for j in range(1, p + 1):
            h = highs[j]
            l = lows[j]
            pc_prev = closes[j] # Wait, closes[1] is prev close of closes[0]. 
            # Let's be precise.
            # To calc ATR of the *current* completed candles to get a baseline.
            # We want ATR of the past 14 days.
            # The candles are at index 1, 2, 3...
            # For candle at index k (Yesterday):
            #   H = highs[k], L = lows[k], PC = closes[k+1] (Close of day before yesterday)
            #   TR = max(H-L, abs(H-PC), abs(L-PC))
            # Wait, TR definition: Max(H-L, |H-Cp|, |L-Cp|). Cp is Previous Close.
            # For candle k: Close is closes[k]. Previous Close is closes[k+1].
            # So for candle k: TR = max(highs[k]-lows[k], abs(highs[k]-closes[k+1]), abs(lows[k]-closes[k+1]))
            
            if len(closes) > j:
                cp = closes[j+1] if j+1 < len(closes) else closes[j]
                tr = max(h - l, abs(h - cp), abs(l - cp))
                trs.append(tr)
            
        return sum(trs) / len(trs) if trs else 0.0

    def _is_invalidated(self, symbol, current_price, pivot):
        """检查近期是否有假突破记录"""
        if symbol in self._invalidated_pivots:
            rec = self._invalidated_pivots[symbol]
            # 如果当前价格仍低于 Pivot，且时间在 3 天内，视为无效
            if current_price < pivot: 
                return True
            else:
                del self._invalidated_pivots[symbol] # 价格重新站上，清除失效标记
        return False

    def _mark_breakout(self, symbol, pivot):
        """标记突破，用于后续失效检测"""
        self._invalidated_pivots[symbol] = {"pivot": pivot, "time": "now"}
