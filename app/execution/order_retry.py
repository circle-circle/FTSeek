#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单重试机制 (Order Retry)

功能:
1. 市价单失败 → 自动切换限价单
2. 限价单未成交 → 自动调整价格
3. 最大重试次数限制 (3 次)
4. 重试间隔控制
"""

import logging
import time
import threading
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger("ftseek.order_retry")
logger.propagate = False  # 防止日志重复


class RetryReason(Enum):
    """重试原因"""
    MARKET_FAILED = "market_failed"       # 市价单失败
    LIMIT_NOT_FILLED = "limit_not_filled" # 限价单未成交
    PARTIAL_FILLED = "partial_filled"     # 部分成交
    PRICE_CHANGED = "price_changed"       # 价格变化过大


@dataclass
class RetryRecord:
    """重试记录"""
    order_id: str
    code: str
    action: str
    attempt: int
    reason: RetryReason
    timestamp: str
    original_price: float
    new_price: Optional[float]
    success: bool
    message: str = ""


class OrderRetryManager:
    """订单重试管理器"""
    
    def __init__(self, max_retries: int = 3, retry_interval: int = 5):
        """
        初始化

        Args:
            max_retries: 最大重试次数
            retry_interval: 重试间隔 (秒)
        """
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.retry_history: Dict[str, List[RetryRecord]] = {}  # order_id -> records
        self._lock = threading.Lock()  # 2026-05-19: 线程安全保护
        logger.info(f"订单重试管理器已初始化 | 最大重试：{max_retries}次 | 间隔：{retry_interval}秒")
    
    def should_retry(self, order_id: str) -> bool:
        """
        判断是否应该重试

        Args:
            order_id: 订单 ID

        Returns:
            bool: 是否应该重试
        """
        with self._lock:
            if order_id not in self.retry_history:
                return True  # 首次尝试

            records = self.retry_history[order_id]
            return len(records) < self.max_retries

    def get_retry_count(self, order_id: str) -> int:
        """获取重试次数"""
        with self._lock:
            if order_id not in self.retry_history:
                return 0
            return len(self.retry_history[order_id])
    
    def record_retry(self, order_id: str, code: str, action: str,
                    reason: RetryReason, original_price: float,
                    new_price: Optional[float], success: bool,
                    message: str = ""):
        """
        记录重试

        Args:
            order_id: 订单 ID
            code: 股票代码
            action: BUY/SELL
            reason: 重试原因
            original_price: 原价格
            new_price: 新价格
            success: 是否成功
            message: 消息
        """
        with self._lock:
            # 2026-05-19: 在锁内计算 attempt，避免竞态
            attempt = len(self.retry_history.get(order_id, [])) + 1

            record = RetryRecord(
                order_id=order_id,
                code=code,
                action=action,
                attempt=attempt,
                reason=reason,
                timestamp=datetime.now().isoformat(),
                original_price=original_price,
                new_price=new_price,
                success=success,
                message=message,
            )

            if order_id not in self.retry_history:
                self.retry_history[order_id] = []

            self.retry_history[order_id].append(record)

        if success:
            logger.info(f"[{code}] 重试成功 (第{record.attempt}次): {reason.value}")
        else:
            logger.warning(f"[{code}] 重试失败 (第{record.attempt}次): {reason.value} - {message}")
    
    def get_next_price(self, order_id: str, code: str, action: str,
                      current_price: float, order_type: str) -> float:
        """
        计算下次重试价格
        
        Args:
            order_id: 订单 ID
            code: 股票代码
            action: BUY/SELL
            current_price: 当前价格
            order_type: 订单类型 (MARKET/LIMIT)
        
        Returns:
            float: 新价格
        """
        retry_count = self.get_retry_count(order_id)
        
        # 防御性检查：确保 current_price 有效
        if current_price is None or current_price <= 0:
            logger.error(f"[{code}] 无效价格：{current_price}，使用最小价格 0.01")
            return 0.01
        
        # 市价单失败 → 切换限价单 (1% 偏移)
        if order_type == 'MARKET':
            if action == 'BUY':
                new_price = current_price * 1.01
            else:
                new_price = current_price * 0.99
        # 限价单未成交 → 调整价格
        elif order_type == 'LIMIT':
            # 每次重试增加 0.5% 偏移，最大不超过 3%
            offset = min(0.01 + (retry_count * 0.005), 0.03)
            
            if action == 'BUY':
                new_price = current_price * (1 + offset)
            else:
                # SELL: 降低价格，但不低于当前价格的 97%
                new_price = max(current_price * (1 - offset), current_price * 0.97)
        else:
            new_price = current_price
        
        # 确保价格为正数
        if new_price <= 0:
            logger.warning(f"[{code}] 计算价格为负或零，修正为 0.01")
            new_price = 0.01
        
        # 港股价格精度处理 (2026-03-19 修复)
        if code.startswith("HK."):
            new_price = self._round_hk_price(new_price, action)
        else:
            new_price = round(new_price, 2)
        
        return new_price
    
    def _round_hk_price(self, price: float, action: str = 'BUY') -> float:
        """
        港股价格精度处理
        
        富途港股价格最小变动单位规则
        """
        # 确定最小变动单位
        if price < 0.25:
            tick = 0.001
        elif price < 0.50:
            tick = 0.005
        elif price < 10.00:
            tick = 0.01
        elif price < 20.00:
            tick = 0.02
        elif price < 100.00:
            tick = 0.05
        elif price < 200.00:
            tick = 0.10
        elif price < 500.00:
            tick = 0.20
        elif price < 1000.00:
            tick = 0.50
        elif price < 2000.00:
            tick = 1.00
        elif price < 5000.00:
            tick = 2.00
        else:
            tick = 5.00
        
        # 向上取整（买入）或向下取整（卖出）
        import math
        if action == 'BUY':
            return math.ceil(price / tick) * tick
        else:
            return math.floor(price / tick) * tick
    
    def get_retry_history(self, order_id: str) -> List[RetryRecord]:
        """获取重试历史"""
        return self.retry_history.get(order_id, [])
    
    def cleanup_old_records(self, max_age_hours: int = 24):
        """清理旧记录"""
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        
        for order_id in list(self.retry_history.keys()):
            records = self.retry_history[order_id]
            # 只保留最近的记录
            recent_records = [
                r for r in records
                if datetime.fromisoformat(r.timestamp) > cutoff
            ]
            
            if recent_records:
                self.retry_history[order_id] = recent_records
            else:
                del self.retry_history[order_id]
        
        logger.debug(f"清理旧记录完成")
    
    def get_statistics(self) -> Dict:
        """获取统计信息"""
        total_orders = len(self.retry_history)
        total_retries = sum(len(records) for records in self.retry_history.values())
        successful_retries = sum(
            sum(1 for r in records if r.success)
            for records in self.retry_history.values()
        )
        
        return {
            'total_orders': total_orders,
            'total_retries': total_retries,
            'successful_retries': successful_retries,
            'retry_success_rate': round(successful_retries / total_retries * 100, 2) if total_retries else 0,
        }


# 全局单例
order_retry_manager = OrderRetryManager()
