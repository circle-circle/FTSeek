"""
工作流模块 - 定义多 Agent 协作流程

提供定期复盘、周度复盘等标准工作流。
"""

from .daily_review import DailyReviewWorkflow
from .weekly_review import WeeklyReviewWorkflow

__version__ = "1.0.0"
__all__ = [
    "DailyReviewWorkflow",
    "WeeklyReviewWorkflow",
]
