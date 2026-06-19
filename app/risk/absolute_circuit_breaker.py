"""
绝对风控底线 — Daily/Hourly/Consecutive Loss 硬熔断

Task #1 (2026-06-17): 系统级安全基础设施
- 不受任何 feature flag 或开关控制
- 触发后: 禁止新开仓, 已有仓位止损/止盈不受影响
- 恢复: 人工确认 OR 下一交易日自动重置
- 审计日志持久化到 data/audit/circuit_breaker.jsonl (不可删除)
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

from app.utils.timezone import now_et, _et_trade_date

logger = logging.getLogger(__name__)

# ── 审计日志路径 ──
_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "audit"
_AUDIT_FILE = _AUDIT_DIR / "circuit_breaker.jsonl"


def _write_audit(event: Dict[str, Any]) -> None:
    """追加写入审计日志 (不可删除, append-only)"""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        event["timestamp"] = now_et().isoformat()
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"[AbsoluteCircuitBreaker] 审计日志写入失败: {e}")


class AbsoluteCircuitBreaker:
    """
    绝对风控底线熔断器 (单例)

    三重保护:
    1. DAILY_MAX_LOSS: 日内累计亏损 > $2000 → 暂停新仓
    2. HOURLY_MAX_LOSS: 小时累计亏损 > $1000 → 暂停 1 小时
    3. MAX_CONSECUTIVE_LOSSES: 连续亏损 5 笔 → 暂停该策略

    此类不受任何 feature flag 控制, 是最后防线。
    """

    _instance: Optional["AbsoluteCircuitBreaker"] = None
    _lock = threading.Lock()

    # ── 硬限参数 (不可通过配置覆盖) ──
    DAILY_MAX_LOSS = -2000.0       # 日亏 > $2K → 暂停新仓
    HOURLY_MAX_LOSS = -1000.0      # 时亏 > $1K → 暂停 1 小时
    MAX_CONSECUTIVE_LOSSES = 5     # 连亏 5 笔 → 暂停该策略

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._state_lock = threading.Lock()

        # 日计数
        self._trade_date: str = _et_trade_date(now_et())
        self._daily_pnl: float = 0.0

        # 小时计数
        self._hourly_pnl: float = 0.0
        self._hour_start: datetime = now_et().replace(minute=0, second=0, microsecond=0)
        self._hourly_paused_until: Optional[datetime] = None

        # 连续亏损 (按 strategy_id)
        self._consecutive_losses: Dict[str, int] = {}
        self._strategy_paused: Dict[str, bool] = {}

        # 全局暂停 (日级别)
        self._daily_paused: bool = False

        # 手动重置标记
        self._manual_override: bool = False

        logger.info(
            "[AbsoluteCircuitBreaker] 初始化完成 | "
            f"DAILY_MAX_LOSS=${abs(self.DAILY_MAX_LOSS)} | "
            f"HOURLY_MAX_LOSS=${abs(self.HOURLY_MAX_LOSS)} | "
            f"MAX_CONSECUTIVE_LOSSES={self.MAX_CONSECUTIVE_LOSSES}"
        )

    def _check_new_day(self) -> None:
        """新交易日自动重置日计数"""
        today = _et_trade_date(now_et())
        if today != self._trade_date:
            logger.info(
                f"[AbsoluteCircuitBreaker] 新交易日 {today}, 重置日计数 "
                f"(前日PnL=${self._daily_pnl:.2f})"
            )
            _write_audit({
                "event": "daily_reset",
                "prev_date": self._trade_date,
                "new_date": today,
                "prev_daily_pnl": self._daily_pnl,
            })
            self._trade_date = today
            self._daily_pnl = 0.0
            self._daily_paused = False
            self._hourly_pnl = 0.0
            self._hourly_paused_until = None
            self._consecutive_losses.clear()
            self._strategy_paused.clear()
            self._manual_override = False

    def _check_new_hour(self) -> None:
        """检查是否进入新的小时窗口"""
        current_hour = now_et().replace(minute=0, second=0, microsecond=0)
        if current_hour > self._hour_start:
            self._hour_start = current_hour
            self._hourly_pnl = 0.0

    def can_open_new_position(self, strategy_id: str = "") -> Tuple[bool, str]:
        """
        检查是否允许开新仓

        Returns:
            (allowed: bool, reason: str)
            - allowed=True: 可以开仓, reason=""
            - allowed=False: 被拦截, reason 说明原因
        """
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()

            # 手动重置后放行
            if self._manual_override:
                return True, ""

            # 检查日级别暂停
            if self._daily_paused:
                reason = (
                    f"日亏损触发熔断: 累计${self._daily_pnl:.2f} "
                    f"超过限额${self.DAILY_MAX_LOSS}"
                )
                return False, reason

            # 检查小时级别暂停
            if self._hourly_paused_until and now_et() < self._hourly_paused_until:
                remaining = (self._hourly_paused_until - now_et()).total_seconds()
                reason = (
                    f"小时亏损触发熔断: 累计${self._hourly_pnl:.2f} "
                    f"超过限额${self.HOURLY_MAX_LOSS}, "
                    f"剩余{int(remaining)}秒恢复"
                )
                return False, reason

            # 检查策略连续亏损
            if strategy_id and self._strategy_paused.get(strategy_id, False):
                losses = self._consecutive_losses.get(strategy_id, 0)
                reason = (
                    f"策略 {strategy_id} 连亏{losses}笔, "
                    f"已达上限{self.MAX_CONSECUTIVE_LOSSES}, 暂停该策略"
                )
                return False, reason

            return True, ""

    def record_trade_result(self, strategy_id: str, pnl: float, symbol: str = "") -> None:
        """
        记录一笔交易结果, 更新熔断状态

        Args:
            strategy_id: 策略ID
            pnl: 盈亏金额 (正=盈利, 负=亏损)
            symbol: 标的代码
        """
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()

            # 累加日/小时PnL
            self._daily_pnl += pnl
            self._hourly_pnl += pnl

            # 连续亏损计数
            if pnl < 0:
                self._consecutive_losses[strategy_id] = (
                    self._consecutive_losses.get(strategy_id, 0) + 1
                )
            else:
                # 盈利则重置连续亏损
                self._consecutive_losses[strategy_id] = 0
                self._strategy_paused[strategy_id] = False

            # ── 触发检查 ──
            triggered = False

            # 1. 日亏损检查
            if self._daily_pnl <= self.DAILY_MAX_LOSS and not self._daily_paused:
                self._daily_paused = True
                triggered = True
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 日亏损触发! "
                    f"累计${self._daily_pnl:.2f} <= ${self.DAILY_MAX_LOSS} | "
                    f"暂停所有新仓 (恢复: 人工确认 OR 下一交易日)"
                )
                _write_audit({
                    "event": "daily_loss_triggered",
                    "daily_pnl": self._daily_pnl,
                    "threshold": self.DAILY_MAX_LOSS,
                    "last_trade": {"strategy_id": strategy_id, "pnl": pnl, "symbol": symbol},
                })

            # 2. 小时亏损检查
            if self._hourly_pnl <= self.HOURLY_MAX_LOSS and not self._hourly_paused_until:
                self._hourly_paused_until = now_et() + timedelta(hours=1)
                triggered = True
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 小时亏损触发! "
                    f"累计${self._hourly_pnl:.2f} <= ${self.HOURLY_MAX_LOSS} | "
                    f"暂停新仓1小时 (至 {self._hourly_paused_until.strftime('%H:%M')})"
                )
                _write_audit({
                    "event": "hourly_loss_triggered",
                    "hourly_pnl": self._hourly_pnl,
                    "threshold": self.HOURLY_MAX_LOSS,
                    "paused_until": self._hourly_paused_until.isoformat(),
                    "last_trade": {"strategy_id": strategy_id, "pnl": pnl, "symbol": symbol},
                })

            # 3. 连续亏损检查
            if (strategy_id and
                self._consecutive_losses.get(strategy_id, 0) >= self.MAX_CONSECUTIVE_LOSSES and
                not self._strategy_paused.get(strategy_id, False)):
                self._strategy_paused[strategy_id] = True
                triggered = True
                logger.critical(
                    f"🚨 [AbsoluteCircuitBreaker] 策略连亏触发! "
                    f"{strategy_id} 连亏{self._consecutive_losses[strategy_id]}笔 "
                    f">= {self.MAX_CONSECUTIVE_LOSSES} | 暂停该策略"
                )
                _write_audit({
                    "event": "consecutive_loss_triggered",
                    "strategy_id": strategy_id,
                    "consecutive_losses": self._consecutive_losses[strategy_id],
                    "threshold": self.MAX_CONSECUTIVE_LOSSES,
                    "last_trade": {"pnl": pnl, "symbol": symbol},
                })

            if not triggered:
                logger.debug(
                    f"[AbsoluteCircuitBreaker] 记录: {symbol} strategy={strategy_id} "
                    f"pnl=${pnl:.2f} | 日累计=${self._daily_pnl:.2f} "
                    f"时累计=${self._hourly_pnl:.2f} "
                    f"连亏={self._consecutive_losses.get(strategy_id, 0)}"
                )

    def manual_reset(self, reason: str = "") -> None:
        """
        人工确认重置 — 由运维/Dashboard 调用

        Args:
            reason: 重置原因 (审计留痕)
        """
        with self._state_lock:
            logger.warning(
                f"[AbsoluteCircuitBreaker] 手动重置! reason={reason} | "
                f"daily_pnl=${self._daily_pnl:.2f} daily_paused={self._daily_paused}"
            )
            _write_audit({
                "event": "manual_reset",
                "reason": reason,
                "state_before": {
                    "daily_pnl": self._daily_pnl,
                    "daily_paused": self._daily_paused,
                    "hourly_paused_until": (
                        self._hourly_paused_until.isoformat()
                        if self._hourly_paused_until else None
                    ),
                    "strategy_paused": dict(self._strategy_paused),
                },
            })
            self._manual_override = True
            self._daily_paused = False
            self._hourly_paused_until = None
            self._strategy_paused.clear()

    def get_status(self) -> Dict[str, Any]:
        """获取熔断器完整状态"""
        with self._state_lock:
            self._check_new_day()
            self._check_new_hour()
            return {
                "trade_date": self._trade_date,
                "daily_pnl": round(self._daily_pnl, 2),
                "daily_paused": self._daily_paused,
                "hourly_pnl": round(self._hourly_pnl, 2),
                "hourly_paused_until": (
                    self._hourly_paused_until.isoformat()
                    if self._hourly_paused_until else None
                ),
                "consecutive_losses": dict(self._consecutive_losses),
                "strategy_paused": dict(self._strategy_paused),
                "manual_override": self._manual_override,
                "thresholds": {
                    "daily_max_loss": self.DAILY_MAX_LOSS,
                    "hourly_max_loss": self.HOURLY_MAX_LOSS,
                    "max_consecutive_losses": self.MAX_CONSECUTIVE_LOSSES,
                },
            }


# ── 全局单例访问 ──
def get_absolute_circuit_breaker() -> AbsoluteCircuitBreaker:
    """获取 AbsoluteCircuitBreaker 全局单例"""
    return AbsoluteCircuitBreaker()
