#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 健康检查模块

功能:
1. 每 5 分钟检测新闻源 API 可用性
2. 记录 API 状态历史
3. 触发告警条件判断
4. 支持降级策略
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger('health_check')


class HealthStatus(Enum):
    """健康状态枚举"""
    HEALTHY = "healthy"      # 正常
    DEGRADED = "degraded"    # 降级
    UNHEALTHY = "unhealthy"  # 故障
    UNKNOWN = "unknown"      # 未知


@dataclass
class SourceHealth:
    """单个新闻源健康状态"""
    source_id: str
    source_name: str
    status: HealthStatus = HealthStatus.UNKNOWN
    last_check_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    last_failure_time: Optional[datetime] = None
    consecutive_failures: int = 0
    total_checks: int = 0
    successful_checks: int = 0
    failed_checks: int = 0
    avg_response_time_ms: float = 0.0
    error_message: str = ""
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'source_id': self.source_id,
            'source_name': self.source_name,
            'status': self.status.value,
            'last_check_time': self.last_check_time.isoformat() if self.last_check_time else None,
            'last_success_time': self.last_success_time.isoformat() if self.last_success_time else None,
            'last_failure_time': self.last_failure_time.isoformat() if self.last_failure_time else None,
            'consecutive_failures': self.consecutive_failures,
            'total_checks': self.total_checks,
            'successful_checks': self.successful_checks,
            'failed_checks': self.failed_checks,
            'avg_response_time_ms': round(self.avg_response_time_ms, 2),
            'error_message': self.error_message
        }
    
    @property
    def success_rate(self) -> float:
        """计算成功率"""
        if self.total_checks == 0:
            return 0.0
        return self.successful_checks / self.total_checks


@dataclass
class HealthReport:
    """健康检查报告"""
    check_time: datetime
    overall_status: HealthStatus
    sources: Dict[str, SourceHealth]
    available_sources_count: int
    total_sources_count: int
    degradation_active: bool
    alert_triggered: bool
    alert_message: str = ""
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'check_time': self.check_time.isoformat(),
            'overall_status': self.overall_status.value,
            'sources': {k: v.to_dict() for k, v in self.sources.items()},
            'available_sources_count': self.available_sources_count,
            'total_sources_count': self.total_sources_count,
            'degradation_active': self.degradation_active,
            'alert_triggered': self.alert_triggered,
            'alert_message': self.alert_message
        }


class HealthChecker:
    """
    API 健康检查器
    
    功能:
    1. 定期检查新闻源 API 可用性
    2. 记录健康状态历史
    3. 触发告警和降级策略
    """
    
    # 健康检查端点
    HEALTH_ENDPOINTS = {
        'jinshi': 'https://api.jin10.com/v1/news?limit=1',
        'sina': 'https://finance.sina.com.cn/',
        'tencent': 'https://stockapp.finance.qq.com/',
        'cls': 'https://www.cls.cn/'
    }
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化健康检查器
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self.config = self._load_config()
        
        # 健康状态存储
        self.sources_health: Dict[str, SourceHealth] = {}
        self._init_sources_health()
        
        # 历史状态记录 (用于趋势分析)
        self.history: List[HealthReport] = []
        self.max_history_length = 100
        
        # HTTP 会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 告警状态
        self.last_alert_time: Optional[datetime] = None
        self.alert_cooldown_minutes = self.config.get('alert', {}).get('notification', {}).get('cooldown_minutes', 15)
        
        # 降级状态
        self.degradation_active = False
        self.degradation_start_time: Optional[datetime] = None
        
        # 后台检查任务
        self._check_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info("HealthChecker initialized")
    
    def _load_config(self) -> dict:
        """加载配置文件"""
        default_config = {
            'health_check': {
                'enabled': True,
                'interval_seconds': 300,
                'timeout_seconds': 10
            },
            'alert_threshold': {
                'consecutive_failures': 2,
                'sources_unavailable': 3
            },
            'degradation_policy': {
                'enabled': True,
                'triggers': {
                    'all_sources_failed': True,
                    'consecutive_failures': 2
                }
            }
        }
        
        if self.config_path and os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    # 深度合并配置
                    self._merge_config(default_config, user_config)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
        
        return default_config
    
    def _merge_config(self, base: dict, override: dict):
        """深度合并配置字典"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_config(base[key], value)
            else:
                base[key] = value
    
    def _init_sources_health(self):
        """初始化新闻源健康状态"""
        # 默认新闻源列表
        default_sources = {
            'jinshi': {'name': '金十数据'},
            'sina': {'name': '新浪财经'},
            'tencent': {'name': '腾讯财经'},
            'cls': {'name': '财联社'}
        }
        
        sources = self.config.get('sources', default_sources)
        if not sources:
            sources = default_sources
            
        for source_id, source_config in sources.items():
            self.sources_health[source_id] = SourceHealth(
                source_id=source_id,
                source_name=source_config.get('name', source_id) if isinstance(source_config, dict) else source_id
            )
    
    async def _ensure_session(self):
        """确保 HTTP 会话存在"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(force_close=True))
    
    async def close(self):
        """关闭资源"""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def check_source(self, source_id: str) -> SourceHealth:
        """
        检查单个新闻源的健康状态
        
        Args:
            source_id: 新闻源 ID
            
        Returns:
            SourceHealth: 健康状态
        """
        if source_id not in self.sources_health:
            logger.warning(f"Unknown source: {source_id}")
            return SourceHealth(source_id=source_id, source_name=source_id)
        
        health = self.sources_health[source_id]
        endpoint = self.HEALTH_ENDPOINTS.get(source_id)
        
        if not endpoint:
            logger.warning(f"No health endpoint for source: {source_id}")
            health.status = HealthStatus.UNKNOWN
            return health
        
        start_time = time.time()
        health.total_checks += 1
        
        try:
            await self._ensure_session()
            
            async with self.session.get(endpoint, allow_redirects=True) as response:
                response_time_ms = (time.time() - start_time) * 1000
                
                # 更新平均响应时间
                n = health.successful_checks + 1
                health.avg_response_time_ms = (
                    (health.avg_response_time_ms * (n - 1) + response_time_ms) / n
                )
                
                if response.status == 200:
                    health.status = HealthStatus.HEALTHY
                    health.last_success_time = datetime.now()
                    health.consecutive_failures = 0
                    health.successful_checks += 1
                    health.error_message = ""
                    logger.debug(f"Source {source_id} is healthy ({response_time_ms:.0f}ms)")
                else:
                    raise Exception(f"HTTP {response.status}")
                    
        except asyncio.TimeoutError:
            health.status = HealthStatus.UNHEALTHY
            health.last_failure_time = datetime.now()
            health.consecutive_failures += 1
            health.failed_checks += 1
            health.error_message = "Request timeout"
            logger.warning(f"Source {source_id} health check timeout")
            
        except Exception as e:
            health.status = HealthStatus.UNHEALTHY
            health.last_failure_time = datetime.now()
            health.consecutive_failures += 1
            health.failed_checks += 1
            health.error_message = str(e)
            logger.warning(f"Source {source_id} health check failed: {e}")
        
        health.last_check_time = datetime.now()
        return health
    
    async def check_all_sources(self) -> HealthReport:
        """
        检查所有新闻源的健康状态
        
        Returns:
            HealthReport: 健康检查报告
        """
        check_time = datetime.now()
        
        # 并行检查所有源
        tasks = [self.check_source(source_id) for source_id in self.sources_health.keys()]
        await asyncio.gather(*tasks)
        
        # 统计结果
        available_count = sum(
            1 for h in self.sources_health.values() 
            if h.status == HealthStatus.HEALTHY
        )
        total_count = len(self.sources_health)
        
        # 判断整体状态
        alert_threshold = self.config.get('health_check', {}).get('alert_threshold', {})
        consecutive_failures_threshold = alert_threshold.get('consecutive_failures', 2)
        sources_unavailable_threshold = alert_threshold.get('sources_unavailable', 3)
        
        # 检查是否触发告警
        alert_triggered = False
        alert_message = ""
        
        sources_with_consecutive_failures = [
            h for h in self.sources_health.values()
            if h.consecutive_failures >= consecutive_failures_threshold
        ]
        
        if len(sources_with_consecutive_failures) >= sources_unavailable_threshold:
            alert_triggered = True
            alert_message = f"{len(sources_with_consecutive_failures)} 个新闻源连续失败 {consecutive_failures_threshold} 次"
            logger.warning(f"Alert triggered: {alert_message}")
        
        # 检查是否触发降级
        degradation_active = False
        if total_count > 0 and available_count == 0:
            degradation_active = True
            if not self.degradation_active:
                self.degradation_active = True
                self.degradation_start_time = check_time
                logger.warning("Degradation activated: All news sources unavailable")
        else:
            if self.degradation_active:
                self.degradation_active = False
                self.degradation_start_time = None
                logger.info("Degradation deactivated: News sources recovered")
        
        # 确定整体状态
        if available_count == total_count:
            overall_status = HealthStatus.HEALTHY
        elif available_count == 0:
            overall_status = HealthStatus.UNHEALTHY
        else:
            overall_status = HealthStatus.DEGRADED
        
        # 创建报告
        report = HealthReport(
            check_time=check_time,
            overall_status=overall_status,
            sources=dict(self.sources_health),
            available_sources_count=available_count,
            total_sources_count=total_count,
            degradation_active=degradation_active,
            alert_triggered=alert_triggered,
            alert_message=alert_message
        )
        
        # 保存历史记录
        self.history.append(report)
        if len(self.history) > self.max_history_length:
            self.history.pop(0)
        
        logger.info(
            f"Health check complete: {available_count}/{total_count} sources available, "
            f"status={overall_status.value}, degradation={degradation_active}"
        )
        
        return report
    
    def should_alert(self) -> bool:
        """判断是否应该发送告警 (考虑冷却时间)"""
        if self.last_alert_time is None:
            return True
        
        cooldown = timedelta(minutes=self.alert_cooldown_minutes)
        return datetime.now() - self.last_alert_time > cooldown
    
    def record_alert_sent(self):
        """记录告警已发送"""
        self.last_alert_time = datetime.now()
    
    def get_degradation_weights(self) -> dict:
        """获取降级策略的权重配置"""
        if self.degradation_active:
            return self.config.get('degradation_policy', {}).get('actions', {
                'news_weight': 0.0,
                'technical_weight': 0.60,
                'cash_weight': 0.40
            })
        else:
            return {
                'news_weight': 0.25,  # 正常权重
                'technical_weight': 0.50,
                'cash_weight': 0.25
            }
    
    def start_background_check(self, interval_seconds: Optional[int] = None):
        """
        启动后台健康检查
        
        Args:
            interval_seconds: 检查间隔 (秒)，默认从配置读取
        """
        if interval_seconds is None:
            interval_seconds = self.config.get('health_check', {}).get('interval_seconds', 300)
        
        self._running = True
        
        async def check_loop():
            """后台检查循环"""
            logger.info(f"Starting background health check (interval: {interval_seconds}s)")
            while self._running:
                try:
                    await self.check_all_sources()
                except Exception as e:
                    logger.error(f"Health check error: {e}")
                
                # 等待下一次检查
                await asyncio.sleep(interval_seconds)
        
        self._check_task = asyncio.create_task(check_loop())
    
    def get_status_summary(self) -> dict:
        """获取健康状态摘要"""
        return {
            'overall_status': self._get_overall_status().value,
            'available_sources': sum(
                1 for h in self.sources_health.values() 
                if h.status == HealthStatus.HEALTHY
            ),
            'total_sources': len(self.sources_health),
            'degradation_active': self.degradation_active,
            'sources': {
                source_id: {
                    'status': health.status.value,
                    'consecutive_failures': health.consecutive_failures,
                    'success_rate': round(health.success_rate * 100, 1)
                }
                for source_id, health in self.sources_health.items()
            }
        }
    
    def _get_overall_status(self) -> HealthStatus:
        """获取整体健康状态"""
        healthy_count = sum(
            1 for h in self.sources_health.values() 
            if h.status == HealthStatus.HEALTHY
        )
        total = len(self.sources_health)
        
        if healthy_count == total:
            return HealthStatus.HEALTHY
        elif healthy_count == 0:
            return HealthStatus.UNHEALTHY
        else:
            return HealthStatus.DEGRADED


# 便捷函数
async def check_news_sources_health(config_path: Optional[str] = None) -> dict:
    """
    检查新闻源健康状态
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        dict: 健康状态摘要
    """
    checker = HealthChecker(config_path)
    try:
        await checker.check_all_sources()
        return checker.get_status_summary()
    finally:
        await checker.close()


# 测试代码
if __name__ == "__main__":
    async def test():
        checker = HealthChecker('/opt/ftseek/FTSeek/config/news_api_config.json')
        
        print("开始健康检查...")
        report = await checker.check_all_sources()
        
        print(f"\n检查时间：{report.check_time}")
        print(f"整体状态：{report.overall_status.value}")
        print(f"可用源：{report.available_sources_count}/{report.total_sources_count}")
        print(f"降级激活：{report.degradation_active}")
        print(f"告警触发：{report.alert_triggered}")
        if report.alert_message:
            print(f"告警信息：{report.alert_message}")
        
        print("\n各新闻源状态:")
        for source_id, health in report.sources.items():
            print(f"  {health.source_name}:")
            print(f"    状态：{health.status.value}")
            print(f"    连续失败：{health.consecutive_failures}")
            print(f"    成功率：{health.success_rate*100:.1f}%")
            print(f"    平均响应：{health.avg_response_time_ms:.0f}ms")
            if health.error_message:
                print(f"    错误：{health.error_message}")
        
        print("\n降级权重:")
        weights = checker.get_degradation_weights()
        print(f"  新闻权重：{weights.get('news_weight', 0)*100:.0f}%")
        print(f"  技术权重：{weights.get('technical_weight', 0)*100:.0f}%")
        print(f"  现金权重：{weights.get('cash_weight', 0)*100:.0f}%")
        
        await checker.close()
    
    asyncio.run(test())
