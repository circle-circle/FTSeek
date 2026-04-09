"""
突发新闻权重倍增器测试 (Breaking News Weight Multiplier Tests)

测试范围:
1. 突发新闻检测功能
2. 权重倍增逻辑
3. 快速止盈/止损触发
4. 端到端链路验证

创建日期：2026-04-08
"""
import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

# ============================================================
# 测试 1: 突发新闻检测功能
# ============================================================

class TestBreakingNewsDetection:
    """突发新闻检测功能测试"""

    def test_check_breaking_news_with_mock_news(self):
        """测试有突发新闻时的检测"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        # Mock 新闻分析器返回结果
        mock_result = {
            'top_news': [
                {'title': '腾讯财报超预期', 'is_breaking': True, 'sentiment_score': 0.85},
                {'title': '游戏业务增长强劲', 'is_breaking': True, 'sentiment_score': 0.75},
                {'title': '分析师上调目标价', 'is_breaking': False, 'sentiment_score': 0.60},
            ]
        }

        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.return_value = mock_result

            result = engine._check_breaking_news("HK.00700", lookback_hours=2)

            assert result is True, "应该检测到突发新闻"

    def test_check_breaking_news_no_breaking(self):
        """测试无突发新闻时的检测"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        mock_result = {
            'top_news': [
                {'title': '常规市场分析', 'is_breaking': False, 'sentiment_score': 0.30},
                {'title': '行业周报', 'is_breaking': False, 'sentiment_score': 0.20},
            ]
        }

        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.return_value = mock_result

            result = engine._check_breaking_news("HK.00700", lookback_hours=2)

            assert result is False, "不应检测到突发新闻"

    def test_check_breaking_news_uses_cache(self):
        """测试缓存机制"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        # 手动设置缓存
        engine._breaking_news_cache["HK.00700"] = (True, time.time())

        # 即使没有新闻分析器，也应返回缓存结果
        result = engine._check_breaking_news("HK.00700")

        assert result is True, "应返回缓存的突发新闻状态"

    def test_check_breaking_news_cache_expiry(self):
        """测试缓存过期"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        # 设置过期缓存（10 分钟前）
        engine._breaking_news_cache["HK.00700"] = (True, time.time() - 600)

        # Mock 新闻分析器返回无突发新闻
        mock_result = {'top_news': []}

        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.return_value = mock_result

            result = engine._check_breaking_news("HK.00700")

            # 缓存过期，应重新检测并返回 False
            assert result is False, "缓存过期后应重新检测"


# ============================================================
# 测试 2: 权重倍增逻辑
# ============================================================

class TestWeightMultiplier:
    """权重倍增逻辑测试"""

    def test_sentiment_strategy_weight_boost(self):
        """测试情绪型策略权重倍增"""
        from app.strategy.consensus_engine import ConsensusEngine, SENTIMENT_STRATEGIES

        engine = ConsensusEngine()

        # 设置缓存：有突发新闻
        engine._breaking_news_cache["HK.00700"] = (True, time.time())

        # 测试 sentiment_ai 策略
        weight = engine._calc_weight("sentiment_ai", "neutral", "HK.00700")

        # 基准权重 0.5 × 突发新闻 2.0 = 1.0
        assert weight == 0.5 * 2.0, f"情绪策略权重应为 1.0，实际为{weight}"

    def test_non_sentiment_strategy_no_boost(self):
        """测试非情绪型策略无权重倍增"""
        from app.strategy.consensus_engine import ConsensusEngine, TREND_STRATEGIES

        engine = ConsensusEngine()

        # 设置缓存：有突发新闻
        engine._breaking_news_cache["HK.00700"] = (True, time.time())

        # 测试 supertrend 策略（趋势型）
        weight = engine._calc_weight("supertrend", "trending", "HK.00700")

        # 基准权重 1.0 × 趋势市 1.5 × 突发新闻 1.0(非情绪) = 1.5
        assert weight == 1.0 * 1.5, f"趋势策略权重应为 1.5，实际为{weight}"

    def test_confluence_v2_weight_with_breaking_news(self):
        """测试 confluence_v2（混合型）在突发新闻下的权重"""
        from app.strategy.consensus_engine import ConsensusEngine, HYBRID_STRATEGIES

        engine = ConsensusEngine()

        # 设置缓存：有突发新闻
        engine._breaking_news_cache["HK.00700"] = (True, time.time())

        # confluence_v2 是混合型，不属于 SENTIMENT_STRATEGIES
        # 但它的子因子可能包含情绪分析
        weight = engine._calc_weight("confluence_v2", "neutral", "HK.00700")

        # 基准权重 1.5 × 突发新闻 1.0(非情绪型) = 1.5
        # 注意：confluence_v2 本身不是情绪策略，不享受倍增
        assert weight == 1.5, f"混合策略权重应为 1.5，实际为{weight}"

    def test_weight_multiplier_constant(self):
        """测试权重倍增常数"""
        from app.strategy.consensus_engine import BREAKING_NEWS_MULTIPLIER

        assert BREAKING_NEWS_MULTIPLIER == 2.0, "突发新闻权重倍增应为 2.0"


# ============================================================
# 测试 3: 快速止盈/止损触发
# ============================================================

class TestBreakingNewsExit:
    """快速止盈/止损触发测试"""

    def test_breaking_news_take_profit_trigger(self):
        """测试快速止盈触发"""
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        calculator = StopLossCalculator(
            breaking_news_take_profit=0.02,  # 2% 止盈
            breaking_news_stop_loss=0.02,    # 2% 止损
        )

        # 突发新闻买入
        config = {'is_breaking': True, 'sentiment_score': 0.8}
        stop_level = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config=config
        )

        assert stop_level.type == "breaking_news", "止损类型应为 breaking_news"
        assert "止盈=102.00" in stop_level.notes, f"应包含止盈价信息，实际：{stop_level.notes}"

        # 测试止盈触发
        triggered, reason = calculator.check_breaking_news_exit(
            stop_level=stop_level,
            current_price=102.50,  # 超过止盈价
            direction="BUY",
            entry_time="2026-04-08T10:00:00",
            current_time="2026-04-08T10:15:00"
        )

        assert triggered is True, "应触发止盈"
        assert "止盈价" in reason, f"触发原因应包含止盈，实际：{reason}"

    def test_breaking_news_stop_loss_trigger(self):
        """测试快速止损触发"""
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        calculator = StopLossCalculator(
            breaking_news_take_profit=0.02,
            breaking_news_stop_loss=0.02,
        )

        config = {'is_breaking': True, 'sentiment_score': 0.8}
        stop_level = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config=config
        )

        # 测试止损触发
        triggered, reason = calculator.check_breaking_news_exit(
            stop_level=stop_level,
            current_price=97.50,  # 低于止损价 98
            direction="BUY",
            entry_time="2026-04-08T10:00:00",
            current_time="2026-04-08T10:15:00"
        )

        assert triggered is True, "应触发止损"
        assert "止损价" in reason, f"触发原因应包含止损，实际：{reason}"

    def test_breaking_news_time_exit(self):
        """测试时间退出（30 分钟超时）"""
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        calculator = StopLossCalculator(
            breaking_news_take_profit=0.02,
            breaking_news_stop_loss=0.02,
            breaking_news_max_minutes=30,
        )

        config = {'is_breaking': True, 'sentiment_score': 0.8}
        stop_level = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config=config
        )

        # 35 分钟后，价格仍在止盈止损之间
        triggered, reason = calculator.check_breaking_news_exit(
            stop_level=stop_level,
            current_price=100.50,  # 在止盈止损之间
            direction="BUY",
            entry_time="2026-04-08T10:00:00",
            current_time="2026-04-08T10:35:00"  # 35 分钟后
        )

        assert triggered is True, "应触发时间退出"
        assert "持仓超时" in reason, f"触发原因应包含超时，实际：{reason}"

    def test_sentiment_adjusted_take_profit(self):
        """测试根据情绪得分调整的止盈"""
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        calculator = StopLossCalculator(
            breaking_news_take_profit=0.02,  # 基准 2%
        )

        # 极强情绪（>0.8）：止盈应收紧至 1.5%
        config_strong = {'is_breaking': True, 'sentiment_score': 0.85}
        stop_level_strong = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config=config_strong
        )

        # 验证止盈价（100 * 1.015 = 101.5）
        assert "止盈=101.50" in stop_level_strong.notes, \
            f"极强情绪止盈应为 101.50，实际：{stop_level_strong.notes}"

        # 一般情绪（<0.6）：止盈应放宽至 3%
        config_weak = {'is_breaking': True, 'sentiment_score': 0.4}
        stop_level_weak = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config=config_weak
        )

        # 验证止盈价（100 * 1.03 = 103）
        assert "止盈=103.00" in stop_level_weak.notes, \
            f"一般情绪止盈应为 103.00，实际：{stop_level_weak.notes}"


# ============================================================
# 测试 4: 端到端链路验证
# ============================================================

class TestEndToEndFlow:
    """端到端链路验证测试"""

    def test_full_breaking_news_signal_flow(self):
        """测试完整突发新闻信号流程"""
        from app.strategy.consensus_engine import ConsensusEngine, Signal, SignalAction
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        # 1. 创建共识引擎
        engine = ConsensusEngine()

        # 2. Mock 突发新闻检测
        engine._breaking_news_cache["HK.00700"] = (True, time.time())

        # 3. 模拟 sentiment_ai 策略投票
        mock_signal = Signal(
            code="HK.00700",
            name="腾讯控股",
            action=SignalAction.BUY,
            price=420.0,
            quantity=200,
            reason="突发新闻：财报超预期",
            strategy="sentiment_ai"
        )

        # 4. 权重计算验证
        weight = engine._calc_weight("sentiment_ai", "neutral", "HK.00700")
        assert weight == 0.5 * 2.0, "情绪策略权重应翻倍"

        # 5. 止盈止损计算
        calculator = StopLossCalculator()
        stop_level = calculator.calculate(
            entry_price=420.0,
            direction="BUY",
            method=StopLossType.BREAKING_NEWS,
            breaking_news_config={'is_breaking': True, 'sentiment_score': 0.85}
        )

        # 6. 验证止盈价（极强情绪：1.5%）
        # 420 * 1.015 = 426.3
        assert "止盈=426.30" in stop_level.notes, \
            f"止盈价应为 426.30，实际：{stop_level.notes}"

        # 7. 验证止损价（2%）
        # 420 * 0.98 = 411.6
        assert "止损=411.60" in stop_level.notes or "止损价 411.6" in stop_level.notes.lower(), \
            f"止损价应为 411.60，实际：{stop_level.notes}"

    def test_no_breaking_news_normal_exit(self):
        """测试无突发新闻时的正常止盈止损"""
        from app.risk.stop_loss import StopLossCalculator, StopLossType

        calculator = StopLossCalculator(
            default_stop_percent=0.03,  # 正常 3% 止损
        )

        # 无突发新闻
        config = {'is_breaking': False, 'sentiment_score': 0.3}
        stop_level = calculator.calculate(
            entry_price=100.0,
            direction="BUY",
            method=StopLossType.FIXED_PERCENT,
            breaking_news_config=config
        )

        # 应使用固定止损（3%）
        assert stop_level.type == "fixed_percent", "应使用固定止损"
        assert stop_level.distance_percent == 0.03, "止损应为 3%"


# ============================================================
# 测试 5: 边缘情况
# ============================================================

class TestEdgeCases:
    """边缘情况测试"""

    def test_news_analyzer_failure_graceful_handling(self):
        """测试新闻分析器失败时的优雅处理"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        # Mock 新闻分析器抛出异常
        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.side_effect = Exception("API 调用失败")

            # 应返回 True（宁可错杀，不可放过）
            result = engine._check_breaking_news("HK.00700")

            assert result is True, "失败时应假设存在突发新闻"

    def test_empty_news_list(self):
        """测试空新闻列表"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        mock_result = {'top_news': []}

        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.return_value = mock_result

            result = engine._check_breaking_news("HK.00700")

            assert result is False, "空新闻列表应无突发新闻"

    def test_mixed_breaking_and_normal_news(self):
        """测试混合突发和普通新闻"""
        from app.strategy.consensus_engine import ConsensusEngine

        engine = ConsensusEngine()

        mock_result = {
            'top_news': [
                {'is_breaking': False, 'sentiment_score': 0.3},
                {'is_breaking': False, 'sentiment_score': 0.2},
                {'is_breaking': True, 'sentiment_score': 0.9},  # 只有 1 条突发
            ]
        }

        with patch.object(engine, '_news_analyzer') as mock_analyzer:
            mock_analyzer.analyze_stock.return_value = mock_result

            result = engine._check_breaking_news("HK.00700")

            # 只要有任意一条 is_breaking=True，就返回 True
            assert result is True, "有 1 条突发新闻即应返回 True"


# ============================================================
# 运行测试
# ============================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
