"""
绝对风控底线 — Daily/Hourly/Consecutive Loss 硬熔断

Task #1 (2026-06-17): 系统级安全基础设施
- 不受任何 feature flag 或开关控制
- 触发后: 禁止新开仓, 已有仓位止损/止盈不受影响
- 恢复: 人工确认 OR 下一交易日自动重置
- 审计日志持久化到 data/audit/circuit_breaker.jsonl (不可删除)
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

        # P0 (2026-08-28 全链路复查): 启动即从台账重建当日计数.
        #   本类此前纯内存态, 而 ftseek-us.service 配 Restart=on-failure/RestartSec=30 →
        #   盘中一次崩溃自愈就把"当日已触发停新仓"清零且零告警: 自称"最后防线、不受任何
        #   feature flag 控制", 实际一次重启即可解除。
        self._restore_from_db()

    def _restore_from_db(self) -> None:
        """从 trade_records 重建当日 日亏/时亏/连亏 (出场日记账 + 净额口径).

        总体对齐: 实盘只有 playbook 一个喂数方
        (app/playbook/pipeline.py: record_trade_result("playbook", ...)), 故重建范围取
        source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') — 新增喂数策略时需同步扩该映射,
        否则恢复值会低于实时累计值。

        已知代价 (fail-closed, 有意): manual_reset 的 _manual_override 只存内存,
        故人工放行不跨重启 — 崩溃自愈后若当日亏损仍超线, 熔断会重新触发。此前重启
        连"触发"本身都一起清零 (即本修复要堵的洞), 所以净效果是变严不变松。
        人工放行需持久化时应另建 override 存储, 不是跳过重建。
        """
        try:
            import sqlite3
            from app.core import paths as _paths
            db_path = str(_paths.get_trade_records_db())
            today = _et_trade_date(now_et())
            hour_prefix = now_et().strftime("%Y-%m-%dT%H")
            _src_filter = "source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')"

            with sqlite3.connect(db_path) as conn:
                day_row = conn.execute(
                    f"SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0), COUNT(*) "
                    f"FROM trade_records WHERE substr(exit_time, 1, 10) = ? "
                    f"AND exit_price > 0 AND {_src_filter}",
                    (today,),
                ).fetchone()
                hour_row = conn.execute(
                    f"SELECT COALESCE(SUM(COALESCE(pnl_net, realized_pnl)), 0) "
                    f"FROM trade_records WHERE substr(exit_time, 1, 13) = ? "
                    f"AND exit_price > 0 AND {_src_filter}",
                    (hour_prefix,),
                ).fetchone()
                # 净额缺失行不参与连亏链 (避免一行 NULL 被当成"非亏"把连亏清零)
                pnl_rows = conn.execute(
                    f"SELECT COALESCE(pnl_net, realized_pnl) AS net FROM trade_records "
                    f"WHERE substr(exit_time, 1, 10) = ? AND exit_price > 0 "
                    f"AND COALESCE(pnl_net, realized_pnl) IS NOT NULL AND {_src_filter} "
                    f"ORDER BY exit_time DESC",
                    (today,),
                ).fetchall()

            self._trade_date = today
            self._daily_pnl = float(day_row[0] or 0.0)
            self._hourly_pnl = float(hour_row[0] or 0.0)

            consecutive = 0
            for _r in pnl_rows:
                if float(_r[0]) < 0:
                    consecutive += 1
                else:
                    break
            if consecutive:
                self._consecutive_losses["playbook"] = consecutive

            if self._daily_pnl <= self.DAILY_MAX_LOSS:
                self._daily_paused = True
            if self._hourly_pnl <= self.HOURLY_MAX_LOSS:
                self._hourly_paused_until = now_et() + timedelta(hours=1)
            if consecutive >= self.MAX_CONSECUTIVE_LOSSES:
                self._strategy_paused["playbook"] = True

            logger.info(
                f"[AbsoluteCircuitBreaker·restore] ET 今日({today}) 已了结 "
                f"{int(day_row[1] or 0)} 笔 净额=${self._daily_pnl:.2f} "
                f"(本小时=${self._hourly_pnl:.2f}, 连亏={consecutive}) | "
                f"daily_paused={self._daily_paused} "
                f"strategy_paused={self._strategy_paused.get('playbook', False)}"
            )
        except Exception as e:
            # 重建失败不阻断启动 (保留零值), 但留 WARNING — 失明的防线不该静默
            logger.warning(
                f"[AbsoluteCircuitBreaker·restore] 当日计数重建失败, 保留零值: {e}"
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
