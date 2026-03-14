"""
CatalystTracker - 催化事件追踪器模块

追踪和分析催化事件，包括财报发布、FDA 审批、产品发布等。
"""

import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from app.v4.data.catalyst_db import Catalyst, CatalystReport


class CatalystTracker:
    """催化事件追踪器

    用于追踪和分析各种催化事件，包括：
    - 财报发布 (earnings)
    - FDA 审批 (fda)
    - 产品发布 (product)
    - 分析师评级变化 (rating)
    - 行业会议 (conference)

    Attributes:
        futu_config: 富途 API 配置 (可选)
        _catalysts: 缓存的催化事件列表
    """

    def __init__(self, futu_config: Optional[Dict[str, Any]] = None) -> None:
        """初始化催化事件追踪器

        Args:
            futu_config: 富途 API 配置字典，可选。如果提供，将使用真实 API 数据；
                        否则使用模拟数据。
        """
        self.futu_config = futu_config
        self._catalysts: List[Catalyst] = []

        # 如果没有提供配置，加载模拟数据
        if not futu_config:
            self._load_mock_data()

    def _load_mock_data(self) -> None:
        """加载模拟催化事件数据"""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        self._catalysts = [
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
            # 更多财报事件
            Catalyst(
                symbol="GOOGL",
                event_type="earnings",
                event_date=today + timedelta(days=4),
                title="Alphabet Q1 财报发布",
                description="Alphabet 公司第一季度财报",
                impact_score=80.0,
                sector="Technology",
                confirmed=True
            ),
            # 更多 FDA 事件
            Catalyst(
                symbol="BNTX",
                event_type="fda",
                event_date=today + timedelta(days=10),
                title="BioNTech FDA 审批",
                description="FDA 对 BioNTech 新疗法的审批",
                impact_score=88.0,
                sector="Healthcare",
                confirmed=True
            ),
        ]

    def calculate_impact_score(
        self,
        event_importance: float,
        historical_volatility: float,
        market_cap_factor: float,
        sector_momentum: float
    ) -> float:
        """计算影响评分

        使用加权公式计算综合影响评分：
        impact_score = (
            event_importance * 0.4 +      # 事件重要性 (40%)
            historical_volatility * 0.3 + # 历史波动率 (30%)
            market_cap_factor * 0.2 +     # 市值因子 (20%)
            sector_momentum * 0.1         # 板块动量 (10%)
        )

        Args:
            event_importance: 事件重要性分数 (0-100)
            historical_volatility: 历史波动率分数 (0-100)
            market_cap_factor: 市值因子分数 (0-100)
            sector_momentum: 板块动量分数 (0-100)

        Returns:
            float: 综合影响评分 (0-100)
        """
        score = (
            event_importance * 0.4 +
            historical_volatility * 0.3 +
            market_cap_factor * 0.2 +
            sector_momentum * 0.1
        )
        # 确保分数在 0-100 范围内
        return max(0.0, min(100.0, score))

    def get_upcoming_catalysts(self, days: int = 7) -> List[Catalyst]:
        """获取未来 N 天的催化事件

        Args:
            days: 未来天数，默认 7 天

        Returns:
            List[Catalyst]: 即将发生的催化事件列表
        """
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = today + timedelta(days=days)

        upcoming = [
            c for c in self._catalysts
            if today <= c.event_date <= end_date
        ]

        # 按影响评分降序排序
        return sorted(upcoming, key=lambda x: x.impact_score, reverse=True)

    def get_today_catalysts(self) -> List[Catalyst]:
        """获取今日催化事件

        Returns:
            List[Catalyst]: 今日催化事件列表
        """
        today = datetime.now().date()

        today_events = [
            c for c in self._catalysts
            if c.event_date.date() == today
        ]

        # 按影响评分降序排序
        return sorted(today_events, key=lambda x: x.impact_score, reverse=True)

    def filter_by_sector(self, sector: str) -> List[Catalyst]:
        """按板块过滤催化事件

        Args:
            sector: 板块名称 (如 "Technology", "Healthcare")

        Returns:
            List[Catalyst]: 指定板块的催化事件列表
        """
        filtered = [
            c for c in self._catalysts
            if c.sector.lower() == sector.lower()
        ]

        # 按影响评分降序排序
        return sorted(filtered, key=lambda x: x.impact_score, reverse=True)

    def filter_by_type(self, event_type: str) -> List[Catalyst]:
        """按事件类型过滤催化事件

        Args:
            event_type: 事件类型 (earnings/fda/product/rating/conference)

        Returns:
            List[Catalyst]: 指定类型的催化事件列表
        """
        filtered = [
            c for c in self._catalysts
            if c.event_type.lower() == event_type.lower()
        ]

        # 按影响评分降序排序
        return sorted(filtered, key=lambda x: x.impact_score, reverse=True)

    def scan_earnings(self) -> List[Catalyst]:
        """扫描财报事件

        Returns:
            List[Catalyst]: 财报事件列表
        """
        return self.filter_by_type("earnings")

    def scan_biotech_fda(self) -> List[Catalyst]:
        """扫描生物科技 FDA 审批事件

        Returns:
            List[Catalyst]: FDA 审批事件列表
        """
        return self.filter_by_type("fda")

    def scan_all(self) -> CatalystReport:
        """全面扫描生成催化事件报告

        扫描所有催化事件并按时间分类。

        Returns:
            CatalystReport: 包含即将发生、今日和已过事件的完整报告
        """
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        upcoming = []
        today_events = []
        past_due = []

        for catalyst in self._catalysts:
            if catalyst.event_date.date() == today.date():
                today_events.append(catalyst)
            elif catalyst.event_date > today:
                upcoming.append(catalyst)
            else:
                past_due.append(catalyst)

        # 按影响评分降序排序
        upcoming.sort(key=lambda x: x.impact_score, reverse=True)
        today_events.sort(key=lambda x: x.impact_score, reverse=True)
        past_due.sort(key=lambda x: x.impact_score, reverse=True)

        return CatalystReport(
            upcoming=upcoming,
            today=today_events,
            past_due=past_due,
            scan_time=time.time()
        )
