"""
Catalyst Database - 催化事件数据模块

提供催化事件的核心数据结构和报告类。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List


@dataclass
class Catalyst:
    """催化事件数据类

    表示一个催化事件，包含事件的所有关键信息。

    Attributes:
        symbol: 股票代码
        event_type: 事件类型 (earnings/fda/product/rating/conference)
        event_date: 事件日期
        title: 事件标题
        description: 事件描述
        impact_score: 影响评分 (0-100)
        sector: 所属板块
        confirmed: 是否已确认
    """
    symbol: str
    event_type: str
    event_date: datetime
    title: str
    description: str
    impact_score: float
    sector: str
    confirmed: bool


@dataclass
class CatalystReport:
    """催化事件报告数据类

    包含催化事件扫描的完整报告。

    Attributes:
        upcoming: 即将发生的事件列表
        today: 今日事件列表
        past_due: 已过事件列表
        scan_time: 扫描时间戳
    """
    upcoming: List[Catalyst]
    today: List[Catalyst]
    past_due: List[Catalyst]
    scan_time: float
