"""统一推送系统 (2026-05-16)

公开 API:
    from app.push import (
        get_push_hub,           # 主入口 (单例)
        PushMessage, Level, Category,
        trade_entry, trade_exit, critical_alert, daily_review,  # 便捷构造器
    )

最简调用:
    get_push_hub().push(trade_entry("NVDA", "LONG", 100, 145.50))

向后兼容:
    旧 send_text / notify_order_filled 不变 (app/notifier/feishu.py, dingtalk.py).
"""
from .message import (
    PushMessage, Level, Category,
    trade_entry, trade_exit, critical_alert, daily_review, approaching_stop,
)
from .hub import PushHub, get_push_hub, reset_push_hub, push_text_safe, HubTextAdapter

__all__ = [
    "PushHub", "get_push_hub", "reset_push_hub", "push_text_safe", "HubTextAdapter",
    "PushMessage", "Level", "Category",
    "trade_entry", "trade_exit", "critical_alert", "daily_review", "approaching_stop",
]
