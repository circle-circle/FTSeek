"""
Playbook Shadow PnL Tracker — 盘后追踪 + MFE/MAE 计算

职责:
  1. 每日盘后更新 playbook_instructions 的结果
  2. 计算 shadow PnL (假设按入场价买入)
  3. 计算 MFE/MAE (最大有利/不利偏移)
  4. 分析止损止盈设置质量

数据源:
  - 日线 K 线: FutuLV3Provider.get_daily_kline() 或 FinnhubProvider.get_kline(resolution="D")
  - 实时报价: FutuLV3Provider.get_quote() 或 FinnhubProvider.get_quote()
  - 交易记录: TradeStore.query_by_date()

Usage:
  from app.playbook.shadow_tracker import ShadowTracker

  tracker = ShadowTracker(playbook_db, market_data_provider)
  tracker.daily_tracking("2026-06-18")
"""
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, date

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)

# 2026 年美国市场假日
US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}


def is_trading_day(d: date = None) -> bool:
    """检查是否为交易日 (排除周末和美国公共假日)"""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:  # 周六=5, 周日=6
        return False
    if d.isoformat() in US_MARKET_HOLIDAYS_2026:
        return False
    return True


class ShadowTracker:
    """Shadow PnL 追踪器"""

    def __init__(
        self,
        playbook_db: PlaybookDB,
        market_data=None,
        trade_store=None,
    ):
        """
        Args:
            playbook_db: PlaybookDB 实例
            market_data: 市场数据提供者 (需要有 get_daily_bars / get_quote 方法)
            trade_store: TradeStore 实例 (用于匹配已执行交易)
        """
        self.db = playbook_db
        self.market_data = market_data
        self.trade_store = trade_store

    def daily_tracking(self, trade_date: str = None):
        """
        每日盘后追踪主入口

        流程:
        1. 假日检查
        2. 获取今日指令
        3. 对每条指令:
           a. SKIP → 计算 shadow_pnl
           b. 非 SKIP → 检查触发、匹配交易、计算 MFE/MAE、更新结果
        4. 写入 DB
        """
        if trade_date is None:
            trade_date = date.today().isoformat()

        if not is_trading_day(date.fromisoformat(trade_date)):
            logger.info(f"{trade_date} is not a trading day, skipping")
            return

        instructions = self.db.get_instructions_by_date(trade_date)
        if not instructions:
            logger.info(f"No instructions for {trade_date}")
            return

        logger.info(f"Tracking {len(instructions)} instructions for {trade_date}")

        for inst in instructions:
            try:
                self._track_instruction(inst, trade_date)
            except Exception as e:
                logger.error(f"Error tracking {inst.get('symbol')}: {e}")
                continue

    def _track_instruction(self, inst: Dict, trade_date: str):
        """追踪单条指令"""
        inst_id = inst["id"]
        symbol = inst["symbol"]
        direction = inst["direction"]

        if direction == "SKIP":
            # SKIP 指令不计 shadow_pnl
            self.db.update_instruction_tracking(inst_id, {
                "outcome": "skipped",
            })
            return

        # 获取日线数据
        day_data = self._get_day_data(symbol, trade_date)
        if not day_data:
            logger.warning(f"No day data for {symbol}, skipping tracking")
            return

        updates = {}

        # 1. 检查是否触发 (价格进入入场区间 ±3%)
        # OKR Week 1: 从 ±2% 放宽到 ±3%，提升触发率
        entry_price = inst.get("entry_price")
        if entry_price and entry_price > 0:
            day_low = day_data.get("low", 0)
            day_high = day_data.get("high", 0)
            entry_low = entry_price * 0.97
            entry_high = entry_price * 1.03
            updates["triggered"] = 1 if (entry_low <= day_high and day_low <= entry_high) else 0

        # 2. 检查是否执行了 (匹配 trade_records)
        executed_trade = self._find_executed_trade(symbol, direction, trade_date)
        if executed_trade:
            updates["executed"] = 1
            updates["execution_price"] = executed_trade.get("entry_price", 0)
            updates["execution_time"] = executed_trade.get("entry_time", "")

        # 3. 如果已执行, 计算 PnL + MFE/MAE
        if inst.get("executed") or updates.get("executed"):
            exec_price = updates.get("execution_price") or inst.get("execution_price")
            if exec_price and exec_price > 0:
                # MFE/MAE 计算
                mfe, mae = self._calc_mfe_mae(
                    symbol, exec_price, direction, trade_date, inst
                )
                updates["max_favorable_excursion"] = mfe
                updates["max_adverse_excursion"] = mae

                # 检查是否已平仓
                exit_trade = self._find_exit_trade(symbol, trade_date)
                if exit_trade:
                    exit_price = exit_trade.get("exit_price", 0)
                    position_size = inst.get("position_size", 0)
                    if exit_price > 0 and position_size > 0:
                        if direction == "BUY":
                            pnl = (exit_price - exec_price) * position_size
                        else:
                            pnl = (exec_price - exit_price) * position_size
                        pnl_pct = (pnl / (exec_price * position_size)) * 100

                        updates["exit_price"] = exit_price
                        updates["exit_time"] = exit_trade.get("exit_time", "")
                        updates["exit_reason"] = exit_trade.get("exit_reason", "unknown")
                        updates["pnl"] = round(pnl, 2)
                        updates["pnl_pct"] = round(pnl_pct, 2)
                        updates["holding_period_hours"] = self._calc_holding_hours(inst, exit_trade)
                        updates["outcome"] = "win" if pnl > 0 else "loss"
                        updates["mfe_analysis"] = analyze_mfe_mae(inst, updates)
                else:
                    # 持仓中: 用收盘价计算浮盈
                    close_price = day_data.get("close", 0)
                    position_size = inst.get("position_size", 0)
                    if close_price > 0 and position_size > 0:
                        if direction == "BUY":
                            pnl = (close_price - exec_price) * position_size
                        else:
                            pnl = (exec_price - close_price) * position_size
                        updates["pnl"] = round(pnl, 2)
                        updates["outcome"] = "holding"

        elif updates.get("triggered"):
            # 触发了但没执行 → 计算 missed PnL
            shadow_pnl = self._calc_shadow_pnl(
                symbol, entry_price, direction, trade_date,
                position_size=inst.get("position_size"),
            )
            updates["shadow_pnl"] = shadow_pnl
            updates["outcome"] = "missed"
        else:
            # 未触发 → 也计算 shadow pnl
            shadow_pnl = self._calc_shadow_pnl(
                symbol, entry_price, direction, trade_date,
                position_size=inst.get("position_size"),
            )
            if shadow_pnl is not None:
                updates["shadow_pnl"] = shadow_pnl

        if updates:
            self.db.update_instruction_tracking(inst_id, updates)

    def _calc_shadow_pnl(
        self, symbol: str, entry_price: Optional[float], direction: str, trade_date: str,
        position_size: Optional[float] = None,
    ) -> Optional[float]:
        """
        计算 shadow PnL: 假设按入场价和实际仓位操作, 到收盘的假设盈亏

        2026-06-19 Phase 0 修复:
        - 使用 instruction 的 position_size 替代硬编码 100 股
        - SKIP 方向指令不计 shadow_pnl (返回 None)
        """
        if not entry_price or entry_price <= 0:
            return None

        # SKIP 指令不计算 shadow pnl — 跳过就是跳过
        if direction == "SKIP":
            return None

        day_data = self._get_day_data(symbol, trade_date)
        if not day_data:
            return None

        close_price = day_data.get("close", 0)
        if close_price <= 0:
            return None

        # 使用实际 position_size, 无则 fallback 100 股
        shares = position_size if (position_size and position_size > 0) else 100

        if direction == "BUY":
            shadow = (close_price - entry_price) * shares
        else:
            shadow = (entry_price - close_price) * shares

        return round(shadow, 2)

    def _calc_mfe_mae(
        self,
        symbol: str,
        exec_price: float,
        direction: str,
        current_date: str,
        inst: Dict,
    ) -> tuple:
        """
        计算 MFE (最大有利偏移) 和 MAE (最大不利偏移)

        MFE = 持仓期间最大浮盈 (金额)
        MAE = 持仓期间最大浮亏 (金额, 负数)

        使用日线 high/low 计算 (不需要分钟级数据)
        """
        if not exec_price or exec_price <= 0:
            return 0, 0

        position_size = inst.get("position_size", 0) or 100

        # 获取从入场到现在的日线数据
        daily_bars = self._get_daily_bars_since_entry(
            symbol, inst.get("execution_time", current_date), current_date
        )

        if not daily_bars:
            return 0, 0

        mfe = inst.get("max_favorable_excursion") or 0
        mae = inst.get("max_adverse_excursion") or 0

        for bar in daily_bars:
            high = bar.get("high", 0)
            low = bar.get("low", 0)

            if direction == "BUY":
                # BUY: MFE 看 high, MAE 看 low
                favorable = (high - exec_price) * position_size
                adverse = (low - exec_price) * position_size
            else:
                # SHORT: MFE 看 low, MAE 看 high
                favorable = (exec_price - low) * position_size
                adverse = (exec_price - high) * position_size

            mfe = max(mfe, favorable)
            mae = min(mae, adverse)

        return round(mfe, 2), round(mae, 2)

    def _get_day_data(self, symbol: str, trade_date: str) -> Optional[Dict]:
        """获取某日的 OHLC 数据"""
        if self.market_data is None:
            return None

        try:
            # 尝试 get_daily_bars (日线)
            if hasattr(self.market_data, "get_daily_bars"):
                bars = self.market_data.get_daily_bars(symbol, count=5)
                if bars:
                    for bar in bars:
                        bar_date = bar.get("time", "")[:10]
                        if bar_date == trade_date:
                            return bar

            # 尝试 get_quote (实时快照, 当日)
            if hasattr(self.market_data, "get_quote"):
                quote = self.market_data.get_quote(symbol)
                if quote and quote.get("price"):
                    return {
                        "open": quote.get("open", 0),
                        "high": quote.get("high", 0),
                        "low": quote.get("low", 0),
                        "close": quote.get("price", 0),
                    }
        except Exception as e:
            logger.warning(f"Failed to get day data for {symbol}: {e}")

        return None

    def _get_daily_bars_since_entry(
        self, symbol: str, entry_time: str, current_date: str
    ) -> List[Dict]:
        """获取从入场到现在的日线 bars"""
        if self.market_data is None:
            return []

        try:
            if hasattr(self.market_data, "get_daily_bars"):
                bars = self.market_data.get_daily_bars(symbol, count=30)
                if not bars:
                    return []

                entry_date = entry_time[:10] if entry_time else current_date
                return [
                    bar for bar in bars
                    if bar.get("time", "")[:10] >= entry_date
                ]
        except Exception as e:
            logger.warning(f"Failed to get daily bars for {symbol}: {e}")

        return []

    def _find_executed_trade(
        self, symbol: str, direction: str, trade_date: str
    ) -> Optional[Dict]:
        """在 trade_records 中查找匹配的执行记录"""
        if self.trade_store is None:
            return None

        try:
            trades = self.trade_store.query_by_date(trade_date)
            for t in trades:
                if (t.get("symbol") == symbol and
                        t.get("source_type") == "MANUAL_PLAYBOOK" and
                        t.get("direction", "").upper() == direction):
                    return t
        except Exception as e:
            logger.warning(f"Failed to find executed trade for {symbol}: {e}")

        return None

    def _find_exit_trade(self, symbol: str, trade_date: str) -> Optional[Dict]:
        """查找平仓记录"""
        if self.trade_store is None:
            return None

        try:
            trades = self.trade_store.query_by_date(trade_date)
            for t in trades:
                if (t.get("symbol") == symbol and
                        t.get("action", "").upper() in ("CLOSE", "EXIT", "SELL") and
                        t.get("source_type") == "MANUAL_PLAYBOOK"):
                    return t
        except Exception as e:
            logger.warning(f"Failed to find exit trade for {symbol}: {e}")

        return None

    def _calc_holding_hours(self, inst: Dict, exit_trade: Dict) -> float:
        """计算持仓时长 (小时)"""
        try:
            entry_time = inst.get("execution_time", "")
            exit_time = exit_trade.get("exit_time", "")
            if entry_time and exit_time:
                entry_dt = datetime.fromisoformat(entry_time)
                exit_dt = datetime.fromisoformat(exit_time)
                return round((exit_dt - entry_dt).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            pass
        return 0


def analyze_mfe_mae(inst: Dict, updates: Dict = None) -> str:
    """
    分析止损止盈设置是否合理

    规则:
    - MFE > 1.5 × TP 收益 → 止盈设低了
    - |MAE| > SL 损失 且最终盈利 → 止损设对了但运气好
    - |MAE| > SL 损失 且最终亏损 → 止损没触发 (可能跳空)
    """
    updates = updates or {}
    notes = []

    mfe = updates.get("max_favorable_excursion") or inst.get("max_favorable_excursion") or 0
    mae = updates.get("max_adverse_excursion") or inst.get("max_adverse_excursion") or 0
    entry = inst.get("entry_price", 0) or inst.get("execution_price", 0) or 0
    tp = inst.get("take_profit", 0) or 0
    sl = inst.get("stop_loss", 0) or 0
    pos_size = inst.get("position_size", 0) or 0
    pnl = updates.get("pnl") or inst.get("pnl") or 0

    if mfe and tp and entry and pos_size:
        tp_profit = abs(tp - entry) * pos_size
        if tp_profit > 0 and mfe > tp_profit * 1.5:
            notes.append(f"止盈设低了(MFE=${mfe:.0f} vs TP收益=${tp_profit:.0f})")

    if mae and sl and entry and pos_size:
        sl_loss = abs(entry - sl) * pos_size
        if sl_loss > 0 and abs(mae) > sl_loss:
            if pnl > 0:
                notes.append(f"止损设对了但运气好(MAE=${mae:.0f} > SL=${sl_loss:.0f})")
            else:
                notes.append(f"止损可能未触发(MAE=${mae:.0f} > SL=${sl_loss:.0f})")

    return "; ".join(notes) if notes else "止损止盈设置合理"
