"""
Intraday 迁移适配器 — 让新模块可以无缝替换 DataSubscriber

这个适配器桥接新旧模块，使新 intraday 模块可以逐步替换 data_subscriber.py 的功能。

使用方式:
    from app.intraday.migration import create_intraday_from_subscriber
    
    # 从现有 DataSubscriber 创建新 intraday 模块
    coordinator = create_intraday_from_subscriber(data_subscriber)
    
    # 或直接创建
    from app.intraday import IntradayCoordinator
    coordinator = IntradayCoordinator(...)
"""

import logging
from typing import Optional, Any

logger = logging.getLogger("ftseek.intraday.migration")


def create_intraday_from_subscriber(data_subscriber) -> Any:
    """
    从现有 DataSubscriber 创建新的 IntradayCoordinator
    
    这个函数提取 data_subscriber 中的关键组件，注入到新模块中。
    
    Args:
        data_subscriber: 现有的 DataSubscriber 实例
    
    Returns:
        IntradayCoordinator 实例
    """
    from app.intraday.coordinator import IntradayCoordinator
    
    # 提取现有组件
    position_manager = getattr(data_subscriber, 'position_manager', None)
    trade_executor = getattr(data_subscriber, 'trade_executor', None)
    consensus_engine = getattr(data_subscriber, '_consensus', None)
    notifier = getattr(data_subscriber, 'notifier', None)
    
    coordinator = IntradayCoordinator(
        position_manager=position_manager,
        trade_executor=trade_executor,
        consensus_engine=consensus_engine,
        notifier=notifier,
    )
    
    logger.info("IntradayCoordinator 已从 DataSubscriber 创建")
    return coordinator


def migrate_stop_loss_logic(data_subscriber, new_position_manager):
    """
    迁移止损逻辑到新的 IntradayPositionManager
    
    Args:
        data_subscriber: 现有 DataSubscriber
        new_position_manager: 新的 IntradayPositionManager 实例
    """
    # 复制状态
    new_position_manager._stop_loss_cooldown = getattr(data_subscriber, '_stop_loss_cooldown', {})
    new_position_manager._stop_loss_fail_count = getattr(data_subscriber, '_stop_loss_fail_count', {})
    new_position_manager._trailing_stop_cache = getattr(data_subscriber, '_trailing_stop_cache', {})
    new_position_manager._peak_price_cache = getattr(data_subscriber, '_peak_price_cache', {})
    new_position_manager._bad_orders = getattr(data_subscriber, '_bad_orders', {})
    
    logger.info("止损状态已迁移到 IntradayPositionManager")


def migrate_kline_buffers(data_subscriber, new_signal_processor):
    """
    迁移 K 线缓存到新的 SignalProcessor
    
    Args:
        data_subscriber: 现有 DataSubscriber
        new_signal_processor: 新的 SignalProcessor 实例
    """
    # 复制 K 线缓存
    kline_buffers = getattr(data_subscriber, '_kline_buffers_by_period', {})
    if kline_buffers:
        for code, buffers in kline_buffers.items():
            for k_type, buffer in buffers.items():
                new_signal_processor._kline_buffers[code][k_type] = list(buffer.close) if hasattr(buffer, 'close') else []
    
    logger.info("K 线缓存已迁移到 SignalProcessor")


class MigrationStatus:
    """迁移状态追踪"""
    
    def __init__(self):
        self.steps_completed = []
        self.steps_pending = [
            "create_coordinator",
            "migrate_stop_loss",
            "migrate_kline_buffers",
            "migrate_workers",
            "switch_traffic",
        ]
    
    def mark_complete(self, step: str):
        if step in self.steps_pending:
            self.steps_pending.remove(step)
            self.steps_completed.append(step)
            logger.info(f"迁移步骤完成: {step}")
    
    @property
    def is_complete(self) -> bool:
        return len(self.steps_pending) == 0
    
    @property
    def progress(self) -> str:
        total = len(self.steps_completed) + len(self.steps_pending)
        done = len(self.steps_completed)
        return f"{done}/{total} 步骤完成"


# 全局迁移状态
migration_status = MigrationStatus()
