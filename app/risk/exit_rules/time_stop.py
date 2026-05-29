"""
FTSeek-US Time Stop 规则 (规则 3)

Task #19 拆分自 position_exit_monitor.py。
持仓 > 最大市场小时数 (盈利豁免)。
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from .base import BaseExitRule
from ..atr_stop_engine import compute_profit_atr_signed

logger = logging.getLogger("ftseek.exit_monitor")

try:
    from pytz import timezone
    ET = timezone('US/Eastern')
except ImportError:
    ET = None


def check_time_stop(monitor, position, current_price, now_ts, direction, policy_guardian=None):
    """Time Stop 判定逻辑 (从 ExitMonitor._check_time_stop 原样迁移)"""
    from ..position_exit_monitor import get_market_hours_us

    try:
        if policy_guardian is not None and hasattr(policy_guardian, "policy"):
            _ts_cfg = policy_guardian.policy.get("exit_rules", {}).get("time_stop", {})
            if _ts_cfg.get("enabled", True) is False:
                return None
    except Exception as _silent_e:
        logger.debug(f"[silent_except:time_stop.py] {_silent_e}")
    try:
        entry_time = position.entry_time
        if entry_time is None:
            return None
        if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=ET) if ET else entry_time
        held_hours = get_market_hours_us(entry_time, datetime.now(ET) if ET else datetime.now())

        max_hours = 72
        min_profit_pct = 0.02
        max_extensions = 2
        extension_hours = 24
        if policy_guardian is not None and hasattr(policy_guardian, "policy"):
            ts_cfg = policy_guardian.policy.get("exit_rules", {}).get("time_stop", {})
            if direction == "SHORT":
                short_ts = policy_guardian.policy.get("short_overrides", {}).get("time_stop", {})
                max_hours = int(short_ts.get("max_holding_hours", 48))
                held_days = held_hours / 24.0 if held_hours > 0 else 0
                accel_enabled = short_ts.get("acceleration_enabled", True)
                if accel_enabled and held_days >= 3:
                    max_hours = int(max_hours * float(short_ts.get("day3_multiplier", 0.6)))
                elif accel_enabled and held_days >= 2:
                    max_hours = int(max_hours * float(short_ts.get("day2_multiplier", 0.8)))
            else:
                max_hours = int(ts_cfg.get("max_holding_hours", 72))
            min_profit_pct = float(ts_cfg.get("min_profit_to_hold", 2.0)) / 100.0
            extension_hours = int(ts_cfg.get("extension_hours", 24))

        if current_price and current_price > 0 and position.avg_entry_price > 0:
            atr = monitor._get_atr(position.symbol) if monitor is not None else 0.0
            profit_atr_signed = compute_profit_atr_signed(
                direction, position.avg_entry_price, current_price, atr
            ) if atr > 0 else 0.0

            if direction == "LONG":
                pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
            else:
                pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price

            if profit_atr_signed >= 1.0 or pnl_pct >= min_profit_pct:
                ts = position.trail_state or {}
                extensions = ts.get("time_stop_extensions", 0)
                max_ext = max_extensions + int(held_hours / max_hours)
                if extensions < max_ext:
                    ts["time_stop_extensions"] = extensions + 1
                    position.trail_state = ts
                    new_limit = max_hours + extensions * extension_hours
                    logger.debug(
                        f"[时间止损·豁免] {position.symbol} 浮盈 {pnl_pct*100:.1f}% "
                        f"({profit_atr_signed:+.1f} ATR), 豁免时间止损 "
                        f"(延长次数 {extensions + 1}/{max_ext}, 新限额 {new_limit}h)"
                    )
                    return None

        if held_hours >= max_hours:
            return {
                "reason": "TIME_STOP",
                "reason_cn": "时间止损",
                "exit_price": 0,
                "description": f"持仓 {held_hours:.1f}h >= {max_hours}h"
            }
    except Exception as e:
        logger.debug(f"时间止损计算失败 {position.symbol}: {e}")
    return None


class TimeStopRule(BaseExitRule):
    rule_name = "TIME_STOP"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_time_stop(
            self.monitor,
            position,
            market_data["current_price"],
            market_data["now_ts"],
            market_data["direction"],
            market_data.get("policy_guardian"),
        )
