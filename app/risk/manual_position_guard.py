"""
ManualPositionGuard — 手动仓位风险监控

解决 P0 矛盾: 7 个手动仓位 (全部 PROTECTED) 在 ExitMonitor 中被跳过,
无任何自动保护。本模块只做监控+告警, 不做自动平仓 (保持手动控制)。

告警阈值:
  - WARNING  : 单仓浮亏 >= 5%  → 钉钉/飞书告警
  - CRITICAL : 单仓浮亏 >= 10% → 紧急告警 + 建议止损价
  - PORTFOLIO: 手动仓位组合浮亏 >= 5% → 组合级告警

设计原则:
  1. 不修改 ExitMonitor, 不触碰自动交易路径
  2. 复用 _get_current_price (Futu → Yahoo fallback)
  3. 告警冷却 30 分钟/标的/级别, 防刷屏
  4. 盘中每 60 秒扫描一次
"""

import logging
import time
import threading
from datetime import datetime
from typing import Dict, Optional, List, Any

logger = logging.getLogger(__name__)

# ── 告警阈值 ──
WARNING_PNL_PCT = -0.05       # -5%
CRITICAL_PNL_PCT = -0.10      # -10%
PORTFOLIO_WARNING_PCT = -0.05  # 组合浮亏 -5%

# ── 冷却 ──
ALERT_COOLDOWN_SECONDS = 1800  # 30 分钟
SCAN_INTERVAL_SECONDS = 60     # 60 秒

# ── 状态 ──
_alert_cooldowns: Dict[str, float] = {}  # "symbol:level" → last_alert_timestamp
_lock = threading.Lock()


def _should_alert(key: str) -> bool:
    """检查是否可以发送告警 (冷却期外)"""
    with _lock:
        last = _alert_cooldowns.get(key, 0)
        return (time.time() - last) >= ALERT_COOLDOWN_SECONDS


def _mark_alerted(key: str):
    """标记已发送告警"""
    with _lock:
        _alert_cooldowns[key] = time.time()


def _calc_pnl_pct(entry_price: float, current_price: float, is_long: bool) -> float:
    """计算盈亏百分比"""
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if is_long:
        return (current_price - entry_price) / entry_price
    else:
        return (entry_price - current_price) / entry_price


def _send_alert(message: str):
    """发送钉钉告警"""
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        notifier = DingTalkNotifier()
        notifier.send_text(message)
    except Exception as e:
        logger.warning(f"[ManualGuard] 钉钉告警发送失败: {e}")


def _is_protected_position(position) -> bool:
    """判断是否为手动/受保护仓位"""
    try:
        from app.positions.source import is_protected, get_protection_state, ProtectionState
        state = get_protection_state(position)
        return state in (ProtectionState.PROTECTED, ProtectionState.FROZEN)
    except Exception:
        # fallback: 检查 metadata
        meta = getattr(position, "metadata", {}) or {}
        source = meta.get("source", "")
        return source in ("manual", "legacy", "unknown")


def _get_direction_str(position) -> str:
    """获取方向字符串"""
    d = getattr(position, "direction", None)
    if d is None:
        return "UNKNOWN"
    if hasattr(d, "value"):
        return d.value.upper()
    return str(d).upper()


def _suggest_stop_price(entry_price: float, current_price: float, is_long: bool, pnl_pct: float) -> str:
    """生成建议止损价"""
    if is_long:
        # 建议: 当前价再跌 2% 的位置
        stop = current_price * 0.98
        return f"建议止损: ${stop:.2f} (现价下方2%)"
    else:
        # 空头: 当前价再涨 2% 的位置
        stop = current_price * 1.02
        return f"建议止损: ${stop:.2f} (现价上方2%)"


def manual_guard_loop(scheduler):
    """手动仓位风险监控主循环"""
    from app.core.scheduler_triggers import get_market_phase, MarketPhase

    logger.info("[ManualGuard] 手动仓位风险监控线程已启动")

    while scheduler.running:
        try:
            phase = get_market_phase()
            # 周末/深夜不扫描
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                _interruptible_sleep(scheduler, 300)
                continue

            # 获取所有仓位
            all_positions = scheduler.position_store.all()
            if not all_positions:
                _interruptible_sleep(scheduler, SCAN_INTERVAL_SECONDS)
                continue

            # 过滤: 只保留受保护/手动仓位
            manual_positions = [p for p in all_positions if _is_protected_position(p)]
            if not manual_positions:
                _interruptible_sleep(scheduler, SCAN_INTERVAL_SECONDS)
                continue

            # 获取价格并检查
            from app.pipeline.order_executor import _get_current_price
            alerts = []
            total_cost = 0.0
            total_unrealized_pnl = 0.0

            for pos in manual_positions:
                symbol = pos.symbol
                entry_price = float(getattr(pos, "avg_entry_price", 0) or 0)
                size = abs(float(getattr(pos, "size", 0) or 0))
                dir_str = _get_direction_str(pos)
                is_long = dir_str == "LONG"

                if entry_price <= 0 or size <= 0:
                    continue

                # 获取当前价格
                current_price = _get_current_price(scheduler, symbol)
                if not isinstance(current_price, (int, float)) or current_price <= 0:
                    continue

                # 计算盈亏
                pnl_pct = _calc_pnl_pct(entry_price, current_price, is_long)
                position_value = entry_price * size
                unrealized_pnl = (current_price - entry_price) * size if is_long else (entry_price - current_price) * size

                total_cost += position_value
                total_unrealized_pnl += unrealized_pnl

                # 检查阈值
                if pnl_pct <= CRITICAL_PNL_PCT:
                    key = f"{symbol}:critical"
                    if _should_alert(key):
                        stop_suggestion = _suggest_stop_price(entry_price, current_price, is_long, pnl_pct)
                        msg = (
                            f"🚨 [ManualGuard·紧急] {symbol}\n"
                            f"方向: {dir_str} | 数量: {size:.0f}\n"
                            f"入场: ${entry_price:.2f} | 现价: ${current_price:.2f}\n"
                            f"浮亏: {pnl_pct*100:.2f}% | 金额: ${unrealized_pnl:.2f}\n"
                            f"{stop_suggestion}\n"
                            f"⚠️ 此仓位为手动持仓, 无自动止损保护, 请立即处理!"
                        )
                        alerts.append(("critical", msg))
                        _mark_alerted(key)

                elif pnl_pct <= WARNING_PNL_PCT:
                    key = f"{symbol}:warning"
                    if _should_alert(key):
                        msg = (
                            f"⚠️ [ManualGuard·警告] {symbol}\n"
                            f"方向: {dir_str} | 数量: {size:.0f}\n"
                            f"入场: ${entry_price:.2f} | 现价: ${current_price:.2f}\n"
                            f"浮亏: {pnl_pct*100:.2f}% | 金额: ${unrealized_pnl:.2f}\n"
                            f"请关注此手动仓位, 接近 -10% 将触发紧急告警。"
                        )
                        alerts.append(("warning", msg))
                        _mark_alerted(key)

            # 组合级告警
            if total_cost > 0:
                portfolio_pnl_pct = total_unrealized_pnl / total_cost
                if portfolio_pnl_pct <= PORTFOLIO_WARNING_PCT:
                    key = "portfolio:warning"
                    if _should_alert(key):
                        # 构建仓位明细
                        lines = []
                        for pos in manual_positions:
                            sym = pos.symbol
                            ep = float(getattr(pos, "avg_entry_price", 0) or 0)
                            sz = abs(float(getattr(pos, "size", 0) or 0))
                            d = _get_direction_str(pos)
                            lines.append(f"  {sym}: {d} {sz:.0f}@${ep:.2f}")
                        details = "\n".join(lines)
                        msg = (
                            f"📊 [ManualGuard·组合告警] 手动仓位组合浮亏\n"
                            f"总成本: ${total_cost:,.0f} | 总浮亏: ${total_unrealized_pnl:,.2f}\n"
                            f"组合收益率: {portfolio_pnl_pct*100:.2f}%\n"
                            f"持仓明细:\n{details}"
                        )
                        alerts.append(("portfolio", msg))
                        _mark_alerted(key)

            # 发送告警
            for level, msg in alerts:
                _send_alert(msg)
                logger.warning(f"[ManualGuard] {level} alert sent")

        except Exception as e:
            logger.error(f"[ManualGuard] 扫描异常: {e}", exc_info=True)

        _interruptible_sleep(scheduler, SCAN_INTERVAL_SECONDS)


def _interruptible_sleep(scheduler, seconds: int):
    """可中断的 sleep, 响应 scheduler.running = False"""
    for _ in range(seconds):
        if not scheduler.running:
            return
        time.sleep(1)
