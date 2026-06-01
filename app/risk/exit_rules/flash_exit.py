"""
FTSeek-US Flash Exit 规则 (规则 1)

Task #19 拆分自 position_exit_monitor.py。
60s 内波动 > flash_multiplier × ATR (优化: 1.0x阈值, 60s扫描)

修复历史:
  - 2026-05-12: opening_drive session 自动设置最低 1.5x ATR (开盘波动本身 2-3x ATR)
  - 2026-05-14 (P0-D3): 拆股标的跳过 FLASH_EXIT, 防止 -50% 跳价误触发
"""
import logging
from typing import Optional, Dict, Any

from .base import BaseExitRule

logger = logging.getLogger("ftseek.exit_monitor")


def check_flash_exit(monitor, position, current_price, atr, trail_state, now_ts, direction, policy, earnings_guard):
    """Flash Exit 判定逻辑 (从 ExitMonitor._check_flash_exit 原样迁移)"""
    if atr <= 0:
        return None

    # 修复 (2026-06-02 P0): 入场后 min_holding_seconds 内不触发 FLASH_EXIT
    # 辩论共识: 76%交易为闪退(88/116), 全亏-$4,333; 开仓即闪退是核心问题
    min_holding_sec = 120  # default fallback
    if policy:
        try:
            min_holding_sec = int(
                policy.policy.get('exit_rules', {}).get('flash_exit', {}).get('min_holding_seconds', 120)
            )
        except (TypeError, ValueError):
            min_holding_sec = 120
    entry_ts = trail_state.get("entry_time_ts", 0)
    if entry_ts > 0 and (now_ts - entry_ts) < min_holding_sec:
        logger.debug(
            f"[FlashExit] {position.symbol} 持仓 {(now_ts - entry_ts):.0f}s < {min_holding_sec}s, 跳过闪退"
        )
        return None

    # P0-D3: 拆股 symbol 跳过 flash exit
    try:
        from app.data.split_event_guard import get_split_guard
        if get_split_guard().is_split_today(position.symbol):
            return None
    except Exception as _silent_e:
        logger.debug(f"[silent_except:flash_exit.py] {_silent_e}")

    if policy:
        flash_multiplier = policy.get_flash_multiplier(position.symbol)
        if earnings_guard and hasattr(earnings_guard, 'is_earnings_period'):
            try:
                if earnings_guard.is_earnings_period(position.symbol):
                    flash_multiplier = policy.policy.get('exit_rules', {}).get('earnings_guard', {}).get('flash_multiplier_override', 0.5)
            except Exception as _silent_e:
                logger.debug(f"[silent_except:flash_exit.py] {_silent_e}")

        # 幻方增强 (2026-04-29): 做空 + 高VIX 环境下收紧 Flash Exit
        if direction == "SHORT":
            try:
                vix = monitor._get_vix() if monitor is not None else None
                if vix and vix > 30:
                    short_vix_mult = 0.6  # 做空回补反弹更暴力
                    flash_multiplier = min(flash_multiplier, short_vix_mult)
                    logger.debug(
                        f"[FlashExit] {position.symbol} SHORT+VIX={vix:.1f}>30, "
                        f"收紧 flash_multiplier -> {flash_multiplier}"
                    )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:flash_exit.py] {_silent_e}")
    else:
        flash_multiplier = 0.8

    # 修复 (2026-05-12): opening_drive session 自动加倍阈值
    try:
        from app.shared.market_hours import get_shared_kernel
        session = get_shared_kernel().get_current_session()
        if session == "opening_drive":
            flash_multiplier = max(flash_multiplier, 1.5)  # 开盘最低 1.5x ATR
    except Exception as _silent_e:
        logger.debug(f"[silent_except:flash_exit.py] {_silent_e}")

    # 绝对下限: 1.0x ATR (防止过于敏感)
    flash_multiplier = max(flash_multiplier, 1.0)

    prev_price = trail_state.get("prev_scan_price", current_price)
    trail_state["prev_scan_price"] = current_price

    if prev_price > 0 and prev_price != current_price:
        adverse_change = prev_price - current_price if direction == "LONG" else current_price - prev_price
        if adverse_change > 0 and adverse_change >= flash_multiplier * atr:
            trail_state["last_trigger_time"] = now_ts
            return {
                "reason": "FLASH_EXIT",
                "reason_cn": f"异常动量平仓 (快速{'下跌' if direction == 'LONG' else '上涨'})",
                "exit_price": current_price,
                "description": f"30s 内波动 {adverse_change:.2f} ({adverse_change/atr:.1f}x ATR)"
            }
    return None


class FlashExitRule(BaseExitRule):
    """Flash Exit: 异常动量平仓 (60s 内波动 > flash_multiplier × ATR)"""

    rule_name = "FLASH_EXIT"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_flash_exit(
            self.monitor,
            position,
            market_data["current_price"],
            market_data.get("effective_atr", market_data.get("atr", 0.0)),
            trail_state,
            market_data["now_ts"],
            market_data["direction"],
            market_data.get("policy_guardian"),
            market_data.get("earnings_guard"),
        )
