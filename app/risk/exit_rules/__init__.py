"""
FTSeek-US 退出规则包 (Exit Rules Package)

Task #19 拆分自 position_exit_monitor.py 巨石模块。

将原 6 种退出规则按职责拆分到独立文件:
  - flash_exit       规则 1   : 异常动量平仓
  - day_trade_exit   规则 1.5 : Day Trade Mode (含 profit_lock)
  - scale_out        规则 2.5 : 分批止盈
  - trailing_stop    规则 2   : ATR 动态止损 + Chandelier 追踪
  - time_stop        规则 3   : 持仓时长止损
  - pre_close        规则 3.5 : 盘末分层强制退出

每条规则继承 BaseExitRule, 实现 should_exit(position, market_data, trail_state)
返回 None 表示不触发, 返回 dict 表示触发 (兼容 ExitQualityScorer 与 _execute_exit)。

编排器 (position_exit_monitor.ExitMonitor) 负责:
  1. 数据准备 (ATR / session 融合 / pnl 计算)
  2. 规则遍历 + 收集触发
  3. Pre-Close 优先级最高 (直接执行)
  4. 多规则触发时调用 ExitQualityScorer 选最优
  5. 执行平仓 + 记录归因 / trace_id / 钉钉推送
"""
from .base import BaseExitRule, ExitDecision
from .flash_exit import FlashExitRule, check_flash_exit
from .day_trade_exit import (
    DayTradeExitRule,
    check_day_trade_exit,
    check_atr_day_trade_exit,
    should_use_day_trade_mode,
)
from .scale_out import ScaleOutRule, check_scale_out
from .graduated_stop import GraduatedStopRule, check_graduated_stop
from .trailing_stop import TrailingStopRule, check_trailing_stop
from .time_stop import TimeStopRule, check_time_stop
from .pre_close import PreCloseExitRule, check_pre_close_exit
from .thesis_failure import (
    ThesisFailureRule,
    THESIS_FAILURE_MAP,
    check_thesis_failure,
)

# Task #48: 退出原因枚举 + 优先级注册表
from .exit_reason_enum import ExitReason
from .exit_rule_registry import (
    ExitRulePriority,
    ExitRuleContext,
    EXCLUSIVE_RULES,
    RULE_PRIORITIES,
    ExitRuleRegistry,
)

__all__ = [
    "BaseExitRule",
    "ExitDecision",
    "FlashExitRule",
    "DayTradeExitRule",
    "ScaleOutRule",
    "GraduatedStopRule",
    "TrailingStopRule",
    "TimeStopRule",
    "PreCloseExitRule",
    "ThesisFailureRule",
    "THESIS_FAILURE_MAP",
    "check_flash_exit",
    "check_day_trade_exit",
    "check_atr_day_trade_exit",
    "should_use_day_trade_mode",
    "check_scale_out",
    "check_graduated_stop",
    "check_trailing_stop",
    "check_time_stop",
    "check_pre_close_exit",
    "check_thesis_failure",
    # Task #48
    "ExitReason",
    "ExitRulePriority",
    "ExitRuleContext",
    "EXCLUSIVE_RULES",
    "RULE_PRIORITIES",
    "ExitRuleRegistry",
]
