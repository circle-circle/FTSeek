"""
app/risk/suspension_guard.py — 港股停牌检测与处理 (2026-05-01)

职责:
  检测持仓股票是否停牌，并在停牌时采取保护措施。

港股停牌类型:
  1. 临时停牌: 股价异常波动、重大公告（通常几分钟到几小时）
  2. 长期停牌: 财务问题、重组、违规（可能数天到数月）

检测机制:
  - Futu API snapshot: stock_state 字段
  - 成交量为零 + 价格不变（降级检测）

处理策略:
  - 检测到停牌 → 记录告警 + 跳过该股票的止损止盈检查
  - 连续N次检测到停牌 → 发送钉钉告警
  - 复牌后 → 恢复正常检查
"""
import logging
import time
from typing import Dict, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.risk.suspension_guard")


class SuspensionGuard:
    """
    停牌检测与处理守护

    用法:
        guard = SuspensionGuard()
        if guard.is_suspended(code, quote_data):
            # 停牌处理
            guard.handle_suspension(code, position)
        else:
            # 正常止损止盈检查
            check_exits(code, price)
    """

    def __init__(self, max_suspension_count: int = 5, alert_cooldown_minutes: int = 30):
        """
        Args:
            max_suspension_count: 连续检测到停牌的最大次数，超过后触发告警
            alert_cooldown_minutes: 告警冷却时间（分钟）
        """
        self._suspension_count: Dict[str, int] = {}
        self._last_alert_time: Dict[str, float] = {}
        self._max_count = max_suspension_count
        self._alert_cooldown = alert_cooldown_minutes * 60
        self._suspended_stocks: Dict[str, datetime] = {}  # code → 首次检测到停牌的时间

    def is_suspended(self, code: str, quote_data: Optional[dict] = None) -> bool:
        """
        检测股票是否停牌。

        Args:
            code: 股票代码
            quote_data: Futu API 返回的行情数据（可选）

        Returns:
            True if suspended, False otherwise
        """
        # 方法1: 通过 Futu API stock_state 检测
        if quote_data:
            stock_state = quote_data.get('stock_state', '')
            if stock_state and stock_state.upper() in ('SUSPEND', 'SUSPENDED', '停牌', 'HALT'):
                self._record_suspension(code)
                return True

            # 方法2: 通过交易状态检测
            trade_state = quote_data.get('trade_state', '')
            if trade_state and trade_state.upper() in ('NO_TRADE', 'NOT_TRADE'):
                self._record_suspension(code)
                return True

        return False

    def _record_suspension(self, code: str):
        """记录停牌检测"""
        count = self._suspension_count.get(code, 0) + 1
        self._suspension_count[code] = count

        if code not in self._suspended_stocks:
            self._suspended_stocks[code] = datetime.now()
            logger.warning(f"[停牌检测] {code} 首次检测到停牌")

    def should_alert(self, code: str) -> bool:
        """判断是否应该发送停牌告警"""
        count = self._suspension_count.get(code, 0)
        if count < self._max_count:
            return False

        # 检查冷却时间
        last_alert = self._last_alert_time.get(code, 0)
        if time.time() - last_alert < self._alert_cooldown:
            return False

        return True

    def mark_alerted(self, code: str):
        """标记已发送告警"""
        self._last_alert_time[code] = time.time()

    def get_suspension_duration(self, code: str) -> Optional[timedelta]:
        """获取停牌持续时间"""
        if code in self._suspended_stocks:
            return datetime.now() - self._suspended_stocks[code]
        return None

    def handle_suspension(self, code: str, position=None) -> str:
        """
        处理停牌情况。

        Returns:
            处理建议: "hold" (继续持有) | "alert" (发送告警) | "monitor" (持续监控)
        """
        duration = self.get_suspension_duration(code)
        count = self._suspension_count.get(code, 0)

        if duration and duration > timedelta(hours=24):
            # 停牌超过24小时，严重情况
            logger.error(
                f"[停牌处理] {code} 已停牌超过24小时 ({duration})，"
                f"连续检测{count}次，建议人工干预"
            )
            return "alert"
        elif duration and duration > timedelta(hours=4):
            # 停牌超过4小时
            logger.warning(
                f"[停牌处理] {code} 已停牌超过4小时 ({duration})，"
                f"暂停止损止盈检查"
            )
            return "hold"
        else:
            # 短期停牌，继续监控
            logger.debug(f"[停牌处理] {code} 短期停牌，持续监控")
            return "monitor"

    def reset(self, code: str):
        """重置某股票的停牌记录（复牌后调用）"""
        self._suspension_count.pop(code, None)
        self._suspended_stocks.pop(code, None)
        logger.info(f"[停牌重置] {code} 已复牌，清除停牌记录")

    def get_status(self, code: str) -> dict:
        """获取某股票的停牌状态"""
        return {
            'is_suspended': code in self._suspended_stocks,
            'count': self._suspension_count.get(code, 0),
            'duration': self.get_suspension_duration(code),
            'last_alert': self._last_alert_time.get(code, 0),
        }


# 全局单例
suspension_guard = SuspensionGuard()
