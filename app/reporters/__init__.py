"""
FTSeek 报告生成模块

提供周度、月度绩效报告生成和推送功能
"""
from app.reporters.weekly_report import (
    WeeklyReport,
    WeeklyReportGenerator,
    get_reporter as get_weekly_reporter,
    generate_weekly_report,
)
from app.reporters.monthly_report import (
    MonthlyReport,
    MonthlyReportGenerator,
    get_reporter as get_monthly_reporter,
    generate_monthly_report,
)

__all__ = [
    # 周报
    'WeeklyReport',
    'WeeklyReportGenerator',
    'get_weekly_reporter',
    'generate_weekly_report',
    
    # 月报
    'MonthlyReport',
    'MonthlyReportGenerator',
    'get_monthly_reporter',
    'generate_monthly_report',
]
