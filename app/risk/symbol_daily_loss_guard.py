"""
SymbolDailyLossGuard (2026-05-15, 基于昨日 LCID 8 笔 -$634 教训)

防止单标的反复亏损叠加. 触发任一规则 → 当日 cooldown:
- 同 symbol 当日累计亏损 > N% 净值
- 同 symbol 当日连续亏损 N 笔
- 同 symbol 当日交易 ≥ N 笔且胜率 < 30%

设计:
- 启动时从 trade_records 重建当日状态 (重启后立即生效)
- 内存维护 + DB 日级 cache
- 触发后告警 + 写入 daily_review
- 所有日期判断统一使用美东时间 (app.utils.timezone)
- 非交易日不触发告警 (防止周末/节假日推送噪音)
- 重启去重: 同一 ET 交易日内已推送的告警不重复发

可控:
- yaml `risk.symbol_daily_loss_guard.*` 全部参数化
- 默认 enabled=true (关键防御, 永远开)
- 可单 symbol 手动豁免 (overrides.exempt_symbols)
"""
from __future__ import annotations
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from app.utils.timezone import now_et, now_cst, get_trade_date, to_et

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  交易日历查询 (统一入口)
# ─────────────────────────────────────────────────────────────

def _is_us_trading_day() -> bool:
    """判断当前是否为美股交易日 (周末+节假日)"""
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return get_holiday_calendar().is_trading_day("US", now_et().date())
    except Exception:
        # fallback: 仅周末判断
        return now_et().weekday() < 5


def _et_eod_timestamp() -> float:
    """计算 ET 当日 23:59:59 的 Unix timestamp (用于 cooldown 截止)"""
    dt = now_et()
    eod = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return eod.timestamp()


# ─────────────────────────────────────────────────────────────
#  状态数据结构
# ─────────────────────────────────────────────────────────────

@dataclass
class _SymbolState:
    symbol: str
    total_pnl_today: float = 0.0
    consecutive_losses: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    last_trade_ts: float = 0.0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    _alert_sent: bool = False  # 防止同日内重启重复推送

    @property
    def loss_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return 1.0 - (self.winning_trades / self.total_trades)


# ─────────────────────────────────────────────────────────────
#  主类
# ─────────────────────────────────────────────────────────────

class SymbolDailyLossGuard:
    """单标的日级亏损护栏 (单例)"""

    def __init__(
        self,
        max_daily_loss_pct: float = 0.01,    # 1% 净值
        max_consecutive_losses: int = 3,
        min_trades_for_winrate_check: int = 5,
        max_loss_rate: float = 0.7,           # 5+ 笔且亏损率 > 70% 触发
        cooldown_until_eod: bool = True,
    ):
        self._states: Dict[str, _SymbolState] = {}
        self._lock = threading.Lock()
        self._exempt_symbols: Set[str] = set()
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.min_trades_for_winrate_check = min_trades_for_winrate_check
        self.max_loss_rate = max_loss_rate
        self.cooldown_until_eod = cooldown_until_eod
        self._equity_provider = None
        self._today: str = get_trade_date()  # ET 日期

    def set_equity_provider(self, fn):
        """注入 equity 获取函数 (cap 计算用)"""
        self._equity_provider = fn

    def set_exempt(self, symbols: List[str]):
        with self._lock:
            self._exempt_symbols = set(symbols or [])

    def _ensure_today_state(self):
        """每日重置. 使用 ET 日期做跨日检测."""
        today = get_trade_date()
        if today != self._today:
            with self._lock:
                self._states.clear()
                self._today = today
            logger.info(f"[SymbolDailyLossGuard] 跨日重置 → {today} (ET)")

    def restore_from_db(self):
        """
        启动时从 trade_records 重建当日状态.

        关键: exit_time 存储为 CST naive datetime, 需要转换为 ET 日期后匹配.
        查询策略: 取最近 36 小时的交易, 在 Python 中用 ET 日期过滤.
        """
        try:
            from app.core.paths import get_trade_records_db
            today_et = get_trade_date()

            # 非交易日不重建 (避免把昨天 ET 交易算到今天)
            if not _is_us_trading_day():
                logger.info(
                    f"[SymbolDailyLossGuard·restore] 非交易日 ({today_et} ET), 跳过重建"
                )
                return

            conn = sqlite3.connect(str(get_trade_records_db()))
            # P1 修复 (2026-06-02): cutoff 必须用 CST 匹配 DB 中的 CST naive exit_time
            # 旧 bug: now_et() 返回 ET 时间, 与 CST DB 值比较导致 12h 偏移
            # 修复: 使用 now_cst() 计算 cutoff, 与 DB 存储格式一致
            cutoff_cst = now_cst().replace(tzinfo=None) - timedelta(hours=36)
            rows = conn.execute(
                "SELECT symbol, realized_pnl, exit_time FROM trade_records "
                "WHERE exit_time >= ? AND realized_pnl IS NOT NULL "
                "ORDER BY exit_time",
                (cutoff_cst.strftime("%Y-%m-%dT%H:%M:%S"),),
            ).fetchall()
            conn.close()

            with self._lock:
                for sym, pnl, exit_time_str in rows:
                    if not sym:
                        continue
                    # 将 CST naive exit_time 转为 ET 日期
                    try:
                        exit_dt = datetime.fromisoformat(exit_time_str)
                        exit_et_date = to_et(exit_dt).strftime("%Y-%m-%d")
                    except Exception:
                        # fallback: _et_trade_date 逻辑 (CST 减 12 小时 ≈ ET 日期)
                        exit_et_date = (
                            datetime.fromisoformat(exit_time_str) - timedelta(hours=12)
                        ).strftime("%Y-%m-%d")

                    if exit_et_date != today_et:
                        continue  # 不属于今天的 ET 交易日

                    state = self._states.setdefault(sym, _SymbolState(symbol=sym))
                    state.total_pnl_today += float(pnl or 0)
                    state.total_trades += 1
                    if pnl is not None and pnl > 0:
                        state.winning_trades += 1
                        state.consecutive_losses = 0
                    elif pnl is not None and pnl < 0:
                        state.consecutive_losses += 1
                    state.last_trade_ts = time.time()
                    # 检查是否触发 cooldown (restore 时标记 _alert_sent 避免重推)
                    self._check_trigger(state, suppress_alert=True)

            logger.info(
                f"[SymbolDailyLossGuard·restore] 重建 {len(self._states)} symbols 状态, "
                f"ET 今日({today_et})已平仓 {sum(s.total_trades for s in self._states.values())} 笔"
            )
        except Exception as e:
            logger.warning(f"[SymbolDailyLossGuard·restore] 失败: {e}")

    def record_trade(self, symbol: str, realized_pnl: float):
        """每笔交易平仓时调"""
        self._ensure_today_state()
        if not symbol:
            return
        with self._lock:
            state = self._states.setdefault(symbol, _SymbolState(symbol=symbol))
            state.total_pnl_today += realized_pnl
            state.total_trades += 1
            if realized_pnl > 0:
                state.winning_trades += 1
                state.consecutive_losses = 0
            elif realized_pnl < 0:
                state.consecutive_losses += 1
            state.last_trade_ts = time.time()
            self._check_trigger(state, suppress_alert=False)

    def _check_trigger(self, state: _SymbolState, suppress_alert: bool = False):
        """检查 + 标记 cooldown. 必须在 _lock 内调"""
        if state.cooldown_until > time.time():
            return  # 已 cooldown

        triggered = False
        reason = ""

        # 1) 累计亏损上限
        if self._equity_provider:
            try:
                equity = float(self._equity_provider())
                if equity > 0:
                    loss_pct = -state.total_pnl_today / equity
                    if loss_pct >= self.max_daily_loss_pct:
                        reason = f"daily_loss_pct={loss_pct*100:.2f}% >= {self.max_daily_loss_pct*100}%"
                        triggered = True
            except Exception:
                pass

        # 2) 连续亏损 N 笔
        if not triggered and state.consecutive_losses >= self.max_consecutive_losses:
            reason = f"consecutive_losses={state.consecutive_losses} >= {self.max_consecutive_losses}"
            triggered = True

        # 3) 5+ 笔且胜率 < 30%
        if not triggered and state.total_trades >= self.min_trades_for_winrate_check:
            if state.loss_rate >= self.max_loss_rate:
                reason = f"loss_rate={state.loss_rate*100:.0f}% (n={state.total_trades})"
                triggered = True

        if triggered:
            self._trigger(state, reason, suppress_alert=suppress_alert)

    def _trigger(self, state: _SymbolState, reason: str, suppress_alert: bool = False):
        """标记 cooldown + 推送告警 (带去重)"""
        # 设置 cooldown 截止 (ET EOD)
        if self.cooldown_until_eod:
            state.cooldown_until = _et_eod_timestamp()
        else:
            state.cooldown_until = time.time() + 86400

        state.cooldown_reason = reason
        logger.warning(
            f"🚫 [SymbolDailyLossGuard] {state.symbol} 当日 cooldown 触发: {reason} "
            f"(today_pnl=${state.total_pnl_today:.2f} trades={state.total_trades} "
            f"win={state.winning_trades})"
        )

        # 告警去重: 同一 symbol 同日内只推一次
        if suppress_alert or state._alert_sent:
            if not state._alert_sent:
                state._alert_sent = True  # restore 时标记, 防止后续重启重推
            return

        state._alert_sent = True

        # 非交易日不推送 (防御兜底)
        if not _is_us_trading_day():
            logger.info(
                f"[SymbolDailyLossGuard] 非交易日, 跳过 {state.symbol} 告警推送"
            )
            return

        try:
            from app.utils.critical_alerts import send_critical
            send_critical(
                f"🚫 标的日亏损护栏触发: {state.symbol}\n"
                f"原因: {reason}\n"
                f"今日: {state.total_trades} 笔, 胜 {state.winning_trades}, "
                f"PnL ${state.total_pnl_today:.2f}\n"
                f"当日剩余时间禁止开仓",
                async_mode=True,
            )
        except Exception:
            pass

    def is_blocked(self, symbol: str) -> tuple:
        """
        Returns: (blocked: bool, reason: str)
        """
        self._ensure_today_state()
        if symbol in self._exempt_symbols:
            return False, ""
        with self._lock:
            state = self._states.get(symbol)
            if not state:
                return False, ""
            if state.cooldown_until > time.time():
                return True, state.cooldown_reason
        return False, ""

    def snapshot(self) -> List[Dict]:
        """供 dashboard / daily_review 读"""
        self._ensure_today_state()
        with self._lock:
            return [
                {
                    "symbol": s.symbol,
                    "total_pnl": s.total_pnl_today,
                    "trades": s.total_trades,
                    "wins": s.winning_trades,
                    "consecutive_losses": s.consecutive_losses,
                    "blocked": s.cooldown_until > time.time(),
                    "reason": s.cooldown_reason,
                }
                for s in self._states.values()
            ]


# ─────────────────────────────────────────────────────────────
#  单例工厂
# ─────────────────────────────────────────────────────────────

_singleton: Optional[SymbolDailyLossGuard] = None


def get_symbol_daily_loss_guard() -> SymbolDailyLossGuard:
    global _singleton
    if _singleton is None:
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            _singleton = SymbolDailyLossGuard(
                max_daily_loss_pct=float(cfg.get_dotted("risk.symbol_daily_loss_guard.max_daily_loss_pct", 0.01)),
                max_consecutive_losses=int(cfg.get_dotted("risk.symbol_daily_loss_guard.max_consecutive_losses", 3)),
                min_trades_for_winrate_check=int(cfg.get_dotted("risk.symbol_daily_loss_guard.min_trades_for_winrate_check", 5)),
                max_loss_rate=float(cfg.get_dotted("risk.symbol_daily_loss_guard.max_loss_rate", 0.7)),
            )
            exempt = cfg.get_dotted("risk.symbol_daily_loss_guard.exempt_symbols", []) or []
            _singleton.set_exempt(exempt)
        except Exception:
            _singleton = SymbolDailyLossGuard()
    return _singleton
