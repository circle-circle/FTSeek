"""监控指标收集器"""
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from pathlib import Path
import logging

from app.storage.metrics_tables import MetricsTables

logger = logging.getLogger(__name__)


class MetricsCollector:
    """
    监控指标收集器
    
    功能:
    1. 收集系统指标 (内存、CPU)
    2. 收集交易指标 (延迟、成功率)
    3. 收集 Agent 指标 (响应时间、投票分布)
    4. 指标持久化 (实时写入 + 小时聚合)
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        初始化指标收集器
        
        Args:
            db_path: 数据库路径，用于持久化
        """
        self.signals: List[Dict] = []
        self.orders: List[Dict] = []
        self.fills: List[Dict] = []
        self.data_source_requests: Dict[str, List[Dict]] = defaultdict(list)
        self.risk_checks: List[Dict] = []
        self.voting_results: List[Dict] = []
        self.circuit_breaker_triggers: List[Dict] = []
        
        # 持久化
        if db_path is None:
            try:
                from app.core.paths import get_trade_records_db
                db_path = str(get_trade_records_db())
            except Exception:
                db_path = "data/ftseek_us.db"  # fallback
        self.db_path = db_path
        self.metrics_tables = MetricsTables(self.db_path) if self.db_path else None
        
        # 上次聚合时间
        self._last_hourly_aggregation = None
        
        logger.info(f"MetricsCollector 初始化完成，db_path={self.db_path}")
    
    def collect_system_metrics(self) -> Dict[str, float]:
        """收集系统指标"""
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage('/').percent,
            "timestamp": datetime.now().isoformat(),
        }
    
    def collect_trading_metrics(self) -> Dict[str, int]:
        """收集交易指标"""
        return {
            "signals_count": len(self.signals),
            "orders_count": len(self.orders),
            "fills_count": len(self.fills),
            "pending_orders": len([o for o in self.orders if o.get("status") == "pending"]),
        }
    
    def collect_data_source_metrics(self) -> Dict[str, any]:
        """收集数据源指标"""
        result = {}
        for source, requests in self.data_source_requests.items():
            if not requests:
                result[source] = {"success_rate": 0, "avg_latency_ms": 0, "total": 0}
                continue
            
            total = len(requests)
            success = sum(1 for r in requests if r.get("success"))
            latencies = [r.get("latency_ms", 0) for r in requests]
            avg_latency = sum(latencies) / len(latencies) if latencies else 0
            
            result[source] = {
                "success_rate": success / total if total > 0 else 0,
                "avg_latency_ms": avg_latency,
                "total": total,
            }
        
        return result
    
    def collect_risk_metrics(self) -> Dict[str, any]:
        """收集风控指标"""
        allowed = sum(1 for r in self.risk_checks if r.get("allowed"))
        rejected = sum(1 for r in self.risk_checks if not r.get("allowed"))
        
        return {
            "risk_checks": len(self.risk_checks),
            "risk_allowed": allowed,
            "risk_rejected": rejected,
            "circuit_breaker_triggers": len(self.circuit_breaker_triggers),
        }
    
    def collect_voting_metrics(self) -> Dict[str, any]:
        """收集投票指标"""
        if not self.voting_results:
            return {"voting_rounds": 0, "avg_consensus": 0, "decisions": {}}
        
        consensuses = [r.get("consensus", 0) for r in self.voting_results]
        avg_consensus = sum(consensuses) / len(consensuses) if consensuses else 0
        
        decisions = defaultdict(int)
        for r in self.voting_results:
            decisions[r.get("decision", "UNKNOWN")] += 1
        
        return {
            "voting_rounds": len(self.voting_results),
            "avg_consensus": avg_consensus,
            "decisions": dict(decisions),
        }
    
    def record_signal(self, signal_id: str, **kwargs):
        """记录信号"""
        self.signals.append({
            "signal_id": signal_id,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    def record_order(self, order_id: str, **kwargs):
        """记录订单"""
        self.orders.append({
            "order_id": order_id,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
            **kwargs
        })
    
    def record_fill(self, order_id: str, quantity: float, **kwargs):
        """记录成交"""
        self.fills.append({
            "order_id": order_id,
            "quantity": quantity,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
        
        # 更新订单状态
        for order in self.orders:
            if order["order_id"] == order_id:
                order["status"] = "filled"
                break
    
    def record_data_source_request(self, source: str, success: bool, latency_ms: float, **kwargs):
        """记录数据源请求"""
        self.data_source_requests[source].append({
            "success": success,
            "latency_ms": latency_ms,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    def record_risk_check(self, signal_id: str, allowed: bool, reason: str = None, **kwargs):
        """记录风控检查"""
        self.risk_checks.append({
            "signal_id": signal_id,
            "allowed": allowed,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    def record_circuit_breaker_trigger(self, source: str, **kwargs):
        """记录熔断触发"""
        self.circuit_breaker_triggers.append({
            "source": source,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    def record_voting_round(self, symbol: str, scores: list, consensus: float, decision: str, **kwargs):
        """记录投票轮次"""
        self.voting_results.append({
            "symbol": symbol,
            "scores": scores,
            "consensus": consensus,
            "decision": decision,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    # ============================================================
    # 持久化方法
    # ============================================================
    
    def persist_system_metrics(self):
        """持久化系统指标"""
        if not self.metrics_tables:
            return
        
        try:
            metrics = self.collect_system_metrics()
            timestamp = datetime.now().isoformat()
            
            batch = [
                {
                    'metric_type': 'system',
                    'metric_name': 'cpu_percent',
                    'metric_value': metrics['cpu_percent'],
                    'metric_unit': '%',
                },
                {
                    'metric_type': 'system',
                    'metric_name': 'memory_percent',
                    'metric_value': metrics['memory_percent'],
                    'metric_unit': '%',
                },
                {
                    'metric_type': 'system',
                    'metric_name': 'disk_percent',
                    'metric_value': metrics['disk_percent'],
                    'metric_unit': '%',
                },
            ]
            
            self.metrics_tables.record_metrics_batch(batch, timestamp)
            logger.debug(f"持久化系统指标：{len(batch)} 条")
        except Exception as e:
            logger.error(f"持久化系统指标失败：{e}")
    
    def persist_trading_metrics(self):
        """持久化交易指标"""
        if not self.metrics_tables:
            return
        
        try:
            metrics = self.collect_trading_metrics()
            timestamp = datetime.now().isoformat()
            
            batch = [
                {
                    'metric_type': 'trading',
                    'metric_name': 'signals_count',
                    'metric_value': metrics['signals_count'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'orders_count',
                    'metric_value': metrics['orders_count'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'fills_count',
                    'metric_value': metrics['fills_count'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'pending_orders',
                    'metric_value': metrics['pending_orders'],
                    'metric_unit': 'count',
                },
            ]
            
            self.metrics_tables.record_metrics_batch(batch, timestamp)
            logger.debug(f"持久化交易指标：{len(batch)} 条")
        except Exception as e:
            logger.error(f"持久化交易指标失败：{e}")
    
    def persist_data_source_metrics(self):
        """持久化数据源指标"""
        if not self.metrics_tables:
            return
        
        try:
            metrics = self.collect_data_source_metrics()
            timestamp = datetime.now().isoformat()
            
            batch = []
            for source, stats in metrics.items():
                batch.append({
                    'metric_type': 'system',
                    'metric_name': f'{source}_success_rate',
                    'metric_value': stats['success_rate'],
                    'metric_unit': 'ratio',
                    'labels': {'source': source},
                })
                batch.append({
                    'metric_type': 'system',
                    'metric_name': f'{source}_avg_latency_ms',
                    'metric_value': stats['avg_latency_ms'],
                    'metric_unit': 'ms',
                    'labels': {'source': source},
                })
            
            if batch:
                self.metrics_tables.record_metrics_batch(batch, timestamp)
                logger.debug(f"持久化数据源指标：{len(batch)} 条")
        except Exception as e:
            logger.error(f"持久化数据源指标失败：{e}")
    
    def persist_risk_metrics(self):
        """持久化风控指标"""
        if not self.metrics_tables:
            return
        
        try:
            metrics = self.collect_risk_metrics()
            timestamp = datetime.now().isoformat()
            
            batch = [
                {
                    'metric_type': 'trading',
                    'metric_name': 'risk_checks',
                    'metric_value': metrics['risk_checks'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'risk_allowed',
                    'metric_value': metrics['risk_allowed'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'risk_rejected',
                    'metric_value': metrics['risk_rejected'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'trading',
                    'metric_name': 'circuit_breaker_triggers',
                    'metric_value': metrics['circuit_breaker_triggers'],
                    'metric_unit': 'count',
                },
            ]
            
            self.metrics_tables.record_metrics_batch(batch, timestamp)
            logger.debug(f"持久化风控指标：{len(batch)} 条")
        except Exception as e:
            logger.error(f"持久化风控指标失败：{e}")
    
    def persist_voting_metrics(self):
        """持久化投票指标"""
        if not self.metrics_tables:
            return
        
        try:
            metrics = self.collect_voting_metrics()
            timestamp = datetime.now().isoformat()
            
            batch = [
                {
                    'metric_type': 'agent',
                    'metric_name': 'voting_rounds',
                    'metric_value': metrics['voting_rounds'],
                    'metric_unit': 'count',
                },
                {
                    'metric_type': 'agent',
                    'metric_name': 'avg_consensus',
                    'metric_value': metrics['avg_consensus'],
                    'metric_unit': 'ratio',
                },
            ]
            
            # 记录各决策类型的数量
            for decision, count in metrics.get('decisions', {}).items():
                batch.append({
                    'metric_type': 'agent',
                    'metric_name': f'decision_{decision}',
                    'metric_value': count,
                    'metric_unit': 'count',
                })
            
            if batch:
                self.metrics_tables.record_metrics_batch(batch, timestamp)
                logger.debug(f"持久化投票指标：{len(batch)} 条")
        except Exception as e:
            logger.error(f"持久化投票指标失败：{e}")
    
    def persist_all_metrics(self):
        """持久化所有指标"""
        self.persist_system_metrics()
        self.persist_trading_metrics()
        self.persist_data_source_metrics()
        self.persist_risk_metrics()
        self.persist_voting_metrics()
    
    def aggregate_hourly_metrics(self):
        """执行小时聚合"""
        if not self.metrics_tables:
            return
        
        try:
            now = datetime.now()
            current_hour = now.strftime("%Y-%m-%d %H:00")
            
            # 避免重复聚合
            if self._last_hourly_aggregation == current_hour:
                return
            
            self.metrics_tables.aggregate_hourly_metrics(current_hour)
            self._last_hourly_aggregation = current_hour
            logger.info(f"完成小时指标聚合：{current_hour}")
        except Exception as e:
            logger.error(f"小时指标聚合失败：{e}")
    
    def should_aggregate_hourly(self) -> bool:
        """检查是否应该执行小时聚合"""
        now = datetime.now()
        current_hour = now.strftime("%Y-%m-%d %H:00")
        
        # 如果是新的小时，需要聚合
        if self._last_hourly_aggregation != current_hour:
            # 确保上一个小时的数据已经完整
            if now.minute >= 5:  # 每小时的第 5 分钟执行聚合
                return True
        
        return False
    
    def cleanup_old_metrics(self, days: int = 30):
        """
        清理旧指标
        
        Args:
            days: 保留天数
        """
        if not self.metrics_tables:
            return
        
        try:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            self.metrics_tables.cleanup_old_metrics(cutoff)
            
            # 小时聚合数据保留更久
            cutoff_hourly = (datetime.now() - timedelta(days=days * 3)).strftime("%Y-%m-%d %H:00")
            self.metrics_tables.cleanup_old_hourly_metrics(cutoff_hourly)
            
            logger.info(f"清理 {days} 天前的旧指标")
        except Exception as e:
            logger.error(f"清理旧指标失败：{e}")
