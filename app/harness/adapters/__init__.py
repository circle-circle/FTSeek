"""
Adapter 模块 - 将现有模块适配为 Harness Agent

提供 Adapter 基类和具体实现，用于将现有复盘模块集成到 Harness 框架。
"""

from .base_adapter import BaseAdapter
from .review_adapter import ReviewAdapter
from .risk_adapter import RiskAdapter
from .collector_adapter import CollectorAdapter
from .report_adapter import ReportAdapter

__all__ = [
    "BaseAdapter",
    "ReviewAdapter",
    "RiskAdapter",
    "CollectorAdapter",
    "ReportAdapter",
]
