#!/usr/bin/env python3
"""
分层策略集成测试 (2026-04-09)

测试三层策略架构:
1. 事件驱动层 - 突发新闻→LLM 分析→高优先级信号
2. 跨市场套利层 - 美港股联动→开盘快速下单
3. 技术增强层 - 多周期共振 + 积小胜仓位管理

运行测试:
PYTHONPATH=/opt/ftseek/FTSeek python -m pytest tests/strategy/test_layered_integration.py -v
"""
import pytest
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# 确保导入路径正确
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.strategy.event_driven import EventDrivenStrategy, get_event_strategy, Event, EventType
from app.strategy.cross_market import CrossMarketArbitrage, get_cross_market_strategy, OvernightChange
from app.strategy.tech_enhancer import TechnicalEnhancer, SmallWinAccumulator, get_technical_enhancer, get_small_win_accumulator
from app.strategy.stock_pool import get_stock_pool_manager, Tier, Sector
from app.strategy.layered_integration import LayeredStrategyManager, get_layered_strategy_manager, LayeredSignal


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.test.layered_integration")


class TestEventDrivenStrategy:
    """事件驱动策略测试"""

    def test_event_detector_keywords(self):
        """测试事件检测器关键词匹配"""
        from app.strategy.event_driven import EventDetector

        detector = EventDetector()

        # 测试财报事件
        news = {
            'id': 'test_001',
            'title': '腾讯控股 Q4 财报超预期 净利润增长 30%',
            'content': '腾讯控股发布 2025 年 Q4 财报，净利润 XXX 亿元...',
            'source': 'sina',
            'published_at': datetime.now().isoformat(),
            'related_stocks': ['HK.00700']
        }

        events = detector.detect([news])
        assert len(events) > 0
        assert events[0].type == EventType.EARNINGS
        assert events[0].related_stocks == ['HK.00700']

    def test_event_analyzer_simple_analysis(self):
        """测试简单规则分析 (降级方案)"""
        from app.strategy.event_driven import QwenEventAnalyzer, Event

        analyzer = QwenEventAnalyzer()

        event = Event(
            id='test_001',
            type=EventType.EARNINGS,
            title='腾讯控股 Q4 财报超预期 净利润增长 30%',
            content='业绩表现强劲，超出市场预期...',
            source='sina',
            published_at=datetime.now(),
            related_stocks=['HK.00700']
        )

        analyzed = analyzer.analyze(event)

        assert analyzed.sentiment in ['positive', 'negative', 'neutral']
        assert -1.0 <= analyzed.sentiment_score <= 1.0
        assert 0.0 <= analyzed.confidence <= 100.0

    def test_event_driven_strategy_signal_generation(self):
        """测试事件驱动策略信号生成"""
        strategy = get_event_strategy()

        test_news = [
            {
                'id': 'test_001',
                'title': '腾讯控股 Q4 财报超预期 净利润增长 30%',
                'content': '腾讯控股发布 2025 年 Q4 财报，显示净利润达到 XXX 亿元，同比增长 30%...',
                'source': 'sina',
                'published_at': datetime.now().isoformat(),
                'related_stocks': ['HK.00700']
            }
        ]

        signals = strategy.on_news(test_news)

        # 信号可能因为置信度不足或其他原因被过滤，这里主要测试流程
        logger.info(f"生成信号数：{len(signals)}")
        assert signals is not None  # 至少返回空列表


class TestCrossMarketArbitrage:
    """跨市场套利策略测试"""

    def test_us_to_hk_analysis(self):
        """测试美股→港股套利分析"""
        strategy = get_cross_market_strategy()

        # 模拟美股隔夜数据
        us_data = [
            OvernightChange(
                code='BABA',
                name='Alibaba',
                close_price=85.5,
                change_pct=0.045,  # +4.5% (>3% 阈值)
                volume=15000000,
                turnover=1280000000,
                market='US',
                timestamp=datetime.now()
            ),
            OvernightChange(
                code='JD',
                name='JD.com',
                close_price=28.3,
                change_pct=-0.038,  # -3.8% (<-3% 阈值)
                volume=8000000,
                turnover=226000000,
                market='US',
                timestamp=datetime.now()
            ),
        ]

        signals = strategy.analyze_us_to_hk(us_data)

        logger.info(f"美股→港股信号数：{len(signals)}")
        assert signals is not None

        # 验证信号属性
        for signal in signals:
            assert signal.code.startswith('HK.')  # 必须是港股
            assert signal.extra is not None
            assert 'arb_type' in signal.extra
            assert signal.extra['arb_type'] == 'us_to_hk'

    def test_hk_to_us_analysis(self):
        """测试港股→美股套利分析"""
        strategy = get_cross_market_strategy()

        # 模拟港股白天数据
        hk_data = [
            OvernightChange(
                code='HK.00700',
                name='腾讯控股',
                close_price=350.0,
                change_pct=0.035,  # +3.5%
                volume=20000000,
                turnover=7000000000,
                market='HK',
                timestamp=datetime.now()
            ),
        ]

        signals = strategy.analyze_hk_to_us(hk_data)

        logger.info(f"港股→美股信号数：{len(signals)}")
        assert signals is not None

    def test_arbitrage_summary(self):
        """测试套利摘要"""
        strategy = get_cross_market_strategy()

        summary = strategy.get_arbitrage_summary()

        assert 'us_overnight_count' in summary
        assert 'hk_daytime_count' in summary
        assert 'enabled_pairs' in summary


class TestTechnicalEnhancer:
    """技术增强层测试"""

    def test_multi_timeframe_alignment(self):
        """测试多周期共振检测"""
        enhancer = get_technical_enhancer()

        # 模拟上涨趋势 K 线
        test_klines = [
            {'close': 100 + i * 0.5, 'high': 102 + i * 0.5, 'low': 99 + i * 0.5}
            for i in range(30)
        ]

        multi_tf = enhancer.check_multi_timeframe_alignment(
            code="HK.00700",
            klines_5m=test_klines,
            klines_15m=test_klines,
            klines_1h=test_klines,
            klines_1d=test_klines
        )

        assert multi_tf.timeframe_5m in ['up', 'down', 'neutral']
        assert multi_tf.timeframe_15m in ['up', 'down', 'neutral']
        assert multi_tf.timeframe_1h in ['up', 'down', 'neutral']
        assert multi_tf.timeframe_1d in ['up', 'down', 'neutral']
        assert 0.0 <= multi_tf.alignment_score <= 1.0
        assert multi_tf.direction in ['long', 'short', 'neutral']

        logger.info(
            f"多周期共振检测：5M={multi_tf.timeframe_5m}, "
            f"15M={multi_tf.timeframe_15m}, 1H={multi_tf.timeframe_1h}, "
            f"1D={multi_tf.timeframe_1d}, 得分={multi_tf.alignment_score:.0%}, "
            f"方向={multi_tf.direction}"
        )

    def test_small_win_accumulator_position_calculation(self):
        """测试积小胜策略仓位计算"""
        accumulator = get_small_win_accumulator(base_position=100)

        # 高胜率场景
        for _ in range(10):
            accumulator.record_trade(0.02)  # 2% 盈利

        position_high = accumulator.calculate_position()
        assert position_high == 100  # 高胜率 → 正常仓位

        # 中等胜率场景
        accumulator._win_history.clear()
        for _ in range(5):
            accumulator.record_trade(0.02)
        for _ in range(5):
            accumulator.record_trade(-0.01)  # 5 赢 5 亏

        position_medium = accumulator.calculate_position()
        assert position_medium < 100  # 中等胜率 → 降仓位

        logger.info(f"高胜率仓位：{position_high}, 中等胜率仓位：{position_medium}")

    def test_small_win_accumulator_exit_strategy(self):
        """测试积小胜退出策略"""
        accumulator = get_small_win_accumulator()

        exit_info = accumulator.get_exit_strategy(
            entry_price=100.0,
            current_price=102.0  # +2%
        )

        assert 'target_price' in exit_info
        assert 'trailing_stop_price' in exit_info
        assert 'quick_take_pct' in exit_info
        assert 'should_exit' in exit_info

        logger.info(f"退出策略：{exit_info}")


class TestStockPoolManager:
    """股票池管理器测试"""

    def test_tier_classification(self):
        """测试股票分层"""
        manager = get_stock_pool_manager()

        tier1 = manager.get_tier1_stocks()
        tier2 = manager.get_tier2_stocks()
        tier3 = manager.get_tier3_stocks()

        logger.info(f"Tier1: {len(tier1)}只，Tier2: {len(tier2)}只，Tier3: {len(tier3)}只")

        assert len(tier1) > 0 or len(tier2) > 0 or len(tier3) > 0

        # 测试单个股票查询
        if tier1:
            tier = manager.get_stock_tier(tier1[0])
            assert tier == Tier.TIER1

    def test_sector_classification(self):
        """测试板块归类"""
        manager = get_stock_pool_manager()

        tech_stocks = manager.get_sector_stocks(Sector.TECHNOLOGY)
        finance_stocks = manager.get_sector_stocks(Sector.FINANCE)

        logger.info(f"科技板块：{len(tech_stocks)}只，金融板块：{len(finance_stocks)}只")

    def test_cross_market_mapping(self):
        """测试跨市场映射"""
        manager = get_stock_pool_manager()

        pair = manager.get_hk_us_pair("HK.00700")

        if pair:
            assert pair.hk_code == "HK.00700"
            assert pair.us_code in ["TCEHY", "Tencent"]
            logger.info(f"跨市场配对：{pair.hk_name} ↔ {pair.us_name}")


class TestLayeredStrategyManager:
    """分层策略管理器集成测试"""

    def test_layered_manager_initialization(self):
        """测试分层管理器初始化"""
        manager = get_layered_strategy_manager(config={'base_position': 100})

        assert manager.event_strategy is not None
        assert manager.cross_market_strategy is not None
        assert manager.tech_enhancer is not None
        assert manager.accumulator is not None
        assert manager.stock_pool is not None

    def test_process_news_integration(self):
        """测试新闻处理集成"""
        manager = get_layered_strategy_manager()

        test_news = [
            {
                'id': 'test_001',
                'title': '腾讯控股 Q4 财报超预期 净利润增长 30%',
                'content': '腾讯控股发布 2025 年 Q4 财报...',
                'source': 'sina',
                'published_at': datetime.now().isoformat(),
                'related_stocks': ['HK.00700']
            }
        ]

        signals = manager.process_news(test_news)

        # 验证返回类型
        for signal in signals:
            assert isinstance(signal, LayeredSignal)
            assert signal.layer == "event_driven"
            assert signal.priority in [1, 2, 3]
            assert 0.0 <= signal.confidence <= 1.0

        logger.info(f"事件驱动层生成 {len(signals)} 个信号")

    def test_kline_cache_update(self):
        """测试 K 线缓存更新"""
        manager = get_layered_strategy_manager()

        # 更新 K 线缓存
        for i in range(10):
            kline = {
                'close': 100 + i * 0.5,
                'high': 102 + i * 0.5,
                'low': 99 + i * 0.5,
                'open': 100 + (i-1) * 0.5,
                'volume': 1000000,
                'timestamp': datetime.now().isoformat()
            }
            manager.update_kline_cache("HK.00700", "K_5M", kline)

        # 验证缓存
        klines = manager._kline_cache.get("HK.00700", {}).get("K_5M", [])
        assert len(klines) == 10

    def test_layer_summary(self):
        """测试分层摘要"""
        manager = get_layered_strategy_manager()

        summary = manager.get_layer_summary()

        assert 'event_driven' in summary
        assert 'cross_market' in summary
        assert 'technical' in summary

        logger.info(f"分层策略摘要：{summary}")


class TestIntegrationWithRealData:
    """真实数据集成测试 (需要实际数据源)"""

    @pytest.mark.skip(reason="需要实际数据源，仅手动运行")
    def test_full_pipeline_with_real_news(self):
        """完整流程测试 (使用真实新闻数据)"""
        manager = get_layered_strategy_manager()

        # 从新闻采集器获取真实新闻
        from app.news_push.collector import get_collector
        collector = get_collector()

        all_news = collector.fetch_all()

        if all_news:
            # 转换为事件驱动格式
            news_list = [
                {
                    'id': news.id,
                    'title': news.title,
                    'content': news.content,
                    'source': news.source,
                    'published_at': news.published_at,
                    'related_stocks': news.related_stocks
                }
                for news in all_news[:10]  # 仅测试前 10 条
            ]

            signals = manager.process_news(news_list)

            logger.info(f"处理真实新闻 {len(news_list)} 条，生成信号 {len(signals)} 个")

            for sig in signals:
                logger.info(
                    f"信号：{sig.signal.code} | 层={sig.layer} | "
                    f"优先级={sig.priority} | 置信度={sig.confidence:.0%}"
                )

    @pytest.mark.skip(reason="需要实际数据源，仅手动运行")
    def test_cross_market_with_real_data(self):
        """跨市场套利测试 (使用真实美港股数据)"""
        manager = get_layered_strategy_manager()

        # 这里需要接入真实的美港股行情 API
        # 示例结构:
        # us_data = fetch_us_overnight_data()  # 美股隔夜涨跌数据
        # hk_data = fetch_hk_daytime_data()    # 港股白天涨跌数据

        # signals_us_to_hk = manager.analyze_us_overnight(us_data)
        # signals_hk_to_us = manager.analyze_hk_daytime(hk_data)

        logger.info("跨市场套利真实数据测试需要接入实际行情 API")


if __name__ == "__main__":
    # 手动运行测试
    pytest.main([__file__, "-v", "-s"])
