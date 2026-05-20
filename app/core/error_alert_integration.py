#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
错误处理器告警集成

集成飞书/钉钉告警到 ErrorHandler。

使用示例:
    from app.core.error_handler import ErrorHandler
    from app.core.error_alert_integration import setup_error_alerts

    # 初始化告警集成
    setup_error_alerts()

    # 之后 ErrorHandler.handle() 会自动触发告警

版本历史:
  2026-05-19: 创建
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def setup_error_alerts(notifier=None):
    """
    初始化错误告警集成

    Args:
        notifier: 通知器实例 (DingTalkNotifier/FeishuNotifier)
                  如果为 None，则只记录日志，不发送通知
    """
    from app.core.error_handler import get_error_handler

    handler = get_error_handler()

    def alert_callback(component: str, error: Exception, context: dict):
        """
        告警回调函数

        Args:
            component: 组件名称
            error: 异常对象
            context: 上下文
        """
        from datetime import datetime

        # 构建告警消息
        error_rate = handler.get_error_rate(component)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        message = (
            f"🚨 FTSeek 错误告警\n\n"
            f"**组件**: {component}\n"
            f"**错误率**: {error_rate:.1f} 次/小时\n"
            f"**最新错误**: {type(error).__name__}\n"
            f"**错误信息**: {str(error)[:100]}\n"
            f"**时间**: {timestamp}\n"
        )

        if context:
            message += f"\n**上下文**: {str(context)[:200]}"

        # 发送通知
        if notifier is not None:
            try:
                notifier.send_message(message)
                logger.info(f"[错误告警] 已发送通知：{component}")
            except Exception as e:
                logger.error(f"[错误告警] 发送通知失败：{e}")
        else:
            # 只记录日志
            logger.warning(f"[错误告警] {message}")

    # 注册回调
    handler.register_alert_callback(alert_callback)

    # 设置默认阈值
    default_thresholds = {
        'llm_gate': 10,
        'fee_gate': 5,
        'time_gate': 3,
        'market_regime_gate': 5,
        'kline_process': 20,
        'quote_handler': 50,
        'trade_executor': 10,
        'position_manager': 10,
    }

    for component, threshold in default_thresholds.items():
        handler.set_alert_threshold(component, threshold)

    logger.info(f"[ErrorHandler] 告警集成已初始化 (notifier={notifier is not None})")


__all__ = ['setup_error_alerts']
