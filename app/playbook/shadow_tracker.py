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
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, date
from app.core.scheduler_triggers import ET as _ET

from app.playbook.db import PlaybookDB

logger = logging.getLogger(__name__)

# ── 2026-10-17 归因修复: 终态门拦截原因 (见 _terminal_block_reason_for docstring) ──
_TBR_TELEMETRY = Path(__file__).resolve().parents[2] / "data" / "trigger_miss_telemetry.jsonl"
_TBR_CACHE: Dict[str, Any] = {"key": None, "map": {}}


def _terminal_block_reason_for(
    inst_id: Any, trade_date: Any, path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """该指令**当日全部门拦截原因** (无行/异常 → None)。只读, 不做任何写入。

    2026-10-17 归因修复背景: 被门拦下且未成交的指令, EOD 兜底会把 skip_reason 统一写成
    "未触发 (no_trigger: 盘中价格未突破入场确认条件)"(本文件 :282), 而多数门
    (low_atr_block / streak_ban / vwap_gate_block / after_cutoff / extreme_gap ...) 只在
    trigger_miss_telemetry 留行、不写指令级 skip_reason ⇒ 库里**分不清"门拦截"与"价格未到"**。
    三处代码注释自认此黑洞: pipeline.py:80 / 本文件 :258 / trigger_flow.py:879。
    实测 2026-10-16: US.COIN 终态写"价格未到", 而日志 43 行连败封禁 + 当日 35 根 bar 触及入场价;
    US.TSM 终态写"价格未到", 而日志 低ATR门 + `#2 触发窗截止` + 147/390 根 bar 触及 415。

    口径 (为什么存"分布"而不是只存"末次"):
      - 末次拦截恒为 after_cutoff (14:00 ET 后每轮短路, 当日最后一轮必是它) ⇒ 只存末次等于
        用无区分度的标签换掉另一个无区分度标签。实测: 有 after_cutoff 的 292 对里,
        **34% 的主因 ≠ 末次** (COIN: streak_ban 85 + after_cutoff 39; TSM: low_atr 85 + after_cutoff 39)。
      - 故返回: {"counts": {reason: 轮次}, "primary": 主因, "last": 末次, "ts": 末次 ET 时刻}
        其中 primary = 剔除 after_cutoff 后的最高频原因 (无其他原因时 = "after_cutoff"
        ⇒ 诚实表达"到截止都没触发")。判读留给消费方, 写者不做结论。
    返回 None = 该 (inst_id, date) 无遥测行 (多为 entry_confirm 弃单臂: 它们由
    entry_confirm_forward.jsonl 承载且自带具体 skip_reason, 不在本修复的缺口内)。
    缓存按 (路径, mtime, size) 失效, 使一次 EOD 批量追踪只读一遍文件。
    """
    try:
        p = str(path or _TBR_TELEMETRY)
        st = os.stat(p)
        key = (p, st.st_mtime, st.st_size)
        if _TBR_CACHE.get("key") != key:
            m: Dict[Any, Dict[str, Any]] = {}
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except Exception as _parse_err:
                        # R5 (同 overnight_decision.py:301 先例): 坏行跳过但必须留痕 —
                        #   静默 = 归因输入腐化不可见 (except:pass 棘轮 922→921 清剿)。
                        logger.warning(
                            f"[归因] 遥测档坏行跳过: {_parse_err} | {ln[:120]!r}",
                            exc_info=True)
                        continue
                    iid, d, reason = r.get("inst_id"), r.get("date"), r.get("reason")
                    if iid is None or not d or not reason:
                        continue
                    got = m.setdefault((iid, d), {"counts": {}, "last": None, "ts": None})
                    got["counts"][reason] = got["counts"].get(reason, 0) + 1
                    got["last"] = reason                       # 追加序 ⇒ 末行胜 (末次拦截)
                    got["ts"] = r.get("ts") or got["ts"]       # 追加序 ⇒ 末行时刻
            _TBR_CACHE["map"] = m
            _TBR_CACHE["key"] = key
        got = _TBR_CACHE["map"].get((inst_id, trade_date))
        if not got:
            return None
        counts = dict(got.get("counts") or {})
        primary = None
        others = {r: c for r, c in counts.items() if r != "after_cutoff"}
        if others:
            primary = max(others.items(), key=lambda kv: kv[1])[0]
        elif counts:
            primary = max(counts.items(), key=lambda kv: kv[1])[0]
        return {
            "counts": counts,
            "primary": primary,
            "last": got.get("last"),
            "ts": got.get("ts"),
        }
    except FileNotFoundError:
        return None
    except Exception as _e:  # 审计降级优于阻断结算
        logger.warning(
            f"[归因] terminal_block_reason 读取失败 (降级跳过): {_e}",
            exc_info=True)
        return None

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

        # 测试卫生守卫 (DEB-20260823 X3, 对齐 pipeline 同款约定): 单测 TEST 标记指令
        # 不进影子台账 (不写 tracking, 不计 shadow_pnl)
        if "TEST" in str(symbol or "").upper():
            return

        # 2026-06-24 防御守卫 (OKR): 已终态 outcome (win/loss/skipped/missed) 不再重处理.
        # 防止 daily_tracking 在 trade_store 不可用 / exit 查询失败时把已终态的 win/loss
        # 覆盖回 'holding' (曾因 stub 无 trade_store 导致 21 条 win/loss 被误覆盖).
        # 'holding' 与 NULL 仍需继续追踪直到产生最终 outcome.
        _existing_outcome = inst.get("outcome")
        # DEB-20260904 #S6: 'void' 也是终态 (db.py:438 终态集; 幽灵仓标记),
        # 漏掉会把 void 指令重新追踪/覆盖
        if _existing_outcome in ("win", "loss", "skipped", "missed", "void"):
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
            from app.playbook.config import get_playbook_config as _get_pb_cfg
            _vol_gate = float(_get_pb_cfg().get("intraday_volume_ratio", 1.5))
            vol_confirmed = vol_ratio >= _vol_gate if vol_ratio else False
            updates["triggered"] = 1 if (breakout and vol_confirmed) else 0

        # 2. 检查是否执行了 (匹配 trade_records; P2b: 带 decision_id 轮次级联接,
        #    防重武装后新轮行认领上一轮成交 — 夜间复燃污染路径)
        executed_trade = self._find_executed_trade(
            symbol, direction, trade_date, decision_id=inst.get("decision_id"))
        # 2026-08-27 (P1 幻影回填·纵深防御B): 成交时刻早于指令行创建时刻 → 物理上
        #   不可能属于本指令 (典型: 再入场新轮行认领第一轮成交)。丢弃该匹配并告警,
        #   即使未来联接键再被削弱也不会重演 8/26 #1777 (被盖 #1370 成交)。
        if executed_trade and self._is_phantom_attach(executed_trade, inst):
            logger.warning(
                f"[shadow_tracker] {symbol} inst#{inst['id']} 认领的成交 entry "
                f"{executed_trade.get('entry_time')} 早于指令创建 {inst.get('created_at')} "
                f">30s → 判为跨轮误联, 丢弃 (幻影回填防御)"
            )
            executed_trade = None
        if executed_trade:
            updates["executed"] = 1
            updates["execution_price"] = executed_trade.get("entry_price", 0)
            updates["execution_time"] = executed_trade.get("entry_time", "")

        # 3. 如果已执行, 计算 PnL + MFE/MAE
        if inst.get("executed") or updates.get("executed"):
            exec_price = updates.get("execution_price") or inst.get("execution_price")
            if exec_price and exec_price > 0:
                # MFE/MAE 计算 (2026-09-02 方案A: tick 口径主路径, 见 _calc_mfe_mae docstring;
                #   executed_trade 已含跨轮误联防御结果, 传入避免二次查询)
                mfe, mae = self._calc_mfe_mae(
                    symbol, exec_price, direction, trade_date, inst,
                    executed_trade=executed_trade,
                )
                updates["max_favorable_excursion"] = mfe
                updates["max_adverse_excursion"] = mae

                # 检查是否已平仓 (P2b: 同款轮次联接)
                exit_trade = self._find_exit_trade(
                    symbol, trade_date, decision_id=inst.get("decision_id"))
                if exit_trade:
                    exit_price = exit_trade.get("exit_price", 0) or 0
                    position_size = inst.get("position_size") or 0
                    if exit_price > 0 and position_size > 0:
                        # direction 是 BUY/SELL_SHORT (playbook 指令格式)
                        if direction == "BUY":
                            pnl = (exit_price - exec_price) * position_size
                        else:
                            pnl = (exec_price - exit_price) * position_size
                        # 2026-07-21 量纲统一: pnl_pct 存"比例"(0-1), 与 trade_records / pipeline 回填一致.
                        #   旧代码 ×100 存百分比 → 同一笔在 playbook_instructions 与 trade_records 差 100 倍.
                        pnl_pct = (pnl / (exec_price * position_size))

                        updates["exit_price"] = exit_price
                        updates["exit_time"] = exit_trade.get("exit_time", "")
                        updates["exit_reason"] = exit_trade.get("exit_reason", "unknown")
                        updates["pnl"] = round(pnl, 2)
                        updates["pnl_pct"] = round(pnl_pct, 4)
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

        elif updates.get("triggered") or _already_triggered:
            # 触发了但没执行 → 计算 missed PnL
            # 2026-08-26 修复 (RFC-066 漏斗审计): 原条件只看影子自己回填的 triggered,
            #   漏看 auto_execute 盘中已写的 inst.triggered=1 → "盘中真触发但被资金帽/
            #   护栏拦截"的行落进 else 分支被误标 '未触发 (no_trigger)', 污染漏斗归因.
            #   实锤: 8/24 US.AU/AVGO/NTES 三笔再入场均被单日notional帽拦截,
            #   日志有 '方向穿越触发' 实据, DB 却标 no_trigger.
            shadow_pnl = self._calc_shadow_pnl(
                symbol, entry_price, direction, trade_date,
                position_size=inst.get("position_size"),
            )
            updates["shadow_pnl"] = shadow_pnl
            updates["outcome"] = "missed"
            # 2026-07-20 归因补齐: missed 曾 94/96 无 skip_reason → 漏斗/门成本不可测.
            #   只在原 reason 为空时补标 (不覆盖 auto_execute 写入的具体拒因).
            if not inst.get("skip_reason"):
                updates["skip_reason"] = "已触发未执行 (trigger_no_execution: 资金护栏/时段/执行失败, 详见当日auto_execute日志)"
        else:
            # 未触发 → 计算 shadow pnl, 并标记终态 missed (避免 run_playbook_track 幂等失败反复推送)
            shadow_pnl = self._calc_shadow_pnl(
                symbol, entry_price, direction, trade_date,
                position_size=inst.get("position_size"),
            )
            if shadow_pnl is not None:
                updates["shadow_pnl"] = shadow_pnl
            updates["outcome"] = "missed"
            # 2026-07-20 归因补齐: 与上分支同理, 未触发是漏斗最大黑洞 (81/94), 必须可归因.
            if not inst.get("skip_reason"):
                updates["skip_reason"] = "未触发 (no_trigger: 盘中价格未突破入场确认条件)"

        if updates:
            self.db.update_instruction_tracking(inst_id, updates)
            # ── 2026-10-17 归因修复 (零行为变更): 非成交行把"当日末次门拦截原因"浅合并进
            #    metadata_json.terminal_block_reason。**不动 skip_reason 字面** —— 它有 6+
            #    消费方按字面解析 (dashboard chain / plan_vs_actual / short_supply_funnel ...);
            #    走 db.merge_instruction_metadata 而非塞进 updates (后者被 _TRACKING_FIELDS
            #    白名单静默丢弃, 见 db.py:822-845 的 reprice 同型事故)。
            #    读出方式: json_extract(metadata_json, '$.terminal_block_reason')。
            #    回滚: 删本块即可 (已写键是纯注记)。
            try:
                if not (inst.get("executed") or updates.get("executed")):
                    _tbr = _terminal_block_reason_for(inst_id, trade_date)
                    if _tbr and _tbr.get("primary") and self.db.merge_instruction_metadata(
                        inst_id,
                        {
                            "terminal_block_reason": _tbr.get("primary"),
                            "terminal_block_reasons": _tbr.get("counts"),
                            "terminal_block_reason_last": _tbr.get("last"),
                            "terminal_block_reason_ts": _tbr.get("ts"),
                        },
                    ):
                        logger.info(
                            f"[归因] inst#{inst_id} {symbol} 非成交行补记门拦截原因: "
                            f"primary={_tbr.get('primary')} counts={_tbr.get('counts')} "
                            f"last={_tbr.get('last')} @{_tbr.get('ts')}"
                        )
            except Exception as _e:
                logger.warning(
                    f"[归因] inst#{inst_id} terminal_block_reason 写入失败 (不影响结算): {_e}",
                    exc_info=True,
                )

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

        # 使用实际 position_size; 缺失时按固定名义额反推股数, 而非硬编码 100 股.
        #   B2 (2026-07-21): 100 股对高价股 (META $626→$62.6k notional) 严重高估 shadow_pnl,
        #   与真实 playbook 单 (~$3,800 notional) 不可比. 改用 ~$3,800 名义额反推, 使 missed
        #   理论盈亏与实际执行单同量纲.
        if position_size and position_size > 0:
            shares = position_size
        else:
            _shadow_notional = 3800.0
            shares = max(1, int(_shadow_notional / entry_price))

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
        executed_trade: Optional[Dict] = None,
    ) -> tuple:
        """
        计算 MFE (最大有利偏移) 和 MAE (最大不利偏移)

        2026-09-02 方案A (DEB-20260902-mfe-caliber-and-shadow-zero, 用户裁"按建议执行"):
        持仓行改同源 tick 口径 — 读 trade_records.metadata 的 peak_price/trough_price
        (与平仓终值 _close_trade_record 同源, 轮询采样粒度), 修复日 bar 口径把入场前
        当日极值计入的污染 (ORCL 实证: 台账 +118.4 vs 入场后真实 $18.8, 09:31 spike
        早于建仓 31min)。MAE 统一正数量纲 (幅度, 与平仓行一致; 历史持仓行是负数,
        analyze_mfe_mae/feishu 均用 abs 兼容)。

        peak/trough 方向语义 (pipeline.py 退出监控): LONG peak=最高价/trough=最低价;
        SHORT peak=最低价(最有利)/trough=最高价(最不利)。

        兜底: trade 联接失败或 meta 峰谷缺失 (旧仓/跨轮误联丢弃) → 日 bar 路径
        (原口径) + warning 留痕 — 联接缺陷知悉而非静默。
        """
        if not exec_price or exec_price <= 0:
            return 0, 0

        position_size = inst.get("position_size", 0) or 100

        # ── 主路径: tick 口径 (trade_records.metadata peak/trough) ──
        trade = executed_trade
        if trade is None:
            trade = self._find_executed_trade(
                symbol, direction, current_date, decision_id=inst.get("decision_id"))
            # 2026-09-02 (核查批): 二次重查补同款幻影校验 — 否则 _track_instruction
            #   守卫丢弃的跨轮误联成交会在此处被捡回 (守卫纵深缺口)。
            if trade is not None and self._is_phantom_attach(trade, inst):
                logger.warning(
                    f"[shadow_tracker] {symbol} MFE/MAE 重查命中幻影成交 "
                    f"(entry {trade.get('entry_time')} 早于指令创建 {inst.get('created_at')}), "
                    f"丢弃回退日 bar 口径"
                )
                trade = None
        meta = trade.get("metadata_json") if trade else None
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = None
        peak = float(meta.get("peak_price", 0) or 0) if isinstance(meta, dict) else 0.0
        trough = float(meta.get("trough_price", 0) or 0) if isinstance(meta, dict) else 0.0
        if peak > 0 and trough > 0:
            if direction == "BUY":
                mfe = max(0.0, (peak - exec_price) * position_size)
                mae = max(0.0, (exec_price - trough) * position_size)
            else:
                mfe = max(0.0, (exec_price - peak) * position_size)
                mae = max(0.0, (trough - exec_price) * position_size)
            return round(mfe, 2), round(mae, 2)

        # ── 兜底: 日 bar 口径 (原路径, 含入场前极值污染 — 已知缺陷) ──
        logger.warning(
            f"[shadow_tracker] {symbol} tick 峰谷不可得 (联接失败/meta缺失), "
            f"MFE/MAE 回退日 bar 口径 (含入场前极值, DEB-20260902 议题一知悉)"
        )

        # 获取从入场到现在的日线数据
        daily_bars = self._get_daily_bars_since_entry(
            symbol, inst.get("execution_time", current_date), current_date
        )

        if not daily_bars:
            return 0, 0

        mfe = inst.get("max_favorable_excursion") or 0
        # 历史持仓行 MAE 是负数量纲 → 取 abs 统一正数幅度 (本轮兜底输出量纲不变混)
        mae = abs(inst.get("max_adverse_excursion") or 0)

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
            # adverse 负=不利 (BUY low<exec / SHORT high>exec), -adverse=不利幅度;
            # 正=当日无不利, 不计入。
            mae = max(mae, -adverse)

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

    def _is_phantom_attach(self, trade: Dict, inst: Dict) -> bool:
        """幻影回填判定: 成交时刻早于指令创建 >30s → 跨轮误联 (2026-08-27 P1 纵深防御B).

        30s 时钟偏斜容差 (auto_execute 先 claim 后写行, created_at 可能略晚于成交)。
        异常/字段缺失 → False (保守: 不误杀正常认领; 校验失败留 debug 由调用方定夺)。
        """
        try:
            _entry = str(trade.get("entry_time") or "")
            _created = str(inst.get("created_at") or "")
            if not (_entry and _created):
                return False
            _ed = datetime.fromisoformat(_entry)
            _cd = datetime.fromisoformat(_created)
            # 时区归一 (对齐 _calc_holding_hours 约定): naive 按 ET 解释
            _attach = (lambda d: _ET.localize(d)) if hasattr(_ET, "localize") else (lambda d: d.replace(tzinfo=_ET))
            _ed = _attach(_ed) if _ed.tzinfo is None else _ed.astimezone(_ET)
            _cd = _attach(_cd) if _cd.tzinfo is None else _cd.astimezone(_ET)
            return (_cd - _ed).total_seconds() > 30
        except Exception:
            return False

    def _find_executed_trade(
        self, symbol: str, direction: str, trade_date: str, decision_id: str = None
    ) -> Optional[Dict]:
        """在 trade_records 中查找匹配的执行记录

        2026-06-23: 修复方向映射 — playbook_instructions.direction 是 BUY/SELL_SHORT,
        trade_records.direction 是 LONG/SHORT. 需要归一化比较.

        P2b (2026-08-24 复查): 同日同标的同方向可能存在多轮成交 (intraday_reentry 重武装),
        原 symbol+direction+date 启发式会把【上一轮】成交认领到重武装后未成交的新轮指令行
        (盖章 executed=1 + 第一轮盈亏) — 复位 SQL 清掉的 outcome 当晚即被重新写脏.
        有 decision_id 上下文时改为轮次级精确联接: ①优先取 decision_id 完全一致的记录;
        ②否则只接受无归属 (decision_id 空) 的记录 — 带其他 decision_id 的成交证明属于
        别的指令/轮次, 不得认领. 无上下文 (旧调用方) 维持原启发式不变.
        """
        if self.trade_store is None:
            return None

        # 方向归一化: BUY→LONG, SELL_SHORT→SHORT
        _dir_map = {"BUY": "LONG", "SELL_SHORT": "SHORT"}
        _normalized = _dir_map.get(direction.upper(), direction.upper())

        try:
            trades = self.trade_store.query_by_date(trade_date)
            candidates = [
                t for t in trades
                if (t.get("symbol") == symbol and
                    t.get("source_type") in ("MANUAL_PLAYBOOK", "PLAYBOOK_SHADOW", "PLAYBOOK_SIM", "PLAYBOOK_LIVE") and
                    t.get("direction", "").upper() == _normalized)
            ]
            if not candidates:
                return None
            if decision_id:
                linked = [t for t in candidates
                          if (t.get("decision_id") or "") == decision_id]
                if linked:
                    return linked[0]
                unattributed = [t for t in candidates
                                if not (t.get("decision_id") or "")]
                return unattributed[0] if unattributed else None
            return candidates[0]
        except Exception as e:
            logger.warning(f"Failed to find executed trade for {symbol}: {e}")

        return None

    def _find_exit_trade(self, symbol: str, trade_date: str,
                         decision_id: str = None) -> Optional[Dict]:
        """查找平仓记录 — monitor_playbook_shadow_exits 直接更新 trade_records 的 exit 字段

        2026-06-23: 修复 — shadow 退出不是单独的 trade record, 而是同一条 record 的
        exit_price/exit_reason/exit_time 字段被 UPDATE。查找 source_type='PLAYBOOK_SHADOW'
        且 exit_price > 0 的记录。

        P2b (2026-08-24): 与 _find_executed_trade 同款轮次联接 — 原 symbol+date 扫描
        在多轮场景会把上一轮的平仓价盖到本轮 (本轮已成交未平仓时尤其致命).
        匹配语义与 _find_executed_trade 一致: 精确联接 > 无归属 > (无上下文)原启发式.
        """
        if self.trade_store is None:
            return None

        def _is_candidate(t):
            return (t.get("symbol") == symbol and
                    t.get("source_type") in ("MANUAL_PLAYBOOK", "PLAYBOOK_SHADOW",
                                             "PLAYBOOK_SIM", "PLAYBOOK_LIVE") and
                    float(t.get("exit_price", 0) or 0) > 0)

        try:
            trades = self.trade_store.query_by_date(trade_date)
            candidates = [t for t in trades if _is_candidate(t)]
            if not candidates:
                return None
            picked = None
            if decision_id:
                linked = [t for t in candidates
                          if (t.get("decision_id") or "") == decision_id]
                if linked:
                    picked = linked[0]
                else:
                    unattributed = [t for t in candidates
                                    if not (t.get("decision_id") or "")]
                    picked = unattributed[0] if unattributed else None
            else:
                picked = candidates[0]
            if picked:
                return {
                    "exit_price": float(picked.get("exit_price", 0)),
                    "exit_time": picked.get("exit_time", ""),
                    "exit_reason": picked.get("exit_reason", "unknown"),
                }
        except Exception as e:
            logger.warning(f"Failed to find exit trade for {symbol}: {e}")

        return None

    def _calc_holding_hours(self, inst: Dict, exit_trade: Dict) -> float:
        """计算持仓时长 (小时)

        2026-07-21 修复: entry/exit 时间时区混存 (ET/-04:00, 北京/+08:00, naive) →
        naive−aware 抛 TypeError 被静默吞成 None; +08:00−(-04:00) 得负值 (如 EOG -6.7h).
        统一归一到 ET 再相减: naive 视为已是 ET, aware 转 ET; 残留负值取绝对值兜底.
        """
        try:
            entry_time = inst.get("execution_time", "")
            exit_time = exit_trade.get("exit_time", "")
            if entry_time and exit_time:
                entry_dt = datetime.fromisoformat(entry_time)
                exit_dt = datetime.fromisoformat(exit_time)
                # 2026-07-25 (时区SSOT批漏网补修): pytz 时区禁 replace(tzinfo=) — 产生 -04:56 LMT 偏移
                _attach = (lambda d: _ET.localize(d)) if hasattr(_ET, "localize") else (lambda d: d.replace(tzinfo=_ET))
                entry_dt = _attach(entry_dt) if entry_dt.tzinfo is None else entry_dt.astimezone(_ET)
                exit_dt = _attach(exit_dt) if exit_dt.tzinfo is None else exit_dt.astimezone(_ET)
                return round(abs((exit_dt - entry_dt).total_seconds()) / 3600, 1)
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
