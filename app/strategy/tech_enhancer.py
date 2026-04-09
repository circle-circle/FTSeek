#!/usr/bin/env python3
"""
技术指标增强层

优化技术指标和行情判断，实现"积小胜为大胜"的交易策略。

核心功能:
1. 多周期共振检测
2. 积小胜 accumulator (快速止盈 + 移动止损)
3. 高胜率信号加权
4. 动态仓位管理
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.indicators import IndicatorBuffer, rsi, macd, sma, bollinger_bands, supertrend

logger = logging.getLogger("ftseek.strategy.tech_enhancer")
logger.propagate = False


@dataclass
class MultiTimeframeSignal:
    """多周期信号"""
    code: str
    timeframe_5m: str  # up/down/neutral
    timeframe_15m: str
    timeframe_1h: str
    timeframe_1d: str
    alignment_score: float  # 0-1 (共振程度)
    direction: str  # long/short


class TechnicalEnhancer:
    """
    技术指标增强器

    增强现有策略信号，提高胜率和盈亏比
    """

    def __init__(self):
        # 多周期缓存
        self._kline_cache: Dict[str, Dict[str, List]] = {}

        logger.info("技术指标增强器已初始化")

    def check_multi_timeframe_alignment(self, code: str,
                                        klines_5m: List = None,
                                        klines_15m: List = None,
                                        klines_1h: List = None,
                                        klines_1d: List = None) -> MultiTimeframeSignal:
        """
        检查多周期共振

        Args:
            code: 股票代码
            klines_5m: 5 分钟 K 线
            klines_15m: 15 分钟 K 线
            klines_1h: 1 小时 K 线
            klines_1d: 日线 K 线

        Returns:
            MultiTimeframeSignal: 多周期信号
        """
        # 计算各周期趋势
        trends = {}

        if klines_5m and len(klines_5m) > 20:
            trends['5m'] = self._calc_trend(klines_5m)
        else:
            trends['5m'] = 'neutral'

        if klines_15m and len(klines_15m) > 20:
            trends['15m'] = self._calc_trend(klines_15m)
        else:
            trends['15m'] = 'neutral'

        if klines_1h and len(klines_1h) > 20:
            trends['1h'] = self._calc_trend(klines_1h)
        else:
            trends['1h'] = 'neutral'

        if klines_1d and len(klines_1d) > 20:
            trends['1d'] = self._calc_trend(klines_1d)
        else:
            trends['1d'] = 'neutral'

        # 计算共振得分
        alignment_score, direction = self._calc_alignment_score(trends)

        return MultiTimeframeSignal(
            code=code,
            timeframe_5m=trends['5m'],
            timeframe_15m=trends['15m'],
            timeframe_1h=trends['1h'],
            timeframe_1d=trends['1d'],
            alignment_score=alignment_score,
            direction=direction
        )

    def _calc_trend(self, klines: List) -> str:
        """
        计算单周期趋势

        使用多指标综合判断:
        - 价格在 MA20 上方
        - RSI > 50
        - MACD 金叉
        """
        if len(klines) < 20:
            return 'neutral'

        closes = [k['close'] for k in klines]
        highs = [k['high'] for k in klines]
        lows = [k['low'] for k in klines]

        # 1. MA20 判断
        ma20 = sma(closes, 20)
        current_price = closes[-1]
        price_above_ma = current_price > ma20 if ma20 else True

        # 2. RSI 判断
        rsi_val = rsi(closes, 14)
        rsi_bullish = rsi_val > 50 if rsi_val else True

        # 3. MACD 判断
        macd_result = macd(closes, 12, 26, 9)
        macd_bullish = False
        if macd_result:
            macd_line, signal_line, histogram = macd_result
            macd_bullish = histogram > 0

        # 综合判断
        bullish_count = sum([price_above_ma, rsi_bullish, macd_bullish])
        bearish_count = 3 - bullish_count

        if bullish_count >= 2:
            return 'up'
        elif bearish_count >= 2:
            return 'down'
        else:
            return 'neutral'

    def _calc_alignment_score(self, trends: Dict[str, str]) -> Tuple[float, str]:
        """
        计算共振得分和方向

        Returns:
            (alignment_score, direction)
            - alignment_score: 0-1 (1=完全共振)
            - direction: long/short/neutral
        """
        # 统计各方向数量
        up_count = sum(1 for t in trends.values() if t == 'up')
        down_count = sum(1 for t in trends.values() if t == 'down')
        neutral_count = sum(1 for t in trends.values() if t == 'neutral')

        total = len(trends)

        # 计算共振得分 (同向周期数/总周期数)
        max_aligned = max(up_count, down_count)
        alignment_score = max_aligned / total if total > 0 else 0

        # 确定方向
        if up_count > down_count and up_count >= 2:
            direction = 'long'
        elif down_count > up_count and down_count >= 2:
            direction = 'short'
        else:
            direction = 'neutral'

        return alignment_score, direction

    def enhance_signal(self, signal: Signal,
                       multi_tf_signal: MultiTimeframeSignal) -> Signal:
        """
        增强信号质量

        Args:
            signal: 原始信号
            multi_tf_signal: 多周期信号

        Returns:
            增强后的信号
        """
        # 多周期共振加持
        if multi_tf_signal.alignment_score >= 0.75:
            # 4 周期中 3 周期同向 → 加强信号
            if signal.extra:
                signal.extra['alignment_score'] = multi_tf_signal.alignment_score
                signal.extra['multi_tf_confirmed'] = True

            # 调整仓位
            if multi_tf_signal.direction == 'long' and signal.action == SignalAction.BUY:
                signal.quantity = int(signal.quantity * 1.2)  # 加仓 20%
            elif multi_tf_signal.direction == 'short' and signal.action == SignalAction.SELL:
                signal.quantity = int(signal.quantity * 1.2)

            logger.info(f"{signal.code} 多周期共振确认：{multi_tf_signal.alignment_score:.0%}")

        return signal


class SmallWinAccumulator:
    """
    积小胜为大胜策略

    核心思想:
    - 降低单笔盈利预期 (目标 1-3%)
    - 提高胜率 (>60%)
    - 快速止盈 (到达目标价 50% 平仓)
    - 移动止损 (保护盈利)
    """

    def __init__(self, base_position: int = 100):
        self.base_position = base_position

        # 胜率追踪
        self._win_history: List[bool] = []
        self._recent_trades: List[Dict] = []

        # 配置
        self.target_return = 0.02      # 目标收益 2%
        self.quick_take_profit = 0.50  # 50% 仓位快速止盈
        self.trailing_stop = 0.01      # 1% 移动止损

        logger.info("积小胜策略已初始化")
        logger.info(f"  目标收益：{self.target_return:.1%}")
        logger.info(f"  快速止盈比例：{self.quick_take_profit:.0%}")
        logger.info(f"  移动止损：{self.trailing_stop:.1%}")

    def calculate_position(self, win_rate: float = None) -> int:
        """
        根据胜率计算仓位

        Args:
            win_rate: 当前胜率 (0-1)

        Returns:
            建议仓位数量
        """
        if win_rate is None:
            win_rate = self.get_recent_win_rate()

        # 高胜率 → 正常仓位
        # 中等胜率 → 降仓位
        # 低胜率 → 轻仓或跳过
        if win_rate > 0.65:
            position = self.base_position
            multiplier = 1.0
        elif win_rate > 0.55:
            position = int(self.base_position * 0.7)
            multiplier = 0.7
        elif win_rate > 0.45:
            position = int(self.base_position * 0.5)
            multiplier = 0.5
        else:
            position = int(self.base_position * 0.3)
            multiplier = 0.3

        logger.debug(f"胜率{win_rate:.1%} → 仓位倍率{multiplier:.1f}x")
        return position

    def record_trade(self, pnl_pct: float):
        """
        记录交易结果

        Args:
            pnl_pct: 盈亏百分比
        """
        is_win = pnl_pct > 0
        self._win_history.append(is_win)
        self._recent_trades.append({
            'pnl_pct': pnl_pct,
            'is_win': is_win,
        })

        # 保留最近 20 笔
        if len(self._win_history) > 20:
            self._win_history = self._win_history[-20:]
        if len(self._recent_trades) > 20:
            self._recent_trades = self._recent_trades[-20:]

    def get_recent_win_rate(self, window: int = 10) -> float:
        """
        获取近期胜率

        Args:
            window: 统计窗口 (笔数)

        Returns:
            胜率 (0-1)
        """
        if not self._win_history:
            return 0.5

        recent = self._win_history[-window:]
        wins = sum(1 for w in recent if w)
        return wins / len(recent)

    def should_take_profit(self, current_pnl_pct: float,
                           peak_pnl_pct: float) -> bool:
        """
        判断是否止盈

        Args:
            current_pnl_pct: 当前盈亏%
            peak_pnl_pct: 峰值盈亏%

        Returns:
            是否止盈
        """
        # 达到目标收益
        if current_pnl_pct >= self.target_return:
            return True

        # 从峰值回撤超过移动止损
        # 修复：添加除零保护
        if peak_pnl_pct > 0.001:  # 避免除以 0
            drawdown = (peak_pnl_pct - current_pnl_pct) / peak_pnl_pct
            if drawdown > self.trailing_stop:
                return True

        return False

    def get_exit_strategy(self, entry_price: float,
                         current_price: float) -> Dict:
        """
        获取退出策略

        Args:
            entry_price: 入场价
            current_price: 当前价

        Returns:
            退出策略 Dict
        """
        pnl_pct = (current_price - entry_price) / entry_price

        # 计算目标价
        target_price = entry_price * (1 + self.target_return)

        # 计算移动止损价
        peak_price = entry_price * (1 + pnl_pct) if pnl_pct > 0 else entry_price
        trailing_stop_price = peak_price * (1 - self.trailing_stop)

        return {
            'target_price': target_price,
            'trailing_stop_price': trailing_stop_price,
            'quick_take_pct': self.quick_take_profit,
            'should_exit': self.should_take_profit(pnl_pct, pnl_pct),
        }


class EnhancedConfluenceStrategy(BaseStrategy):
    """
    增强版融合策略

    在 confluence_v2 基础上增加:
    1. 多周期共振检测
    2. 积小胜仓位管理
    3. 动态胜率加权

    核心逻辑 (集成 confluence_v2):
    - RSI 超卖/超买检测
    - MACD 金叉/死叉
    - 布林带突破
    - SuperTrend 趋势
    - MA 交叉
    - 成交量放大
    """

    name = "enhanced_confluence"

    # 策略配置
    RSI_OVERSOLD = 30
    RSI_OVERBOUGHT = 70
    MACD_GOLDEN_THRESHOLD = 0
    BB_BREAKOUT_STD = 2.0
    MA_SHORT = 5
    MA_LONG = 20
    VOLUME_RATIO_THRESHOLD = 1.5

    def __init__(self, config: dict = None):
        super().__init__((config or {}) if config else {})

        self.enhancer = TechnicalEnhancer()
        self.accumulator = SmallWinAccumulator(
            base_position=(config or {}).get('base_position', 100)
        )

        # 胜率缓存
        self._win_rate_cache: Dict[str, float] = {}

        # K 线缓冲区 (用于因子计算)
        self._kline_buffers: Dict[str, IndicatorBuffer] = {}
        self._kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] = {}

        # 指标缓存
        self._indicator_cache: Dict[str, Dict] = {}

        logger.info("增强版融合策略已初始化")

    def _calc_factors(self, code: str, kline: dict, kline_type: str) -> Dict:
        """
        计算 confluence_v2 核心因子

        Returns:
            {
                'rsi': float,
                'macd': (macd_line, signal_line, histogram),
                'bb': (upper, middle, lower),
                'supertrend': (trend, value),
                'ma_short': float,
                'ma_long': float,
                'volume_ratio': float,
                'factor_scores': {}
            }
        """
        # 获取 K 线缓冲区
        buf = self._kline_buffers.get(code)
        if not buf or buf.size < 30:
            return {}

        closes = list(buf.close)
        highs = list(buf.high)
        lows = list(buf.low)
        volumes = list(buf.volume)

        # 1. RSI
        rsi = rsi(closes, 14)

        # 2. MACD
        macd_result = macd(closes, 12, 26, 9)

        # 3. 布林带
        bb_upper, bb_middle, bb_lower = bollinger_bands(closes, 20, 2.0)

        # 4. SuperTrend
        st_result = supertrend(highs, lows, closes, 10, 3.0)
        st_trend = st_result[0][-1] if st_result else 1  # 1=up, -1=down
        st_value = st_result[1][-1] if st_result else closes[-1]

        # 5. MA 交叉
        ma_short = sma(closes, self.MA_SHORT)
        ma_long = sma(closes, self.MA_LONG)

        # 6. 成交量比率
        avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else volumes[-1]
        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0

        return {
            'rsi': rsi,
            'macd': macd_result,
            'bb': (bb_upper, bb_middle, bb_lower),
            'supertrend': (st_trend, st_value),
            'ma_short': ma_short,
            'ma_long': ma_long,
            'volume_ratio': volume_ratio,
        }

    def _evaluate_factors(self, factors: Dict) -> Dict:
        """
        评估因子得分

        Returns:
            {
                'bullish_signals': int,
                'bearish_signals': int,
                'factor_scores': {'rsi': float, 'macd': float, ...}
            }
        """
        bullish = 0
        bearish = 0
        scores = {}

        # RSI 评分
        rsi = factors.get('rsi', 50)
        if rsi:
            if rsi < self.RSI_OVERSOLD:
                bullish += 2
                scores['rsi'] = 0.8
            elif rsi > self.RSI_OVERBOUGHT:
                bearish += 2
                scores['rsi'] = -0.8
            elif rsi < 40:
                bullish += 1
                scores['rsi'] = 0.4
            elif rsi > 60:
                bearish += 1
                scores['rsi'] = -0.4
            else:
                scores['rsi'] = 0.0

        # MACD 评分
        macd_result = factors.get('macd')
        if macd_result:
            macd_line, signal_line, histogram = macd_result
            if histogram > 0 and macd_line > signal_line:
                bullish += 2
                scores['macd'] = 0.8
            elif histogram < 0 and macd_line < signal_line:
                bearish += 2
                scores['macd'] = -0.8
            elif histogram > 0:
                bullish += 1
                scores['macd'] = 0.4
            elif histogram < 0:
                bearish += 1
                scores['macd'] = -0.4
            else:
                scores['macd'] = 0.0

        # 布林带评分
        bb = factors.get('bb')
        current_price = factors.get('current_price', 0)
        if bb and current_price:
            upper, middle, lower = bb
            if current_price < lower:
                bullish += 2  # 下轨反弹机会
                scores['bb'] = 0.8
            elif current_price > upper:
                bearish += 2  # 上轨回落风险
                scores['bb'] = -0.8
            else:
                scores['bb'] = 0.0

        # SuperTrend 评分
        st = factors.get('supertrend')
        if st:
            trend, _ = st
            if trend == 1:
                bullish += 1
                scores['supertrend'] = 0.6
            else:
                bearish += 1
                scores['supertrend'] = -0.6

        # MA 交叉评分
        ma_short = factors.get('ma_short')
        ma_long = factors.get('ma_long')
        if ma_short and ma_long:
            if ma_short > ma_long:
                bullish += 1
                scores['ma'] = 0.5
            else:
                bearish += 1
                scores['ma'] = -0.5

        # 成交量评分
        volume_ratio = factors.get('volume_ratio', 1.0)
        if volume_ratio > self.VOLUME_RATIO_THRESHOLD:
            # 放量，增强当前趋势信号
            if bullish > bearish:
                bullish += 1
            elif bearish > bullish:
                bearish += 1
        scores['volume'] = min(1.0, (volume_ratio - 1) / 2)

        return {
            'bullish_signals': bullish,
            'bearish_signals': bearish,
            'factor_scores': scores,
            'net_score': bullish - bearish,
        }

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """
        K 线回调

        增强逻辑:
        1. 计算 confluence_v2 核心因子 (RSI/MACD/BB/SuperTrend/MA/Volume)
        2. 因子评分并生成基础信号
        3. 多周期共振确认
        4. 根据胜率调整仓位
        5. 设置止盈止损
        """
        # 1. 计算因子
        factors = self._calc_factors(code, kline, kline_type)
        if not factors:
            return None

        factors['current_price'] = kline.get('close', 0)

        # 2. 评估因子
        eval_result = self._evaluate_factors(factors)
        net_score = eval_result.get('net_score', 0)

        # 信号阈值
        if net_score >= 3:
            action = SignalAction.BUY
            reason = f"增强融合策略：多头信号 (net={net_score})"
        elif net_score <= -3:
            action = SignalAction.SELL
            reason = f"增强融合策略：空头信号 (net={net_score})"
        else:
            return None  # 信号不足

        # 3. 多周期共振确认
        multi_tf_signal = self.enhancer.check_multi_timeframe_alignment(
            code=code,
            klines_5m=self._kline_buffers_by_period.get(code, {}).get('K_5M'),
            klines_15m=self._kline_buffers_by_period.get(code, {}).get('K_15M'),
            klines_1h=self._kline_buffers_by_period.get(code, {}).get('K_60M'),
            klines_1d=self._kline_buffers_by_period.get(code, {}).get('K_DAY')
        )

        # 4. 根据共振得分调整信号
        if multi_tf_signal.alignment_score >= 0.75:
            # 强共振，增强信号
            if multi_tf_signal.direction == 'long' and action == SignalAction.BUY:
                reason += f" | 多周期共振确认 ({multi_tf_signal.alignment_score:.0%})"
            elif multi_tf_signal.direction == 'short' and action == SignalAction.SELL:
                reason += f" | 多周期共振确认 ({multi_tf_signal.alignment_score:.0%})"
            else:
                # 共振方向与信号相反，降低置信度
                return None

        # 5. 创建基础信号
        signal = Signal(
            code=code,
            action=action,
            price=factors['current_price'],
            quantity=self.accumulator.base_position,
            reason=reason,
            strategy=self.name,
            extra={
                'factor_scores': eval_result['factor_scores'],
                'net_score': net_score,
                'alignment_score': multi_tf_signal.alignment_score,
            }
        )

        # 6. 根据胜率调整仓位
        win_rate = self._win_rate_cache.get(code, 0.5)
        position = self.accumulator.calculate_position(win_rate)

        # 多周期共振强时加仓
        if multi_tf_signal.alignment_score >= 0.75:
            position = int(position * 1.2)

        signal.quantity = position

        # 7. 设置退出策略
        exit_info = self.accumulator.get_exit_strategy(
            entry_price=signal.price,
            current_price=signal.price
        )
        if signal.extra is None:
            signal.extra = {}
        signal.extra['exit_strategy'] = exit_info

        return signal

    def update_win_rate(self, code: str, win_rate: float):
        """更新胜率缓存"""
        self._win_rate_cache[code] = win_rate

    def record_trade_result(self, code: str, pnl_pct: float):
        """记录交易结果"""
        self.accumulator.record_trade(pnl_pct)
        self._win_rate_cache[code] = self.accumulator.get_recent_win_rate()


# 全局单例
_enhancer: Optional[TechnicalEnhancer] = None
_accumulator: Optional[SmallWinAccumulator] = None


def get_technical_enhancer() -> TechnicalEnhancer:
    """获取技术指标增强器单例"""
    global _enhancer
    if _enhancer is None:
        _enhancer = TechnicalEnhancer()
    return _enhancer


def get_small_win_accumulator(base_position: int = 100) -> SmallWinAccumulator:
    """获取积小胜策略单例"""
    global _accumulator
    if _accumulator is None:
        _accumulator = SmallWinAccumulator(base_position)
    return _accumulator


if __name__ == "__main__":
    # 测试
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    enhancer = get_technical_enhancer()
    accumulator = get_small_win_accumulator()

    # 测试多周期共振
    test_klines = [
        {'close': 100 + i * 0.5, 'high': 102 + i * 0.5, 'low': 99 + i * 0.5}
        for i in range(30)
    ]

    multi_tf = enhancer.check_multi_timeframe_alignment(
        "HK.00700",
        klines_5m=test_klines,
        klines_15m=test_klines,
        klines_1h=test_klines,
        klines_1d=test_klines
    )

    print("\n📊 多周期共振检测:")
    print(f"  5 分钟：{multi_tf.timeframe_5m}")
    print(f"  15 分钟：{multi_tf.timeframe_15m}")
    print(f"  1 小时：{multi_tf.timeframe_1h}")
    print(f"  日线：{multi_tf.timeframe_1d}")
    print(f"  共振得分：{multi_tf.alignment_score:.0%}")
    print(f"  方向：{multi_tf.direction}")

    # 测试积小胜
    print("\n📊 积小胜策略测试:")
    for i, pnl in enumerate([0.02, -0.01, 0.03, 0.015, -0.005, 0.025]):
        accumulator.record_trade(pnl)
        win_rate = accumulator.get_recent_win_rate()
        position = accumulator.calculate_position(win_rate)
        print(f"  交易{i+1}: PnL={pnl:.1%} → 胜率={win_rate:.1%} → 仓位={position}")
