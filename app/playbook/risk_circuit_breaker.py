"""
Playbook Risk Circuit Breaker — 风控熔断 (spec 5.3)

熔断规则 (以下为 spec 5.3 设计意图; 实际接线 ≠ 设计 — 见文末 DEB-timezone 第4轮登记):
  1. 日内亏损熔断: 当日亏损 > 总资金 1% → 禁用执行按钮 + 飞书告警
  2. 连续亏损熔断:
     - 连续 3 笔亏损 → 仓位减半
     - 连续 5 笔亏损 → 停止开仓 1 天

实际接线 (2026-08-30 DEB-timezone-duplication-audit 第4轮登记, 文档-实现对齐):
  上述"禁用执行按钮"/"飞书告警"/"停止开仓"/"仓位减半"均为 spec 设计意图,
  **从未接线** — 本模块无飞书调用, 全仓无任何执行门消费 check_status 的
  trading_allowed / position_multiplier; 唯一调用方是 pipeline 周五周报路径
  (logger.warning, 仅记录不阻断)。若未来接线为真实拦截, 属新增行为, 须用户明令。

Usage:
  from app.playbook.risk_circuit_breaker import RiskCircuitBreaker

  breaker = RiskCircuitBreaker(playbook_db)
  status = breaker.check_status(total_equity=100000)
  # status = {"daily_loss_ok": True, "consecutive_ok": True, "position_multiplier": 1.0, ...}

TZ-1 (DEB-20260830-timezone-duplication-audit, 2026-08-30):
  ① 窗口判定/赋值原用服务器 CST date.today() (CST 午夜 = ET 正午, 窗口盘中翻日) → 改 ET 日历日;
  ② _stop_until 原为纯内存字段, 唯一调用方 (pipeline 周报) 每轮新建实例 → "连亏5停1日"
     锁存从未跨调用生效 → 落盘 playbook_risk_state (锁存跨进程/跨调用)。
  落盘失败 → 退化为进程内锁存 (仅告警不阻断; 本组件在周报路径仅记录告警, spec 5.4)。
"""
# ── 日亏损防线登记 (2026-08-28 冲突显性化): "当日亏损"事实六套权威并存 ──
#   1. AccountCircuitBreaker  app/risk/account_circuit_breaker.py
#      净值 -1%/-2%/-3% 三档 (减半/停新仓/停新仓 — 全平从未接线)
#   2. AbsoluteCircuitBreaker app/risk/absolute_circuit_breaker.py
#      -$2000/日 + -$1000/时 + 连亏5笔 (类常量, 不受开关控制)
#   3. symbol_day_loss        app/risk/symbol_day_loss.py
#      -$500/标的/日 (出场日+净额, 主线/playbook 共用)
#   4. SymbolDailyLossGuard   app/risk/symbol_daily_loss_guard.py
#      净值0.5%/标的/日 + 连亏N笔 + 低胜率 (yaml risk.symbol_daily_loss_guard.*)
#   5. RiskCircuitBreaker     app/playbook/risk_circuit_breaker.py
#      总资金1% + 连亏3减半/连亏5停1日 (playbook 域; 周报告警组件 — "禁执行按钮+飞书"
#      从未接线, 全仓唯一调用方=周五周报 logger.warning, 仅记录不阻断; 2026-08-30 DEB-timezone 第4轮)
#   6. DrawdownController     app/risk/drawdown_control.py
#      日3% (risk.account.max_daily_drawdown, 持久化防重启归零; 仅主线平仓在喂)
# 六套独立查询/独立状态互不感知; 口径 % 与 $ 混杂, 触发先后交错, 合并/裁剪属
# 人工风险裁决 (每套有各自历史语义与恢复机制)。本登记保证读任一防线时看得见
# 全景; 已统一项: 1/3 出场日+净额 (2026-08-27/28), 其余各口径见各自头注。
import logging
from typing import Dict, Any, Optional
from datetime import date, datetime, timedelta

from app.playbook.db import PlaybookDB
from app.utils.timezone import _et_trade_date, now_et

logger = logging.getLogger(__name__)

_STOP_UNTIL_KEY = "stop_until"


def _et_today() -> date:
    """ET 日历日 (锁存窗口判定口径; 服务器 CST 的 date.today() 在 ET 正午翻日, TZ-1)。"""
    return now_et().date()


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
        self._stop_until: Optional[date] = self._load_stop_until()  # 停止开仓到何时 (TZ-1: 落盘恢复)

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
        today = _et_today()  # TZ-1: ET 日历日 (原 date.today() 为服务器 CST, ET 正午翻日)

        if self._stop_until and today < self._stop_until:
            trading_allowed = False
            stop_reason = f"连续亏损熔断中, 恢复时间: {self._stop_until}"
            alerts.append(f"⛔ {stop_reason}")
        elif not daily_loss_ok:
            stop_reason = "日内亏损超限"
        elif not consecutive_ok:
            stop_reason = "连续亏损超限"
            # 设置停止期 (TZ-1: 落盘, 跨调用/跨进程锁存)
            if self._stop_until is None or self._stop_until < today:
                self._stop_until = today + timedelta(days=1)
                self._save_stop_until()

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
        self._save_stop_until()

    def _load_stop_until(self) -> Optional[date]:
        """从 playbook_risk_state 恢复锁存窗口 (无行/不可解析 → None = 未熔断)"""
        try:
            raw = self.db.get_risk_state(_STOP_UNTIL_KEY)
        except Exception as e:
            logger.warning(f"[RiskCircuitBreaker] stop_until 读取失败, 按未熔断处理: {e}")
            return None
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            logger.warning(f"[RiskCircuitBreaker] stop_until 落盘值不可解析, 忽略: {raw!r}")
            return None

    def _save_stop_until(self) -> None:
        """锁存窗口落盘/清除 (失败 → 退化为进程内锁存, 不阻断调用方)"""
        try:
            self.db.set_risk_state(
                _STOP_UNTIL_KEY,
                self._stop_until.isoformat() if self._stop_until else None,
            )
        except Exception as e:
            logger.warning(f"[RiskCircuitBreaker] stop_until 落盘失败, 锁存退化为进程内: {e}")

    def _get_daily_realized_loss(self) -> float:
        """获取当日已实现亏损 (负数)"""
        today = _et_trade_date(now_et())
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
