#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 服务层 (app/services/)

Phase 10 重构：将 DataSubscriber (3794 行) 拆分为独立服务模块

已提取服务:
  - exit_executor.py: 退出执行服务 (止损/止盈/闪崩检测)
  - consensus_executor.py: 共识执行服务 (交易执行/推送/领域事件)
  - kline_processor.py: K 线处理服务 (ADX/ATR/MTF 更新)
  - subscription_manager.py: 订阅管理服务 (订阅/退订/重连)
  - indicator_service.py: 指标计算服务 (ADX/ATR/追踪止损/MTF)
  - worker_threads.py: Worker 线程管理 (Quote/Kline 队列)

目标:
  - DataSubscriber 从 3794 行降至 ~1500 行
  - 每个服务独立可测试
  - 保持热路径性能 (Futu 回调 < 1ms)
"""

from app.services.exit_executor import ExitExecutor
from app.services.consensus_executor import ConsensusExecutor
from app.services.kline_processor import KlineProcessor
from app.services.subscription_manager import SubscriptionManager
from app.services.indicator_service import IndicatorService
from app.services.worker_threads import WorkerManager

__all__ = [
    "ExitExecutor",
    "ConsensusExecutor",
    "KlineProcessor",
    "SubscriptionManager",
    "IndicatorService",
    "WorkerManager",
]
