"""
FTSeek-US Pre-Close Exit 规则 (规则 3.5)

Task #19 拆分自 position_exit_monitor.py。
盘末强制退出: 分层退出 + 时间加权策略 (2026-05-03 P2 优化)。
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from .base import BaseExitRule
from ..atr_stop_engine import compute_profit_atr_signed
from ..eod_config import load_eod_config

logger = logging.getLogger("ftseek.exit_monitor")

try:
    from pytz import timezone
    ET = timezone('US/Eastern')
except ImportError:
    ET = None


def check_pre_close_exit(position, current_price, atr, trail_state, direction, policy_guardian=None):
    """Pre-Close Exit 判定逻辑 (从 ExitMonitor._check_pre_close_exit 原样迁移)"""
    try:
        if ET:
            now_et = datetime.now(ET)
        else:
            now_et = datetime.now()

        if now_et.weekday() >= 5:
            return None

        pc_cfg = {}
        if policy_guardian is not None and hasattr(policy_guardian, "policy"):
            pc_cfg = policy_guardian.policy.get("exit_rules", {}).get("pre_close_exit", {})

        # C4 修复 (2026-05-13): 全局 EOD 配置优先
        eod_cfg = load_eod_config()

        window_minutes = int(pc_cfg.get("window_minutes", 15))
        minutes_to_close = 16 * 60 - (now_et.hour * 60 + now_et.minute)
        if minutes_to_close > window_minutes or minutes_to_close < 0:
            return None

        # P0 修复 (2026-05-02): 冷却期检查
        last_scan = trail_state.get('_last_pre_close_check', 0)
        now_ts = now_et.timestamp()
        if now_ts - last_scan < 60:
            return None
        trail_state['_last_pre_close_check'] = now_ts

        # P1 修复 (2026-05-08): 入场后最小持有时间保护
        entry_time = position.entry_time
        if entry_time:
            try:
                if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=ET) if ET else entry_time
                held_minutes = (now_et - entry_time).total_seconds() / 60.0
                min_hold_minutes = float(pc_cfg.get("min_hold_minutes_before_pre_close", eod_cfg.min_hold_minutes_before_eod_exit))
                if held_minutes < min_hold_minutes:
                    logger.debug(
                        f"[盘末退出·跳过] {position.symbol} 入场仅 {held_minutes:.0f} 分钟 "
                        f"< {min_hold_minutes:.0f} 分钟，不触发盘末退出"
                    )
                    return None
            except Exception as _silent_e:
                logger.debug(f"[silent_except:pre_close.py] {_silent_e}")

        if atr <= 0 or position.avg_entry_price <= 0:
            return None

        profit_atr_signed = compute_profit_atr_signed(direction, position.avg_entry_price, current_price, atr)

        # P2 优化 (2026-05-03): 时间加权阈值
        time_weighted = pc_cfg.get("time_weighted", False)
        threshold_mult = 1.0
        time_window_label = "standard"

        if time_weighted:
            if minutes_to_close < 5:
                threshold_mult = float(pc_cfg.get("urgent_threshold_mult", 0.6))
                time_window_label = "urgent"
            elif minutes_to_close < 10:
                threshold_mult = float(pc_cfg.get("warning_threshold_mult", 0.8))
                time_window_label = "warning"

        # 层4: ≥hold_threshold ATR → 保留过夜
        hold_threshold = float(pc_cfg.get("hold_threshold_atr", 1.0)) * threshold_mult

        # P1 修复 (2026-05-11): 隔夜风险敞口总上限
        max_overnight_pct = float(pc_cfg.get("max_overnight_exposure_pct", 0.50))
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            account_equity = 0.0
            try:
                from app.risk.capital_safety import get_capital_safety_system
                cs = get_capital_safety_system()
                account_equity = getattr(cs._state, 'current_equity', 1000000)
            except Exception:
                account_equity = float(cfg.get_dotted("capital.fixed_amount", 1000000))

            position_value = current_price * position.size
            position_pct = position_value / account_equity if account_equity > 0 else 0
            if position_pct > max_overnight_pct * 0.3:
                hold_threshold *= 1.5
                logger.warning(
                    f"[盘末退出·隔夜收紧] {position.symbol} 单只占比 {position_pct:.0%}, "
                    f"hold_threshold 提高至 {hold_threshold:.1f} ATR"
                )
        except Exception as e:
            logger.debug(f"[盘末退出·隔夜检查] 失败: {e}")

        if profit_atr_signed >= hold_threshold:
            position_value = current_price * position.size
            if position_value > 100_000:
                logger.warning(
                    f"⚠️ [盘末退出·豁免·大额] {position.symbol} "
                    f"浮盈 {profit_atr_signed:.1f} ATR (阈值{hold_threshold:.1f}, {time_window_label}窗口), "
                    f"持仓价值 ${position_value:,.0f}, "
                    f"允许过夜 — 请确认这是有意持仓"
                )
            else:
                logger.debug(
                    f"[盘末退出·跳过] {position.symbol} 浮盈 {profit_atr_signed:.1f} ATR ≥ {hold_threshold:.1f} "
                    f"({time_window_label}窗口), 允许持仓过夜"
                )
            return None

        # 分层退出 (应用时间加权阈值)
        loss_flat = pc_cfg.get("loss_flat", True)
        half_exit_atr = float(pc_cfg.get("half_exit_below_atr", 0.5)) * threshold_mult
        quarter_exit_atr = float(pc_cfg.get("quarter_exit_below_atr", 1.0)) * threshold_mult

        # 层1: 亏损仓位 → 全平
        if profit_atr_signed < 0:
            if loss_flat:
                return {
                    "reason": "PRE_CLOSE_EXIT",
                    "reason_cn": f"盘末强制退出 (亏损仓位, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                    "exit_price": current_price,
                    "description": (
                        f"浮亏 {profit_atr_signed:.1f} ATR, "
                        f"距收盘 {minutes_to_close} 分钟 ({time_window_label}窗口), "
                        f"不持有亏损过夜"
                    ),
                    "close_ratio": 1.0,
                }

        # 层2: 0~half_exit_atr 微利 → 减半仓
        elif profit_atr_signed < half_exit_atr:
            return {
                "reason": "PRE_CLOSE_EXIT_PARTIAL",
                "reason_cn": f"盘末减仓50% (微利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                "exit_price": current_price,
                "description": (
                    f"浮盈 {profit_atr_signed:.1f} ATR < {half_exit_atr:.1f} "
                    f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                    f"减仓50%降低隔夜风险"
                ),
                "close_ratio": 0.5,
            }

        # 层3: half_exit_atr~quarter_exit_atr → 减 1/4
        elif profit_atr_signed < quarter_exit_atr:
            return {
                "reason": "PRE_CLOSE_EXIT_PARTIAL",
                "reason_cn": f"盘末减仓25% (盈利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                "exit_price": current_price,
                "description": (
                    f"浮盈 {profit_atr_signed:.1f} ATR < {quarter_exit_atr:.1f} "
                    f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                    f"减仓25%"
                ),
                "close_ratio": 0.25,
            }

        return None

    except Exception as e:
        logger.debug(f"盘末退出检查失败 {position.symbol}: {e}")
    return None


class PreCloseExitRule(BaseExitRule):
    """盘末分层强制退出 (优先级最高, 不参与质量评分竞争)"""

    rule_name = "PRE_CLOSE_EXIT"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_pre_close_exit(
            position,
            market_data["current_price"],
            market_data.get("effective_atr", market_data.get("atr", 0.0)),
            trail_state,
            market_data["direction"],
            market_data.get("policy_guardian"),
        )
