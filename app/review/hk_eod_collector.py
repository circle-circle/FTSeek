"""
港股 EOD 数据收集器 (HKEODCollector)

职责:
1. 读取当日交易记录 (positions + trades)
2. 读取盘前作战计划 (daily_battle_plan.json)
3. 读取信号决策链 (trade_chains)
4. 获取 A 股收盘联动数据 (Futu A 股行情)
5. 获取南向资金流向 (Futu)
6. 获取账户状态
7. 交易数据增强 (交叉引用 trade_chains, 异常检测)

设计原则:
- 纯读取，零副作用
- 0 app.* imports（除 DB 路径常量）
- 所有外部调用带 try/except 降级
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("ftseek.review.hk_eod_collector")

FTSEEK_DB = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
DECISIONS_DB = os.environ.get("FTSEEK_DECISIONS_DB_PATH", "data/decisions.db")
BATTLE_PLAN_PATH = os.environ.get(
    "FTSEEK_BATTLE_PLAN_PATH", "data/daily_battle_plan.json"
)

# 港股交易时段
PRE_MARKET_START = (5, 0)    # 05:00 (盘前)
OPENING_START = (9, 30)      # 09:30 开盘
MIDDAY_START = (12, 0)       # 12:00 午休
AFTERNOON_START = (13, 0)    # 13:00 下午盘
CLOSING_START = (15, 30)     # 15:30 尾盘
MARKET_CLOSE = (16, 0)       # 16:00 收盘


class HKEODCollector:
    """港股 EOD 数据收集器 — 纯读取，零副作用"""

    def __init__(self, quote_ctx=None):
        self._quote_ctx = quote_ctx

    def collect(self, trade_date: Optional[str] = None) -> Dict:
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        trades = self._get_today_trades(trade_date)
        plan = self._load_battle_plan(trade_date)
        signal_ctx = self._get_signal_context(trade_date)
        enriched = self._enrich_trades(trades, signal_ctx, plan)

        return {
            "trade_date": trade_date,
            "trades": enriched,
            "positions": self._get_open_positions(),
            "battle_plan": plan,
            "decisions": self._get_today_decisions(trade_date),
            "ashare_context": self._get_ashare_linkage(),
            "southbound_flow": self._get_southbound_flow(),
            "capital_state": self._get_capital_state(trade_date),
            "anomalies": self._detect_anomalies(enriched, plan),
            "signal_context": signal_ctx,
        }

    # ════════════════════════════════════════════════════════════════
    #  交易记录
    # ════════════════════════════════════════════════════════════════

    def _get_today_trades(self, date: str) -> List[Dict]:
        """获取当日交易 — 优先 positions (Futu同步)，补充 trades (内部记录)"""
        trades = []
        seen_keys = set()

        try:
            with sqlite3.connect(FTSEEK_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT * FROM positions
                    WHERE code LIKE 'HK.%'
                      AND status = 'CLOSED'
                      AND date(exit_time) = ?
                      AND ABS(exit_price - entry_price) > 0.001
                    ORDER BY exit_time ASC
                    """,
                    (date,),
                )
                skipped_ghost = 0
                for row in cursor.fetchall():
                    d = dict(row)
                    # 二次确认: 排除幽灵仓 (position_sync auto-close, entry=exit)
                    if abs(float(d.get("exit_price", 0)) - float(d.get("entry_price", 0))) < 0.001:
                        skipped_ghost += 1
                        continue
                    d["source"] = "futu_sync"
                    d["is_intraday"] = (
                        d.get("entry_time", "")[:10] == date
                        and d.get("exit_time", "")[:10] == date
                    )
                    trades.append(d)
                    seen_keys.add((d.get("code"), d.get("entry_time")))
                if skipped_ghost:
                    logger.info(
                        f"[HKEODCollector] 已过滤 {skipped_ghost} 条幽灵仓自动平仓记录 "
                        f"(entry_price=exit_price, 非真实成交)"
                    )

                cursor.execute(
                    """
                    SELECT * FROM trades
                    WHERE code LIKE 'HK.%' AND entry_time >= ?
                    ORDER BY entry_time ASC
                    """,
                    (f"{date} 00:00:00",),
                )
                for row in cursor.fetchall():
                    d = dict(row)
                    key = (d.get("code"), d.get("entry_time"))
                    if key not in seen_keys:
                        d["source"] = "internal"
                        et = d.get("entry_time", "")
                        xt = d.get("exit_time", "")
                        d["is_intraday"] = xt and xt[:10] == date and et[:10] == date
                        trades.append(d)

            logger.info(f"[HKEODCollector] 获取到 {len(trades)} 笔今日交易")
            return trades
        except Exception as e:
            logger.warning(f"[HKEODCollector] 读取交易记录失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    #  信号上下文 (trade_chains)
    # ════════════════════════════════════════════════════════════════

    def _get_signal_context(self, date: str) -> Dict[str, List[Dict]]:
        """获取当日 trade_chains 信号/决策上下文, 按 code 分组"""
        ctx: Dict[str, List[Dict]] = {}
        try:
            with sqlite3.connect(FTSEEK_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT code, created_at, signal_layer, decision_layer, execution_layer
                    FROM trade_chains
                    WHERE code LIKE 'HK.%' AND date(created_at) = ?
                    ORDER BY created_at ASC
                    """,
                    (date,),
                )
                for row in cursor.fetchall():
                    d = dict(row)
                    code = d.get("code", "")
                    if code not in ctx:
                        ctx[code] = []
                    # 解析 JSON 层
                    for layer in ("signal_layer", "decision_layer", "execution_layer"):
                        raw = d.get(layer)
                        if isinstance(raw, str):
                            try:
                                d[layer] = json.loads(raw)
                            except json.JSONDecodeError:
                                d[layer] = {}
                        elif raw is None:
                            d[layer] = {}
                    ctx[code].append(d)
            logger.info(f"[HKEODCollector] trade_chains 上下文: {sum(len(v) for v in ctx.values())} 条, {len(ctx)} 个标的")
        except Exception as e:
            logger.warning(f"[HKEODCollector] trade_chains 查询失败: {e}")
        return ctx

    # ════════════════════════════════════════════════════════════════
    #  交易增强 (交叉引用)
    # ════════════════════════════════════════════════════════════════

    def _enrich_trades(self, trades: List[Dict], signal_ctx: Dict, plan: Optional[Dict]) -> List[Dict]:
        """增强每笔交易: 信号上下文 + 作战计划 + 时间阶段 + 止损/止盈判断"""
        plan_symbols = {t["symbol"] for t in plan.get("targets", [])} if plan else set()

        for t in trades:
            code = t.get("code", "")
            entry_time = t.get("entry_time", "")
            exit_time = t.get("exit_time", "")
            exit_price = t.get("exit_price", 0) or 0
            entry_price = t.get("entry_price", 0) or 0
            stop_loss = t.get("stop_loss", 0) or 0
            take_profit = t.get("take_profit", 0) or 0
            highest = t.get("highest_price")  # may be None
            lowest = t.get("lowest_price")    # may be None
            pnl = t.get("pnl", t.get("realized_pnl", 0)) or 0
            qty = abs(t.get("quantity", 0)) or 1

            # — 时间阶段 —
            t["time_phase"] = self._classify_time_phase(entry_time, exit_time)

            # — 归一化字段 (供归因器使用) —
            if "pnl" not in t or t["pnl"] is None:
                t["pnl"] = t.get("realized_pnl", 0) or 0
            gross_value = entry_price * qty
            if gross_value > 0:
                t["pnl_percent"] = round(pnl / gross_value * 100, 2)
            else:
                t["pnl_percent"] = 0.0
            t["holding_minutes"] = t["time_phase"].get("hold_minutes", 0)

            # — 止损/止盈分析 —
            t["exit_analysis"] = self._analyse_exit(
                entry_price, exit_price, stop_loss, take_profit, highest, lowest, pnl
            )

            # — 信号上下文 (trade_chains 交叉) —
            chains = signal_ctx.get(code, [])
            t["signal_summary"] = self._match_signal_context(entry_time, chains)

            # — 作战计划覆盖 —
            t["in_battle_plan"] = code in plan_symbols

        return trades

    def _classify_time_phase(self, entry_time: str, exit_time: str) -> Dict:
        """分类交易时间阶段"""
        def _parse(t: str) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(t.replace("T", " "))
            except Exception:
                return None

        et = _parse(entry_time)
        xt = _parse(exit_time)

        def _phase(dt: datetime) -> str:
            hhmm = (dt.hour, dt.minute)
            if hhmm < OPENING_START:
                return "盘前"
            if hhmm < (10, 30):
                return "开盘冲刺"
            if hhmm < MIDDAY_START:
                return "早盘"
            if hhmm < AFTERNOON_START:
                return "午休"
            if hhmm < (14, 30):
                return "午后"
            if hhmm < CLOSING_START:
                return "尾盘前"
            return "尾盘"

        hold_minutes = 0
        if et and xt:
            hold_minutes = int((xt - et).total_seconds() / 60)

        return {
            "entry_phase": _phase(et) if et else "未知",
            "exit_phase": _phase(xt) if xt else "未知",
            "hold_minutes": hold_minutes,
            "is_intraday": hold_minutes < 480 if hold_minutes else None,
        }

    def _analyse_exit(self, entry: float, exit_px: float, stop_loss: float,
                      take_profit: float, highest, lowest, pnl: float) -> Dict:
        """分析出场类型: 止损/止盈/主动/到期"""
        result = {"type": "主动平仓", "detail": "", "quality": "—"}

        if not exit_px or not entry:
            return result

        change_pct = (exit_px / entry - 1) * 100

        # 止损判断: exit_price <= stop_loss
        if stop_loss and exit_px <= stop_loss:
            result["type"] = "止损"
            slippage = (exit_px / stop_loss - 1) * 100
            result["detail"] = f"触及止损价{stop_loss:.2f} (滑点{slippage:+.2f}%)"
            result["quality"] = "✅ 纪律执行" if slippage > -1 else "⚠️ 滑点偏大"
            return result

        # 止盈判断: exit_price >= take_profit
        if take_profit and exit_px >= take_profit:
            result["type"] = "止盈"
            result["detail"] = f"达到止盈价{take_profit:.2f}"
            result["quality"] = "✅ 按计划止盈"
            return result

        # 最高/最低价分析
        if highest and pnl > 0:
            max_pct = (highest / entry - 1) * 100
            actual_pct = (exit_px / entry - 1) * 100
            if max_pct > actual_pct * 1.3:
                result["detail"] = f"最高浮盈{max_pct:+.2f}%, 实际{actual_pct:+.2f}%"
                result["quality"] = "⚠️ 回吐较大"
            elif pnl > 0:
                result["detail"] = f"盈利{change_pct:+.2f}%"
                result["quality"] = "✅ 正常盈利"
        elif lowest and pnl < 0:
            min_pct = (lowest / entry - 1) * 100
            result["detail"] = f"最大浮亏{min_pct:+.2f}%"
            result["quality"] = "✅ 截断及时" if abs(change_pct) < 2 else "⚠️ 亏损偏大"

        if not result["detail"]:
            if abs(change_pct) < 0.005:
                result["detail"] = f"保本平仓 (手续费{abs(pnl):.0f})" if pnl < 0 else "保本平仓"
                result["quality"] = "⚠️ 摩擦成本" if pnl < 0 else "✅"
            else:
                result["detail"] = f"{'盈利' if pnl>0 else '亏损'}{abs(change_pct):.2f}%"
                result["quality"] = "✅" if pnl > 0 else "⚠️"

        return result

    def _match_signal_context(self, entry_time: str, chains: List[Dict]) -> Dict:
        """匹配最接近入场时间的 trade_chain 信号"""
        if not chains:
            return {}

        try:
            et_dt = datetime.fromisoformat(entry_time.replace("T", " "))
        except Exception:
            # 无法解析入场时间，返回当日该标的最新链
            if chains:
                c = chains[-1]
                sig = c.get("signal_layer", {}) or {}
                dec = c.get("decision_layer", {}) or {}
                return self._extract_signal_dict(sig, dec, 0)
            return {}

        best = None
        best_diff = timedelta.max
        for c in chains:
            created = c.get("created_at", "")
            try:
                ct = datetime.fromisoformat(created.replace("T", " "))
            except Exception:
                continue
            diff = et_dt - ct
            if timedelta(0) <= diff < best_diff:
                best_diff = diff
                best = c

        # 放宽匹配: 无最近链时取当日首条
        if not best:
            best = chains[0]
            best_diff = et_dt - datetime.fromisoformat(
                best.get("created_at", "").replace("T", " ")
            ) if best.get("created_at") else timedelta(hours=8)

        sig = best.get("signal_layer", {}) or {}
        dec = best.get("decision_layer", {}) or {}
        return self._extract_signal_dict(sig, dec, int(abs(best_diff.total_seconds())))

    @staticmethod
    def _extract_signal_dict(sig: Dict, dec: Dict, offset_sec: int) -> Dict:
        return {
            "signal_score": sig.get("signal_score", 0),
            "ai_sentiment": sig.get("ai_sentiment_score", 0),
            "market_regime": sig.get("market_regime", "unknown"),
            "consensus_passed": sig.get("consensus_passed", False),
            "decision_reason": dec.get("decision_reason", "unknown"),
            "decision_confidence": dec.get("decision_confidence", 0),
            "match_offset_sec": offset_sec,
        }

    # ════════════════════════════════════════════════════════════════
    #  持仓
    # ════════════════════════════════════════════════════════════════

    def _get_open_positions(self) -> List[Dict]:
        try:
            with sqlite3.connect(FTSEEK_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT * FROM positions
                       WHERE code LIKE 'HK.%' AND status = 'OPEN'
                       ORDER BY unrealized_pnl ASC"""
                )
                rows = cursor.fetchall()
            positions = [dict(r) for r in rows]
            logger.info(f"[HKEODCollector] 获取到 {len(positions)} 个持仓")
            return positions
        except Exception as e:
            logger.warning(f"[HKEODCollector] 读取持仓失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    #  盘前作战计划
    # ════════════════════════════════════════════════════════════════

    def _load_battle_plan(self, date: str) -> Optional[Dict]:
        try:
            if not os.path.exists(BATTLE_PLAN_PATH):
                return None
            with open(BATTLE_PLAN_PATH, "r", encoding="utf-8") as f:
                plan = json.load(f)
            if plan.get("date") == date:
                logger.info(f"[HKEODCollector] 加载作战计划: {len(plan.get('targets', []))} 个目标")
                return plan
            logger.warning(f"[HKEODCollector] 作战计划日期不匹配: 计划={plan.get('date')}, 查询={date}")
            return None
        except Exception as e:
            logger.warning(f"[HKEODCollector] 读取作战计划失败: {e}")
            return None

    # ════════════════════════════════════════════════════════════════
    #  信号决策
    # ════════════════════════════════════════════════════════════════

    def _get_today_decisions(self, date: str) -> List[Dict]:
        try:
            if not os.path.exists(DECISIONS_DB):
                return []
            with sqlite3.connect(DECISIONS_DB) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT * FROM decisions
                       WHERE code LIKE 'HK.%' AND created_at >= ?
                       ORDER BY created_at ASC""",
                    (f"{date} 00:00:00",),
                )
                rows = cursor.fetchall()
            decisions = [dict(r) for r in rows]
            logger.info(f"[HKEODCollector] 获取到 {len(decisions)} 条信号决策")
            return decisions
        except Exception as e:
            logger.debug(f"[HKEODCollector] 读取决策记录失败: {e}")
            return []

    # ════════════════════════════════════════════════════════════════
    #  A 股联动
    # ════════════════════════════════════════════════════════════════

    def _get_ashare_linkage(self) -> Optional[Dict]:
        if not self._quote_ctx:
            return None
        try:
            ashare_codes = ["SH.000001", "SH.000300", "SZ.399006", "SH.000016"]
            ret, data = self._quote_ctx.get_market_snapshot(ashare_codes)
            if ret != 0 or data is None:
                logger.warning("[HKEODCollector] Futu A 股快照获取失败")
                return None
            linkage = {}
            for _, row in data.iterrows():
                code = row.get("code", "")
                linkage[code] = {
                    "last_price": float(row.get("last_price", 0)),
                    "open_price": float(row.get("open_price", 0)),
                    "high_price": float(row.get("high_price", 0)),
                    "low_price": float(row.get("low_price", 0)),
                    "change_pct": float(row.get("change_rate", 0)),
                    "volume": float(row.get("volume", 0)),
                }
            logger.info(f"[HKEODCollector] 获取 A 股联动数据: {len(linkage)} 个指数")
            return linkage
        except Exception as e:
            logger.warning(f"[HKEODCollector] A 股联动数据获取失败: {e}")
            return None

    # ════════════════════════════════════════════════════════════════
    #  南向资金
    # ════════════════════════════════════════════════════════════════

    def _get_southbound_flow(self) -> Optional[Dict]:
        if not self._quote_ctx:
            return None
        try:
            logger.debug("[HKEODCollector] 南向资金数据需要外部数据源，暂跳过")
            return None
        except Exception as e:
            logger.debug(f"[HKEODCollector] 南向资金数据获取失败: {e}")
            return None

    # ════════════════════════════════════════════════════════════════
    #  账户状态
    # ════════════════════════════════════════════════════════════════

    def _get_capital_state(self, date: str) -> Dict:
        try:
            with sqlite3.connect(FTSEEK_DB) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT COALESCE(SUM(realized_pnl), 0), COUNT(*)
                       FROM positions WHERE code LIKE 'HK.%' AND status='CLOSED' AND date(exit_time)=?""",
                    (date,),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """SELECT COALESCE(SUM(unrealized_pnl), 0), COUNT(*)
                       FROM positions WHERE code LIKE 'HK.%' AND status='OPEN'"""
                )
                pos_row = cursor.fetchone()
            return {
                "today_realized_pnl": float(row[0]) if row else 0.0,
                "today_trade_count": int(row[1]) if row else 0,
                "unrealized_pnl": float(pos_row[0]) if pos_row else 0.0,
                "open_position_count": int(pos_row[1]) if pos_row else 0,
            }
        except Exception as e:
            logger.warning(f"[HKEODCollector] 账户状态获取失败: {e}")
            return {"today_realized_pnl": 0.0, "today_trade_count": 0,
                    "unrealized_pnl": 0.0, "open_position_count": 0}

    # ════════════════════════════════════════════════════════════════
    #  异常检测
    # ════════════════════════════════════════════════════════════════

    def _detect_anomalies(self, trades: List[Dict], plan: Optional[Dict]) -> List[Dict]:
        """检测异常模式"""
        anomalies = []

        if not trades:
            return anomalies

        def _pnl(t):
            return t.get("pnl", t.get("realized_pnl", 0)) or 0

        def _code(t):
            return t.get("code", "").replace("HK.", "")

        plan_symbols = {t["symbol"] for t in plan.get("targets", [])} if plan else set()

        # 1. 同标的多次交易
        code_counts: Dict[str, List] = {}
        for t in trades:
            code = _code(t)
            code_counts.setdefault(code, []).append(t)
        for code, tlist in code_counts.items():
            if len(tlist) > 1:
                total = sum(_pnl(t) for t in tlist)
                if total < 0:
                    anomalies.append({
                        "level": "⚠️", "type": "重复亏损",
                        "msg": f"{code} 当日交易{len(tlist)}次, 合计亏损{total:,.0f}",
                        "suggestion": "避免在同一标的上反复试仓"
                    })

        # 2. 计划外交易
        off_plan = [t for t in trades if not t.get("in_battle_plan")]
        if off_plan:
            off_pnl = sum(_pnl(t) for t in off_plan)
            anomalies.append({
                "level": "⚠️" if off_pnl < 0 else "ℹ️",
                "type": "计划外交易",
                "msg": f"{len(off_plan)}笔计划外交易 (标的: {', '.join(_code(t) for t in off_plan)}), 合计{off_pnl:+,.0f}",
                "suggestion": "严格按作战计划执行，或加入午间刷新机制"
            })

        # 3. 大额亏损检测
        for t in trades:
            pnl_val = _pnl(t)
            if pnl_val < -5000:
                anomalies.append({
                    "level": "🔴", "type": "大额亏损",
                    "msg": f"{_code(t)} 单笔亏损 {pnl_val:,.0f}",
                    "suggestion": "检查止损设置是否过宽或是否未设止损"
                })

        # 4. 止盈未触发 (最高价 > 止盈价 但 exit 未到)
        for t in trades:
            tp = t.get("take_profit", 0) or 0
            highest = t.get("highest_price")
            if tp and highest and float(highest) >= tp and _pnl(t) <= 0:
                anomalies.append({
                    "level": "⚠️", "type": "止盈未执行",
                    "msg": f"{_code(t)} 最高价{highest:.2f}已达止盈{tp:.2f}, 但未触发止盈",
                    "suggestion": "检查止盈单是否未提交或被撤销"
                })

        # 5. 集中度异常
        total_capital = sum(abs(float(t.get("entry_price", 0)) * int(t.get("quantity", 0))) for t in trades)
        for t in trades:
            trade_capital = float(t.get("entry_price", 0)) * int(t.get("quantity", 0))
            if total_capital > 0 and trade_capital / total_capital > 0.4:
                anomalies.append({
                    "level": "⚠️", "type": "集中度过高",
                    "msg": f"{_code(t)} 占总交易金额的 {trade_capital/total_capital*100:.0f}%",
                    "suggestion": "分散风险，单标的仓位不超过总资金的20%"
                })

        return anomalies
