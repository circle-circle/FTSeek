"""
PatternRecognizer - 技术形态识别器

使用规则引擎识别经典技术形态：
- 杯柄形态 (Cup & Handle)
- 三角突破 (Triangle)
- 旗形整理 (Flag)
- VCP 波动收缩形态 (VCP)
"""

import time
from dataclasses import dataclass
from typing import List, Dict, Optional


@dataclass
class Pattern:
    """技术形态数据结构"""

    pattern_type: str           # cup_and_handle/triangle/flag/vcp
    symbol: str
    completion_percent: float   # 完成度 (0-100)
    breakout_price: float       # 突破价位
    target_price: float         # 目标价位
    stop_loss: float            # 止损价位
    confidence: float           # 置信度 (0-1)
    detected_at: float          # 检测时间 (timestamp)


@dataclass
class PatternReport:
    """形态识别报告"""

    patterns: List[Pattern]
    scan_time: float
    market: str


class PatternRecognizer:
    """
    技术形态识别器

    使用规则引擎检测经典技术形态，包括杯柄形态、三角突破、
    旗形整理和 VCP 波动收缩形态。
    """

    def __init__(self, config: Optional[Dict] = None):
        """
        初始化 PatternRecognizer

        Args:
            config: 配置字典，可包含识别参数阈值
        """
        self.config = config or {}

        # 默认配置
        self.min_cup_depth = self.config.get('min_cup_depth', 0.20)  # 杯身最小下跌深度 20%
        self.max_cup_depth = self.config.get('max_cup_depth', 0.30)  # 杯身最大下跌深度 30%
        self.min_handle_pullback = self.config.get('min_handle_pullback', 0.08)  # 杯柄最小回调 8%
        self.max_handle_pullback = self.config.get('max_handle_pullback', 0.15)  # 杯柄最大回调 15%
        self.min_flagpole_rise = self.config.get('min_flagpole_rise', 0.30)  # 旗杆最小涨幅 30%
        self.min_vcp_contractions = self.config.get('min_vcp_contractions', 2)  # VCP 最小收缩次数

    def detect_cup_and_handle(self, klines: List[Dict]) -> Optional[Pattern]:
        """
        检测杯柄形态 (Cup & Handle)

        识别规则:
        - 杯身：下跌 20-30% 后回升至起点
        - 杯底：U 型底 (至少 7 周)
        - 杯柄：小幅回调 (10-15%)
        - 突破：放量突破杯柄上沿

        Args:
            klines: K 线数据列表，每项包含 time/open/high/low/close/volume

        Returns:
            检测到的 Pattern，如果未检测到则返回 None
        """
        if len(klines) < 50:  # 至少需要 50 根 K 线
            return None

        # 寻找杯身最高点 (杯口)
        cup_start_price = klines[0]['close']

        # 寻找杯底最低点
        cup_bottom_price = klines[0]['close']

        for i in range(len(klines) // 2):
            if klines[i]['close'] < cup_bottom_price:
                cup_bottom_price = klines[i]['close']

        # 计算杯身深度
        if cup_start_price == 0:
            return None

        cup_depth = (cup_start_price - cup_bottom_price) / cup_start_price

        # 检查杯身深度是否在合理范围 (20-30%)
        if not (self.min_cup_depth <= cup_depth <= self.max_cup_depth):
            return None

        # 寻找杯柄区域 (杯身右侧的高点之后)
        handle_start_idx = len(klines) // 2
        handle_high = 0
        handle_low = float('inf')

        for i in range(handle_start_idx, len(klines) - 1):
            if klines[i]['high'] > handle_high:
                handle_high = klines[i]['high']
            if klines[i]['low'] < handle_low:
                handle_low = klines[i]['low']

        # 计算杯柄回调幅度
        if handle_high == 0:
            return None

        handle_pullback = (handle_high - handle_low) / handle_high

        # 检查杯柄回调是否在合理范围 (8-15%)
        if not (self.min_handle_pullback <= handle_pullback <= self.max_handle_pullback):
            return None

        # 检查是否突破
        latest_close = klines[-1]['close']
        breakout_level = handle_high

        if latest_close <= breakout_level:
            return None  # 尚未突破

        # 计算完成度
        completion_percent = min(100, 70 + (cup_depth - self.min_cup_depth) * 100
                                 + (handle_pullback - self.min_handle_pullback) * 200)

        # 计算置信度
        confidence = 0.7  # 基础置信度
        if cup_depth > 0.22:
            confidence += 0.1
        if handle_pullback < 0.12:
            confidence += 0.1
        if klines[-1]['volume'] > klines[-2]['volume'] * 1.5:  # 放量突破
            confidence += 0.1
        confidence = min(1.0, confidence)

        # 计算目标价位和止损
        cup_range = cup_start_price - cup_bottom_price
        target_price = breakout_level + cup_range * 0.8  # 目标价为杯身高度的 80%
        stop_loss = handle_low * 0.97  # 止损设在杯柄底部下方 3%

        return Pattern(
            pattern_type="cup_and_handle",
            symbol="",  # 由调用方设置
            completion_percent=completion_percent,
            breakout_price=breakout_level,
            target_price=target_price,
            stop_loss=stop_loss,
            confidence=confidence,
            detected_at=time.time()
        )

    def detect_triangle(self, klines: List[Dict]) -> Optional[Pattern]:
        """
        检测三角突破形态 (Triangle)

        识别规则:
        - 收敛三角形：高点降低、低点抬高
        - 至少 2 个高点和 2 个低点
        - 成交量收缩后放大突破

        Args:
            klines: K 线数据列表

        Returns:
            检测到的 Pattern，如果未检测到则返回 None
        """
        if len(klines) < 30:
            return None

        # 寻找局部高点和低点
        highs = []
        lows = []

        for i in range(2, len(klines) - 2):
            # 检查是否为局部高点
            is_high = (klines[i]['high'] > klines[i - 1]['high'] and
                       klines[i]['high'] > klines[i - 2]['high'] and
                       klines[i]['high'] > klines[i + 1]['high'] and
                       klines[i]['high'] > klines[i + 2]['high'])
            if is_high:
                highs.append((i, klines[i]['high']))

            # 检查是否为局部低点
            is_low = (klines[i]['low'] < klines[i - 1]['low'] and
                      klines[i]['low'] < klines[i - 2]['low'] and
                      klines[i]['low'] < klines[i + 1]['low'] and
                      klines[i]['low'] < klines[i + 2]['low'])
            if is_low:
                lows.append((i, klines[i]['low']))

        # 至少需要 2 个高点和 2 个低点
        if len(highs) < 2 or len(lows) < 2:
            return None

        # 检查高点是否降低
        recent_highs = sorted(highs, key=lambda x: x[0])[-3:]
        highs_declining = all(
            recent_highs[i][1] >= recent_highs[i + 1][1]
            for i in range(len(recent_highs) - 1)
        )

        # 检查低点是否抬高
        recent_lows = sorted(lows, key=lambda x: x[0])[-3:]
        lows_rising = all(
            recent_lows[i][1] <= recent_lows[i + 1][1]
            for i in range(len(recent_lows) - 1)
        )

        if not (highs_declining and lows_rising):
            return None

        # 检查是否突破
        triangle_top = recent_highs[0][1]
        latest_close = klines[-1]['close']

        if latest_close <= triangle_top:
            return None  # 尚未突破

        # 计算收敛程度
        price_range = recent_highs[0][1] - recent_lows[0][1]
        recent_range = recent_highs[-1][1] - recent_lows[-1][1]
        contraction_ratio = recent_range / price_range if price_range > 0 else 1

        # 计算完成度和置信度
        completion_percent = min(100, 60 + (1 - contraction_ratio) * 50)

        confidence = 0.65
        if len(highs) >= 3 and len(lows) >= 3:
            confidence += 0.1
        if contraction_ratio < 0.5:
            confidence += 0.15
        if klines[-1]['volume'] > klines[-2]['volume'] * 1.5:
            confidence += 0.1
        confidence = min(1.0, confidence)

        # 计算目标价位和止损
        target_price = triangle_top + price_range * 0.7
        stop_loss = recent_lows[-1][1] * 0.98

        return Pattern(
            pattern_type="triangle",
            symbol="",
            completion_percent=completion_percent,
            breakout_price=triangle_top,
            target_price=target_price,
            stop_loss=stop_loss,
            confidence=confidence,
            detected_at=time.time()
        )

    def detect_flag(self, klines: List[Dict]) -> Optional[Pattern]:
        """
        检测旗形整理形态 (Flag)

        识别规则:
        - 旗杆：急剧上涨 (>30%)
        - 旗面：向下倾斜通道
        - 突破：向上突破旗面上沿

        Args:
            klines: K 线数据列表

        Returns:
            检测到的 Pattern，如果未检测到则返回 None
        """
        if len(klines) < 20:
            return None

        # 寻找旗杆起点
        flagpole_start_price = klines[0]['close']

        # 寻找旗杆终点 (最高点)
        flagpole_end_idx = 0
        flagpole_high = klines[0]['high']

        for i in range(min(15, len(klines) // 2)):
            if klines[i]['high'] > flagpole_high:
                flagpole_high = klines[i]['high']
                flagpole_end_idx = i

        # 计算旗杆涨幅
        if flagpole_start_price == 0:
            return None

        flagpole_rise = (flagpole_high - flagpole_start_price) / flagpole_start_price

        # 检查旗杆涨幅是否足够 (>30%)
        if flagpole_rise < self.min_flagpole_rise:
            return None

        # 寻找旗面区域
        flag_start_idx = flagpole_end_idx
        flag_prices = []

        for i in range(flag_start_idx, len(klines) - 1):
            flag_prices.append(klines[i]['close'])

        if len(flag_prices) < 5:
            return None

        # 检查旗面是否向下倾斜
        flag_high = max(flag_prices)
        flag_low = min(flag_prices)

        # 计算旗面回调幅度
        flag_pullback = (flag_high - flag_low) / flag_high

        # 旗面回调应在 5-15% 之间
        if not (0.05 <= flag_pullback <= 0.20):
            return None

        # 检查是否突破
        latest_close = klines[-1]['close']
        breakout_level = flag_high

        if latest_close <= breakout_level:
            return None  # 尚未突破

        # 计算完成度和置信度
        completion_percent = min(100, 70 + flagpole_rise * 50 + flag_pullback * 100)

        confidence = 0.7
        if flagpole_rise > 0.40:
            confidence += 0.1
        if 0.08 <= flag_pullback <= 0.15:
            confidence += 0.1
        if klines[-1]['volume'] > klines[-2]['volume'] * 1.5:
            confidence += 0.1
        confidence = min(1.0, confidence)

        # 计算目标价位和止损
        flagpole_height = flagpole_high - flagpole_start_price
        target_price = breakout_level + flagpole_height * 0.8
        stop_loss = flag_low * 0.97

        return Pattern(
            pattern_type="flag",
            symbol="",
            completion_percent=completion_percent,
            breakout_price=breakout_level,
            target_price=target_price,
            stop_loss=stop_loss,
            confidence=confidence,
            detected_at=time.time()
        )

    def detect_vcp(self, klines: List[Dict]) -> Optional[Pattern]:
        """
        检测 VCP 波动收缩形态 (Volatility Contraction Pattern)

        识别规则:
        - 波动逐步收缩
        - 至少 2-3 次收缩
        - 最后一次收缩量极度萎缩

        Args:
            klines: K 线数据列表

        Returns:
            检测到的 Pattern，如果未检测到则返回 None
        """
        if len(klines) < 40:
            return None

        # 将 K 线分成若干阶段，检查波动收缩
        stage_size = max(10, len(klines) // 5)
        stages = []

        for i in range(0, len(klines) - stage_size, stage_size):
            stage_klines = klines[i:i + stage_size]
            high = max(k['high'] for k in stage_klines)
            low = min(k['low'] for k in stage_klines)
            avg_volume = sum(k['volume'] for k in stage_klines) / len(stage_klines)
            stages.append({
                'high': high,
                'low': low,
                'range': high - low,
                'volume': avg_volume,
                'start_idx': i
            })

        # 至少需要 3 个阶段
        if len(stages) < 3:
            return None

        # 检查波动是否逐步收缩
        contractions = 0
        for i in range(1, len(stages)):
            if stages[i]['range'] < stages[i - 1]['range']:
                contractions += 1

        # 至少需要 2 次收缩
        if contractions < self.min_vcp_contractions:
            return None

        # 检查成交量是否萎缩
        if len(stages) >= 2:
            volume_ratio = stages[-1]['volume'] / stages[0]['volume'] if stages[0]['volume'] > 0 else 1
            if volume_ratio > 0.8:  # 成交量没有明显萎缩
                return None

        # 检查是否突破
        vcp_high = max(s['high'] for s in stages[:-1])
        latest_close = klines[-1]['close']

        if latest_close <= vcp_high:
            return None  # 尚未突破

        # 计算完成度和置信度
        if len(stages) >= 2:
            range_contraction = (
                1 - (stages[-1]['range'] / stages[0]['range'])
                if stages[0]['range'] > 0 else 0
            )
        else:
            range_contraction = 0

        completion_percent = min(100, 60 + contractions * 15 + range_contraction * 30)

        confidence = 0.65
        if contractions >= 3:
            confidence += 0.15
        if volume_ratio < 0.5:
            confidence += 0.15
        if klines[-1]['volume'] > klines[-2]['volume'] * 2:  # 极度萎缩后放量
            confidence += 0.1
        confidence = min(1.0, confidence)

        # 计算目标价位和止损
        vcp_range = vcp_high - min(s['low'] for s in stages)
        target_price = vcp_high + vcp_range * 0.8
        stop_loss = stages[-1]['low'] * 0.97

        return Pattern(
            pattern_type="vcp",
            symbol="",
            completion_percent=completion_percent,
            breakout_price=vcp_high,
            target_price=target_price,
            stop_loss=stop_loss,
            confidence=confidence,
            detected_at=time.time()
        )

    def detect_all(self, symbol: str, klines: List[Dict]) -> PatternReport:
        """
        检测所有形态

        Args:
            symbol: 股票代码
            klines: K 线数据列表

        Returns:
            PatternReport 包含所有检测到的形态
        """
        start_time = time.time()
        patterns = []

        # 检测杯柄形态
        cup_pattern = self.detect_cup_and_handle(klines)
        if cup_pattern:
            cup_pattern.symbol = symbol
            patterns.append(cup_pattern)

        # 检测三角形态
        triangle_pattern = self.detect_triangle(klines)
        if triangle_pattern:
            triangle_pattern.symbol = symbol
            patterns.append(triangle_pattern)

        # 检测旗形形态
        flag_pattern = self.detect_flag(klines)
        if flag_pattern:
            flag_pattern.symbol = symbol
            patterns.append(flag_pattern)

        # 检测 VCP 形态
        vcp_pattern = self.detect_vcp(klines)
        if vcp_pattern:
            vcp_pattern.symbol = symbol
            patterns.append(vcp_pattern)

        # 按置信度排序
        patterns.sort(key=lambda p: p.confidence, reverse=True)

        scan_time = time.time() - start_time

        return PatternReport(
            patterns=patterns,
            scan_time=scan_time,
            market=symbol
        )
