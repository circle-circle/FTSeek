"""
MomentumScanner - 动量扫描器模块

短期动量催化因素识别，扫描财报行情、生物科技催化、科技动量事件。
"""

import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any


@dataclass
class CatalystOpportunity:
    """催化机会数据类

    Attributes:
        symbol: 股票代码
        name: 股票名称
        catalyst_type: 催化类型 (earnings/biotech/tech_momentum/news)
        momentum_score: 动量评分 (0-100)
        expected_volatility: 预期波动率
        time_window: 时间窗口
        risk_level: 风险等级 (low/medium/high)
        timestamp: 时间戳
    """
    symbol: str
    name: str
    catalyst_type: str
    momentum_score: float
    expected_volatility: float
    time_window: str
    risk_level: str
    timestamp: float


@dataclass
class MomentumReport:
    """动量扫描报告数据类

    Attributes:
        opportunities: 催化机会列表
        scan_time: 扫描时间戳
        market_sentiment: 市场情绪 (bullish/neutral/bearish)
        top_picks: 推荐关注列表 (股票代码)
    """
    opportunities: List[CatalystOpportunity]
    scan_time: float
    market_sentiment: str
    top_picks: List[str]


class MomentumScanner:
    """动量扫描器

    用于扫描和识别短期动量催化因素，包括：
    - 财报行情机会
    - 生物科技催化事件
    - 科技动量事件
    - 重大新闻事件

    Attributes:
        futu_config: 富途 API 配置 (可选)
        _opportunities: 缓存的扫描结果
    """

    def __init__(self, futu_config: Optional[Dict[str, Any]] = None) -> None:
        """初始化动量扫描器

        Args:
            futu_config: 富途 API 配置字典，可选。如果提供，将使用真实 API 数据；
                        否则使用模拟数据。
        """
        self.futu_config = futu_config
        self._opportunities: List[CatalystOpportunity] = []

    def _calculate_momentum_score(
        self,
        price_momentum: float,
        volume_momentum: float,
        catalyst_strength: float,
        market_sentiment_score: float
    ) -> float:
        """计算动量评分

        使用加权公式计算综合动量评分：
        momentum_score = (
            price_momentum * 0.3 +      # 价格动量 (5 日/10 日涨幅)
            volume_momentum * 0.25 +    # 成交量动量 (量比)
            catalyst_strength * 0.30 +  # 催化强度
            market_sentiment * 0.15     # 市场情绪
        )

        Args:
            price_momentum: 价格动量分数 (0-100)
            volume_momentum: 成交量动量分数 (0-100)
            catalyst_strength: 催化强度分数 (0-100)
            market_sentiment_score: 市场情绪分数 (0-100)

        Returns:
            float: 综合动量评分 (0-100)
        """
        momentum_score = (
            price_momentum * 0.3 +
            volume_momentum * 0.25 +
            catalyst_strength * 0.30 +
            market_sentiment_score * 0.15
        )
        # 确保分数在 0-100 范围内
        return max(0.0, min(100.0, momentum_score))

    def _get_market_sentiment(self) -> str:
        """获取当前市场情绪

        Returns:
            str: 市场情绪 ('bullish', 'neutral', 'bearish')
        """
        # 模拟市场情绪 - 实际实现中可从 API 获取
        return "neutral"

    def _get_market_sentiment_score(self) -> float:
        """获取市场情绪分数

        Returns:
            float: 市场情绪分数 (0-100)
        """
        sentiment_map = {
            "bullish": 80.0,
            "neutral": 50.0,
            "bearish": 20.0,
        }
        return sentiment_map.get(self._get_market_sentiment(), 50.0)

    def scan_earnings_plays(self) -> List[CatalystOpportunity]:
        """扫描财报行情机会

        识别即将发布财报或刚发布财报的股票，分析其动量机会。

        Returns:
            List[CatalystOpportunity]: 财报行情机会列表
        """
        opportunities: List[CatalystOpportunity] = []
        current_time = time.time()

        # 模拟财报行情数据 - 实际实现中可从富途 API 获取
        earnings_data = [
            {
                "symbol": "HK.00700",
                "name": "腾讯控股",
                "price_momentum": 75.0,
                "volume_momentum": 80.0,
                "catalyst_strength": 85.0,
                "expected_volatility": 0.15,
                "time_window": "1W",
                "risk_level": "medium",
            },
            {
                "symbol": "HK.09988",
                "name": "阿里巴巴",
                "price_momentum": 65.0,
                "volume_momentum": 70.0,
                "catalyst_strength": 75.0,
                "expected_volatility": 0.18,
                "time_window": "1W",
                "risk_level": "medium",
            },
            {
                "symbol": "US.AAPL",
                "name": "苹果公司",
                "price_momentum": 80.0,
                "volume_momentum": 75.0,
                "catalyst_strength": 90.0,
                "expected_volatility": 0.12,
                "time_window": "3D",
                "risk_level": "low",
            },
        ]

        market_sentiment_score = self._get_market_sentiment_score()

        for data in earnings_data:
            momentum_score = self._calculate_momentum_score(
                price_momentum=data["price_momentum"],
                volume_momentum=data["volume_momentum"],
                catalyst_strength=data["catalyst_strength"],
                market_sentiment_score=market_sentiment_score,
            )

            opportunity = CatalystOpportunity(
                symbol=data["symbol"],
                name=data["name"],
                catalyst_type="earnings",
                momentum_score=momentum_score,
                expected_volatility=data["expected_volatility"],
                time_window=data["time_window"],
                risk_level=data["risk_level"],
                timestamp=current_time,
            )
            opportunities.append(opportunity)

        self._opportunities.extend(opportunities)
        return opportunities

    def scan_biotech_catalysts(self) -> List[CatalystOpportunity]:
        """扫描生物科技催化事件

        识别生物科技公司的 FDA 审批、临床数据发布等催化事件。

        Returns:
            List[CatalystOpportunity]: 生物科技催化机会列表
        """
        opportunities: List[CatalystOpportunity] = []
        current_time = time.time()

        # 模拟生物科技催化数据 - 实际实现中可从富途 API 或新闻源获取
        biotech_data = [
            {
                "symbol": "US.MRNA",
                "name": "Moderna",
                "price_momentum": 70.0,
                "volume_momentum": 85.0,
                "catalyst_strength": 95.0,
                "expected_volatility": 0.35,
                "time_window": "2D",
                "risk_level": "high",
            },
            {
                "symbol": "US.BNTX",
                "name": "BioNTech",
                "price_momentum": 65.0,
                "volume_momentum": 75.0,
                "catalyst_strength": 88.0,
                "expected_volatility": 0.30,
                "time_window": "3D",
                "risk_level": "high",
            },
            {
                "symbol": "US.GILD",
                "name": "吉利德科学",
                "price_momentum": 55.0,
                "volume_momentum": 60.0,
                "catalyst_strength": 70.0,
                "expected_volatility": 0.20,
                "time_window": "1W",
                "risk_level": "medium",
            },
        ]

        market_sentiment_score = self._get_market_sentiment_score()

        for data in biotech_data:
            momentum_score = self._calculate_momentum_score(
                price_momentum=data["price_momentum"],
                volume_momentum=data["volume_momentum"],
                catalyst_strength=data["catalyst_strength"],
                market_sentiment_score=market_sentiment_score,
            )

            opportunity = CatalystOpportunity(
                symbol=data["symbol"],
                name=data["name"],
                catalyst_type="biotech",
                momentum_score=momentum_score,
                expected_volatility=data["expected_volatility"],
                time_window=data["time_window"],
                risk_level=data["risk_level"],
                timestamp=current_time,
            )
            opportunities.append(opportunity)

        self._opportunities.extend(opportunities)
        return opportunities

    def scan_tech_momentum(self) -> List[CatalystOpportunity]:
        """扫描科技动量事件

        识别科技股的产品发布、行业趋势等动量事件。

        Returns:
            List[CatalystOpportunity]: 科技动量机会列表
        """
        opportunities: List[CatalystOpportunity] = []
        current_time = time.time()

        # 模拟科技动量数据 - 实际实现中可从富途 API 或新闻源获取
        tech_data = [
            {
                "symbol": "US.NVDA",
                "name": "英伟达",
                "price_momentum": 90.0,
                "volume_momentum": 85.0,
                "catalyst_strength": 92.0,
                "expected_volatility": 0.25,
                "time_window": "1W",
                "risk_level": "medium",
            },
            {
                "symbol": "US.TSLA",
                "name": "特斯拉",
                "price_momentum": 75.0,
                "volume_momentum": 80.0,
                "catalyst_strength": 78.0,
                "expected_volatility": 0.30,
                "time_window": "3D",
                "risk_level": "high",
            },
            {
                "symbol": "US.MSFT",
                "name": "微软",
                "price_momentum": 70.0,
                "volume_momentum": 65.0,
                "catalyst_strength": 72.0,
                "expected_volatility": 0.15,
                "time_window": "1W",
                "risk_level": "low",
            },
        ]

        market_sentiment_score = self._get_market_sentiment_score()

        for data in tech_data:
            momentum_score = self._calculate_momentum_score(
                price_momentum=data["price_momentum"],
                volume_momentum=data["volume_momentum"],
                catalyst_strength=data["catalyst_strength"],
                market_sentiment_score=market_sentiment_score,
            )

            opportunity = CatalystOpportunity(
                symbol=data["symbol"],
                name=data["name"],
                catalyst_type="tech_momentum",
                momentum_score=momentum_score,
                expected_volatility=data["expected_volatility"],
                time_window=data["time_window"],
                risk_level=data["risk_level"],
                timestamp=current_time,
            )
            opportunities.append(opportunity)

        self._opportunities.extend(opportunities)
        return opportunities

    def scan_all(self) -> MomentumReport:
        """执行全面扫描，生成报告

        扫描所有类型的催化事件（财报、生物科技、科技动量），
        生成综合动量扫描报告。

        Returns:
            MomentumReport: 动量扫描报告，包含所有机会和推荐列表
        """
        # 重置机会列表
        self._opportunities = []

        # 执行所有扫描
        self.scan_earnings_plays()
        self.scan_biotech_catalysts()
        self.scan_tech_momentum()

        # 按动量评分降序排序
        sorted_opportunities = sorted(
            self._opportunities,
            key=lambda x: x.momentum_score,
            reverse=True,
        )

        # 获取市场情绪
        market_sentiment = self._get_market_sentiment()

        # 生成 Top 推荐列表 (Top 10)
        top_picks = [opp.symbol for opp in sorted_opportunities[:10]]

        # 生成报告
        report = MomentumReport(
            opportunities=sorted_opportunities,
            scan_time=time.time(),
            market_sentiment=market_sentiment,
            top_picks=top_picks,
        )

        return report

    def get_top_picks(self, limit: int = 10) -> List[CatalystOpportunity]:
        """获取 Top N 推荐机会

        从已扫描的结果中获取动量评分最高的 N 个机会。
        如果尚未执行扫描，则先执行全面扫描。

        Args:
            limit: 返回的机会数量上限，默认 10

        Returns:
            List[CatalystOpportunity]: Top N 推荐机会列表
        """
        # 如果尚未扫描，先执行全面扫描
        if not self._opportunities:
            self.scan_all()

        # 按动量评分降序排序
        sorted_opportunities = sorted(
            self._opportunities,
            key=lambda x: x.momentum_score,
            reverse=True,
        )

        # 返回 Top N
        return sorted_opportunities[:limit]
