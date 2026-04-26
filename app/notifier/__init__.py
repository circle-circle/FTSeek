"""钉钉通知模块

统一入口: get_dingtalk_notifier() - 所有模块必须使用此函数获取 notifier
"""
from .dingtalk import DingTalkNotifier, get_dingtalk_notifier, reset_dingtalk_notifier

__all__ = ["DingTalkNotifier", "get_dingtalk_notifier", "reset_dingtalk_notifier"]
