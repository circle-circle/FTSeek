"""
PreMarketAnalyzer 单元测试模块

测试盘前成交量分析器的核心功能:
- 量比计算
- 异常检测
- 批量分析
- 报告数据结构
- 评分计算
"""

from app.v4.analyzers.premarket import PreMarketAnalyzer, PreMarketReport


class TestPreMarketAnalyzer:
    """PreMarketAnalyzer 测试类"""

    def setup_method(self):
        """每个测试前的准备工作"""
        self.analyzer = PreMarketAnalyzer()

    def test_volume_ratio_calculation(self):
        """测试量比计算逻辑"""
        # 测试正常量比 (1.0)
        report = self.analyzer.analyze_volume("AAPL")
        assert report.volume_ratio >= 0, "量比应该为非负数"
        assert isinstance(report.volume_ratio, float), "量比应该是浮点数"

        # 量比计算应该合理 (通常在 0-10 倍之间)
        assert report.volume_ratio < 20, "量比异常高，可能计算有误"

    def test_abnormal_detection(self):
        """测试异常检测功能"""
        # 测试正常情况 (量比 < 3.0)
        report_normal = self.analyzer.analyze_volume("AAPL")
        if report_normal.volume_ratio < 3.0:
            assert report_normal.is_abnormal is False, "量比 < 3.0 时不应标记为异常"
        else:
            assert report_normal.is_abnormal is True, "量比 >= 3.0 时应标记为异常"

        # 测试阈值可配置性
        analyzer_strict = PreMarketAnalyzer()
        # 可以通过方法获取不同阈值的异常股票
        stocks = analyzer_strict.get_abnormal_stocks(threshold=2.0)
        assert isinstance(stocks, list), "get_abnormal_stocks 应返回列表"

    def test_batch_analysis(self):
        """测试批量分析功能"""
        symbols = ["AAPL", "GOOGL", "MSFT"]
        reports = self.analyzer.analyze_batch(symbols)

        # 验证返回数量
        assert len(reports) == len(symbols), "批量分析应返回与输入数量相同的报告"

        # 验证每个报告的有效性
        for report in reports:
            assert isinstance(report, PreMarketReport), "每个报告应为 PreMarketReport 实例"
            assert report.symbol in symbols, "报告中的股票代码应在输入列表中"

    def test_report_data_structure(self):
        """测试报告数据结构的完整性"""
        report = self.analyzer.analyze_volume("AAPL")

        # 验证所有必需字段存在
        assert hasattr(report, 'symbol'), "报告应包含 symbol 字段"
        assert hasattr(report, 'volume_ratio'), "报告应包含 volume_ratio 字段"
        assert hasattr(report, 'is_abnormal'), "报告应包含 is_abnormal 字段"
        assert hasattr(report, 'abnormal_score'), "报告应包含 abnormal_score 字段"
        assert hasattr(report, 'bid_ask_spread'), "报告应包含 bid_ask_spread 字段"
        assert hasattr(report, 'order_imbalance'), "报告应包含 order_imbalance 字段"
        assert hasattr(report, 'timestamp'), "报告应包含 timestamp 字段"

        # 验证字段类型
        assert isinstance(report.symbol, str), "symbol 应为字符串"
        assert isinstance(report.volume_ratio, float), "volume_ratio 应为浮点数"
        assert isinstance(report.is_abnormal, bool), "is_abnormal 应为布尔值"
        assert isinstance(report.abnormal_score, int), "abnormal_score 应为整数"
        assert isinstance(report.bid_ask_spread, float), "bid_ask_spread 应为浮点数"
        assert isinstance(report.order_imbalance, float), "order_imbalance 应为浮点数"
        assert isinstance(report.timestamp, float), "timestamp 应为浮点数"

    def test_score_calculation(self):
        """测试异常评分计算公式"""
        # 评分公式：abnormal_score = min(100, volume_ratio * 20)
        report = self.analyzer.analyze_volume("AAPL")

        # 验证评分范围在 0-100 之间
        assert 0 <= report.abnormal_score <= 100, "异常评分应在 0-100 范围内"

        # 验证评分与量比的关系
        expected_score = min(100, int(report.volume_ratio * 20))
        assert report.abnormal_score == expected_score, \
            f"评分计算错误：期望 {expected_score}, 实际 {report.abnormal_score}"

        # 测试边界情况：量比 5.0 应该得到 100 分
        # 测试边界情况：量比 3.0 应该得到 60 分
        # (这些需要通过模拟数据来测试，在后续集成测试中验证)


class TestPreMarketReport:
    """PreMarketReport 数据类测试"""

    def test_report_creation(self):
        """测试报告对象的创建"""
        report = PreMarketReport(
            symbol="AAPL",
            volume_ratio=3.5,
            is_abnormal=True,
            abnormal_score=70,
            bid_ask_spread=0.05,
            order_imbalance=0.15,
            timestamp=1709712000.0
        )

        assert report.symbol == "AAPL"
        assert report.volume_ratio == 3.5
        assert report.is_abnormal is True
        assert report.abnormal_score == 70
        assert report.bid_ask_spread == 0.05
        assert report.order_imbalance == 0.15
        assert report.timestamp == 1709712000.0

    def test_report_default_values(self):
        """测试报告字段的默认值"""
        # 验证 is_abnormal 应该基于 volume_ratio 自动计算
        # 这里测试手动创建的情况
        report = PreMarketReport(
            symbol="TEST",
            volume_ratio=1.0,
            is_abnormal=False,
            abnormal_score=20,
            bid_ask_spread=0.0,
            order_imbalance=0.0,
            timestamp=1709712000.0
        )
        assert report.volume_ratio == 1.0
        assert report.is_abnormal is False
