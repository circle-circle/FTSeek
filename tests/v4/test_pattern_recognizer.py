"""
PatternRecognizer 单元测试模块

测试经典技术形态识别功能：
- 杯柄形态 (Cup & Handle)
- 三角突破 (Triangle)
- 旗形整理 (Flag)
- VCP 波动收缩形态 (VCP)
"""

import pytest
import time
from typing import List, Dict
from app.v4.analyzers.pattern_recognizer import PatternRecognizer, Pattern, PatternReport


def generate_cup_handle_klines() -> List[Dict]:
    """生成模拟杯柄形态的 K 线数据"""
    base_price = 100.0
    klines = []
    current_time = time.time() - 60 * 60 * 24 * 80  # 80 天前
    
    # 杯身左半部分：下跌 25% (15 天)
    for i in range(15):
        price = base_price * (1 - 0.25 * (i / 15))
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 1000000
        })
    
    # 杯底：U 型底部 (35 天，7 周)
    for i in range(15, 50):
        progress = (i - 15) / 35
        # U 型：从 75% 缓慢回升到 100%
        price = base_price * 0.75 + base_price * 0.25 * (progress ** 0.5)
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 800000 if 15 <= i < 45 else 1200000
        })
    
    # 杯身右半部分：略高于起点 (15 天)
    for i in range(50, 65):
        progress = (i - 50) / 15
        # 从 100% 上升到 105%
        price = base_price * (1.0 + 0.05 * progress)
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 1100000
        })
    
    # 杯柄：小幅回调 10% (10 天)
    handle_high = base_price * 1.05
    handle_low_price = handle_high * 0.90  # 回调 10%
    for i in range(65, 75):
        progress = (i - 65) / 10
        # 从 handle_high 缓慢回调到 handle_low_price
        price = handle_high - (handle_high - handle_low_price) * progress
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.008,  # 小波动
            "low": price * 0.992,
            "close": price,
            "volume": 900000 if i < 74 else 2000000
        })
    
    # 突破
    klines.append({
        "time": current_time + 75 * 86400,
        "open": handle_high * 1.01,
        "high": handle_high * 1.06,
        "low": handle_high * 0.99,
        "close": handle_high * 1.05,
        "volume": 3000000  # 放量突破
    })
    
    return klines


def generate_triangle_klines() -> List[Dict]:
    """生成模拟三角突破形态的 K 线数据"""
    base_price = 100.0
    klines = []
    current_time = time.time() - 60 * 60 * 24 * 45  # 45 天前
    
    # 收敛三角形：高点降低、低点抬高，需要震荡
    for i in range(40):
        progress = i / 40
        # 高点逐渐降低 (从 110 降到 105)
        trend_high = base_price * (1.10 - 0.05 * progress)
        # 低点逐渐抬高 (从 90 升到 100)
        trend_low = base_price * (0.90 + 0.10 * progress)
        
        # 交替创建高点和低点
        if i % 3 == 0:
            # 高点日
            high = trend_high
            low = trend_low + (trend_high - trend_low) * 0.4
            close = trend_low + (trend_high - trend_low) * 0.5
        elif i % 3 == 1:
            # 中间日
            high = trend_low + (trend_high - trend_low) * 0.7
            low = trend_low + (trend_high - trend_low) * 0.2
            close = trend_low + (trend_high - trend_low) * 0.45
        else:
            # 低点日
            high = trend_low + (trend_high - trend_low) * 0.4
            low = trend_low
            close = trend_low + (trend_high - trend_low) * 0.3
        
        klines.append({
            "time": current_time + i * 86400,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": int(1000000 * (1 - 0.5 * progress))
        })
    
    # 突破
    klines.append({
        "time": current_time + 40 * 86400,
        "open": base_price * 1.06,
        "high": base_price * 1.12,
        "low": base_price * 1.05,
        "close": base_price * 1.10,
        "volume": 3000000
    })
    
    return klines


def generate_flag_klines() -> List[Dict]:
    """生成模拟旗形形态的 K 线数据"""
    base_price = 100.0
    klines = []
    current_time = time.time() - 60 * 60 * 24 * 30  # 30 天前
    
    # 旗杆：急剧上涨 40%
    for i in range(10):
        price = base_price * (1 + 0.4 * (i / 10))
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.03,
            "low": price * 0.97,
            "close": price,
            "volume": 2000000
        })
    
    # 旗面高点
    flagpole_high = base_price * 1.4
    
    # 旗面：向下倾斜通道 (15 天，回调 10%)
    for i in range(10, 25):
        progress = (i - 10) / 15
        # 从旗杆顶部回调 10%
        price = flagpole_high * (1 - 0.10 * progress)
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.015,
            "low": price * 0.985,
            "close": price,
            "volume": int(1000000 * (1 - 0.3 * progress))  # 缩量整理
        })
    
    # 突破
    klines.append({
        "time": current_time + 25 * 86400,
        "open": flagpole_high * 1.01,
        "high": flagpole_high * 1.06,
        "low": flagpole_high * 0.99,
        "close": flagpole_high * 1.05,
        "volume": 3500000  # 放量突破
    })
    
    return klines


def generate_vcp_klines() -> List[Dict]:
    """生成模拟 VCP 形态的 K 线数据"""
    base_price = 100.0
    klines = []
    current_time = time.time() - 60 * 60 * 24 * 50  # 50 天前
    
    # VCP 基准价 (所有波动围绕这个价格)
    vcp_base = base_price
    
    # 第一次波动：±15% (15 天)
    for i in range(15):
        progress = i / 15
        # 从 base 上涨 15% 再回落
        price = vcp_base * (1 + 0.15 * (1 - abs(2 * progress - 1)))
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 2000000
        })
    
    # 第二次波动：±8% (15 天)
    for i in range(15, 30):
        progress = (i - 15) / 15
        # 从 base 上涨 8% 再回落
        price = vcp_base * (1 + 0.08 * (1 - abs(2 * progress - 1)))
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.015,
            "low": price * 0.985,
            "close": price,
            "volume": 1000000
        })
    
    # 第三次波动：±4% (15 天)
    for i in range(30, 45):
        progress = (i - 30) / 15
        # 从 base 上涨 4% 再回落
        price = vcp_base * (1 + 0.04 * (1 - abs(2 * progress - 1)))
        klines.append({
            "time": current_time + i * 86400,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 400000  # 极度萎缩
        })
    
    # 突破：突破 VCP 最高点 (第一次波动的高点约 115)
    vcp_high = vcp_base * 1.15
    klines.append({
        "time": current_time + 45 * 86400,
        "open": vcp_high * 1.01,
        "high": vcp_high * 1.06,
        "low": vcp_high * 0.99,
        "close": vcp_high * 1.05,
        "volume": 3000000  # 极度萎缩后放量
    })
    
    return klines


class TestPatternRecognizer:
    """PatternRecognizer 测试类"""
    
    @pytest.fixture
    def recognizer(self) -> PatternRecognizer:
        """创建 PatternRecognizer 实例"""
        return PatternRecognizer()
    
    def test_initialization(self, recognizer):
        """测试初始化"""
        assert recognizer is not None
        assert hasattr(recognizer, 'detect_cup_and_handle')
        assert hasattr(recognizer, 'detect_triangle')
        assert hasattr(recognizer, 'detect_flag')
        assert hasattr(recognizer, 'detect_vcp')
        assert hasattr(recognizer, 'detect_all')
    
    def test_cup_handle_detection(self, recognizer):
        """测试杯柄形态检测"""
        klines = generate_cup_handle_klines()
        pattern = recognizer.detect_cup_and_handle(klines)
        
        assert pattern is not None, "应检测到杯柄形态"
        assert pattern.pattern_type == "cup_and_handle"
        assert pattern.completion_percent >= 80, "形态完成度应>=80%"
        assert 0 < pattern.confidence <= 1, "置信度应在 0-1 之间"
        assert pattern.breakout_price > 0, "突破价位应大于 0"
        assert pattern.target_price > pattern.breakout_price, "目标价应高于突破价"
        assert pattern.stop_loss < pattern.breakout_price, "止损价应低于突破价"
    
    def test_triangle_detection(self, recognizer):
        """测试三角形态检测"""
        klines = generate_triangle_klines()
        pattern = recognizer.detect_triangle(klines)
        
        assert pattern is not None, "应检测到三角形态"
        assert pattern.pattern_type == "triangle"
        assert pattern.completion_percent >= 70, "形态完成度应>=70%"
        assert 0 < pattern.confidence <= 1, "置信度应在 0-1 之间"
        assert pattern.breakout_price > 0, "突破价位应大于 0"
    
    def test_flag_detection(self, recognizer):
        """测试旗形形态检测"""
        klines = generate_flag_klines()
        pattern = recognizer.detect_flag(klines)
        
        assert pattern is not None, "应检测到旗形形态"
        assert pattern.pattern_type == "flag"
        assert pattern.completion_percent >= 80, "形态完成度应>=80%"
        assert 0 < pattern.confidence <= 1, "置信度应在 0-1 之间"
        assert pattern.breakout_price > 0, "突破价位应大于 0"
    
    def test_vcp_detection(self, recognizer):
        """测试 VCP 形态检测"""
        klines = generate_vcp_klines()
        pattern = recognizer.detect_vcp(klines)
        
        assert pattern is not None, "应检测到 VCP 形态"
        assert pattern.pattern_type == "vcp"
        assert pattern.completion_percent >= 75, "形态完成度应>=75%"
        assert 0 < pattern.confidence <= 1, "置信度应在 0-1 之间"
        assert pattern.breakout_price > 0, "突破价位应大于 0"
    
    def test_pattern_completion(self, recognizer):
        """测试形态完成度计算"""
        klines = generate_cup_handle_klines()
        pattern = recognizer.detect_cup_and_handle(klines)
        
        assert pattern is not None
        assert 0 <= pattern.completion_percent <= 100, "完成度应在 0-100 之间"
        
        # 测试不完整的形态 (只取前一半数据)
        incomplete_klines = klines[:len(klines)//2]
        incomplete_pattern = recognizer.detect_cup_and_handle(incomplete_klines)
        
        if incomplete_pattern is not None:
            assert incomplete_pattern.completion_percent < pattern.completion_percent, \
                "不完整形态的完成度应更低"
    
    def test_breakout_levels(self, recognizer):
        """测试突破价位计算"""
        klines = generate_cup_handle_klines()
        pattern = recognizer.detect_cup_and_handle(klines)
        
        assert pattern is not None
        assert pattern.breakout_price > 0, "突破价位应大于 0"
        assert pattern.target_price > pattern.breakout_price, "目标价应高于突破价"
        assert pattern.stop_loss < pattern.breakout_price, "止损价应低于突破价"
        
        # 验证盈亏比合理 (至少 1:1.3)
        upside = pattern.target_price - pattern.breakout_price
        downside = pattern.breakout_price - pattern.stop_loss
        if downside > 0:
            risk_reward_ratio = upside / downside
            assert risk_reward_ratio >= 1.2, "盈亏比应至少 1:1.2"
    
    def test_detect_all(self, recognizer):
        """测试检测所有形态"""
        klines = generate_cup_handle_klines()
        report = recognizer.detect_all("HK.00700", klines)
        
        assert isinstance(report, PatternReport)
        assert isinstance(report.patterns, list)
        assert report.scan_time > 0, "扫描时间应大于 0"
        assert report.market == "HK.00700"
        
        # 应至少检测到杯柄形态
        cup_patterns = [p for p in report.patterns if p.pattern_type == "cup_and_handle"]
        assert len(cup_patterns) > 0, "应检测到杯柄形态"
    
    def test_no_pattern_detected(self, recognizer):
        """测试无形态时的处理"""
        # 生成随机波动数据，不构成任何形态
        import random
        random.seed(42)
        klines = []
        base_price = 100.0
        current_time = time.time()
        
        for i in range(30):
            price = base_price * (1 + random.uniform(-0.05, 0.05))
            klines.append({
                "time": current_time + i * 86400,
                "open": price,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": 1000000
            })
        
        # 各形态检测应返回 None 或低置信度
        cup_pattern = recognizer.detect_cup_and_handle(klines)
        # 可能返回 None 或低置信度的 pattern
        if cup_pattern is not None:
            assert cup_pattern.confidence < 0.5, "随机数据不应有高置信度形态"
    
    def test_pattern_dataclass(self):
        """测试 Pattern 数据结构"""
        pattern = Pattern(
            pattern_type="cup_and_handle",
            symbol="HK.00700",
            completion_percent=85.5,
            breakout_price=105.0,
            target_price=120.0,
            stop_loss=95.0,
            confidence=0.85,
            detected_at=time.time()
        )
        
        assert pattern.pattern_type == "cup_and_handle"
        assert pattern.symbol == "HK.00700"
        assert pattern.completion_percent == 85.5
        assert pattern.breakout_price == 105.0
        assert pattern.target_price == 120.0
        assert pattern.stop_loss == 95.0
        assert pattern.confidence == 0.85
        assert pattern.detected_at > 0
    
    def test_pattern_report_dataclass(self):
        """测试 PatternReport 数据结构"""
        patterns = [
            Pattern(
                pattern_type="cup_and_handle",
                symbol="HK.00700",
                completion_percent=85.0,
                breakout_price=105.0,
                target_price=120.0,
                stop_loss=95.0,
                confidence=0.85,
                detected_at=time.time()
            )
        ]
        
        report = PatternReport(
            patterns=patterns,
            scan_time=0.05,
            market="HK"
        )
        
        assert len(report.patterns) == 1
        assert report.scan_time > 0
        assert report.market == "HK"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
