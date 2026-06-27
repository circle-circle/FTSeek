"""
PlaybookScreener 单元测试
"""
import pytest
from unittest.mock import Mock
from pathlib import Path

from app.playbook.screener import PlaybookScreener, ScreenResult
from app.playbook.data_collector import (
    PlaybookData, 
    FundamentalData, 
    TechnicalData, 
    InsiderData, 
    NewsData, 
    EarningsData,
    SentimentData
)
from app.playbook.market_regime import MarketRegime


@pytest.fixture
def mock_data_collector():
    """模拟数据收集器"""
    return Mock()


@pytest.fixture
def screener(mock_data_collector, tmp_path):
    """创建测试用的 screener"""
    config_path = tmp_path / "test_screening.yaml"
    config_path.write_text("""
screening_factors:
  insider_cluster_buy:
    weight: 4
    description: "Insider cluster buy"
  insider_net_buy:
    weight: 2
    description: "Insider net buy"
  rsi_oversold:
    weight: 2
    condition: "rsi_14 < 40"
    description: "RSI oversold"
  above_ma20:
    weight: 2
    condition: "price > ma20"
    description: "Price above MA20"
  macd_golden_cross:
    weight: 1
    description: "MACD golden cross"
  news_positive:
    weight: 1
    condition: "sentiment_score > 0"
    description: "Positive news"
  earnings_beat_streak:
    weight: 1
    condition: "beat_rate > 0.5"
    description: "Earnings beat streak"
  volume_expansion:
    weight: 1
    condition: "volume_5d_avg_ratio > 1.2"
    description: "Volume expansion"
  pe_below_industry:
    weight: 1
    description: "PE below industry"
  rsi_overbought:
    weight: -2
    condition: "rsi_14 > 70"
    description: "RSI overbought"
  insider_net_sell:
    weight: -2
    description: "Insider net sell"
  news_negative:
    weight: -1
    condition: "sentiment_score < -0.3"
    description: "Negative news"
  earnings_imminent:
    weight: -3
    condition: "days_until_earnings < 3"
    description: "Earnings imminent"

weight_adjustments: []
""")
    
    return PlaybookScreener(
        data_collector=mock_data_collector,
        config_path=str(config_path)
    )


def create_test_data(
    symbol: str,
    price: float = 100.0,
    ma20: float = 95.0,
    rsi: float = 50.0,
    macd_signal: str = "neutral",
    volume_ratio: float = 1.0,
    sentiment: float = 0.0,
    insider_cluster: float = 0.0,
    insider_net: int = 0,
    beat_rate: float = 0.5,
    days_until_earnings: int = 30,
    pe: float = None,       # None = 不触发 pe_below_industry
    industry: str = "Technology"
) -> PlaybookData:
    """创建测试用的 PlaybookData"""
    return PlaybookData(
        symbol=symbol,
        fundamental=FundamentalData(pe=pe, industry=industry, quality="ok"),
        technical=TechnicalData(
            price=price,
            ma20=ma20,
            rsi_14=rsi,
            macd_signal=macd_signal,
            volume_5d_avg_ratio=volume_ratio,
            quality="ok"
        ),
        insider=InsiderData(
            cluster_score=insider_cluster,
            net_shares_30d=insider_net,
            quality="ok"
        ),
        news=NewsData(sentiment_score=sentiment, quality="ok"),
        earnings=EarningsData(
            beat_rate=beat_rate,
            days_until=days_until_earnings,
            quality="ok"
        ),
        sentiment=SentimentData(quality="ok")
    )


class TestPlaybookScreener:
    """测试 PlaybookScreener"""
    
    def test_screen_basic(self, screener, mock_data_collector):
        """测试基本筛选功能和排序"""
        watchlist = ["AAPL", "MSFT", "GOOGL", "TSLA"]
        
        # AAPL: insider_cluster=4 + rsi_oversold=2 + above_ma20=2 = 8
        aapl_data = create_test_data(
            "AAPL", price=150.0, ma20=140.0, rsi=35.0, insider_cluster=0.8
        )
        
        # MSFT: above_ma20=2 + news_positive=1 = 3
        msft_data = create_test_data(
            "MSFT", price=300.0, ma20=280.0, sentiment=0.5
        )
        
        # GOOGL: rsi_overbought=-2 + insider_net_sell=-2 = -4
        googl_data = create_test_data(
            "GOOGL", price=120.0, ma20=125.0, rsi=75.0, insider_net=-1000
        )
        
        # TSLA: above_ma20=2 + macd_golden_cross=1 + volume_expansion=1 = 4
        tsla_data = create_test_data(
            "TSLA", price=250.0, ma20=240.0, macd_signal="golden_cross",
            volume_ratio=1.5
        )
        
        def mock_collect(symbol):
            return {"AAPL": aapl_data, "MSFT": msft_data,
                    "GOOGL": googl_data, "TSLA": tsla_data}.get(symbol)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON,
            positions=[]
        )
        
        # top_n 不再截断 — 返回全量通过门槛的候选 (4个)
        assert len(results) == 4
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)
        
        # AAPL (8) > TSLA (4) > MSFT (3)
        assert results[0].symbol == "AAPL"
        assert results[0].score == 8
        assert results[1].symbol == "TSLA"
        assert results[1].score == 4
        assert results[2].symbol == "MSFT"
        assert results[2].score == 3
    
    def test_screen_exclude_held_positions(self, screener, mock_data_collector):
        """测试排除已持仓股票"""
        watchlist = ["AAPL", "MSFT", "GOOGL"]
        
        def mock_collect(symbol):
            return create_test_data(symbol, price=100.0, ma20=90.0)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        positions = [{"symbol": "MSFT", "quantity": 100, "avg_cost": 280.0}]
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=positions
        )
        
        symbols = [r.symbol for r in results]
        assert "MSFT" not in symbols
        assert len(results) == 2
    
    def test_screen_exclude_earnings_imminent(self, screener, mock_data_collector):
        """测试排除即将发布财报的股票 (< 3天)"""
        watchlist = ["AAPL", "MSFT", "GOOGL"]
        
        def mock_collect(symbol):
            if symbol == "GOOGL":
                return create_test_data(symbol, days_until_earnings=2)
            return create_test_data(symbol, price=100.0, ma20=90.0)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        symbols = [r.symbol for r in results]
        assert "GOOGL" not in symbols
        assert len(results) == 2
    
    def test_screen_regime_no_truncation(self, screener, mock_data_collector):
        """测试不同市场环境下: top_n 不再截断, 返回全量通过门槛的候选. NO_TRADE 仍返回空."""
        watchlist = [f"STOCK{i}" for i in range(20)]
        
        def mock_collect(symbol):
            return create_test_data(symbol, price=100.0, ma20=90.0)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        # RISK_ON / CAUTIOUS / RISK_OFF: 全量返回 (20个, 不截断)
        assert len(screener.screen(watchlist, MarketRegime.RISK_ON, [])) == 20
        assert len(screener.screen(watchlist, MarketRegime.CAUTIOUS, [])) == 20
        assert len(screener.screen(watchlist, MarketRegime.RISK_OFF, [])) == 20
        
        # NO_TRADE: 仍然返回空
        assert len(screener.screen(watchlist, MarketRegime.NO_TRADE, [])) == 0
    
    def test_screen_negative_factors(self, screener, mock_data_collector):
        """测试负向因子扣分 (earnings>=3天, 不被排除)"""
        watchlist = ["BAD_STOCK"]
        
        bad_data = create_test_data(
            "BAD_STOCK",
            price=100.0,
            ma20=110.0,        # below MA20 → 不触发 above_ma20
            rsi=75.0,          # rsi_overbought=-2
            sentiment=-0.5,    # news_negative=-1
            insider_net=-2000, # insider_net_sell=-2
            days_until_earnings=5  # >= 3, 不被排除, 不触发 earnings_imminent
        )
        
        mock_data_collector.collect.return_value = bad_data
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        assert len(results) == 1
        # -2 (RSI) - 1 (news) - 2 (insider) = -5
        assert results[0].score == -5
        assert 'rsi_overbought' in results[0].factors
        assert 'news_negative' in results[0].factors
        assert 'insider_net_sell' in results[0].factors
    
    def test_screen_earnings_imminent_as_factor(self, screener, mock_data_collector):
        """earnings_imminent 作为排除规则: < 3 天的股票被排除"""
        watchlist = ["AAPL", "GOOGL"]
        
        def mock_collect(symbol):
            if symbol == "GOOGL":
                return create_test_data(symbol, days_until_earnings=1)
            return create_test_data(symbol, price=100.0, ma20=90.0)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        assert len(results) == 1
        assert results[0].symbol == "AAPL"
    
    def test_screen_sector_concentration_penalty(self, screener, mock_data_collector):
        """测试行业集中度惩罚"""
        watchlist = ["AAPL", "MSFT"]
        
        def mock_collect(symbol):
            return create_test_data(
                symbol, price=100.0, ma20=90.0, industry="Technology"
            )
        
        mock_data_collector.collect.side_effect = mock_collect
        
        # 已持仓两个科技股
        positions = [
            {"symbol": "GOOGL", "industry": "Technology"},
            {"symbol": "META", "industry": "Technology"}
        ]
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=positions
        )
        
        for result in results:
            assert 'sector_concentration' in result.factors
            # above_ma20=2, sector_concentration=-3 → score=-1
            assert result.score == -1
    
    def test_screen_data_collection_failure(self, screener, mock_data_collector):
        """测试数据收集失败的情况"""
        watchlist = ["AAPL", "BAD_STOCK"]
        
        def mock_collect(symbol):
            if symbol == "BAD_STOCK":
                raise Exception("Data collection failed")
            return create_test_data(symbol, price=100.0, ma20=90.0)
        
        mock_data_collector.collect.side_effect = mock_collect
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        symbols = [r.symbol for r in results]
        assert "BAD_STOCK" not in symbols
        assert len(results) == 1
    
    def test_screen_positive_factors(self, screener, mock_data_collector):
        """测试所有正向因子 (insider 互斥: cluster 优先于 net_buy)"""
        watchlist = ["PERFECT_STOCK"]
        
        perfect_data = create_test_data(
            "PERFECT_STOCK",
            price=150.0,           # above_ma20=+2
            ma20=140.0,
            rsi=35.0,             # rsi_oversold=+2
            macd_signal="golden_cross",  # macd_golden_cross=+1
            volume_ratio=1.5,     # volume_expansion=+1
            sentiment=0.6,        # news_positive=+1
            insider_cluster=0.8,  # insider_cluster_buy=+4 (优先, 互斥 net_buy)
            insider_net=5000,     # 不触发 (被 cluster 互斥)
            beat_rate=0.75,       # earnings_beat_streak=+1
            pe=None               # 不触发 pe_below_industry
        )
        
        mock_data_collector.collect.return_value = perfect_data
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        assert len(results) == 1
        # 4+2+2+1+1+1+1 = 12 (insider_cluster 与 insider_net 互斥)
        assert results[0].score == 12
        
        factors = results[0].factors
        assert 'insider_cluster_buy' in factors
        assert 'insider_net_buy' not in factors  # 被互斥
        assert 'above_ma20' in factors
        assert 'rsi_oversold' in factors
        assert 'macd_golden_cross' in factors
        assert 'volume_expansion' in factors
        assert 'news_positive' in factors
        assert 'earnings_beat_streak' in factors
    
    def test_screen_insider_net_buy_without_cluster(self, screener, mock_data_collector):
        """测试 insider_net_buy 在无 cluster 时独立触发"""
        watchlist = ["STOCK1"]
        
        data = create_test_data(
            "STOCK1",
            insider_cluster=0.2,  # < 0.5, 不触发 cluster
            insider_net=3000,     # > 0, 触发 net_buy
        )
        
        mock_data_collector.collect.return_value = data
        
        results = screener.screen(
            watchlist=watchlist, regime=MarketRegime.RISK_ON, positions=[]
        )
        
        assert len(results) == 1
        assert 'insider_net_buy' in results[0].factors
        assert 'insider_cluster_buy' not in results[0].factors
        # above_ma20=2 + insider_net_buy=2 + earnings_beat=1 = 5
        # (pe=None, so no pe_below; beat_rate=0.5, NOT > 0.5, so no earnings_beat)
        # Actually beat_rate default is 0.5, condition is > 0.5, so no trigger
        # above_ma20=2 (100>95) + insider_net_buy=2 = 4
        assert results[0].score == 4
    
    def test_screen_empty_watchlist(self, screener):
        """测试空 watchlist"""
        results = screener.screen([], MarketRegime.RISK_ON, [])
        assert len(results) == 0
    
    def test_screen_all_held(self, screener, mock_data_collector):
        """测试所有股票都已持仓"""
        watchlist = ["AAPL", "MSFT"]
        positions = [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
        
        results = screener.screen(watchlist, MarketRegime.RISK_ON, positions)
        assert len(results) == 0
    
    def test_screen_no_trade_regime(self, screener):
        """测试 NO_TRADE 环境直接返回空"""
        results = screener.screen(
            ["AAPL"], MarketRegime.NO_TRADE, []
        )
        assert len(results) == 0


class TestScreenResult:
    """测试 ScreenResult 数据类"""
    
    def test_screen_result_creation(self):
        data = create_test_data("AAPL")
        result = ScreenResult(
            symbol="AAPL", score=10,
            factors={"above_ma20": {"weight": 2, "value": 150.0}},
            data=data
        )
        assert result.symbol == "AAPL"
        assert result.score == 10
    
    def test_screen_result_repr(self):
        data = create_test_data("AAPL")
        result = ScreenResult(symbol="AAPL", score=10, factors={}, data=data)
        assert "AAPL" in repr(result)
        assert "score=10" in repr(result)
