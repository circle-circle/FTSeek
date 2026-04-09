"""
统一日志模块

提供跨日志源的统一查询接口:
    from app.logging import get_unified_logger

    logger = get_unified_logger()

    # 查询交易决策日志
    decisions = logger.query_decisions(symbol="HK.09992", limit=10)

    # 查询失败统计
    failures = logger.get_failure_stats(period='today')

    # 查询执行质量
    execution = logger.get_execution_stats(days=7)

    # 获取完整报告
    report = logger.get_full_report()
"""

from .unified import (
    UnifiedLogger,
    UnifiedLogQuery,
    get_unified_logger,
    reset_unified_logger,
    query_decisions,
    get_failure_stats,
    get_execution_stats,
    get_health_score,
    get_full_report,
    get_summary,
)

__all__ = [
    'UnifiedLogger',
    'UnifiedLogQuery',
    'get_unified_logger',
    'reset_unified_logger',
    'query_decisions',
    'get_failure_stats',
    'get_execution_stats',
    'get_health_score',
    'get_full_report',
    'get_summary',
]
