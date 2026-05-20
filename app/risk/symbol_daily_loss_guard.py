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
from datetime import datetime
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


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

    @property
    def loss_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return 1.0 - (self.winning_trades / self.total_trades)


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
        self._today: str = datetime.now().strftime("%Y-%m-%d")

    def set_equity_provider(self, fn):
        """注入 equity 获取函数 (cap 计算用)"""
        self._equity_provider = fn

    def set_exempt(self, symbols: List[str]):
        with self._lock:
            self._exempt_symbols = set(symbols or [])

    def _ensure_today_state(self):
        """每日重置. 跨日检测."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._today:
            with self._lock:
                self._states.clear()
                self._today = today
            logger.info(f"[SymbolDailyLossGuard] 跨日重置 → {today}")

    def restore_from_db(self):
        """启动时从 trade_records 重建当日状态"""
        try:
            from app.core.paths import get_trade_records_db
            today = datetime.now().strftime("%Y-%m-%d")
            conn = sqlite3.connect(str(get_trade_records_db()))
            rows = conn.execute(
                "SELECT symbol, realized_pnl, exit_time FROM trade_records "
                "WHERE date(exit_time) = ? AND realized_pnl IS NOT NULL "
                "ORDER BY exit_time",
                (today,),
            ).fetchall()
            conn.close()
            with self._lock:
                for sym, pnl, exit_time in rows:
                    if not sym:
                        continue
                    state = self._states.setdefault(sym, _SymbolState(symbol=sym))
                    state.total_pnl_today += float(pnl or 0)
                    state.total_trades += 1
                    if pnl is not None and pnl > 0:
                        state.winning_trades += 1
                        state.consecutive_losses = 0
                    elif pnl is not None and pnl < 0:
                        state.consecutive_losses += 1
                    state.last_trade_ts = time.time()
                    # 检查是否触发 cooldown
                    self._check_trigger(state)
            logger.info(
                f"[SymbolDailyLossGuard·restore] 重建 {len(self._states)} symbols 状态, "
                f"今日已平仓 {sum(s.total_trades for s in self._states.values())} 笔"
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
            self._check_trigger(state)

    def _check_trigger(self, state: _SymbolState):
        """检查 + 标记 cooldown. 必须在 _lock 内调"""
        if state.cooldown_until > time.time():
            return  # 已 cooldown

        # 1) 累计亏损上限
        if self._equity_provider:
            try:
                equity = float(self._equity_provider())
                if equity > 0:
                    loss_pct = -state.total_pnl_today / equity
                    if loss_pct >= self.max_daily_loss_pct:
                        return self._trigger(state,
                            f"daily_loss_pct={loss_pct*100:.2f}% >= {self.max_daily_loss_pct*100}%"
                        )
            except Exception:
                pass

        # 2) 连续亏损 N 笔
        if state.consecutive_losses >= self.max_consecutive_losses:
            return self._trigger(state,
                f"consecutive_losses={state.consecutive_losses} >= {self.max_consecutive_losses}"
            )

        # 3) 5+ 笔且胜率 < 30%
        if state.total_trades >= self.min_trades_for_winrate_check:
            if state.loss_rate >= self.max_loss_rate:
                return self._trigger(state,
                    f"loss_rate={state.loss_rate*100:.0f}% (n={state.total_trades})"
                )

    def _trigger(self, state: _SymbolState, reason: str):
        """标记 cooldown + 推送告警"""
        if self.cooldown_until_eod:
            # 当日剩余时间 (UTC 24:00 简化处理)
            now = datetime.now()
            eod = now.replace(hour=23, minute=59, second=59).timestamp()
            state.cooldown_until = eod
        else:
            state.cooldown_until = time.time() + 86400
        state.cooldown_reason = reason
        logger.warning(
            f"🚫 [SymbolDailyLossGuard] {state.symbol} 当日 cooldown 触发: {reason} "
            f"(today_pnl=${state.total_pnl_today:.2f} trades={state.total_trades} "
            f"win={state.winning_trades})"
        )
        # 关键告警
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
