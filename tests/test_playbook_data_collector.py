"""
PlaybookDataCollector 单元测试

测试 6 维度数据收集的正确性和降级策略
"""
import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime

from app.playbook.data_collector import (
    PlaybookDataCollector,
    PlaybookData,
    FundamentalData,
    TechnicalData,
    InsiderData,
    NewsData,
    EarningsData,
    SentimentData,
)


@pytest.fixture
def mock_providers():
    """创建 mock providers"""
    return {
        "fundamental": Mock(),
        "finnhub": Mock(),
        "insider": Mock(),
        "earnings": Mock(),
    }


@pytest.fixture
def collector(mock_providers):
    """创建 PlaybookDataCollector 实例 (密封: 阻断真实 MarketDataRouter/Futu 网络)"""
    c = PlaybookDataCollector(
        fundamental_provider=mock_providers["fundamental"],
        finnhub_provider=mock_providers["finnhub"],
        insider_provider=mock_providers["insider"],
        earnings_calendar=mock_providers["earnings"],
    )
    # _collect_technical 会懒加载真实 MarketDataRouter (Futu/twelvedata 网络调用),
    # 测试必须密封 → 返回 None, technical 走 missing 降级路径
    c._get_market_data_router = lambda: None
    return c


class TestPlaybookDataCollector:
    """测试 PlaybookDataCollector"""

    def test_collect_returns_playbook_data(self, collector, mock_providers):
        """测试 collect() 返回 PlaybookData"""
        symbol = "US.AAPL"
        
        # Mock 所有 provider 返回成功
        mock_providers["fundamental"].get_data.return_value = {
            "status": "ok",
            "data": {"peTTM": 25.5, "marketCapitalization": 2500000, "industry": "Technology"},
            "data_source": "finnhub",
            "fetched_at": datetime.utcnow().isoformat(),
        }
        mock_providers["finnhub"].get_company_news.return_value = [
            {"headline": "Apple releases new iPhone"},
            {"headline": "Strong quarterly earnings"},
        ]
        mock_providers["insider"].get_data.return_value = {
            "status": "ok",
            "data": {
                "buy_count_30d": 3,
                "sell_count_30d": 1,
                "net_shares_30d": 5000,
                "cluster_score": 0.7,
                "cluster_buyers": ["CEO", "CFO"],
                "transactions": [],
            },
            "data_source": "sec_edgar",
        }
        mock_providers["earnings"].get_next_earnings_date.return_value = {
            "date": "2026-07-15",
        }
        mock_providers["earnings"].get_earnings_history.return_value = [
            {"quarter": "Q1", "surprise": 0.05},
            {"quarter": "Q2", "surprise": 0.03},
        ]
        mock_providers["earnings"].get_sector_atr_multiplier.return_value = 1.2

        result = collector.collect(symbol)

        assert isinstance(result, PlaybookData)
        assert result.symbol == symbol

    def test_collect_fundamental_ok(self, collector, mock_providers):
        """测试基本面数据收集成功"""
        # 2026-06-29 契约: Provider 返回 {"data": {"financials": {...}}} 嵌套 + snake_case
        mock_providers["fundamental"].get_data.return_value = {
            "status": "ok",
            "data": {
                "financials": {
                    "pe_ttm": 25.5,
                    "market_cap": 2500000,
                    "industry": "Technology",
                    "roe": 0.15,
                    "revenue_yoy": 0.12,
                    "52w_high": 200.0,
                    "52w_low": 150.0,
                },
            },
            "data_source": "finnhub",
            "fetched_at": "2026-06-18T10:00:00Z",
        }

        result = collector._collect_fundamental("US.AAPL")

        assert result.quality == "ok"
        assert result.pe == 25.5
        assert result.market_cap == 2500000
        assert result.industry == "Technology"
        assert result.roe == 0.15
        assert result.revenue_growth_yoy == 0.12
        assert result.high_52w == 200.0
        assert result.low_52w == 150.0
        assert result.source == "finnhub"

    def test_collect_fundamental_partial(self, collector, mock_providers):
        """测试基本面数据部分缺失"""
        mock_providers["fundamental"].get_data.return_value = {
            "status": "ok",
            "data": {
                "financials": {
                    "pe_ttm": 25.5,
                    # 缺少 market_cap 和 industry
                },
            },
        }

        result = collector._collect_fundamental("US.AAPL")

        assert result.quality == "partial"
        assert result.pe == 25.5
        assert result.market_cap is None

    def test_collect_fundamental_missing(self, collector, mock_providers):
        """测试基本面数据完全缺失"""
        mock_providers["fundamental"].get_data.return_value = {
            "status": "failed",
            "error": "API error",
        }

        result = collector._collect_fundamental("US.AAPL")

        assert result.quality == "missing"

    @staticmethod
    def _txn(code, shares):
        """SEC Form4 交易桩 (fetch_company_form4s 元素契约)"""
        return {
            "transaction_code": code,
            "shares": shares,
            "insider_name": "Some Insider",
            "price_per_share": 100.0,
            "transaction_date": "2026-06-15",
        }

    def test_collect_insider_strong_buy(self, collector, mock_providers):
        """测试内部人数据 - 强买入信号 (契约: SEC fetch_company_form4s + detect_cluster_buys)"""
        mock_providers["insider"].fetch_company_form4s.return_value = [
            self._txn("P", 2000) for _ in range(5)
        ]
        cluster = Mock()
        cluster.score = 0.8
        cluster.buyers = [
            {"name": "CEO", "role": "CEO"},
            {"name": "CFO", "role": "CFO"},
            {"name": "CTO", "role": "CTO"},
        ]
        mock_providers["insider"].detect_cluster_buys.return_value = cluster

        result = collector._collect_insider("US.AAPL")

        assert result.quality == "ok"
        assert result.signal == "strong_buy"  # cluster_score > 0.5
        assert result.buy_count_30d == 5
        assert result.cluster_score == 0.8
        assert len(result.cluster_buyers) == 3

    def test_collect_insider_buy(self, collector, mock_providers):
        """测试内部人数据 - 买入信号"""
        mock_providers["insider"].fetch_company_form4s.return_value = [
            self._txn("P", 1000), self._txn("P", 1000), self._txn("P", 500),
            self._txn("S", 500),
        ]
        cluster = Mock()
        cluster.score = 0.3  # < 0.5, 不是集群买入
        cluster.buyers = []
        mock_providers["insider"].detect_cluster_buys.return_value = cluster

        result = collector._collect_insider("US.AAPL")

        assert result.signal == "buy"  # buy > sell 且 net > 0

    def test_collect_insider_sell(self, collector, mock_providers):
        """测试内部人数据 - 卖出信号"""
        mock_providers["insider"].fetch_company_form4s.return_value = [
            self._txn("P", 1000),
            self._txn("S", 800), self._txn("S", 800), self._txn("S", 800),
            self._txn("S", 800), self._txn("S", 800),
        ]
        cluster = Mock()
        cluster.score = 0.1
        cluster.buyers = []
        mock_providers["insider"].detect_cluster_buys.return_value = cluster

        result = collector._collect_insider("US.AAPL")

        assert result.signal == "sell"

    def test_collect_news_positive(self, collector, mock_providers):
        """测试新闻数据 - 正面情绪"""
        mock_providers["finnhub"].get_company_news.return_value = [
            {"headline": "Apple stock surges after strong earnings beat"},
            {"headline": "Positive growth outlook for Q3"},
            {"headline": "Analysts upgrade to strong buy"},
        ]

        result = collector._collect_news("US.AAPL")

        assert result.quality == "ok"
        assert result.sentiment_score > 0  # 正面
        assert len(result.headlines) == 3
        assert result.source == "finnhub"

    def test_collect_news_negative(self, collector, mock_providers):
        """测试新闻数据 - 负面情绪"""
        mock_providers["finnhub"].get_company_news.return_value = [
            {"headline": "Apple stock drops after earnings miss"},
            {"headline": "Negative outlook for Q3"},
            {"headline": "Analysts downgrade stock"},
        ]

        result = collector._collect_news("US.AAPL")

        assert result.sentiment_score < 0  # 负面

    def test_collect_news_missing(self, collector, mock_providers):
        """测试新闻数据缺失"""
        mock_providers["finnhub"].get_company_news.return_value = []

        result = collector._collect_news("US.AAPL")

        assert result.quality == "missing"

    def test_collect_earnings_ok(self, collector, mock_providers):
        """测试财报数据收集成功 (2026-06-22 起 Finnhub 为主源: calendar + stock/earnings)"""
        mock_providers["finnhub"]._get.return_value = {
            "earningsCalendar": [{"date": "2026-07-15"}],
        }
        mock_providers["finnhub"].get_company_earnings.return_value = [
            {"quarter": "Q1", "epsActual": 1.05, "epsEstimate": 1.00},
            {"quarter": "Q2", "epsActual": 1.03, "epsEstimate": 1.00},
            {"quarter": "Q3", "epsActual": 0.98, "epsEstimate": 1.00},
            {"quarter": "Q4", "epsActual": 1.08, "epsEstimate": 1.00},
        ]

        result = collector._collect_earnings("US.AAPL")

        assert result.quality == "ok"
        assert result.next_date == "2026-07-15"
        assert result.days_until is not None
        assert result.beat_rate == 0.75  # 3/4 超预期
        # 2026-06-22: sector_atr_multiplier 收敛为固定 1.0 (原 AlphaVantage 源退役)
        assert result.sector_atr_multiplier == 1.0
        assert len(result.history) == 4

    def test_collect_earnings_partial(self, collector, mock_providers):
        """测试财报数据部分缺失 (无未来财报日期, 但有历史 → partial)"""
        mock_providers["finnhub"]._get.return_value = {}  # calendar 无数据
        mock_providers["finnhub"].get_company_earnings.return_value = [
            {"quarter": "Q1", "epsActual": 1.05, "epsEstimate": 1.00},
        ]

        result = collector._collect_earnings("US.AAPL")

        assert result.quality == "partial"
        assert result.next_date is None

    def test_collect_sentiment_ok(self, collector, mock_providers):
        """测试情绪数据 - Finnhub sentiment 已对接 (2026-06-19 Phase 0)"""
        mock_providers["finnhub"].get_sentiment_score.return_value = 0.35

        result = collector._collect_sentiment("US.AAPL")

        assert result.news_sentiment == 0.35
        assert result.vix >= 0
        # sentiment ok + vix ok → quality ok
        assert result.quality == "ok"

    def test_collect_technical_missing(self, collector, mock_providers):
        """测试技术面数据 - router 不可用时降级 missing (fixture 已密封 router=None)"""
        result = collector._collect_technical("US.AAPL")

        assert result.quality == "missing"

    def test_collect_batch(self, collector, mock_providers):
        """测试批量收集多只股票"""
        symbols = ["US.AAPL", "US.GOOG", "US.MSFT"]
        
        # Mock 所有 provider 返回成功
        mock_providers["fundamental"].get_data.return_value = {
            "status": "ok",
            "data": {"peTTM": 25.0},
        }
        mock_providers["finnhub"].get_company_news.return_value = []
        mock_providers["insider"].get_data.return_value = {"status": "failed"}
        mock_providers["earnings"].get_next_earnings_date.return_value = None
        mock_providers["earnings"].get_earnings_history.return_value = []
        mock_providers["earnings"].get_sector_atr_multiplier.return_value = 1.0

        results = collector.collect_batch(symbols, max_workers=2)

        assert len(results) == 3
        assert all(isinstance(r, PlaybookData) for r in results)
        assert {r.symbol for r in results} == set(symbols)

    def test_collect_batch_with_error(self, collector, mock_providers):
        """测试批量收集时某个股票失败"""
        symbols = ["US.AAPL", "US.INVALID"]
        
        # AAPL 成功
        def get_data_side_effect(symbol):
            if symbol == "US.AAPL":
                return {"status": "ok", "data": {"peTTM": 25.0}}
            else:
                raise Exception("API error")
        
        mock_providers["fundamental"].get_data.side_effect = get_data_side_effect
        mock_providers["finnhub"].get_company_news.return_value = []
        mock_providers["insider"].get_data.return_value = {"status": "failed"}
        mock_providers["earnings"].get_next_earnings_date.return_value = None
        mock_providers["earnings"].get_earnings_history.return_value = []
        mock_providers["earnings"].get_sector_atr_multiplier.return_value = 1.0

        results = collector.collect_batch(symbols)

        assert len(results) == 2
        # 即使失败也返回空数据
        invalid_result = next(r for r in results if r.symbol == "US.INVALID")
        assert invalid_result.fundamental.quality == "missing"


class TestDataQuality:
    """测试数据质量判断逻辑"""

    def test_fundamental_quality_ok(self):
        """测试基本面数据质量 - ok"""
        data = FundamentalData(
            pe=25.5,
            market_cap=2500000,
            industry="Technology",
        )
        assert data.quality == "missing"  # 默认值，需要在 collector 中设置

    def test_fundamental_quality_partial(self):
        """测试基本面数据质量 - partial"""
        data = FundamentalData(
            pe=25.5,
            # 缺少 market_cap 和 industry
        )
        # quality 由 collector 设置

    def test_insider_quality_ok(self):
        """测试内部人数据质量"""
        data = InsiderData(
            buy_count_30d=3,
            sell_count_30d=1,
            quality="ok",
        )
        assert data.quality == "ok"
