"""
Playbook Risk Circuit Breaker — 风控熔断 (spec 5.3)

熔断规则:
  1. 日内亏损熔断: 当日亏损 > 总资金 1% → 禁用执行按钮 + 飞书告警
  2. 连续亏损熔断:
     - 连续 3 笔亏损 → 仓位减半
     - 连续 5 笔亏损 → 停止开仓 1 天

Usage:
  from app.playbook.risk_circuit_breaker import RiskCircuitBreaker

  breaker = RiskCircuitBreaker(playbook_db)
  status = breaker.check_status(total_equity=100000)
  # status = {"daily_loss_ok": True, "consecutive_ok": True, "position_multiplier": 1.0, ...}
"""
import logging
from typing import Dict, Any, Optional
from datetime import date, datetime, timedelta

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)


class RiskCircuitBreaker:
    """风控熔断器"""

    def __init__(
        self,
        playbook_db: PlaybookDB = None,
        daily_loss_limit_pct: float = 1.0,
        consecutive_loss_reduce: int = 3,
        consecutive_loss_stop: int = 5,
    ):
        """
        Args:
            playbook_db: PlaybookDB 实例
            daily_loss_limit_pct: 日内亏损上限 (占总资金百分比)
            consecutive_loss_reduce: 连续 N 笔亏损 → 仓位减半
            consecutive_loss_stop: 连续 N 笔亏损 → 停止开仓 1 天
        """
        self.db = playbook_db or PlaybookDB()
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.consecutive_loss_reduce = consecutive_loss_reduce
        self.consecutive_loss_stop = consecutive_loss_stop
        self._stop_until: Optional[date] = None  # 停止开仓到何时

    def check_status(self, total_equity: float = 100000) -> Dict[str, Any]:
        """
        检查所有熔断状态

        Returns:
            {
                "daily_loss_ok": bool,          # 日内亏损是否在限额内
                "daily_loss": float,             # 当日已实现亏损
                "daily_loss_limit": float,       # 日内亏损上限
                "daily_loss_pct": float,         # 当日亏损占比
                "consecutive_losses": int,        # 连续亏损笔数
                "consecutive_ok": bool,          # 连续亏损是否在限额内
                "position_multiplier": float,    # 仓位乘数 (1.0 / 0.5 / 0.0)
                "trading_allowed": bool,         # 是否允许开仓
                "stop_reason": str,              # 停止原因 (如有)
                "alerts": List[str],             # 告警信息
            }
        """
        alerts = []

        # 1. 日内亏损检查
        daily_loss = self._get_daily_realized_loss()
        daily_loss_limit = total_equity * (self.daily_loss_limit_pct / 100)
        daily_loss_ok = abs(daily_loss) <= daily_loss_limit
        daily_loss_pct = (abs(daily_loss) / total_equity * 100) if total_equity > 0 else 0

        if not daily_loss_ok:
            alerts.append(f"⛔ 日内亏损熔断: 已亏 ${abs(daily_loss):.0f} > 限额 ${daily_loss_limit:.0f} ({daily_loss_pct:.1f}%)")

        # 2. 连续亏损检查
        consecutive_losses = self._get_consecutive_losses()
        consecutive_ok = consecutive_losses < self.consecutive_loss_stop

        position_multiplier = 1.0
        if consecutive_losses >= self.consecutive_loss_stop:
            position_multiplier = 0.0
            alerts.append(f"⛔ 连续 {consecutive_losses} 笔亏损 → 停止开仓 1 天")
        elif consecutive_losses >= self.consecutive_loss_reduce:
            position_multiplier = 0.5
            alerts.append(f"⚠️ 连续 {consecutive_losses} 笔亏损 → 仓位减半")

        # 3. 停止期检查
        trading_allowed = daily_loss_ok and consecutive_ok
        stop_reason = ""

        if self._stop_until and date.today() < self._stop_until:
            trading_allowed = False
            stop_reason = f"连续亏损熔断中, 恢复时间: {self._stop_until}"
            alerts.append(f"⛔ {stop_reason}")
        elif not daily_loss_ok:
            stop_reason = "日内亏损超限"
        elif not consecutive_ok:
            stop_reason = "连续亏损超限"
            # 设置停止期
            if self._stop_until is None or self._stop_until < date.today():
                self._stop_until = date.today() + timedelta(days=1)

        return {
            "daily_loss_ok": daily_loss_ok,
            "daily_loss": round(daily_loss, 2),
            "daily_loss_limit": round(daily_loss_limit, 2),
            "daily_loss_pct": round(daily_loss_pct, 2),
            "consecutive_losses": consecutive_losses,
            "consecutive_ok": consecutive_ok,
            "position_multiplier": position_multiplier,
            "trading_allowed": trading_allowed,
            "stop_reason": stop_reason,
            "alerts": alerts,
        }

    def reset(self):
        """重置熔断器 (新交易日开始)"""
        self._stop_until = None

    def _get_daily_realized_loss(self) -> float:
        """获取当日已实现亏损 (负数)"""
        today = date.today().isoformat()
        instructions = self.db.get_instructions_by_date(today)

        total_loss = 0.0
        for inst in instructions:
            if inst.get("executed") and inst.get("outcome") == "loss":
                pnl = inst.get("pnl", 0) or 0
                if pnl < 0:
                    total_loss += pnl

        return total_loss

    def _get_consecutive_losses(self) -> int:
        """获取从最近一笔开始往回的连续亏损数"""
        recent = self.db.get_recent_instructions(limit=50)

        # 只看已执行的、有结果的
        executed = [
            r for r in recent
            if r.get("executed") and r.get("outcome") in ("win", "loss")
        ]

        # 按时间倒序 (最近的在前)
        executed.sort(key=lambda x: x.get("execution_time", ""), reverse=True)

        count = 0
        for r in executed:
            if r.get("outcome") == "loss":
                count += 1
            else:
                break

        return count
