#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一日志 API (2026-04-06 新增)

整合:
- DecisionLog (决策日志)
- FailureTracker (失败追踪)
- ExecutionMonitor (执行质量监控)

提供统一查询接口:
    from app.logging.unified import get_unified_logger

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

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass

from app.core.decision_log import DecisionLog, DecisionType
from app.logs.failure_tracker import failure_tracker
from app.execution.execution_monitor import get_execution_monitor, ExecutionMonitor

logger = logging.getLogger("ftseek.logging.unified")
logger.propagate = False


@dataclass
class UnifiedLogQuery:
    """统一日志查询参数"""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    symbol: Optional[str] = None
    strategy: Optional[str] = None
    log_type: Optional[str] = None  # decision/failure/execution
    limit: int = 100


class UnifiedLogger:
    """
    统一日志查询器

    提供跨日志源的统一查询接口
    """

    def __init__(
        self,
        decision_log: DecisionLog = None,
        failure_tracker_inst=None,
        execution_monitor: ExecutionMonitor = None
    ):
        """
        初始化统一日志器

        Args:
            decision_log: 决策日志实例
            failure_tracker_inst: 失败追踪器实例
            execution_monitor: 执行质量监控器实例
        """
        self.decision_log = decision_log or DecisionLog()
        self.failure_tracker = failure_tracker_inst if failure_tracker_inst is not None else failure_tracker
        self.execution_monitor = execution_monitor or get_execution_monitor()

    # ==================== 决策日志查询 ====================

    def query_decisions(
        self,
        symbol: str = None,
        start_time: datetime = None,
        end_time: datetime = None,
        decision_type: str = None,
        strategy: str = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        查询决策日志

        Args:
            symbol: 股票代码
            start_time: 开始时间
            end_time: 结束时间
            decision_type: 决策类型 (trade_decision/config_change/system_event)
            strategy: 策略名称
            limit: 返回数量限制

        Returns:
            决策日志列表
        """
        return self.decision_log.query(
            start_time=start_time,
            end_time=end_time,
            decision_type=decision_type,
            symbol=symbol,
            limit=limit
        )

    def get_decision_statistics(self, days: int = 7) -> Dict:
        """
        获取决策统计

        Args:
            days: 统计天数

        Returns:
            统计信息字典
        """
        return self.decision_log.get_statistics()

    def export_decision_log(self, filepath: str, format: str = 'json') -> bool:
        """
        导出决策日志

        Args:
            filepath: 输出文件路径
            format: 导出格式 (json/csv)

        Returns:
            是否成功
        """
        if format == 'json':
            return self.decision_log.export_to_json(filepath)
        elif format == 'csv':
            return self.decision_log.export_to_csv(filepath)
        return False

    # ==================== 失败统计查询 ====================

    def get_failure_stats(self, period: str = 'today') -> Dict:
        """
        获取失败统计

        Args:
            period: 'today' 或 'hour'

        Returns:
            失败统计信息
        """
        return self.failure_tracker.get_statistics(period)

    def get_failure_records(self, code: str = None, limit: int = 100) -> List[Dict]:
        """
        获取失败记录

        Args:
            code: 股票代码
            limit: 返回数量限制

        Returns:
            失败记录列表
        """
        # 从 failure_tracker 的记录中过滤
        records = self.failure_tracker.records
        if code:
            records = [r for r in records if r.code == code]
        return [
            {
                'timestamp': r.timestamp,
                'code': r.code,
                'reason': r.reason,
                'category': r.category,
                'message': r.message,
            }
            for r in records[-limit:]
        ]

    def print_failure_stats(self):
        """打印失败统计"""
        self.failure_tracker.print_statistics()

    # ==================== 执行质量查询 ====================

    def get_execution_stats(self, days: int = 7) -> Dict:
        """
        获取执行质量统计

        Args:
            days: 统计天数

        Returns:
            执行质量统计信息
        """
        return {
            'today': self.execution_monitor.get_today_stats(),
            'slippage': self.execution_monitor.get_slippage_stats(days),
            'reject': self.execution_monitor.get_reject_stats(days),
            'retry': self.execution_monitor.get_retry_stats(days),
        }

    def get_health_score(self) -> float:
        """
        获取执行健康度评分

        Returns:
            健康度评分 (0-100)
        """
        return self.execution_monitor.get_health_score()

    def get_execution_dashboard(self) -> Dict:
        """
        获取执行质量看板数据

        Returns:
            看板数据字典
        """
        return self.execution_monitor.get_dashboard()

    def export_execution_report(self) -> str:
        """
        导出执行质量报告

        Returns:
            报告文本
        """
        return self.execution_monitor.export_report()

    # ==================== 统一查询 ====================

    def query(self, query: UnifiedLogQuery) -> Dict[str, List[Dict]]:
        """
        统一查询所有日志源

        Args:
            query: 查询参数

        Returns:
            {
                'decisions': [...],
                'failures': [...],
                'executions': [...]
            }
        """
        result = {
            'decisions': [],
            'failures': [],
            'executions': [],
        }

        # 查询决策日志
        if query.log_type in (None, 'decision'):
            result['decisions'] = self.decision_log.query(
                start_time=query.start_time,
                end_time=query.end_time,
                symbol=query.symbol,
                limit=query.limit
            )

        # 查询失败记录
        if query.log_type in (None, 'failure'):
            result['failures'] = self.get_failure_records(
                code=query.symbol,
                limit=query.limit
            )

        # 执行质量数据需要通过数据库查询，这里简化处理
        if query.log_type in (None, 'execution'):
            # 执行质量主要通过 dashboard 获取统计数据
            result['executions'] = [self.execution_monitor.get_today_stats()]

        return result

    def get_full_report(self) -> str:
        """
        获取完整日志报告

        Returns:
            报告文本
        """
        lines = [
            "=" * 80,
            "  FTSeek 统一日志报告",
            "=" * 80,
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        # 决策日志统计
        lines.extend([
            "📋 决策日志统计",
            "-" * 40,
        ])
        decision_stats = self.decision_log.get_statistics()
        lines.append(f"总条目数：{decision_stats.get('total_entries', 0)}")
        if decision_stats.get('date_range', {}).get('earliest'):
            lines.append(f"时间范围：{decision_stats['date_range']['earliest']} ~ {decision_stats['date_range']['latest']}")
        if decision_stats.get('by_type'):
            lines.append("按类型:")
            for log_type, count in decision_stats['by_type'].items():
                lines.append(f"  {log_type}: {count}")
        lines.append("")

        # 失败统计
        lines.extend([
            "❌ 失败统计",
            "-" * 40,
        ])
        failure_stats = self.failure_tracker.get_statistics('today')
        lines.append(f"今日总计：{failure_stats.get('total', 0)}次")
        if failure_stats.get('by_reason'):
            for reason, data in failure_stats['by_reason'].items():
                lines.append(f"  {reason}: {data['count']}次 ({data['ratio_pct']:.1f}%)")
        lines.append("")

        # 执行质量
        lines.extend([
            "⚙️ 执行质量",
            "-" * 40,
        ])
        health_score = self.execution_monitor.get_health_score()
        lines.append(f"健康度评分：{health_score:.0f} / 100")
        exec_stats = self.execution_monitor.get_today_stats()
        lines.append(f"今日订单：{exec_stats.get('total_orders', 0)}")
        lines.append(f"成交：{exec_stats.get('filled_orders', 0)}")
        lines.append(f"拒绝：{exec_stats.get('rejected_orders', 0)} ({exec_stats.get('reject_rate', 0)*100:.1f}%)")
        lines.append(f"平均滑点：{exec_stats.get('avg_slippage', 0)*100:.2f}%")
        lines.append(f"平均延迟：{exec_stats.get('avg_latency_ms', 0):.0f}ms")
        lines.append("")

        lines.append("=" * 80)

        return "\n".join(lines)

    def get_summary(self) -> Dict:
        """
        获取日志摘要

        Returns:
            摘要信息字典
        """
        return {
            'timestamp': datetime.now().isoformat(),
            'decisions': self.decision_log.get_statistics(),
            'failures': self.failure_tracker.get_statistics('today'),
            'execution': {
                'health_score': self.execution_monitor.get_health_score(),
                'today': self.execution_monitor.get_today_stats(),
            },
        }


# ==================== 全局单例 ====================

_unified_logger: Optional[UnifiedLogger] = None


def get_unified_logger() -> UnifiedLogger:
    """获取统一日志器实例"""
    global _unified_logger
    if _unified_logger is None:
        _unified_logger = UnifiedLogger()
    return _unified_logger


def reset_unified_logger():
    """重置日志器 (用于测试)"""
    global _unified_logger
    _unified_logger = None


# ==================== 便捷函数 ====================

def query_decisions(**kwargs) -> List[Dict]:
    """便捷函数：查询决策日志"""
    return get_unified_logger().query_decisions(**kwargs)


def get_failure_stats(period: str = 'today') -> Dict:
    """便捷函数：获取失败统计"""
    return get_unified_logger().get_failure_stats(period)


def get_execution_stats(days: int = 7) -> Dict:
    """便捷函数：获取执行质量统计"""
    return get_unified_logger().get_execution_stats(days)


def get_health_score() -> float:
    """便捷函数：获取健康度评分"""
    return get_unified_logger().get_health_score()


def get_full_report() -> str:
    """便捷函数：获取完整报告"""
    return get_unified_logger().get_full_report()


def get_summary() -> Dict:
    """便捷函数：获取日志摘要"""
    return get_unified_logger().get_summary()
