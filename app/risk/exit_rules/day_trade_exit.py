"""
FTSeek-US Day Trade Mode 规则 (规则 1.5)

Task #19 拆分自 position_exit_monitor.py。
短线主动管理: 紧止损 + 主动止盈 + 利润锁定 + 时间衰减。

注: profit_lock 逻辑作为 Day Trade Mode 的内置组件保留在本文件中
    (使用 dt_profit_lock_* 状态键, 与 DayTrade 流程深度耦合)。
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from .base import BaseExitRule
from .exit_reason_enum import ExitReason

logger = logging.getLogger("ftseek.exit_monitor")

try:
    from pytz import timezone
    ET = timezone('US/Eastern')
except ImportError:
    ET = None


def should_use_day_trade_mode(position, policy_guardian=None) -> bool:
    """判断是否应使用日内短线模式 (从 ExitMonitor._should_use_day_trade_mode 原样迁移)"""
    dt_cfg = {}
    if policy_guardian and hasattr(policy_guardian, "policy"):
        dt_cfg = policy_guardian.policy.get("day_trade_mode", {})

    if not dt_cfg.get("enabled", True):
        return False

    apply_cond = dt_cfg.get("apply_conditions", {})

    # 排除 ETF (使用 position_limits 中的共享 ETF 列表)
    if apply_cond.get("exclude_etf", True):
        from app.risk.position_limits import ETF_EXPOSURE_DISCOUNT
        if position.symbol in ETF_EXPOSURE_DISCOUNT:
            return False

    # 排除财报期 (由 earnings_guard 注入的标记)
    if apply_cond.get("exclude_earnings_period", True):
        if getattr(position, "trail_state", {}).get("is_earnings_period", False):
            return False

    # 持仓天数检查
    max_days = int(apply_cond.get("max_holding_days", 3))
    if position.entry_time:
        try:
            now_et = datetime.now(ET) if ET else datetime.now()
            entry_time = position.entry_time
            if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                if ET and hasattr(now_et, 'tzinfo') and now_et.tzinfo is not None:
                    entry_time = entry_time.replace(tzinfo=ET)
            held_days = (now_et - entry_time).total_seconds() / 86400.0
            if held_days > max_days:
                return False
        except Exception as _silent_e:
            logger.debug(f"[silent_except:day_trade_exit.py] {_silent_e}")

    return True


def check_atr_day_trade_exit(position, current_price, entry_price, atr, trail_state,
                              direction, pnl_pct, policy_guardian=None):
    """任务 #45 (P1): ATR 驱动的日内交易退出

    新逻辑:
      - SL: 亏损达到 1.0×ATR/entry 时全平 (ATR_LOSS_1X)
      - TP1 部分止盈: 盈利 1.5×ATR/entry 时减仓 50% (ATR_PROFIT_1.5X)
      - TP2 全平: 盈利 2.5×ATR/entry 时清仓 (ATR_PROFIT_2.5X)

    返回与既有 dict 协议兼容: reason / reason_cn / exit_price / close_ratio / close_qty
    ATR 不可用时返回 None, 由调用方回退到 legacy 逻辑。
    """
    dt_atr_cfg = {}
    if policy_guardian is not None and hasattr(policy_guardian, "policy"):
        dt_atr_cfg = (
            policy_guardian.policy
            .get("day_trade_atr", {})
            or policy_guardian.policy.get("exit_policy", {}).get("day_trade_atr", {})
        )

    if not dt_atr_cfg.get("enabled", False):
        # 任务 #48: 明确降级路径日志
        logger.debug(
            f"[DayTrade·降级] {getattr(position, 'symbol', '?')} "
            f"ATR模式禁用 (day_trade_atr.enabled=false), 降级到 legacy"
        )
        return None

    # 不适用于该仓位 (ETF / 财报期 / 持仓>3天) 时, 让出给 legacy / 趋势模式
    if not should_use_day_trade_mode(position, policy_guardian):
        return None

    if entry_price is None or entry_price <= 0:
        return None

    # ATR 不可用时回退到 entry_price * 2% 作估算 (与 pre_close 保持一致)
    if atr is None or atr <= 0:
        # 任务 #48: ATR 数据不可用时明确记录降级事件 (不中止, 依然用估算 ATR 跳出, 但调用方可从日志定位问题)
        logger.warning(
            f"[DayTrade·降级] {getattr(position, 'symbol', '?')} "
            f"ATR数据不可用 (atr={atr}), 使用 entry_price×2% 估算后继续; "
            f"若本轮未触发, 调用方会降级到 legacy"
        )
    effective_atr = atr if (atr is not None and atr > 0) else entry_price * 0.02
    if effective_atr <= 0:
        return None

    atr_pct = effective_atr / entry_price  # ATR 占入场价的百分比
    if atr_pct <= 0:
        return None

    profit_target_atr = float(dt_atr_cfg.get("profit_target_atr", 1.5))
    full_exit_atr = float(dt_atr_cfg.get("full_exit_atr", 2.5))
    stop_loss_atr = float(dt_atr_cfg.get("stop_loss_atr", 1.0))
    partial_close_ratio = float(dt_atr_cfg.get("partial_close_ratio", 0.5))
    partial_close_ratio = min(max(partial_close_ratio, 0.05), 0.95)

    # 1. 止损 (优先): pnl_pct ≤ -stop_loss_atr × atr_pct
    if pnl_pct <= -stop_loss_atr * atr_pct:
        return {
            "reason": ExitReason.ATR_LOSS_1X.value,
            "reason_cn": (
                f"ATR 短线止损 (亏损 {abs(pnl_pct)*100:.2f}% ≥ "
                f"{stop_loss_atr:.1f}×ATR/entry={stop_loss_atr*atr_pct*100:.2f}%)"
            ),
            "exit_price": current_price,
            "close_ratio": 1.0,
            "description": (
                f"ATR-driven Day Trade SL: pnl={pnl_pct*100:.2f}% ≤ "
                f"-{stop_loss_atr:.1f}×ATR_pct ({stop_loss_atr*atr_pct*100:.2f}%); "
                f"ATR={effective_atr:.4f}, entry={entry_price:.4f}"
            ),
        }

    # 2. 全平 (盈利达 2.5×ATR)
    if pnl_pct >= full_exit_atr * atr_pct:
        return {
            "reason": "ATR_PROFIT_2.5X",
            "reason_cn": (
                f"ATR 短线全平 (盈利 {pnl_pct*100:.2f}% ≥ "
                f"{full_exit_atr:.1f}×ATR/entry={full_exit_atr*atr_pct*100:.2f}%)"
            ),
            "exit_price": current_price,
            "close_ratio": 1.0,
            "description": (
                f"ATR-driven Day Trade TP-Full: pnl={pnl_pct*100:.2f}% ≥ "
                f"{full_exit_atr:.1f}×ATR_pct ({full_exit_atr*atr_pct*100:.2f}%)"
            ),
        }

    # 3. 部分止盈 (盈利达 1.5×ATR, 单次)
    if pnl_pct >= profit_target_atr * atr_pct and not trail_state.get("dt_atr_partial_taken"):
        size = max(int(getattr(position, "size", 0) or 0), 0)
        # 若已无 partial_close 基础设施 / 持仓过小 — 标记建议但不全平
        # 这里走标准 close_ratio 协议, 由上层 _execute_exit 决定是否能拆单
        close_qty = max(1, int(size * partial_close_ratio)) if size > 0 else 0
        if size <= 1 or close_qty >= size:
            # 持仓过小, 无法部分平仓 — 不在此处全平 (避免吃掉 2.5×ATR 的潜在收益), 仅标记
            logger.info(
                f"[ATR_DAYTRADE_TP1·跳过部分平仓] {position.symbol} "
                f"size={size} 不足以拆单, 等待 {full_exit_atr:.1f}×ATR 全平或止损"
            )
            trail_state["dt_atr_partial_taken"] = True  # 防止反复日志
            return None

        trail_state["dt_atr_partial_taken"] = True
        try:
            from app.risk.trail_state_store import save_trail_state
            save_trail_state(position.symbol, trail_state)
        except Exception as _silent_e:
            logger.debug(f"[silent_except:day_trade_exit.py] {_silent_e}")

        return {
            "reason": "ATR_PROFIT_1.5X",
            "reason_cn": (
                f"ATR 短线部分止盈 (盈利 {pnl_pct*100:.2f}% ≥ "
                f"{profit_target_atr:.1f}×ATR/entry={profit_target_atr*atr_pct*100:.2f}%, "
                f"减仓 {partial_close_ratio*100:.0f}%)"
            ),
            "exit_price": current_price,
            "close_ratio": partial_close_ratio,
            "close_qty": close_qty,
            "description": (
                f"ATR-driven Day Trade TP-Partial: pnl={pnl_pct*100:.2f}% ≥ "
                f"{profit_target_atr:.1f}×ATR_pct ({profit_target_atr*atr_pct*100:.2f}%), "
                f"plan close {close_qty}/{size}"
            ),
        }

    return None


def check_day_trade_exit(position, current_price, entry_price, atr, trail_state, now_ts,
                         direction, pnl_pct, policy_guardian=None):
    """Day Trade Mode 判定逻辑 (从 ExitMonitor._check_day_trade_exit 原样迁移)

    包含: Initial Stop, Take Profit Tiers, Profit Lock (单向TP移动), Time Decay

    任务 #45: 优先尝试 ATR 驱动新逻辑 (day_trade_atr.enabled=true), 异常或未启用时
    回退到下方 legacy 逻辑 (与 day_trade_legacy.enabled / day_trade_mode.enabled 解耦)。
    """
    # ── 任务 #45 (P1): ATR 驱动的新逻辑优先 ──
    try:
        atr_result = check_atr_day_trade_exit(
            position, current_price, entry_price, atr, trail_state,
            direction, pnl_pct, policy_guardian,
        )
        if atr_result is not None:
            return atr_result
        else:
            # 任务 #48: ATR 未触发 → 明确降级事件
            logger.debug(
                f"[DayTrade·降级] {getattr(position, 'symbol', '?')} "
                f"ATR检查未触发, 降级到 legacy 检查"
            )
    except Exception as _e:
        logger.warning(
            f"[ATR_DAYTRADE·异常] {getattr(position, 'symbol', '?')} "
            f"ATR-driven 退出检查失败, 回退到 legacy: {_e}"
        )

    # ── Legacy: TP=4%/8%/15% + ATR initial stop ──
    legacy_enabled = True
    if policy_guardian is not None and hasattr(policy_guardian, "policy"):
        legacy_cfg = policy_guardian.policy.get("day_trade_legacy", {})
        # 默认 enabled (向后兼容); 配置 enabled=false 时跳过 legacy
        legacy_enabled = bool(legacy_cfg.get("enabled", True))
    if not legacy_enabled:
        # 任务 #48: ATR 与 legacy 均被禁用 → 警告日志 (无日内退出规则)
        atr_enabled = False
        if policy_guardian is not None and hasattr(policy_guardian, "policy"):
            atr_enabled = bool(
                (policy_guardian.policy.get("day_trade_atr", {}) or {}).get("enabled", False)
            )
        if not atr_enabled:
            logger.warning(
                f"[DayTrade·配置异常] {getattr(position, 'symbol', '?')} "
                f"ATR和legacy均禁用, 无日内退出规则生效 (请检查 day_trade_atr / day_trade_legacy 配置)"
            )
        return None

    if not should_use_day_trade_mode(position, policy_guardian):
        return None

    dt_cfg = {}
    if policy_guardian and hasattr(policy_guardian, "policy"):
        dt_cfg = policy_guardian.policy.get("day_trade_mode", {})
    if not dt_cfg:
        return None

    symbol = position.symbol  # for PROFIT_LOCK log messages
    is_short = (direction == "SHORT")
    short_cfg = dt_cfg.get("short", {})

    # 幻方 P2: entry_session 路径依赖
    entry_session = trail_state.get("entry_session", "")
    entry_session_mult = 1.0
    if entry_session == "opening_drive":
        entry_session_mult = 0.85
    elif entry_session == "morning":
        entry_session_mult = 0.92

    # 1. Initial Stop
    init_stop = dt_cfg.get("initial_stop", {})
    stop_atr_mult = float(init_stop.get("atr_multiplier", 1.5)) * entry_session_mult
    max_loss = float(init_stop.get("max_loss_pct", 5.0)) / 100.0 * entry_session_mult
    if is_short:
        stop_atr_mult = float(short_cfg.get("initial_stop_atr", 1.2)) * entry_session_mult
        max_loss = float(short_cfg.get("max_loss_pct", 4.0)) / 100.0 * entry_session_mult

    if atr > 0:
        atr_stop_price = entry_price - stop_atr_mult * atr if not is_short else entry_price + stop_atr_mult * atr
    else:
        atr_stop_price = entry_price * (1 - max_loss) if not is_short else entry_price * (1 + max_loss)

    hard_stop_price = entry_price * (1 - max_loss) if not is_short else entry_price * (1 + max_loss)

    if not is_short:
        effective_stop = max(atr_stop_price, hard_stop_price)
        if current_price <= effective_stop:
            return {
                "reason": "DAYTRADE_STOP",
                "reason_cn": f"短线止损 (ATR {stop_atr_mult:.1f}x / max -{max_loss*100:.0f}%)",
                "exit_price": current_price,
                "description": f"触及短线止损 ${effective_stop:.2f} (ATR止损={atr_stop_price:.2f}, 硬止损={hard_stop_price:.2f})"
            }
    else:
        effective_stop = min(atr_stop_price, hard_stop_price)
        if current_price >= effective_stop:
            return {
                "reason": "DAYTRADE_STOP",
                "reason_cn": f"做空短线止损 (ATR {stop_atr_mult:.1f}x / max +{max_loss*100:.0f}%)",
                "exit_price": current_price,
                "description": f"触及做空短线止损 ${effective_stop:.2f}"
            }

    # 2. Take Profit Tiers
    tp_cfg = dt_cfg.get("take_profit", {})
    triggered = trail_state.get("dt_triggered_tiers", [])

    tp1_pct = float(tp_cfg.get("tier_1", {}).get("profit_pct", 2.0)) / 100.0
    if is_short:
        tp1_pct = float(short_cfg.get("take_profit_tier_1", 1.5)) / 100.0
    tp1_close = float(tp_cfg.get("tier_1", {}).get("close_pct", 50)) / 100.0

    if 0 not in triggered and pnl_pct >= tp1_pct:
        close_qty = max(1, int(position.size * tp1_close))
        triggered.append(0)
        trail_state["dt_triggered_tiers"] = triggered
        trail_state["dt_breakeven_activated"] = True
        return {
            "reason": ExitReason.DAYTRADE_TP1.value,
            "reason_cn": f"短线止盈 Tier1 (+{tp1_pct*100:.0f}% 平 {tp1_close*100:.0f}%)",
            "exit_price": current_price,
            "close_qty": close_qty,
            "description": f"短线止盈 Tier1: +{pnl_pct*100:.1f}% ≥ {tp1_pct*100:.0f}%, 平仓 {close_qty} 股, 剩余移保本"
        }

    tp2_pct = float(tp_cfg.get("tier_2", {}).get("profit_pct", 5.0)) / 100.0
    if is_short:
        tp2_pct = float(short_cfg.get("take_profit_tier_2", 4.0)) / 100.0
    tp2_close = float(tp_cfg.get("tier_2", {}).get("close_pct", 50)) / 100.0

    if 1 not in triggered and pnl_pct >= tp2_pct:
        triggered.append(1)
        trail_state["dt_triggered_tiers"] = triggered
        return {
            "reason": ExitReason.DAYTRADE_TP2.value,
            "reason_cn": f"短线止盈 Tier2 (+{tp2_pct*100:.0f}% 清仓)",
            "exit_price": current_price,
            "description": f"短线止盈 Tier2: +{pnl_pct*100:.1f}% ≥ {tp2_pct*100:.0f}%, 清仓"
        }

    # 3. Profit Lock (浮盈保护 — 单向 TP 移动)
    # 修复 (2026-05-27): profit_lock 不再框死利润
    # 新逻辑: 激活后 TP 只能单向移动 (LONG 上移 / SHORT 下移)
    pl_cfg = dt_cfg.get("profit_lock", {})
    if pl_cfg.get("enabled", True):
        activation = float(pl_cfg.get("activation_pct", 1.5)) / 100.0
        lock_level = float(pl_cfg.get("lock_level_pct", 0.5)) / 100.0
        trail_back_pct = float(pl_cfg.get("trail_back_pct", 0)) / 100.0
        if trail_back_pct <= 0:
            trail_back_pct = activation - lock_level

        if pnl_pct >= activation:
            tp_changed = False

            # 首次激活
            if not trail_state.get("dt_profit_lock_activated"):
                if not is_short:
                    initial_tp = entry_price * (1 + lock_level)
                else:
                    initial_tp = entry_price * (1 - lock_level)
                trail_state["dt_profit_lock_activated"] = True
                trail_state["dt_profit_lock_tp"] = initial_tp
                tp_changed = True
                logger.info(
                    f"PROFIT_LOCK activated for {symbol}: locked_tp={initial_tp:.4f}, "
                    f"activation={activation*100:.1f}%, pnl={pnl_pct*100:.2f}%"
                )

            # 更新峰值 PnL
            trail_state["dt_peak_pnl"] = max(trail_state.get("dt_peak_pnl", 0), pnl_pct)

            # 单向 TP 移动: 根据新峰值计算候选 TP, 只允许有利方向移动
            peak_pnl = trail_state["dt_peak_pnl"]
            current_tp = trail_state.get("dt_profit_lock_tp", 0)

            if not is_short:
                candidate_tp = entry_price * (1 + max(peak_pnl - trail_back_pct, lock_level))
                if candidate_tp > current_tp:
                    old_tp = current_tp
                    trail_state["dt_profit_lock_tp"] = candidate_tp
                    tp_changed = True
                    logger.info(
                        f"PROFIT_LOCK: TP raised for {symbol}: "
                        f"{old_tp:.4f} → {candidate_tp:.4f} (peak={peak_pnl*100:.2f}%)"
                    )
                elif candidate_tp < current_tp:
                    logger.info(
                        f"PROFIT_LOCK: TP move rejected for {symbol}: "
                        f"{candidate_tp:.4f} <= {current_tp:.4f}"
                    )
            else:
                candidate_tp = entry_price * (1 - max(peak_pnl - trail_back_pct, lock_level))
                if candidate_tp < current_tp:
                    old_tp = current_tp
                    trail_state["dt_profit_lock_tp"] = candidate_tp
                    tp_changed = True
                    logger.info(
                        f"PROFIT_LOCK: TP lowered for {symbol}: "
                        f"{old_tp:.4f} → {candidate_tp:.4f} (peak={peak_pnl*100:.2f}%)"
                    )
                elif candidate_tp > current_tp:
                    logger.info(
                        f"PROFIT_LOCK: TP move rejected for {symbol}: "
                        f"{candidate_tp:.4f} >= {current_tp:.4f}"
                    )

            # 仅在 TP 实际发生变更时持久化
            if tp_changed:
                try:
                    from app.risk.trail_state_store import save_trail_state
                    save_trail_state(symbol, trail_state)
                except Exception as _persist_err:
                    logger.debug(
                        f"PROFIT_LOCK: trail_state 持久化失败 {symbol}: {_persist_err}"
                    )

        # 检查是否触发退出 (价格穿越锁定 TP)
        if trail_state.get("dt_profit_lock_activated"):
            locked_tp = trail_state.get("dt_profit_lock_tp", 0)
            if locked_tp > 0:
                should_exit_lock = False
                if not is_short and current_price <= locked_tp:
                    should_exit_lock = True
                elif is_short and current_price >= locked_tp:
                    should_exit_lock = True

                if should_exit_lock:
                    return {
                        "reason": "DAYTRADE_PROFIT_LOCK",
                        "reason_cn": (
                            f"利润锁定退出 (峰值 +{trail_state.get('dt_peak_pnl', 0)*100:.1f}% "
                            f"→ 当前 +{pnl_pct*100:.1f}%, TP={locked_tp:.2f})"
                        ),
                        "exit_price": current_price,
                        "description": (
                            f"价格穿越锁定 TP ${locked_tp:.2f}, "
                            f"峰值 PnL +{trail_state.get('dt_peak_pnl', 0)*100:.1f}% → 当前 +{pnl_pct*100:.1f}%"
                        ),
                    }

    # 保本检查 (Tier1 触发后)
    if trail_state.get("dt_breakeven_activated") and pnl_pct <= 0.002:
        return {
            "reason": "DAYTRADE_BREAKEVEN",
            "reason_cn": "短线保本退出",
            "exit_price": current_price,
            "description": "Tier1 触发后回撤至保本线, 确保不亏损"
        }

    # 4. Time Decay
    td_cfg = dt_cfg.get("time_decay", {})
    if td_cfg.get("enabled", True):
        max_idle = int(td_cfg.get("max_idle_minutes", 120)) * 60
        min_progress = float(td_cfg.get("min_progress_pct", 0.3)) / 100.0
        if is_short:
            max_idle = int(short_cfg.get("max_idle_minutes", 90)) * 60

        # 幻方 P2 (2026-05-01): session-aware decay rate
        # Task #19: 通过 position_exit_monitor 模块属性获取 get_shared_kernel,
        # 以保留测试中 patch('app.risk.position_exit_monitor.get_shared_kernel') 的兼容性。
        from app.risk import position_exit_monitor as _pem
        session = _pem.get_shared_kernel().get_current_session()
        session_ratio = _pem.get_shared_kernel().get_session_vol_ratio(session)
        effective_max_idle = max_idle / max(session_ratio, 0.3)

        # 修复 (2026-05-12): 隔夜持仓重置基准时间
        if "dt_baseline_pnl" not in trail_state:
            entry_time = getattr(position, 'entry_time', None)
            if entry_time:
                try:
                    from datetime import datetime as _dt
                    if isinstance(entry_time, str):
                        et = _dt.fromisoformat(entry_time.replace(' ', 'T'))
                    else:
                        et = entry_time
                    holding_minutes = (now_ts - et.timestamp()) / 60
                    if holding_minutes > 60:
                        trail_state["dt_baseline_time"] = now_ts
                        trail_state["dt_baseline_pnl"] = pnl_pct
                        logger.info(
                            f"[DayTrade·time_decay] {position.symbol} 隔夜持仓 ({holding_minutes:.0f}min), "
                            f"重置时间衰减基准 (有效超时={effective_max_idle/60:.0f}min)"
                        )
                except Exception as _silent_e:
                    logger.debug(f"[silent_except:day_trade_exit.py] {_silent_e}")
            if "dt_baseline_pnl" not in trail_state:
                trail_state["dt_baseline_pnl"] = pnl_pct
                trail_state["dt_baseline_time"] = now_ts

        # 首次扫描宽限期: 前 30min 不触发 time_decay
        idle_seconds = now_ts - trail_state.get("dt_baseline_time", now_ts)
        if idle_seconds < 1800:
            return None

        baseline_pnl = trail_state.get("dt_baseline_pnl", 0)
        progress = pnl_pct - baseline_pnl

        if progress >= min_progress:
            trail_state["dt_baseline_pnl"] = pnl_pct
            trail_state["dt_baseline_time"] = now_ts
        else:
            idle_seconds = now_ts - trail_state.get("dt_baseline_time", now_ts)
            if idle_seconds >= effective_max_idle:
                return {
                    "reason": "DAYTRADE_TIME_DECAY",
                    "reason_cn": f"短线时间衰减 ({idle_seconds/60:.0f}min 无进展, session={session})",
                    "exit_price": current_price,
                    "description": (
                        f"持仓 {idle_seconds/60:.0f}min 无进展 "
                        f"(基准={baseline_pnl*100:.1f}%, 当前={pnl_pct*100:.1f}%, "
                        f"进展={progress*100:.1f}% < {min_progress*100:.1f}%, "
                        f"有效超时={effective_max_idle/60:.0f}min)"
                    )
                }

    return None


class DayTradeExitRule(BaseExitRule):
    """Day Trade Mode: 紧止损 + 主动止盈 + 利润锁定 + 时间衰减"""

    rule_name = "DAYTRADE"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_day_trade_exit(
            position,
            market_data["current_price"],
            market_data["entry_price"],
            market_data.get("effective_atr", market_data.get("atr", 0.0)),
            trail_state,
            market_data["now_ts"],
            market_data["direction"],
            market_data["pnl_pct"],
            market_data.get("policy_guardian"),
        )
