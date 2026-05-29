"""退出原因统一枚举 - 替代散落的字符串常量

Task #48: 退出规则优先级注册表 — 编码化优先级与互斥关系

设计要点:
  - 使用 str mixin (str, Enum) 保证序列化为字符串, 向后兼容现有 trade_records.exit_reason 字段
  - 提供 is_day_trade / is_pre_close 等分类辅助方法, 方便分组判断
  - 仅是 reason 的"字面量集合", 不替代 ExitRulePriority/EXCLUSIVE_RULES (那些在 exit_rule_registry)

向后兼容:
  ExitReason.ATR_LOSS_1X == "ATR_LOSS_1X" 为 True
  json.dumps(ExitReason.ATR_LOSS_1X) → "\"ATR_LOSS_1X\""
  数据库写入时与历史记录格式完全一致
"""
from enum import Enum


class ExitReason(str, Enum):
    """所有退出原因的枚举定义

    使用 str mixin 保证序列化为字符串, 向后兼容现有数据库记录
    """
    # Flash Exit (异常动量)
    FLASH_EXIT_MOMENTUM = "FLASH_EXIT_MOMENTUM"
    FLASH_EXIT_VOLUME = "FLASH_EXIT_VOLUME"

    # Day Trade ATR
    ATR_LOSS_1X = "ATR_LOSS_1X"
    DAYTRADE_TP1 = "DAYTRADE_TP1"
    DAYTRADE_TP2 = "DAYTRADE_TP2"

    # Day Trade Legacy
    DAYTRADE_LEGACY_SL = "DAYTRADE_LEGACY_SL"
    DAYTRADE_LEGACY_TP = "DAYTRADE_LEGACY_TP"

    # Trailing Stop
    TRAILING_STOP = "TRAILING_STOP"
    CHANDELIER_TRAIL = "CHANDELIER_TRAIL"
    ATR_TRAILING = "ATR_TRAILING"

    # Pre-Close (盘末)
    PRE_CLOSE_LOSS = "PRE_CLOSE_LOSS"
    PRE_CLOSE_SMALL_GAIN = "PRE_CLOSE_SMALL_GAIN"
    PRE_CLOSE_PARTIAL = "PRE_CLOSE_PARTIAL"
    PRE_CLOSE_OVERNIGHT = "PRE_CLOSE_OVERNIGHT"

    # Time Stop
    TIME_STOP_EXCEEDED = "TIME_STOP_EXCEEDED"

    # Scale Out
    SCALE_OUT_PARTIAL = "SCALE_OUT_PARTIAL"
    SCALE_OUT_FULL = "SCALE_OUT_FULL"

    # Stop Loss / Take Profit (通用)
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"

    # 手动 / 系统
    MANUAL_CLOSE = "MANUAL_CLOSE"
    SYSTEM_CLOSE = "SYSTEM_CLOSE"

    @classmethod
    def is_day_trade(cls, reason: 'ExitReason') -> bool:
        """判断是否为日内交易退出"""
        return reason in (
            cls.ATR_LOSS_1X,
            cls.DAYTRADE_TP1,
            cls.DAYTRADE_TP2,
            cls.DAYTRADE_LEGACY_SL,
            cls.DAYTRADE_LEGACY_TP,
        )

    @classmethod
    def is_pre_close(cls, reason: 'ExitReason') -> bool:
        """判断是否为盘末退出"""
        return reason in (
            cls.PRE_CLOSE_LOSS,
            cls.PRE_CLOSE_SMALL_GAIN,
            cls.PRE_CLOSE_PARTIAL,
            cls.PRE_CLOSE_OVERNIGHT,
        )

    @classmethod
    def is_flash_exit(cls, reason: 'ExitReason') -> bool:
        """判断是否为 Flash Exit (异常动量)"""
        return reason in (cls.FLASH_EXIT_MOMENTUM, cls.FLASH_EXIT_VOLUME)

    @classmethod
    def is_trailing(cls, reason: 'ExitReason') -> bool:
        """判断是否为追踪止盈/止损"""
        return reason in (cls.TRAILING_STOP, cls.CHANDELIER_TRAIL, cls.ATR_TRAILING)
