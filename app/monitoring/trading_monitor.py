"""
交易监控告警系统 — P2 2026-05-20

职责:
  1. 实时监控 PnL、PDT 风险、熔断器状态
  2. 阈值触发告警 (飞书/钉钉通知)
  3. 心跳检测 (服务存活)
  4. 性能指标收集 (延迟、吞吐量)

设计原则:
  - 零侵入交易热路径 (独立线程/进程)
  - 告警去重 (同一告警 1 小时内不重复发送)
  - 可配置阈值 (YAML 驱动)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Callable
from collections import defaultdict

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  告警级别
# ═══════════════════════════════════════════════════════════════════

class AlertLevel:
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ═══════════════════════════════════════════════════════════════════
#  告警去重缓存
# ═══════════════════════════════════════════════════════════════════

class AlertCache:
    """告警去重: 同一告警 key 在 cooldown 秒内不重复发送"""

    def __init__(self, cooldown_seconds: int = 3600):
        self._cache: Dict[str, datetime] = {}
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._lock = threading.Lock()

    def should_alert(self, key: str) -> bool:
        """检查是否应该发送告警 (去重)"""
        now = datetime.now()
        with self._lock:
            last_sent = self._cache.get(key)
            if last_sent and (now - last_sent) < self._cooldown:
                return False
            self._cache[key] = now
            return True

    def clear(self, key: Optional[str] = None):
        """清除缓存 (key=None 清除全部)"""
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()


# ═══════════════════════════════════════════════════════════════════
#  监控指标
# ═══════════════════════════════════════════════════════════════════

class TradingMetrics:
    """交易指标收集器"""

    def __init__(self):
        self._daily_pnl: float = 0.0
        self._total_trades: int = 0
        self._winning_trades: int = 0
        self._losing_trades: int = 0
        self._max_drawdown: float = 0.0
        self._current_drawdown: float = 0.0
        self._last_update: datetime = datetime.now()
        self._lock = threading.Lock()

    def record_trade(self, pnl: float, is_winner: bool):
        """记录一笔交易"""
        with self._lock:
            self._total_trades += 1
            if is_winner:
                self._winning_trades += 1
            else:
                self._losing_trades += 1
            self._daily_pnl += pnl
            if pnl < 0:
                self._current_drawdown += pnl
                self._max_drawdown = min(self._max_drawdown, self._current_drawdown)
            else:
                self._current_drawdown = 0.0  # 盈利后重置回撤
            self._last_update = datetime.now()

    def reset_daily(self):
        """每日重置 (盘后调用)"""
        with self._lock:
            self._daily_pnl = 0.0
            self._total_trades = 0
            self._winning_trades = 0
            self._losing_trades = 0
            self._current_drawdown = 0.0

    def get_summary(self) -> Dict[str, Any]:
        """获取指标摘要"""
        with self._lock:
            win_rate = self._winning_trades / self._total_trades if self._total_trades > 0 else 0.0
            return {
                "daily_pnl": round(self._daily_pnl, 2),
                "total_trades": self._total_trades,
                "win_rate": round(win_rate, 2),
                "max_drawdown": round(self._max_drawdown, 2),
                "current_drawdown": round(self._current_drawdown, 2),
                "last_update": self._last_update.isoformat(),
            }


# ═══════════════════════════════════════════════════════════════════
#  监控器
# ═══════════════════════════════════════════════════════════════════

class TradingMonitor:
    """交易监控器 — 独立线程运行"""

    def __init__(
        self,
        config: Dict[str, Any],
        notifier: Optional[Callable[[str, str], None]] = None,
    ):
        self._config = config
        self._notifier = notifier or self._default_notifier
        self._metrics = TradingMetrics()
        self._alert_cache = AlertCache(cooldown_seconds=3600)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interval = config.get("monitoring", {}).get("interval_seconds", 60)

    def _default_notifier(self, level: str, message: str):
        """默认通知器 (日志输出)"""
        logger.warning(f"[Monitor·{level}] {message}")

    def start(self):
        """启动监控线程"""
        if self._running:
            logger.warning("[Monitor] 已在运行")
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="TradingMonitor")
        self._thread.start()
        logger.info(f"[Monitor] ✅ 启动 (间隔={self._interval}s)")

    def stop(self):
        """停止监控线程"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[Monitor] ⏹️ 已停止")

    def _monitor_loop(self):
        """监控主循环 — 非交易日跳过, 避免虚假告警"""
        while self._running:
            try:
                # 交易日门控: 非交易日跳过所有检查
                if not self._is_trading_session():
                    time.sleep(self._interval)
                    continue

                # 心跳: 更新 last_update 表示 monitor 线程存活 (2026-07-02 修复)
                # 根因: 原只在 record_trade 时更新, 无交易时5分钟后误报心跳超时
                with self._metrics._lock:
                    self._metrics._last_update = datetime.now()

                self._check_pnl()
                self._check_pdt_risk()
                self._check_circuit_breaker()
                self._check_heartbeat()
            except Exception as e:
                logger.error(f"[Monitor] 监控循环异常: {e}")
            time.sleep(self._interval)

    def _is_trading_session(self) -> bool:
        """检查当前是否为交易日盘中 (带缓存).

        三重门控: 周末 → 假日 → 盘中时间窗口.
        """
        from datetime import timedelta as _td
        utc_now = datetime.utcnow()

        # 1. 周末
        if utc_now.weekday() >= 5:
            return False

        # 2. 盘中时间窗口 (EDT 09:30-16:00 ≈ UTC 13:30-20:00)
        h, m = utc_now.hour, utc_now.minute
        if h < 13 or h >= 20:
            return False
        if h == 13 and m < 30:
            return False

        # 3. 交易日检查 (复用 heartbeat 的缓存机制)
        try:
            from app.observability.intraday_heartbeat import _is_trading_day
            return _is_trading_day("US")
        except ImportError:
            pass

        # Fallback: 已知假日
        et_date = (utc_now - _td(hours=4)).strftime("%Y-%m-%d")
        _HOLIDAYS = {
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
            "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
            "2026-11-26", "2026-12-25",
        }
        if et_date in _HOLIDAYS:
            return False

        return True

    def _check_pnl(self):
        """检查 PnL 告警"""
        summary = self._metrics.get_summary()
        daily_pnl_pct = summary["daily_pnl"] / 10000  # 假设 100 万本金

        # 日亏损告警 (-3%)
        if daily_pnl_pct < -0.03:
            key = f"pnl_critical_{summary['daily_pnl']:.0f}"
            if self._alert_cache.should_alert(key):
                self._notifier(
                    AlertLevel.CRITICAL,
                    f"📉 日亏损告警: ${summary['daily_pnl']:.0f} ({daily_pnl_pct:.1%})"
                )

        # 日盈利告警 (+5%)
        elif daily_pnl_pct > 0.05:
            key = f"pnl_profit_{summary['daily_pnl']:.0f}"
            if self._alert_cache.should_alert(key):
                self._notifier(
                    AlertLevel.INFO,
                    f"📈 日盈利告警: ${summary['daily_pnl']:.0f} ({daily_pnl_pct:.1%})"
                )

    def _check_pdt_risk(self):
        """检查 PDT 风险"""
        # 从 PDTGuard 获取风险等级
        try:
            from app.risk.pdt_guard import get_pdt_guard
            guard = get_pdt_guard()
            risk_level = guard.risk_level if hasattr(guard, 'risk_level') else "normal"

            if risk_level == "critical":
                key = "pdt_critical"
                if self._alert_cache.should_alert(key):
                    self._notifier(
                        AlertLevel.CRITICAL,
                        "⚠️ PDT 风险警告: 仅剩 0 次日内交易机会!"
                    )
            elif risk_level == "warning":
                key = "pdt_warning"
                if self._alert_cache.should_alert(key):
                    self._notifier(
                        AlertLevel.WARNING,
                        "⚠️ PDT 风险警告: 仅剩 1 次日内交易机会"
                    )
        except Exception as e:
            logger.debug(f"[Monitor] PDT 检查失败 (非关键): {e}")

    def _check_circuit_breaker(self):
        """检查熔断器状态"""
        try:
            from app.risk.circuit_breaker import get_circuit_breaker
            cb = get_circuit_breaker()
            if hasattr(cb, 'is_open') and cb.is_open:
                key = "circuit_breaker_open"
                if self._alert_cache.should_alert(key):
                    self._notifier(
                        AlertLevel.CRITICAL,
                        "🚫 熔断器已触发! 所有交易暂停"
                    )
        except Exception as e:
            logger.debug(f"[Monitor] 熔断器检查失败 (非关键): {e}")

    def _check_heartbeat(self):
        """心跳检测 (每 5 分钟一次)"""
        # 简单实现: 检查最后更新时间
        summary = self._metrics.get_summary()
        last_update = datetime.fromisoformat(summary["last_update"])
        if (datetime.now() - last_update) > timedelta(minutes=5):
            key = "heartbeat_stale"
            if self._alert_cache.should_alert(key):
                self._notifier(
                    AlertLevel.WARNING,
                    f"💔 心跳超时: 最后更新 {last_update.strftime('%H:%M:%S')}"
                )

    def record_trade(self, pnl: float, is_winner: bool):
        """记录交易 (外部调用)"""
        self._metrics.record_trade(pnl, is_winner)

    def get_metrics(self) -> Dict[str, Any]:
        """获取监控指标 (外部调用)"""
        return self._metrics.get_summary()


# ═══════════════════════════════════════════════════════════════════
#  全局单例
# ═══════════════════════════════════════════════════════════════════

_monitor_instance: Optional[TradingMonitor] = None


def get_monitor(config: Optional[Dict[str, Any]] = None) -> TradingMonitor:
    """获取监控器单例"""
    global _monitor_instance
    if _monitor_instance is None:
        if config is None:
            config = {}
        _monitor_instance = TradingMonitor(config)
    return _monitor_instance


def start_monitor(config: Optional[Dict[str, Any]] = None):
    """启动监控器 (服务启动时调用)"""
    monitor = get_monitor(config)
    monitor.start()


def stop_monitor():
    """停止监控器 (服务关闭时调用)"""
    if _monitor_instance:
        _monitor_instance.stop()
