#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
告警服务模块 - 飞书通知

功能:
1. 飞书 webhook 通知
2. 告警模板管理
3. 连续失败检测
4. 告警冷却控制
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger('alert_service')


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"          # 信息
    WARNING = "warning"    # 警告
    ERROR = "error"        # 错误
    CRITICAL = "critical"  # 严重


@dataclass
class AlertEvent:
    """告警事件"""
    event_id: str
    level: AlertLevel
    title: str
    message: str
    source: str
    timestamp: datetime
    details: dict
    notified: bool = False
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'event_id': self.event_id,
            'level': self.level.value,
            'title': self.title,
            'message': self.message,
            'source': self.source,
            'timestamp': self.timestamp.isoformat(),
            'details': self.details,
            'notified': self.notified
        }


class AlertService:
    """
    告警服务
    
    功能:
    1. 发送飞书通知
    2. 管理告警历史
    3. 告警去重和冷却
    """
    
    # 告警模板
    TEMPLATES = {
        'source_failure': {
            'title': '🚨 新闻源故障告警',
            'format': '{source_name} 连续失败 {failure_count} 次',
            'level': AlertLevel.WARNING
        },
        'all_sources_failure': {
            'title': '🔴 所有新闻源不可用',
            'format': '所有 {total_count} 个新闻源均不可用，已触发降级策略',
            'level': AlertLevel.CRITICAL
        },
        'degradation_activated': {
            'title': '⚠️ 降级策略已激活',
            'format': '新闻权重降至 0%，技术权重 60%，现金权重 40%',
            'level': AlertLevel.ERROR
        },
        'service_recovered': {
            'title': '✅ 服务已恢复',
            'format': '{source_name} 已恢复正常',
            'level': AlertLevel.INFO
        }
    }
    
    def __init__(self, webhook_url: Optional[str] = None, config_path: Optional[str] = None):
        """
        初始化告警服务
        
        Args:
            webhook_url: 飞书 webhook URL (可选，也可从环境变量读取)
            config_path: 配置文件路径
        """
        self.webhook_url = webhook_url or os.getenv('FEISHU_WEBHOOK_URL')
        self.config_path = config_path
        self.config = self._load_config()
        
        # 告警历史
        self.alert_history: List[AlertEvent] = []
        self.max_history_length = 100
        
        # 告警冷却
        self.last_alert_time: Dict[str, datetime] = {}
        self.cooldown_minutes = self.config.get('notification', {}).get('cooldown_minutes', 15)
        
        # HTTP 会话
        self.session: Optional[aiohttp.ClientSession] = None
        
        # 统计
        self.stats = {
            'total_alerts': 0,
            'successful_notifications': 0,
            'failed_notifications': 0,
            'suppressed_alerts': 0
        }
        
        logger.info(f"AlertService initialized (webhook: {'configured' if self.webhook_url else 'not configured'})")
    
    def _load_config(self) -> dict:
        """加载配置文件"""
        default_config = {
            'enabled': True,
            'provider': 'feishu',
            'triggers': {
                'source_failure': True,
                'all_sources_failure': True,
                'degradation_activated': True
            },
            'notification': {
                'consecutive_failures_threshold': 2,
                'cooldown_minutes': 15
            }
        }
        
        if self.config_path and os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    alert_config = user_config.get('alert', {})
                    for key, value in alert_config.items():
                        if isinstance(value, dict) and key in default_config:
                            default_config[key].update(value)
                        else:
                            default_config[key] = value
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
        
        return default_config
    
    async def _ensure_session(self):
        """确保 HTTP 会话存在"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(force_close=True))
    
    async def close(self):
        """关闭资源"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    def _generate_event_id(self, source: str, event_type: str) -> str:
        """生成事件 ID"""
        timestamp = int(time.time() / 60)  # 按分钟粒度
        return f"{source}:{event_type}:{timestamp}"
    
    def _is_cooldown_active(self, event_type: str) -> bool:
        """检查是否在冷却期内"""
        if event_type not in self.last_alert_time:
            return False
        
        last_time = self.last_alert_time[event_type]
        cooldown = timedelta(minutes=self.cooldown_minutes)
        return datetime.now() - last_time < cooldown
    
    def _build_feishu_message(self, event: AlertEvent) -> dict:
        """
        构建飞书消息
        
        Args:
            event: 告警事件
            
        Returns:
            dict: 飞书消息格式
        """
        # 颜色映射
        color_map = {
            AlertLevel.INFO: '#00BFFF',
            AlertLevel.WARNING: '#FFA500',
            AlertLevel.ERROR: '#FF4500',
            AlertLevel.CRITICAL: '#DC143C'
        }
        
        # 构建消息卡片
        message = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": event.title
                    },
                    "template": self._get_feishu_template(event.level)
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**告警详情**\n{event.message}"
                        }
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**来源**: {event.source}\n**时间**: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                        }
                    },
                    {
                        "tag": "hr"
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "FTSeek 监控系统"
                            }
                        ]
                    }
                ]
            }
        }
        
        # 添加额外详情
        if event.details:
            detail_text = "\n".join(f"• {k}: {v}" for k, v in event.details.items())
            message["card"]["elements"].insert(1, {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**详细信息**\n{detail_text}"
                }
            })
        
        return message
    
    def _get_feishu_template(self, level: AlertLevel) -> str:
        """获取飞书卡片模板颜色"""
        template_map = {
            AlertLevel.INFO: 'blue',
            AlertLevel.WARNING: 'orange',
            AlertLevel.ERROR: 'red',
            AlertLevel.CRITICAL: 'red'
        }
        return template_map.get(level, 'gray')
    
    async def send_alert(
        self,
        event_type: str,
        title: str,
        message: str,
        source: str = "news_monitor",
        details: Optional[dict] = None,
        force: bool = False
    ) -> bool:
        """
        发送告警
        
        Args:
            event_type: 事件类型 (用于去重)
            title: 告警标题
            message: 告警消息
            source: 告警来源
            details: 额外详情
            force: 是否强制发送 (忽略冷却)
            
        Returns:
            bool: 是否发送成功
        """
        # 检查冷却
        if not force and self._is_cooldown_active(event_type):
            logger.debug(f"Alert suppressed (cooldown): {event_type}")
            self.stats['suppressed_alerts'] += 1
            return False
        
        # 检查 webhook 配置
        if not self.webhook_url:
            logger.warning("Feishu webhook URL not configured")
            return False
        
        # 创建事件
        event = AlertEvent(
            event_id=self._generate_event_id(source, event_type),
            level=self._get_event_level(event_type),
            title=title,
            message=message,
            source=source,
            timestamp=datetime.now(),
            details=details or {}
        )
        
        # 发送通知
        success = await self._send_feishu_notification(event)
        
        if success:
            event.notified = True
            self.last_alert_time[event_type] = datetime.now()
            self.stats['successful_notifications'] += 1
            logger.info(f"Alert sent: {title}")
        else:
            self.stats['failed_notifications'] += 1
            logger.error(f"Failed to send alert: {title}")
        
        # 记录历史
        self.alert_history.append(event)
        if len(self.alert_history) > self.max_history_length:
            self.alert_history.pop(0)
        
        self.stats['total_alerts'] += 1
        return success
    
    def _get_event_level(self, event_type: str) -> AlertLevel:
        """根据事件类型获取告警级别"""
        template = self.TEMPLATES.get(event_type)
        if template:
            return template['level']
        return AlertLevel.WARNING
    
    async def _send_feishu_notification(self, event: AlertEvent) -> bool:
        """
        发送飞书通知
        
        Args:
            event: 告警事件
            
        Returns:
            bool: 是否发送成功
        """
        try:
            await self._ensure_session()
            
            message = self._build_feishu_message(event)
            
            async with self.session.post(
                self.webhook_url,
                json=message,
                headers={'Content-Type': 'application/json'}
            ) as response:
                result = await response.json()
                
                if response.status == 200 and result.get('StatusCode') == 0:
                    return True
                else:
                    logger.warning(f"Feishu API error: {result}")
                    return False
                    
        except asyncio.TimeoutError:
            logger.error("Feishu notification timeout")
            return False
        except Exception as e:
            logger.error(f"Feishu notification failed: {e}")
            return False
    
    async def alert_source_failure(
        self,
        source_name: str,
        failure_count: int,
        error_message: str = ""
    ):
        """
        新闻源故障告警
        
        Args:
            source_name: 新闻源名称
            failure_count: 连续失败次数
            error_message: 错误信息
        """
        template = self.TEMPLATES['source_failure']
        message = template['format'].format(
            source_name=source_name,
            failure_count=failure_count
        )
        
        await self.send_alert(
            event_type=f'source_failure_{source_name}',
            title=template['title'],
            message=message,
            source=source_name,
            details={
                'failure_count': failure_count,
                'error': error_message
            }
        )
    
    async def alert_all_sources_failure(self, total_count: int):
        """
        所有新闻源不可用告警
        
        Args:
            total_count: 新闻源总数
        """
        template = self.TEMPLATES['all_sources_failure']
        message = template['format'].format(total_count=total_count)
        
        await self.send_alert(
            event_type='all_sources_failure',
            title=template['title'],
            message=message,
            source='health_check',
            details={
                'total_sources': total_count,
                'available_sources': 0
            },
            force=True  # 强制发送
        )
    
    async def alert_degradation_activated(self):
        """降级策略激活告警"""
        template = self.TEMPLATES['degradation_activated']
        
        await self.send_alert(
            event_type='degradation_activated',
            title=template['title'],
            message=template['format'],
            source='degradation_policy',
            details={
                'news_weight': '0%',
                'technical_weight': '60%',
                'cash_weight': '40%'
            },
            force=True  # 强制发送
        )
    
    async def alert_service_recovered(self, source_name: str):
        """
        服务恢复告警
        
        Args:
            source_name: 恢复的新闻源名称
        """
        template = self.TEMPLATES['service_recovered']
        message = template['format'].format(source_name=source_name)
        
        await self.send_alert(
            event_type=f'service_recovered_{source_name}',
            title=template['title'],
            message=message,
            source=source_name
        )
    
    def get_alert_history(self, limit: int = 10) -> List[dict]:
        """获取告警历史"""
        return [event.to_dict() for event in self.alert_history[-limit:]]
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self.stats,
            'cooldown_minutes': self.cooldown_minutes,
            'webhook_configured': bool(self.webhook_url)
        }


# 便捷函数
async def send_news_alert(
    alert_type: str,
    message: str,
    details: Optional[dict] = None
) -> bool:
    """
    发送新闻相关告警
    
    Args:
        alert_type: 告警类型
        message: 告警消息
        details: 额外详情
        
    Returns:
        bool: 是否发送成功
    """
    service = AlertService()
    try:
        return await service.send_alert(
            event_type=alert_type,
            title='新闻监控告警',
            message=message,
            details=details
        )
    finally:
        await service.close()


# 测试代码
if __name__ == "__main__":
    async def test():
        service = AlertService()
        
        print("告警服务测试")
        print(f"Webhook 配置：{'已配置' if service.webhook_url else '未配置'}")
        print(f"冷却时间：{service.cooldown_minutes} 分钟")
        
        # 测试告警
        if service.webhook_url:
            print("\n发送测试告警...")
            success = await service.send_alert(
                event_type='test',
                title='🧪 测试告警',
                message='这是 FTSeek 新闻监控的测试告警',
                source='test',
                details={
                    'test_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'version': '1.0.0'
                }
            )
            print(f"发送结果：{'成功' if success else '失败'}")
        else:
            print("\n⚠️ 未配置飞书 webhook，跳过发送测试")
            print("请设置环境变量 FEISHU_WEBHOOK_URL")
        
        # 打印统计
        print("\n统计信息:")
        stats = service.get_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")
        
        await service.close()
    
    asyncio.run(test())
