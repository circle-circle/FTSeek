"""
CatalystTracker 单元测试模块

测试催化事件追踪器的核心功能，包括：
- 即将发生事件查询
- 今日事件查询
- 板块过滤
- 事件类型过滤
- 财报扫描
- 影响评分计算
"""

import pytest
from datetime import datetime, timedelta
from typing import List

from app.v4.data.catalyst_db import Catalyst, CatalystReport
from app.v4.analyzers.catalyst_tracker import CatalystTracker


class TestCatalystTracker:
    """CatalystTracker 测试类"""

    @pytest.fixture
    def tracker(self) -> CatalystTracker:
        """创建测试用的追踪器实例"""
        return CatalystTracker()

    @pytest.fixture
    def sample_catalysts(self) -> List[Catalyst]:
        """创建样本催化事件数据"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        return [
            # 今日事件
            Catalyst(
                symbol="AAPL",
                event_type="earnings",
                event_date=today,
                title="Apple Q1 财报发布",
                description="苹果公司第一季度财报电话会议",
                impact_score=85.0,
                sector="Technology",
                confirmed=True
            ),
            # 未来 3 天事件
            Catalyst(
                symbol="MRNA",
                event_type="fda",
                event_date=today + timedelta(days=3),
                title="Moderna FDA 审批",
                description="FDA 对 Moderna 新疫苗的审批决定",
                impact_score=92.0,
                sector="Healthcare",
                confirmed=True
            ),
            # 未来 7 天事件
            Catalyst(
                symbol="TSLA",
                event_type="product",
                event_date=today + timedelta(days=7),
                title="Tesla 新产品发布",
                description="Tesla 新款车型发布会",
                impact_score=78.0,
                sector="Consumer Cyclical",
                confirmed=False
            ),
            # 过去事件
            Catalyst(
                symbol="NVDA",
                event_type="earnings",
                event_date=today - timedelta(days=2),
                title="NVIDIA 财报发布",
                description="NVIDIA 第四季度财报",
                impact_score=90.0,
                sector="Technology",
                confirmed=True
            ),
            # 板块过滤测试 - Healthcare
            Catalyst(
                symbol="JNJ",
                event_type="conference",
                event_date=today + timedelta(days=5),
                title="强生行业会议",
                description="强生参加医疗健康行业会议",
                impact_score=65.0,
                sector="Healthcare",
                confirmed=True
            ),
            # 板块过滤测试 - Technology
            Catalyst(
                symbol="MSFT",
                event_type="rating",
                event_date=today + timedelta(days=2),
                title="微软分析师评级上调",
                description="高盛上调微软评级至买入",
                impact_score=72.0,
                sector="Technology",
                confirmed=True
            ),
        ]

    def test_upcoming_catalysts(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试获取未来 N 天的催化事件"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 获取未来 7 天的事件
        upcoming = tracker.get_upcoming_catalysts(days=7)

        # 验证结果
        assert len(upcoming) >= 4  # 至少包含今日 + 未来 7 天内的事件
        assert all(c.event_date >= datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) for c in upcoming)

        # 验证包含预期的事件
        symbols = [c.symbol for c in upcoming]
        assert "AAPL" in symbols  # 今日
        assert "MRNA" in symbols  # 3 天后
        assert "TSLA" in symbols  # 7 天后
        assert "MSFT" in symbols  # 2 天后
        assert "JNJ" in symbols   # 5 天后

    def test_today_catalysts(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试获取今日催化事件"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 获取今日事件
        today_catalysts = tracker.get_today_catalysts()

        # 验证结果
        today = datetime.now().date()
        assert len(today_catalysts) >= 1
        assert all(c.event_date.date() == today for c in today_catalysts)

        # 验证包含 AAPL
        symbols = [c.symbol for c in today_catalysts]
        assert "AAPL" in symbols

    def test_sector_filter(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试按板块过滤"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 过滤 Technology 板块
        tech_catalysts = tracker.filter_by_sector("Technology")
        assert len(tech_catalysts) >= 2
        assert all(c.sector == "Technology" for c in tech_catalysts)

        tech_symbols = [c.symbol for c in tech_catalysts]
        assert "AAPL" in tech_symbols
        assert "MSFT" in tech_symbols

        # 过滤 Healthcare 板块
        healthcare_catalysts = tracker.filter_by_sector("Healthcare")
        assert len(healthcare_catalysts) >= 2
        assert all(c.sector == "Healthcare" for c in healthcare_catalysts)

        healthcare_symbols = [c.symbol for c in healthcare_catalysts]
        assert "MRNA" in healthcare_symbols
        assert "JNJ" in healthcare_symbols

    def test_type_filter(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试按事件类型过滤"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 过滤 earnings 类型
        earnings = tracker.filter_by_type("earnings")
        assert len(earnings) >= 2
        assert all(c.event_type == "earnings" for c in earnings)

        earnings_symbols = [c.symbol for c in earnings]
        assert "AAPL" in earnings_symbols
        assert "NVDA" in earnings_symbols

        # 过滤 fda 类型
        fda_events = tracker.filter_by_type("fda")
        assert len(fda_events) >= 1
        assert all(c.event_type == "fda" for c in fda_events)
        assert "MRNA" in [c.symbol for c in fda_events]

    def test_earnings_scan(self, tracker: CatalystTracker) -> None:
        """测试财报扫描功能"""
        # 执行财报扫描
        earnings = tracker.scan_earnings()

        # 验证返回的是 Catalyst 列表
        assert isinstance(earnings, list)

        # 验证所有结果都是 earnings 类型
        for catalyst in earnings:
            assert isinstance(catalyst, Catalyst)
            assert catalyst.event_type == "earnings"

    def test_impact_scoring(self, tracker: CatalystTracker) -> None:
        """测试影响评分计算"""
        # 测试影响评分公式
        # impact_score = (
        #     event_importance * 0.4 +
        #     historical_volatility * 0.3 +
        #     market_cap_factor * 0.2 +
        #     sector_momentum * 0.1
        # )

        score = tracker.calculate_impact_score(
            event_importance=80.0,
            historical_volatility=70.0,
            market_cap_factor=90.0,
            sector_momentum=60.0
        )

        # 计算预期分数
        expected = (80.0 * 0.4 + 70.0 * 0.3 + 90.0 * 0.2 + 60.0 * 0.1)
        assert score == expected
        assert score == 77.0

        # 验证分数范围在 0-100
        assert 0 <= score <= 100

        # 测试边界情况
        max_score = tracker.calculate_impact_score(100.0, 100.0, 100.0, 100.0)
        assert max_score == 100.0

        min_score = tracker.calculate_impact_score(0.0, 0.0, 0.0, 0.0)
        assert min_score == 0.0

    def test_scan_all(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试全面扫描生成报告"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 执行全面扫描
        report = tracker.scan_all()

        # 验证返回的是 CatalystReport
        assert isinstance(report, CatalystReport)

        # 验证报告结构
        assert hasattr(report, 'upcoming')
        assert hasattr(report, 'today')
        assert hasattr(report, 'past_due')
        assert hasattr(report, 'scan_time')

        # 验证各部分都是列表
        assert isinstance(report.upcoming, list)
        assert isinstance(report.today, list)
        assert isinstance(report.past_due, list)

        # 验证扫描时间
        assert report.scan_time > 0

        # 验证 past_due 包含 NVDA (过去 2 天的事件)
        past_symbols = [c.symbol for c in report.past_due]
        assert "NVDA" in past_symbols

    def test_biotech_fda_scan(self, tracker: CatalystTracker, sample_catalysts: List[Catalyst]) -> None:
        """测试生物科技 FDA 审批扫描"""
        # 设置样本数据
        tracker._catalysts = sample_catalysts

        # 执行 FDA 扫描
        fda_events = tracker.scan_biotech_fda()

        # 验证返回的是 Catalyst 列表
        assert isinstance(fda_events, list)

        # 验证所有结果都是 fda 类型
        for catalyst in fda_events:
            assert isinstance(catalyst, Catalyst)
            assert catalyst.event_type == "fda"

        # 验证包含 MRNA
        symbols = [c.symbol for c in fda_events]
        assert "MRNA" in symbols


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
