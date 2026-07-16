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
from app.core.scheduler_triggers import ET as _ET

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
        d = datetime.now(_ET).date()
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
            trade_date = datetime.now(_ET).date().isoformat()

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
                # 2026-06-26: 记录 horizon 信息到追踪日志
                _horizon = inst.get("horizon", "short")
                _horizon_conf = inst.get("horizon_confidence")
                if _horizon_conf is not None:
                    logger.debug(f"[shadow_tracker] {inst.get('symbol')} horizon={_horizon} confidence={_horizon_conf}")
                self._track_instruction(inst, trade_date)
            except Exception as e:
                logger.error(f"Error tracking {inst.get('symbol')}: {e}")
                continue

    def _track_instruction(self, inst: Dict, trade_date: str):
        """追踪单条指令"""
        inst_id = inst["id"]
        symbol = inst["symbol"]
        direction = inst["direction"]

        # 2026-06-24 防御守卫 (OKR): 已终态 outcome (win/loss/skipped/missed) 不再重处理.
        # 防止 daily_tracking 在 trade_store 不可用 / exit 查询失败时把已终态的 win/loss
        # 覆盖回 'holding' (曾因 stub 无 trade_store 导致 21 条 win/loss 被误覆盖).
        # 'holding' 与 NULL 仍需继续追踪直到产生最终 outcome.
        _existing_outcome = inst.get("outcome")
        if _existing_outcome in ("win", "loss", "skipped", "missed"):
            return

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

        # 1. 检查是否触发 (突破MA20 + 成交量确认)
        # 2026-06-25: 从 ±5% 价格区间改为突破确认, 与 _check_intraday_trigger 对齐
        # shadow_tracker 盘后跑, 用 day_high/day_low 判断当日是否突破 MA20
        # 2026-07-16 P0 修复: 不覆盖 auto_execute 已设置的 triggered=1.
        #   根因: shadow_tracker 用日K线 breakout 判定触发, auto_execute 用盘中穿越判定.
        #   两者标准不同 → shadow_tracker 盘后覆盖 triggered=0 → 退出参数分析失真.
        #   修复: 只在 inst.triggered 未被 auto_execute 设置(=0 或 NULL) 时回填 shadow 判定.
        _already_triggered = bool(inst.get("triggered"))
        entry_price = inst.get("entry_price")
        if entry_price and entry_price > 0 and not _already_triggered:
            # 2026-07-10: low/high/close 可能显式为 None (盘前/数据不全),
            #   .get(key, 0) 仅在键缺失时返回默认, 值为 None 时仍返 None →
            #   后续 day_close > ma20 触发 'NoneType > int'. 与 ma20/position_size 同样 or 0 兜底.
            day_low = day_data.get("low", 0) or 0
            day_high = day_data.get("high", 0) or 0
            day_close = day_data.get("close", 0) or 0
            ma20 = day_data.get("ma20") or 0
            vol_ratio = day_data.get("volume_5d_avg_ratio", 0) or day_data.get("volume_ratio", 0)
            direction = inst.get("direction", "")
            is_short = "SHORT" in direction.upper()
            # 突破: LONG day_high > MA20, SHORT day_low < MA20
            breakout = (day_low < ma20 < day_high) if ma20 > 0 else False
            if is_short:
                breakout = breakout and day_close < ma20
            else:
                breakout = breakout and day_close > ma20
            vol_confirmed = vol_ratio >= 1.5 if vol_ratio else False
            updates["triggered"] = 1 if (breakout and vol_confirmed) else 0

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
                    exit_price = exit_trade.get("exit_price", 0) or 0
                    position_size = inst.get("position_size") or 0
                    if exit_price > 0 and position_size > 0:
                        # direction 是 BUY/SELL_SHORT (playbook 指令格式)
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
                        # 2026-07-02: ORPHAN_CLEANUP等幽灵仓清理不应设outcome=win/loss
                        _exit_reason = exit_trade.get("exit_reason", "unknown")
                        if "ORPHAN" in _exit_reason.upper() or "PHANTOM" in _exit_reason.upper() or "GHOST" in _exit_reason.upper():
                            updates["outcome"] = "void"
                        else:
                            updates["outcome"] = "win" if pnl > 0 else "loss"
                        updates["mfe_analysis"] = analyze_mfe_mae(inst, updates)
                else:
                    # 持仓中: 用收盘价计算浮盈
                    close_price = day_data.get("close", 0) or 0
                    position_size = inst.get("position_size") or 0
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
            # 未触发 → 计算 shadow pnl, 并标记终态 missed (避免 run_playbook_track 幂等失败反复推送)
            shadow_pnl = self._calc_shadow_pnl(
                symbol, entry_price, direction, trade_date,
                position_size=inst.get("position_size"),
            )
            if shadow_pnl is not None:
                updates["shadow_pnl"] = shadow_pnl
            updates["outcome"] = "missed"

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
        """获取某日的 OHLC 数据 + MA20 + volume_ratio"""
        if self.market_data is None:
            return None

        try:
            # 尝试 get_daily_bars (日线)
            if hasattr(self.market_data, "get_daily_bars"):
                bars = self.market_data.get_daily_bars(symbol, count=30)
                if bars:
                    # 找到目标日的 bar
                    target_bar = None
                    for bar in bars:
                        bar_date = bar.get("time", "")[:10]
                        if bar_date == trade_date:
                            target_bar = bar
                            break
                    if target_bar:
                        # 计算 MA20 (最近20天收盘价均值) — 仅当 mock/上游未提供时
                        if "ma20" not in target_bar:
                            closes = [b.get("close", 0) for b in bars[-20:]]
                            ma20 = sum(closes) / len(closes) if closes else 0
                            target_bar["ma20"] = ma20
                        # 计算 volume_ratio (当日量 / 前5日均量) — 仅当 mock/上游未提供时
                        if "volume_5d_avg_ratio" not in target_bar and "volume_ratio" not in target_bar:
                            vols = [b.get("volume", 0) for b in bars[-6:-1]]  # 前5天
                            avg_vol = sum(vols) / len(vols) if vols else 0
                            today_vol = target_bar.get("volume", 0)
                            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0
                            target_bar["volume_5d_avg_ratio"] = vol_ratio
                        return target_bar

            # 尝试 get_quote (实时快照, 当日)
            if hasattr(self.market_data, "get_quote"):
                quote = self.market_data.get_quote(symbol)
                if quote and quote.get("price"):
                    return {
                        "open": quote.get("open", 0),
                        "high": quote.get("high", 0),
                        "low": quote.get("low", 0),
                        "close": quote.get("price", 0),
                        "ma20": 0,  # 实时快照无法计算 MA20
                        "volume_5d_avg_ratio": 0,
                    }
        except Exception as e:
            logger.warning(f"Failed to get day data for {symbol}: {e}")

        return None

    def _get_daily_bars_since_entry(
        self, symbol: str, entry_time: str, current_date: str
    ) -> List[Dict]:
        """获取从入场到现在的日线 bars

        2026-07-04: 加fallback — get_daily_bars失败时用get_quote获取当日high/low
        """
        if self.market_data is None:
            return []

        # 路径1: get_daily_bars (首选, 有完整OHLC)
        try:
            if hasattr(self.market_data, "get_daily_bars"):
                bars = self.market_data.get_daily_bars(symbol, count=30)
                if bars:
                    entry_date = entry_time[:10] if entry_time else current_date
                    filtered = [
                        bar for bar in bars
                        if bar.get("time", "")[:10] >= entry_date
                    ]
                    if filtered:
                        return filtered
        except Exception as e:
            logger.debug(f"[shadow] get_daily_bars failed for {symbol}: {e}")

        # 路径2: fallback — get_quote获取当日high/low (精度低但不返回空)
        try:
            if hasattr(self.market_data, "get_quote"):
                quote = self.market_data.get_quote(symbol)
                if quote and isinstance(quote, dict):
                    high = quote.get("high", 0) or quote.get("last", 0) or 0
                    low = quote.get("low", 0) or quote.get("last", 0) or 0
                    if high > 0 or low > 0:
                        return [{"high": high, "low": low, "close": quote.get("last", 0), "time": current_date}]
        except Exception as e:
            logger.debug(f"[shadow] get_quote fallback failed for {symbol}: {e}")

        return []

    def _find_executed_trade(
        self, symbol: str, direction: str, trade_date: str
    ) -> Optional[Dict]:
        """在 trade_records 中查找匹配的执行记录

        2026-06-23: 修复方向映射 — playbook_instructions.direction 是 BUY/SELL_SHORT,
        trade_records.direction 是 LONG/SHORT. 需要归一化比较.
        """
        if self.trade_store is None:
            return None

        # 方向归一化: BUY→LONG, SELL_SHORT→SHORT
        _dir_map = {"BUY": "LONG", "SELL_SHORT": "SHORT"}
        _normalized = _dir_map.get(direction.upper(), direction.upper())

        try:
            trades = self.trade_store.query_by_date(trade_date)
            for t in trades:
                if (t.get("symbol") == symbol and
                        t.get("source_type") in ("MANUAL_PLAYBOOK", "PLAYBOOK_SHADOW", "PLAYBOOK_SIM", "PLAYBOOK_LIVE") and
                        t.get("direction", "").upper() == _normalized):
                    return t
        except Exception as e:
            logger.warning(f"Failed to find executed trade for {symbol}: {e}")

        return None

    def _find_exit_trade(self, symbol: str, trade_date: str) -> Optional[Dict]:
        """查找平仓记录 — monitor_playbook_shadow_exits 直接更新 trade_records 的 exit 字段

        2026-06-23: 修复 — shadow 退出不是单独的 trade record, 而是同一条 record 的
        exit_price/exit_reason/exit_time 字段被 UPDATE。查找 source_type='PLAYBOOK_SHADOW'
        且 exit_price > 0 的记录。
        """
        if self.trade_store is None:
            return None

        try:
            trades = self.trade_store.query_by_date(trade_date)
            for t in trades:
                if (t.get("symbol") == symbol and
                        t.get("source_type") in ("MANUAL_PLAYBOOK", "PLAYBOOK_SHADOW", "PLAYBOOK_SIM", "PLAYBOOK_LIVE") and
                        float(t.get("exit_price", 0) or 0) > 0):
                    return {
                        "exit_price": float(t.get("exit_price", 0)),
                        "exit_time": t.get("exit_time", ""),
                        "exit_reason": t.get("exit_reason", "unknown"),
                    }
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
