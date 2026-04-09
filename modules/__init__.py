#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek Modules Package

包含:
- news_monitor: 新闻监控模块 (多源备用)
- health_check: API 健康检查模块
- alert_service: 飞书告警服务
- decision_log: 决策日志系统
"""

from modules.news_monitor import NewsMonitor, NewsItem, NewsAggregation, NewsSource, NewsSentiment, NewsImportance
from modules.health_check import HealthChecker, HealthStatus, SourceHealth, HealthReport
from modules.alert_service import AlertService, AlertLevel, AlertEvent
from modules.decision_log import DecisionLog, DecisionRecord, WeightAllocation, NewsFactor, HkUsFactor, TechnicalFactor

__all__ = [
    # News Monitor
    'NewsMonitor',
    'NewsItem',
    'NewsAggregation',
    'NewsSource',
    'NewsSentiment',
    'NewsImportance',
    # Health Check
    'HealthChecker',
    'HealthStatus',
    'SourceHealth',
    'HealthReport',
    # Alert Service
    'AlertService',
    'AlertLevel',
    'AlertEvent',
    # Decision Log
    'DecisionLog',
    'DecisionRecord',
    'WeightAllocation',
    'NewsFactor',
    'HkUsFactor',
    'TechnicalFactor'
]
