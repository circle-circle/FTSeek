"""
P2 修复 (2026-05-01): 盘中实时归因报告

每小时整点输出简版归因报告，覆盖：
1. 当前敞口分布 (含 ETF 折扣后)
2. 被拒绝信号 Top 5 (按 strength 排序)
3. 活跃退出条件状态
4. API 调用计数 / 数据源状态

与 post_audit (事后审计) 互补: 这个是盘中的, 帮助及时发现运行层问题。
"""
import logging
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger("ftseek.intraday_attribution")

# 全局累积拒绝记录 (跨扫描周期保留)
_rejection_log: List[Dict] = []
_rejection_log_lock = threading.Lock()
MAX_REJECTION_LOG = 200  # 最多保留 200 条拒绝记录


def record_rejection(symbol: str, reason: str, strength: float = 0, direction: str = "LONG"):
    """记录一次风控拒绝 (由 signal_pipeline / risk_engine 调用)"""
    entry = {
        "symbol": symbol,
        "reason": reason,
        "strength": strength,
        "direction": direction,
        "time": datetime.now().isoformat(),
    }
    with _rejection_log_lock:
        _rejection_log.append(entry)
        if len(_rejection_log) > MAX_REJECTION_LOG:
            _rejection_log[:] = _rejection_log[-MAX_REJECTION_LOG:]


def get_top_rejections(n: int = 5) -> List[Dict]:
    """获取按 strength 排序的 Top N 被拒绝信号"""
    with _rejection_log_lock:
        sorted_log = sorted(_rejection_log, key=lambda x: x.get("strength", 0), reverse=True)
        return sorted_log[:n]


def get_rejection_summary() -> Dict[str, int]:
    """按拒绝原因统计"""
    summary: Dict[str, int] = defaultdict(int)
    with _rejection_log_lock:
        for entry in _rejection_log:
            reason = entry.get("reason", "UNKNOWN")
            # 简化原因: 取第一个词
            short_reason = reason.split(":")[0].split(" ")[0]
            summary[short_reason] += 1
    return dict(summary)


def generate_attribution_report(
    position_store=None,
    capital_safety=None,
    data_router=None,
    exit_monitor=None,
) -> str:
    """生成盘中归因报告 (纯文本, 用于日志输出)

    Args:
        position_store: PositionStore 实例
        capital_safety: CapitalSafetySystem 实例
        data_router: MarketDataRouter 实例
        exit_monitor: ExitMonitor 实例

    Returns:
        格式化的归因报告文本
    """
    lines = []
    lines.append("=" * 60)
    lines.append(f"📊 FTSeek-US 盘中归因报告 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)

    # ── 1. 敞口分布 ──
    lines.append("\n── 1. 敞口分布 ──")
    if position_store:
        try:
            positions = position_store.all()
            if positions:
                from app.risk.position_limits import get_etf_discount

                # P0 修复 (2026-05-09): 按来源分类展示持仓
        # 按来源分类展示持仓
                main_positions = []
                other_positions = []
                
                for p in positions:
                    meta = getattr(p, 'metadata', {}) or {}
                    source = meta.get('source_type', 'MAIN_PIPELINE')
                    if source == 'MAIN_PIPELINE':
                        main_positions.append(p)
                    else:
                        other_positions.append(p)
                
                total_value = 0.0
                
                # 主线策略持仓
                if main_positions:
                    lines.append("  🟢 主线策略 (LLM投票):")
                    for p in main_positions:
                        value = p.size * p.avg_entry_price
                        total_value += value
                        direction = p.direction.value if hasattr(p.direction, 'value') else str(p.direction)
                        pnl_str = f" SL=${p.current_sl:.2f}" if hasattr(p, 'current_sl') and p.current_sl > 0 else ""
                        lines.append(
                            f"    {p.symbol:25s} {direction:5s} "
                            f"{p.size:>6.0f}股 @ ${p.avg_entry_price:>8.2f} = ${value:>10,.0f}{pnl_str}"
                        )
                
                # 其他来源 (对账恢复/人工)
                if other_positions:
                    lines.append("  ⚪ 其他来源 (对账恢复/人工):")
                    for p in other_positions:
                        value = p.size * p.avg_entry_price
                        total_value += value
                        direction = p.direction.value if hasattr(p.direction, 'value') else str(p.direction)
                        source = (getattr(p, 'metadata', {}) or {}).get('source', 'unknown')
                        pnl_str = f" SL=${p.current_sl:.2f}" if hasattr(p, 'current_sl') and p.current_sl > 0 else ""
                        lines.append(
                            f"    {p.symbol:25s} {direction:5s} "
                            f"{p.size:>6.0f}股 @ ${p.avg_entry_price:>8.2f} = ${value:>10,.0f} [{source}]{pnl_str}"
                        )

                lines.append(f"  ── 持仓数: 主线{len(main_positions)} + 其他{len(other_positions)} = {len(positions)} | 总市值: ${total_value:,.0f}")
            else:
                lines.append("  (无持仓)")
        except Exception as e:
            lines.append(f"  ⚠️ 获取敞口失败: {e}")

    # ── 2. 资金状态 ──
    lines.append("\n── 2. 资金状态 ──")
    if capital_safety:
        try:
            state = getattr(capital_safety, '_state', None)
            if state:
                lines.append(f"  当前净值: ${state.current_equity:,.0f}")
                lines.append(f"  峰值净值: ${state.peak_equity:,.0f}")
                if state.peak_equity > 0:
                    drawdown = (state.peak_equity - state.current_equity) / state.peak_equity
                    lines.append(f"  回撤: {drawdown*100:.1f}%")
        except Exception as e:
            lines.append(f"  ⚠️ 获取资金状态失败: {e}")

    # ── 3. 被拒绝信号 Top 5 ──
    lines.append("\n── 3. 被拒绝信号 Top 5 (按 strength) ──")
    top = get_top_rejections(5)
    if top:
        for i, r in enumerate(top, 1):
            lines.append(
                f"  {i}. {r['symbol']:25s} {r['direction']:5s} "
                f"strength={r['strength']:3.0f} reason={r['reason']}"
            )
    else:
        lines.append("  (无拒绝记录)")

    # ── 4. 拒绝原因统计 ──
    lines.append("\n── 4. 拒绝原因分布 ──")
    summary = get_rejection_summary()
    if summary:
        for reason, count in sorted(summary.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {reason:30s}: {count:>4d} 次")
    else:
        lines.append("  (无拒绝)")

    # ── 5. 数据源状态 ──
    lines.append("\n── 5. 数据源状态 ──")
    if data_router:
        try:
            status = getattr(data_router, 'status', {})
            if status:
                for k, v in status.items():
                    lines.append(f"  {k}: {v}")
            else:
                lines.append("  (状态不可用)")
        except Exception:
            lines.append("  (获取状态失败)")
    else:
        lines.append("  (data_router 不可用)")

    lines.append("\n" + "=" * 60)
    return "\n".join(lines)


class IntradayAttributionScheduler:
    """盘中归因定时器 (后台线程, 每小时一次)"""

    def __init__(self, interval_minutes: int = 60):
        self._interval = interval_minutes * 60
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._position_store = None
        self._capital_safety = None
        self._data_router = None
        self._exit_monitor = None

    def inject_deps(self, position_store=None, capital_safety=None, data_router=None, exit_monitor=None):
        self._position_store = position_store
        self._capital_safety = capital_safety
        self._data_router = data_router
        self._exit_monitor = exit_monitor

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="IntradayAttribution", daemon=True)
        self._thread.start()
        logger.info(f"[盘中归因] 已启动 (间隔={self._interval // 60}min)")

    def stop(self):
        self._running = False

    def _loop(self):
        next_run = time.time() + self._interval
        while self._running:
            # 假日/周末: 跳过归因报告 (市场关闭无需归因)
            from app.core.scheduler_triggers import sleep_if_market_closed
            if sleep_if_market_closed(60, lambda: self._running):
                next_run = time.time() + self._interval
                continue

            # 用短间隔轮询代替长 sleep, 确保 stop() 能快速响应
            time.sleep(min(10, self._interval))
            if not self._running:
                break
            if time.time() < next_run:
                continue
            next_run = time.time() + self._interval
            try:
                report = generate_attribution_report(
                    position_store=self._position_store,
                    capital_safety=self._capital_safety,
                    data_router=self._data_router,
                    exit_monitor=self._exit_monitor,
                )
                try:
                    from app.core.scheduler_triggers import is_market_open
                    _log_fn = logger.info if is_market_open() else logger.debug
                except ImportError:
                    _log_fn = logger.info
                _log_fn(report)
            except Exception as e:
                logger.error(f"[盘中归因] 生成报告异常: {e}")


# 全局单例
_attribution_scheduler: Optional[IntradayAttributionScheduler] = None


def get_intraday_attribution() -> IntradayAttributionScheduler:
    global _attribution_scheduler
    if _attribution_scheduler is None:
        _attribution_scheduler = IntradayAttributionScheduler(interval_minutes=60)
    return _attribution_scheduler
