"""
监控指标查询 API

提供指标查询接口
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from pathlib import Path

from app.storage.metrics_tables import MetricsTables

logger = logging.getLogger(__name__)


class MetricsAPI:
    """
    监控指标查询 API
    
    提供:
    1. 时间范围查询
    2. 指标类型筛选
    3. 聚合统计
    """
    
    def __init__(self, metrics_tables: MetricsTables):
        """
        初始化指标 API
        
        Args:
            metrics_tables: MetricsTables 实例
        """
        self.metrics_tables = metrics_tables
        logger.info("MetricsAPI 初始化完成")
    
    def get_system_metrics(
        self,
        start_time: str,
        end_time: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取系统指标
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            系统指标字典
        """
        metrics = self.metrics_tables.query_metrics_by_time_range(
            start_time=start_time,
            end_time=end_time,
            metric_type="system",
        )
        
        # 按指标名称分组
        result = {}
        for m in metrics:
            name = m['metric_name']
            if name not in result:
                result[name] = []
            result[name].append({
                'timestamp': m['timestamp'],
                'value': m['metric_value'],
                'unit': m.get('metric_unit'),
            })
        
        return result
    
    def get_trading_metrics(
        self,
        start_time: str,
        end_time: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取交易指标
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            交易指标字典
        """
        metrics = self.metrics_tables.query_metrics_by_time_range(
            start_time=start_time,
            end_time=end_time,
            metric_type="trading",
        )
        
        # 按指标名称分组
        result = {}
        for m in metrics:
            name = m['metric_name']
            if name not in result:
                result[name] = []
            result[name].append({
                'timestamp': m['timestamp'],
                'value': m['metric_value'],
                'unit': m.get('metric_unit'),
            })
        
        return result
    
    def get_agent_metrics(
        self,
        start_time: str,
        end_time: str,
        agent_id: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        获取 Agent 指标
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
            agent_id: Agent ID 筛选
        
        Returns:
            Agent 指标字典
        """
        metrics = self.metrics_tables.query_metrics_by_time_range(
            start_time=start_time,
            end_time=end_time,
            metric_type="agent",
        )
        
        # 按指标名称分组，支持 agent_id 筛选
        result = {}
        for m in metrics:
            labels = m.get('labels', {})
            if agent_id and labels.get('agent_id') != agent_id:
                continue
            
            name = m['metric_name']
            if name not in result:
                result[name] = []
            result[name].append({
                'timestamp': m['timestamp'],
                'value': m['metric_value'],
                'unit': m.get('metric_unit'),
                'labels': labels,
            })
        
        return result
    
    def get_hourly_aggregation(
        self,
        start_hour: str,
        end_hour: str,
        metric_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取小时聚合指标
        
        Args:
            start_hour: 开始小时
            end_hour: 结束小时
            metric_type: 指标类型
        
        Returns:
            聚合指标列表
        """
        return self.metrics_tables.query_hourly_metrics(
            start_hour=start_hour,
            end_hour=end_hour,
            metric_type=metric_type,
        )
    
    def get_latest_system_status(self) -> Dict[str, Any]:
        """
        获取最新系统状态
        
        Returns:
            系统状态字典
        """
        metrics = self.metrics_tables.get_latest_metrics(
            metric_type="system",
            limit_per_metric=1,
        )
        
        status = {}
        for m in metrics:
            status[m['metric_name']] = {
                'value': m['metric_value'],
                'unit': m.get('metric_unit'),
                'timestamp': m['timestamp'],
            }
        
        return status
    
    def get_latest_trading_status(self) -> Dict[str, Any]:
        """
        获取最新交易状态
        
        Returns:
            交易状态字典
        """
        metrics = self.metrics_tables.get_latest_metrics(
            metric_type="trading",
            limit_per_metric=1,
        )
        
        status = {}
        for m in metrics:
            status[m['metric_name']] = {
                'value': m['metric_value'],
                'unit': m.get('metric_unit'),
                'timestamp': m['timestamp'],
            }
        
        return status
    
    def get_metric_summary(
        self,
        start_time: str,
        end_time: str,
    ) -> Dict[str, Any]:
        """
        获取指标摘要
        
        Args:
            start_time: 开始时间
            end_time: 结束时间
        
        Returns:
            指标摘要字典
        """
        stats = self.metrics_tables.get_metric_statistics(
            start_time=start_time,
            end_time=end_time,
        )
        
        # 按类型分组
        summary = {
            'system': {},
            'trading': {},
            'agent': {},
        }
        
        for key, stat in stats.items():
            metric_type = stat['metric_type']
            metric_name = stat['metric_name']
            
            if metric_type in summary:
                summary[metric_type][metric_name] = {
                    'count': stat['count'],
                    'avg': stat['avg_value'],
                    'min': stat['min_value'],
                    'max': stat['max_value'],
                }
        
        return summary
    
    def get_performance_report(
        self,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """
        获取性能报告
        
        Args:
            hours: 报告时长 (小时)
        
        Returns:
            性能报告字典
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        start_str = start_time.isoformat()
        end_str = end_time.isoformat()
        
        # 获取系统指标
        system_metrics = self.get_system_metrics(start_str, end_str)
        
        # 获取交易指标
        trading_metrics = self.get_trading_metrics(start_str, end_str)
        
        # 获取小时聚合
        start_hour = start_time.strftime("%Y-%m-%d %H:00")
        end_hour = end_time.strftime("%Y-%m-%d %H:00")
        hourly_data = self.get_hourly_aggregation(start_hour, end_hour)
        
        # 生成报告
        report = {
            'period': {
                'start': start_str,
                'end': end_str,
                'hours': hours,
            },
            'system': {},
            'trading': {},
            'hourly_trend': hourly_data,
        }
        
        # 系统指标统计
        for name, data in system_metrics.items():
            if data:
                values = [d['value'] for d in data]
                report['system'][name] = {
                    'current': data[-1]['value'] if data else None,
                    'avg': sum(values) / len(values),
                    'min': min(values),
                    'max': max(values),
                }
        
        # 交易指标统计
        for name, data in trading_metrics.items():
            if data:
                values = [d['value'] for d in data]
                report['trading'][name] = {
                    'current': data[-1]['value'] if data else None,
                    'total': sum(values),
                    'avg': sum(values) / len(values),
                }
        
        return report
    
    def get_agent_performance(
        self,
        agent_id: str,
        hours: int = 24,
    ) -> Dict[str, Any]:
        """
        获取 Agent 性能报告
        
        Args:
            agent_id: Agent ID
            hours: 报告时长 (小时)
        
        Returns:
            Agent 性能报告
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        start_str = start_time.isoformat()
        end_str = end_time.isoformat()
        
        # 获取 Agent 指标
        agent_metrics = self.get_agent_metrics(start_str, end_str, agent_id)
        
        report = {
            'agent_id': agent_id,
            'period': {
                'start': start_str,
                'end': end_str,
                'hours': hours,
            },
            'metrics': {},
        }
        
        for name, data in agent_metrics.items():
            if data:
                values = [d['value'] for d in data]
                report['metrics'][name] = {
                    'count': len(values),
                    'avg': sum(values) / len(values),
                    'min': min(values),
                    'max': max(values),
                }
        
        return report
