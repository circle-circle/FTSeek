#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 指标采集器

功能：
1. 采集关键系统指标 (CPU/内存/交易成功率/OpenD 连接状态等)
2. 支持自定义指标注册
3. 指标持久化和查询
4. Prometheus 格式导出 (可选)

预期收益：
- 故障发现时间 30 分钟 → 2 分钟
- 5+ 核心指标实时监控
"""

import time
import json
import threading
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from enum import Enum
import logging

# 导入统一日志配置
from ..core.logging_config import setup_logging, trace_context

logger = setup_logging("monitoring.metrics")


# ============================================================
# 指标类型定义
# ============================================================
class MetricType(Enum):
    """指标类型"""
    GAUGE = "gauge"           # 瞬时值 (如 CPU 使用率)
    COUNTER = "counter"       # 累计值 (如交易总数)
    HISTOGRAM = "histogram"   # 分布值 (如请求延迟)
    STATUS = "status"         # 状态值 (如连接状态)


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MetricDefinition:
    """指标定义"""
    name: str
    type: MetricType
    description: str
    unit: str = ""
    labels: Dict[str, str] = field(default_factory=dict)
    alert_threshold: Optional[float] = None
    alert_level: AlertLevel = AlertLevel.WARNING


@dataclass
class MetricValue:
    """指标值"""
    name: str
    value: Any
    timestamp: str
    labels: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 核心指标定义
# ============================================================
CORE_METRICS = [
    # 系统资源指标
    MetricDefinition(
        name="system_cpu_percent",
        type=MetricType.GAUGE,
        description="CPU 使用率",
        unit="%",
        alert_threshold=90.0,
        alert_level=AlertLevel.WARNING
    ),
    MetricDefinition(
        name="system_memory_percent",
        type=MetricType.GAUGE,
        description="内存使用率",
        unit="%",
        alert_threshold=85.0,
        alert_level=AlertLevel.WARNING
    ),
    MetricDefinition(
        name="system_disk_percent",
        type=MetricType.GAUGE,
        description="磁盘使用率",
        unit="%",
        alert_threshold=90.0,
        alert_level=AlertLevel.WARNING
    ),
    
    # OpenD 连接状态
    MetricDefinition(
        name="opend_connection_status",
        type=MetricType.STATUS,
        description="OpenD 连接状态 (1=连接，0=断开)",
        unit="",
        alert_threshold=0.5,
        alert_level=AlertLevel.CRITICAL
    ),
    MetricDefinition(
        name="opend_disconnect_count",
        type=MetricType.COUNTER,
        description="OpenD 断线次数",
        unit="次"
    ),
    
    # 交易指标
    MetricDefinition(
        name="trade_success_count",
        type=MetricType.COUNTER,
        description="成功交易次数",
        unit="次"
    ),
    MetricDefinition(
        name="trade_fail_count",
        type=MetricType.COUNTER,
        description="失败交易次数",
        unit="次"
    ),
    MetricDefinition(
        name="trade_success_rate",
        type=MetricType.GAUGE,
        description="交易成功率",
        unit="%",
        alert_threshold=90.0,
        alert_level=AlertLevel.WARNING
    ),
    MetricDefinition(
        name="trade_total_amount",
        type=MetricType.COUNTER,
        description="交易总金额",
        unit="HKD"
    ),
    
    # 策略指标
    MetricDefinition(
        name="signal_count",
        type=MetricType.COUNTER,
        description="信号生成次数",
        unit="次"
    ),
    MetricDefinition(
        name="signal_confidence_avg",
        type=MetricType.GAUGE,
        description="平均信号置信度",
        unit=""
    ),
    
    # 性能指标
    MetricDefinition(
        name="request_latency_ms",
        type=MetricType.HISTOGRAM,
        description="请求延迟",
        unit="ms"
    ),
    MetricDefinition(
        name="requests_per_second",
        type=MetricType.GAUGE,
        description="每秒请求数",
        unit="req/s"
    ),
]


# ============================================================
# 指标采集器
# ============================================================
class MetricsCollector:
    """
    指标采集器
    
    功能：
    1. 自动采集系统指标
    2. 支持自定义指标注册
    3. 指标历史记录
    4. 指标查询和导出
    """
    
    def __init__(self, storage_dir: Path = None, retention_hours: int = 24):
        """
        初始化指标采集器
        
        Args:
            storage_dir: 指标存储目录
            retention_hours: 数据保留时间 (小时)
        """
        self.storage_dir = storage_dir or Path('/opt/ftseek/FTSeek/data/metrics')
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.retention_hours = retention_hours
        
        # 指标定义注册表
        self._metrics_defs: Dict[str, MetricDefinition] = {
            m.name: m for m in CORE_METRICS
        }
        
        # 当前指标值
        self._current_values: Dict[str, MetricValue] = {}
        
        # 指标历史记录 (内存缓存)
        self._history: Dict[str, List[MetricValue]] = defaultdict(list)
        
        # 采集线程
        self._running = False
        self._collect_thread: Optional[threading.Thread] = None
        self._collect_interval = 10  # 采集间隔 (秒)
        
        # 回调函数
        self._alert_callbacks: List[Callable] = []
        
        # 锁
        self._lock = threading.RLock()
        
        # 启动时间
        self.start_time = datetime.now()
        
        logger.info(f"指标采集器初始化完成 [storage={self.storage_dir}]")
    
    # ============================================================
    # 指标注册
    # ============================================================
    
    def register_metric(self, definition: MetricDefinition):
        """注册自定义指标"""
        with self._lock:
            self._metrics_defs[definition.name] = definition
            logger.info(f"注册指标：{definition.name}")
    
    def get_metric_definition(self, name: str) -> Optional[MetricDefinition]:
        """获取指标定义"""
        return self._metrics_defs.get(name)
    
    # ============================================================
    # 指标采集
    # ============================================================
    
    def collect_system_metrics(self):
        """采集系统指标"""
        try:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=1)
            self._set_metric("system_cpu_percent", cpu_percent)
            
            # 内存
            memory = psutil.virtual_memory()
            self._set_metric("system_memory_percent", memory.percent)
            
            # 磁盘
            disk = psutil.disk_usage('/')
            self._set_metric("system_disk_percent", disk.percent)
            
        except Exception as e:
            logger.error(f"采集系统指标失败：{e}")
    
    def collect_opend_metrics(self, connection_status: bool = None, disconnect_count: int = None):
        """
        采集 OpenD 连接指标
        
        Args:
            connection_status: 连接状态 (True=连接，False=断开)
            disconnect_count: 断线次数
        """
        if connection_status is not None:
            self._set_metric("opend_connection_status", 1 if connection_status else 0)
        
        if disconnect_count is not None:
            # 获取当前值并累加
            current = self._current_values.get("opend_disconnect_count")
            if current:
                # 只在增加时更新
                if disconnect_count > current.value:
                    self._set_metric("opend_disconnect_count", disconnect_count)
            else:
                self._set_metric("opend_disconnect_count", disconnect_count)
    
    def collect_trade_metrics(
        self,
        success: bool = None,
        amount: float = None,
        code: str = None
    ):
        """
        采集交易指标
        
        Args:
            success: 交易是否成功
            amount: 交易金额
            code: 股票代码
        """
        if success is not None:
            if success:
                self._increment_counter("trade_success_count")
            else:
                self._increment_counter("trade_fail_count")
            
            # 计算成功率
            success_count = self._get_counter_value("trade_success_count", 0)
            fail_count = self._get_counter_value("trade_fail_count", 0)
            total = success_count + fail_count
            if total > 0:
                success_rate = (success_count / total) * 100
                self._set_metric("trade_success_rate", round(success_rate, 2))
        
        if amount is not None:
            self._increment_counter("trade_total_amount", amount)
    
    def collect_signal_metrics(self, confidence: float = None):
        """
        采集策略信号指标
        
        Args:
            confidence: 信号置信度
        """
        self._increment_counter("signal_count")
        
        if confidence is not None:
            # 计算平均置信度
            current_avg = self._get_gauge_value("signal_confidence_avg", 0)
            count = self._get_counter_value("signal_count", 1)
            new_avg = ((current_avg * (count - 1)) + confidence) / count
            self._set_metric("signal_confidence_avg", round(new_avg, 3))
    
    def collect_latency_metric(self, operation: str, latency_ms: float):
        """
        采集延迟指标
        
        Args:
            operation: 操作名称
            latency_ms: 延迟 (毫秒)
        """
        # 记录到历史记录
        self._record_histogram("request_latency_ms", latency_ms, labels={"operation": operation})
    
    # ============================================================
    # 指标存储
    # ============================================================
    
    def _set_metric(self, name: str, value: Any, labels: Dict[str, str] = None):
        """设置指标值"""
        with self._lock:
            timestamp = datetime.now().isoformat()
            metric_value = MetricValue(
                name=name,
                value=value,
                timestamp=timestamp,
                labels=labels or {}
            )
            
            self._current_values[name] = metric_value
            self._history[name].append(metric_value)
            
            # 检查告警阈值
            self._check_alert_threshold(name, value)
    
    def _increment_counter(self, name: str, increment: float = 1):
        """增加计数器"""
        with self._lock:
            current = self._get_counter_value(name, 0)
            self._set_metric(name, current + increment)
    
    def _get_counter_value(self, name: str, default: float = 0) -> float:
        """获取计数器值"""
        if name in self._current_values:
            return float(self._current_values[name].value)
        return default
    
    def _get_gauge_value(self, name: str, default: float = 0) -> float:
        """获取 Gauge 值"""
        if name in self._current_values:
            return float(self._current_values[name].value)
        return default
    
    def _record_histogram(self, name: str, value: float, labels: Dict[str, str] = None):
        """记录直方图数据"""
        with self._lock:
            timestamp = datetime.now().isoformat()
            metric_value = MetricValue(
                name=name,
                value=value,
                timestamp=timestamp,
                labels=labels or {}
            )
            self._history[name].append(metric_value)
            
            # 限制历史记录数量
            if len(self._history[name]) > 1000:
                self._history[name] = self._history[name][-1000:]
    
    def _check_alert_threshold(self, name: str, value: Any):
        """检查告警阈值"""
        definition = self._metrics_defs.get(name)
        if not definition or definition.alert_threshold is None:
            return
        
        try:
            value_float = float(value)
            if value_float >= definition.alert_threshold:
                logger.warning(f"指标告警：{name}={value_float} (阈值={definition.alert_threshold})")
                self._trigger_alert(definition, value_float)
        except (TypeError, ValueError):
            pass
    
    def _trigger_alert(self, definition: MetricDefinition, value: float):
        """触发告警"""
        alert_data = {
            "metric": definition.name,
            "description": definition.description,
            "value": value,
            "threshold": definition.alert_threshold,
            "level": definition.alert_level.value,
            "timestamp": datetime.now().isoformat()
        }
        
        for callback in self._alert_callbacks:
            try:
                callback(alert_data)
            except Exception as e:
                logger.error(f"告警回调失败：{e}")
    
    def register_alert_callback(self, callback: Callable):
        """注册告警回调函数"""
        self._alert_callbacks.append(callback)
    
    # ============================================================
    # 指标查询
    # ============================================================
    
    def get_current_value(self, name: str) -> Optional[Any]:
        """获取当前指标值"""
        if name in self._current_values:
            return self._current_values[name].value
        return None
    
    def get_all_current(self) -> Dict[str, Any]:
        """获取所有当前指标值"""
        with self._lock:
            return {
                name: mv.value 
                for name, mv in self._current_values.items()
            }
    
    def get_history(
        self,
        name: str,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """获取指标历史"""
        with self._lock:
            history = self._history.get(name, [])
            
            if start_time:
                history = [
                    h for h in history 
                    if datetime.fromisoformat(h.timestamp) >= start_time
                ]
            
            if end_time:
                history = [
                    h for h in history 
                    if datetime.fromisoformat(h.timestamp) <= end_time
                ]
            
            return [h.to_dict() for h in history[-limit:]]
    
    def get_stats(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指标统计"""
        with self._lock:
            history = self._history.get(name, [])
            if not history:
                return None
            
            values = [h.value for h in history if isinstance(h.value, (int, float))]
            if not values:
                return None
            
            return {
                "count": len(values),
                "current": values[-1] if values else None,
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "last_updated": history[-1].timestamp if history else None
            }
    
    # ============================================================
    # 数据持久化
    # ============================================================
    
    def save_to_file(self):
        """保存当前指标到文件"""
        with self._lock:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = self.storage_dir / f"metrics_{timestamp}.json"
            
            data = {
                "timestamp": datetime.now().isoformat(),
                "metrics": {
                    name: mv.to_dict() 
                    for name, mv in self._current_values.items()
                }
            }
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"指标已保存：{file_path}")
            
            # 清理旧文件
            self._cleanup_old_files()
    
    def _cleanup_old_files(self):
        """清理旧指标文件"""
        cutoff = datetime.now() - timedelta(hours=self.retention_hours)
        
        for file_path in self.storage_dir.glob("metrics_*.json"):
            try:
                # 从文件名解析时间
                timestamp_str = file_path.stem.replace("metrics_", "")
                file_time = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                
                if file_time < cutoff:
                    file_path.unlink()
                    logger.debug(f"清理旧指标文件：{file_path}")
            except Exception as e:
                logger.warning(f"清理文件失败 {file_path}: {e}")
    
    # ============================================================
    # 导出
    # ============================================================
    
    def export_prometheus(self) -> str:
        """导出为 Prometheus 格式"""
        lines = []
        
        with self._lock:
            for name, mv in self._current_values.items():
                definition = self._metrics_defs.get(name)
                if not definition:
                    continue
                
                # HELP
                lines.append(f"# HELP {name} {definition.description}")
                # TYPE
                lines.append(f"# TYPE {name} {definition.type.value}")
                
                # 值
                labels_str = ",".join(f'{k}="{v}"' for k, v in mv.labels.items())
                if labels_str:
                    lines.append(f"{name}{{{labels_str}}} {mv.value}")
                else:
                    lines.append(f"{name} {mv.value}")
        
        return "\n".join(lines)
    
    def export_json(self) -> Dict[str, Any]:
        """导出为 JSON 格式"""
        with self._lock:
            return {
                "timestamp": datetime.now().isoformat(),
                "uptime_seconds": (datetime.now() - self.start_time).total_seconds(),
                "metrics": {
                    name: mv.to_dict()
                    for name, mv in self._current_values.items()
                }
            }
    
    # ============================================================
    # 生命周期
    # ============================================================
    
    def start_auto_collect(self, interval: int = 10):
        """启动自动采集"""
        if self._running:
            logger.warning("自动采集已在运行")
            return
        
        self._running = True
        self._collect_interval = interval
        
        def collect_loop():
            logger.info(f"启动自动指标采集 [interval={interval}s]")
            
            while self._running:
                try:
                    self.collect_system_metrics()
                    self.save_to_file()
                except Exception as e:
                    logger.error(f"自动采集失败：{e}")
                
                time.sleep(interval)
        
        self._collect_thread = threading.Thread(target=collect_loop, daemon=True)
        self._collect_thread.start()
    
    def stop_auto_collect(self):
        """停止自动采集"""
        self._running = False
        if self._collect_thread:
            self._collect_thread.join(timeout=5)
            logger.info("自动指标采集已停止")
    
    def get_status(self) -> Dict[str, Any]:
        """获取采集器状态"""
        return {
            "running": self._running,
            "collect_interval": self._collect_interval,
            "metrics_count": len(self._current_values),
            "uptime_seconds": (datetime.now() - self.start_time).total_seconds(),
            "storage_dir": str(self.storage_dir)
        }


# ============================================================
# 全局单例
# ============================================================
_collector: Optional[MetricsCollector] = None


def get_collector() -> MetricsCollector:
    """获取全局指标采集器实例"""
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


def init_collector(storage_dir: Path = None) -> MetricsCollector:
    """初始化全局指标采集器"""
    global _collector
    _collector = MetricsCollector(storage_dir)
    return _collector


# ============================================================
# 便捷函数
# ============================================================
def record_trade(success: bool, amount: float = None, code: str = None):
    """记录交易指标"""
    get_collector().collect_trade_metrics(success, amount, code)


def record_signal(confidence: float):
    """记录信号指标"""
    get_collector().collect_signal_metrics(confidence)


def record_latency(operation: str, latency_ms: float):
    """记录延迟指标"""
    get_collector().collect_latency_metric(operation, latency_ms)


def update_opend_status(connected: bool):
    """更新 OpenD 连接状态"""
    get_collector().collect_opend_metrics(connection_status=connected)


# ============================================================
# 主函数 (测试用)
# ============================================================
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek 指标采集器')
    parser.add_argument('--test', action='store_true', help='测试指标采集')
    parser.add_argument('--export', choices=['json', 'prometheus'], help='导出指标')
    parser.add_argument('--start', action='store_true', help='启动自动采集')
    
    args = parser.parse_args()
    
    collector = get_collector()
    
    if args.test:
        print("\n🧪 测试指标采集:")
        
        # 模拟采集
        collector.collect_system_metrics()
        collector.collect_opend_metrics(connection_status=True)
        collector.collect_trade_metrics(success=True, amount=10000)
        collector.collect_trade_metrics(success=True, amount=15000)
        collector.collect_trade_metrics(success=False)
        collector.collect_signal_metrics(confidence=0.75)
        collector.collect_signal_metrics(confidence=0.82)
        collector.collect_latency_metric("api_request", 120.5)
        
        # 显示当前值
        print("\n📊 当前指标:")
        for name, value in collector.get_all_current().items():
            print(f"  {name}: {value}")
        
        # 显示统计
        print("\n📈 指标统计:")
        for name in collector._current_values.keys():
            stats = collector.get_stats(name)
            if stats:
                print(f"  {name}:")
                print(f"    当前：{stats['current']}, 平均：{stats['avg']:.2f}, 最小：{stats['min']}, 最大：{stats['max']}")
        
        # 保存
        collector.save_to_file()
        print(f"\n✅ 指标已保存到：{collector.storage_dir}")
    
    elif args.export:
        if args.export == 'json':
            print(json.dumps(collector.export_json(), indent=2, ensure_ascii=False))
        elif args.export == 'prometheus':
            print(collector.export_prometheus())
    
    elif args.start:
        print("启动自动采集...")
        collector.start_auto_collect(interval=5)
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            collector.stop_auto_collect()
    
    else:
        # 显示状态
        status = collector.get_status()
        print("📊 指标采集器状态:")
        print(f"  运行中：{status['running']}")
        print(f"  采集间隔：{status['collect_interval']}s")
        print(f"  指标数量：{status['metrics_count']}")
        print(f"  运行时间：{status['uptime_seconds']:.0f}s")
        print(f"  存储目录：{status['storage_dir']}")
