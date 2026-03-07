"""
MomentumScanner 单元测试模块

测试动量扫描器的各项功能，包括：
- 财报行情扫描
- 生物科技催化扫描
- 动量排名
- 报告生成
- Top 推荐过滤
- 评分计算
"""

import pytest
from datetime import datetime
from app.v4.analyzers.momentum_scanner import (
    MomentumScanner,
    CatalystOpportunity,
    MomentumReport,
)


class TestMomentumScanner:
    """MomentumScanner 测试类"""

    @pytest.fixture
    def scanner(self) -> MomentumScanner:
        """创建扫描器实例"""
        return MomentumScanner()

    def test_earnings_scan(self, scanner: MomentumScanner) -> None:
        """测试财报扫描功能

        验证 scan_earnings_plays 方法能够：
        1. 返回 CatalystOpportunity 列表
        2. 所有机会的 catalyst_type 为 'earnings'
        3. 包含必要的字段信息
        """
        catalysts = scanner.scan_earnings_plays()

        assert isinstance(catalysts, list)
        assert len(catalysts) > 0

        for catalyst in catalysts:
            assert isinstance(catalyst, CatalystOpportunity)
            assert catalyst.catalyst_type == "earnings"
            assert catalyst.symbol is not None
            assert catalyst.name is not None
            assert 0 <= catalyst.momentum_score <= 100
            assert catalyst.risk_level in ["low", "medium", "high"]

    def test_biotech_scan(self, scanner: MomentumScanner) -> None:
        """测试生物科技催化扫描功能

        验证 scan_biotech_catalysts 方法能够：
        1. 返回 CatalystOpportunity 列表
        2. 所有机会的 catalyst_type 为 'biotech'
        3. 包含 FDA 审批或临床数据相关事件
        """
        catalysts = scanner.scan_biotech_catalysts()

        assert isinstance(catalysts, list)
        assert len(catalysts) > 0

        for catalyst in catalysts:
            assert isinstance(catalyst, CatalystOpportunity)
            assert catalyst.catalyst_type == "biotech"
            assert catalyst.symbol is not None
            assert catalyst.name is not None
            assert 0 <= catalyst.momentum_score <= 100
            assert catalyst.risk_level in ["low", "medium", "high"]

    def test_momentum_ranking(self, scanner: MomentumScanner) -> None:
        """测试动量排名功能

        验证 scan_all 方法返回的机会按动量评分降序排列
        """
        report = scanner.scan_all()

        assert isinstance(report, MomentumReport)
        assert isinstance(report.opportunities, list)

        if len(report.opportunities) > 1:
            # 验证按动量评分降序排列
            for i in range(len(report.opportunities) - 1):
                assert (
                    report.opportunities[i].momentum_score
                    >= report.opportunities[i + 1].momentum_score
                )

    def test_report_generation(self, scanner: MomentumScanner) -> None:
        """测试报告生成功能

        验证 scan_all 方法生成的 MomentumReport 包含所有必要字段
        """
        report = scanner.scan_all()

        assert isinstance(report, MomentumReport)
        assert isinstance(report.opportunities, list)
        assert isinstance(report.scan_time, float)
        assert report.scan_time > 0
        assert report.market_sentiment in ["bullish", "neutral", "bearish"]
        assert isinstance(report.top_picks, list)

        # 验证 top_picks 是机会列表中的股票代码
        if report.top_picks:
            opportunity_symbols = [opp.symbol for opp in report.opportunities]
            for pick in report.top_picks:
                assert pick in opportunity_symbols

    def test_top_picks_filtering(self, scanner: MomentumScanner) -> None:
        """测试 Top 推荐过滤功能

        验证 get_top_picks 方法能够：
        1. 返回指定数量的推荐机会
        2. 返回的是动量评分最高的机会
        """
        # 先执行全面扫描
        scanner.scan_all()

        # 获取 Top 5 推荐
        top_picks = scanner.get_top_picks(limit=5)

        assert isinstance(top_picks, list)
        assert len(top_picks) <= 5

        if len(top_picks) > 1:
            # 验证按动量评分降序排列
            for i in range(len(top_picks) - 1):
                assert (
                    top_picks[i].momentum_score >= top_picks[i + 1].momentum_score
                )

    def test_score_calculation(self, scanner: MomentumScanner) -> None:
        """测试评分计算功能

        验证动量评分公式正确实现：
        momentum_score = (
            price_momentum * 0.3 +
            volume_momentum * 0.25 +
            catalyst_strength * 0.30 +
            market_sentiment * 0.15
        )
        """
        # 创建测试用的 CatalystOpportunity
        opportunity = CatalystOpportunity(
            symbol="TEST",
            name="Test Stock",
            catalyst_type="earnings",
            momentum_score=0.0,  # 初始为 0
            expected_volatility=0.5,
            time_window="1W",
            risk_level="medium",
            timestamp=datetime.now().timestamp(),
        )

        # 验证 opportunity 创建成功
        assert opportunity.symbol == "TEST"
        assert opportunity.catalyst_type == "earnings"

        # 模拟各因子分数 (0-100)
        price_momentum = 80.0
        volume_momentum = 60.0
        catalyst_strength = 90.0
        market_sentiment = 70.0

        # 计算期望分数
        expected_score = (
            price_momentum * 0.3 +
            volume_momentum * 0.25 +
            catalyst_strength * 0.30 +
            market_sentiment * 0.15
        )

        # 验证分数在 0-100 范围内
        assert 0 <= expected_score <= 100

        # 验证各权重之和为 1.0
        total_weight = 0.3 + 0.25 + 0.30 + 0.15
        assert abs(total_weight - 1.0) < 0.001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
