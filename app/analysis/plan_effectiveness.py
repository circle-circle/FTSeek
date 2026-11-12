"""
盘前计划命中率追踪器 (Plan Effectiveness Tracker)

每日收盘后对标当日 Playbook 指令 vs 实际交易，生成命中率/执行率报告。

核心指标:
- entry_zone_hit: 实际入场价是否落在 instruction.entry_price 附近 (±0.5%)
- direction_match: 实际方向是否与 instruction.direction 一致
- traded: 是否真的下单了
- pnl_contribution: 该目标贡献的实际收益
- hit_rate / traded_rate / avg_pnl_of_planned / unplanned_trade_count

数据源: playbook_instructions 表 vs trade_records 表

口径 (2026-10-20 记录卫生):
- planned 分母 = 非 SKIP 指令 + 被 2 分钟门放弃的指令 (`skip_reason='entry_confirm_reversed'`)。
  后者计入 planned 但不计 traded, 因此 traded_rate 是真实执行率 (例: 7 成交 / 8 可交易 = 87.5%),
  不再出现"只拿已执行目标做分母 ⇒ 恒 100%"的假绿。
- 放弃目标在 targets 中带 `status='abandoned_entry_confirm'` 与 `skip_reason`, 可逐条追溯。

输出: data/plan_effectiveness/daily_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.playbook.instruction_attribution import (
    AMBIGUOUS,
    ATTRIBUTION_SOURCES,
    canonical_plan_direction,
    count_sources,
    resolve_trade_instruction_ids,
)

logger = logging.getLogger("ftseek.analysis.plan_effectiveness")


def _norm_symbol(sym: str) -> str:
    """归一化标的: 去掉 US. 前缀 + 去掉期权后缀, 提取 underlying ticker。

    例:
        "US.NVDA"            -> "NVDA"
        "US.NVDA250530C135"  -> "NVDA"
        "NVDA"               -> "NVDA"
    """
    if not sym:
        return ""
    s = str(sym).upper().strip()
    # 去掉 "US." 等市场前缀
    if "." in s:
        s = s.split(".", 1)[1]
    # 期权格式: TICKER + YYMMDD + C/P + STRIKE
    # 提取开头的纯字母 ticker 部分
    m = re.match(r"^([A-Z]+)", s)
    if m:
        return m.group(1)
    return s


class PlanEffectivenessTracker:
    """盘前计划命中率追踪器"""

    def __init__(self, db_path: Optional[str] = None, output_dir: Optional[str] = None):
        # 解析 db_path
        if db_path:
            self.db_path = Path(db_path)
        else:
            try:
                from app.core.paths import get_trade_records_db
                self.db_path = Path(get_trade_records_db())
            except Exception:
                project_root = Path(__file__).resolve().parent.parent.parent
                self.db_path = project_root / "data" / "ftseek_us.db"

        # 解析 output_dir
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            try:
                from app.core.paths import get_data_dir
                self.output_dir = Path(get_data_dir()) / "plan_effectiveness"
            except Exception:
                project_root = Path(__file__).resolve().parent.parent.parent
                self.output_dir = project_root / "data" / "plan_effectiveness"

        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[PlanEffectiveness] 创建输出目录失败: {e}")

    # ────────────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────────────
    def evaluate_daily_plan(self, date: Optional[str] = None) -> Dict[str, Any]:
        """对标当日盘前计划 vs 实际交易, 返回 JSON 报告。

        Args:
            date: YYYY-MM-DD; 默认 = 今日 (本地)

        Returns:
            报告 dict; 异常或无计划时返回 {"plan_available": False, ...}
        """
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        report: Dict[str, Any] = {
            "date": date,
            "plan_available": False,
            "generated_at": datetime.now().isoformat(),
            "targets": [],
            "hit_rate": 0.0,
            "traded_rate": 0.0,
            "avg_pnl_of_planned": 0.0,
            "unplanned_trade_count": 0,
            "attribution_ambiguous_count": 0,
            # 2026-11-12: 归因来源分层 — 精确 (metadata) / decision_id 回退 / 陈旧 metadata。
            #   五类恒有键 (0 也输出), 防"缺键=没发生"的误读。
            "attribution_source_counts": {name: 0 for name in ATTRIBUTION_SOURCES},
            "planned_count": 0,
            "abandoned_count": 0,
            "abandoned_symbols": [],
            "execution_rate": 0.0,
            "execution_rate_basis": (
                "strict instruction attribution; traded / "
                "(non-SKIP targets + entry_confirm_reversed abandoned)"
            ),
        }

        try:
            plan = self._load_battle_plan(date)
        except Exception as e:
            logger.warning(f"[PlanEffectiveness] 加载作战计划失败: {e}")
            plan = None

        try:
            trades = self._get_trades_for_date(date)
        except Exception as e:
            logger.warning(f"[PlanEffectiveness] 加载交易记录失败: {e}")
            trades = []

        report["total_trades"] = len(trades)

        if not plan or not plan.get("targets"):
            # 即使无计划也保存空报告，便于追溯
            try:
                self._save_report(report, date)
            except Exception as e:
                logger.debug(f"[PlanEffectiveness] 保存空报告失败: {e}")
            return report

        report["plan_available"] = True
        report["plan_date"] = plan.get("date", date)

        targets = plan.get("targets", []) or []
        target_evals: List[Dict[str, Any]] = []
        for t in targets:
            try:
                ev = self._evaluate_target(t, trades)
            except Exception as e:
                logger.debug(f"[PlanEffectiveness] 评估 target 失败: {e}")
                continue
            target_evals.append(ev)

        # 汇总指标
        hit_count = sum(1 for ev in target_evals if ev.get("entry_zone_hit"))
        traded_count = sum(1 for ev in target_evals if ev.get("traded"))
        pnls_of_planned = [
            ev.get("pnl_contribution", 0.0)
            for ev in target_evals
            if ev.get("traded")
        ]
        avg_pnl = (sum(pnls_of_planned) / len(pnls_of_planned)) if pnls_of_planned else 0.0

        planned_trade_indexes = set()
        for ev in target_evals:
            planned_trade_indexes.update(ev.pop("_matched_trade_indexes", []))
        ambiguous_trade_indexes = {
            idx for idx, trade in enumerate(trades)
            if trade.get("_resolved_instruction_id") is AMBIGUOUS
        }
        unplanned = len(trades) - len(planned_trade_indexes) - len(ambiguous_trade_indexes)

        planned_count = len(target_evals)
        abandoned = [
            ev for ev in target_evals
            if ev.get("status") == "abandoned_entry_confirm" and not ev.get("traded")
        ]
        report["targets"] = target_evals
        report["planned_count"] = planned_count
        report["traded_planned_count"] = traded_count
        report["hit_count"] = hit_count
        report["hit_rate"] = (hit_count / planned_count) if planned_count else 0.0
        report["traded_rate"] = (traded_count / planned_count) if planned_count else 0.0
        report["execution_rate"] = report["traded_rate"]
        report["abandoned_count"] = len(abandoned)
        report["abandoned_symbols"] = [ev.get("symbol") for ev in abandoned]
        report["avg_pnl_of_planned"] = avg_pnl
        report["unplanned_trade_count"] = unplanned
        report["attribution_ambiguous_count"] = len(ambiguous_trade_indexes)
        report["attribution_source_counts"] = count_sources(
            trade.get("_attribution_source") or "" for trade in trades)

        try:
            self._save_report(report, date)
        except Exception as e:
            logger.warning(f"[PlanEffectiveness] 保存报告失败: {e}")

        logger.info(
            f"[PlanEffectiveness] {date} | planned={planned_count} | "
            f"traded={traded_count} | abandoned={len(abandoned)} | "
            f"hit_rate={report['hit_rate']:.2%} | traded_rate={report['traded_rate']:.2%} | "
            f"unplanned={unplanned} | ambiguous={len(ambiguous_trade_indexes)} | "
            f"basis={report['execution_rate_basis']}"
        )
        return report

    # ────────────────────────────────────────────────────────────
    # 内部方法
    # ────────────────────────────────────────────────────────────
    def _load_battle_plan(self, date: str) -> Optional[Dict[str, Any]]:
        """从 playbook_instructions 表加载指定日期的可交易/放弃指令。

        2026-10-20: 除非 SKIP 指令外, 额外纳入 `triggered=1` 且
        `skip_reason='entry_confirm_reversed'` 的 2 分钟门放弃目标 —
        否则这些目标从分母消失, traded_rate 会虚高为 100%。

        Returns: 兼容旧 battle_plan 格式的 dict: {"date": ..., "targets": [...]}
        """
        if not self.db_path.exists():
            logger.debug(f"[PlanEffectiveness] DB 不存在: {self.db_path}")
            return None

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, symbol, direction, decision_id, entry_price, stop_loss, take_profit, "
                    "confidence, triggered, executed, skip_reason "
                    "FROM playbook_instructions "
                    "WHERE date = ? AND ("
                    "  direction != 'SKIP' "
                    "  OR (triggered = 1 AND skip_reason = 'entry_confirm_reversed')"
                    ")",
                    (date,),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[PlanEffectiveness] 查询 playbook_instructions 失败: {e}")
            return None

        if not rows:
            return None

        targets = []
        for r in rows:
            entry_price = float(r["entry_price"] or 0)
            # 用 entry_price ±0.5% 构造入场区间 (兼容 _evaluate_target 的 zone 逻辑)
            if entry_price > 0:
                zone = [entry_price * 0.995, entry_price * 1.005]
            else:
                zone = []
            # 方向归一化: SELL_SHORT→SHORT, BUY→LONG
            _DIR_MAP = {"SELL_SHORT": "SHORT", "SELL": "SHORT", "SHORT": "SHORT", "BUY": "LONG", "LONG": "LONG"}
            raw_dir = str(r["direction"] or "").upper()
            direction = _DIR_MAP.get(raw_dir, raw_dir)
            skip_reason = str(r["skip_reason"] or "")
            if r["triggered"] and skip_reason == "entry_confirm_reversed":
                status = "abandoned_entry_confirm"
            else:
                status = "triggered" if r["triggered"] else (
                    "executed" if r["executed"] else "")
            targets.append({
                "instruction_id": r["id"],
                "decision_id": r["decision_id"],
                "symbol": r["symbol"],
                "direction": direction,
                "entry_zone": zone,
                "status": status,
                "skip_reason": skip_reason,
            })

        return {"date": date, "targets": targets}

    def _get_trades_for_date(self, date: str) -> List[Dict[str, Any]]:
        """从 trade_records 表获取当日交易。"""
        if not self.db_path.exists():
            logger.debug(f"[PlanEffectiveness] DB 不存在: {self.db_path}")
            return []

        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM trade_records WHERE trade_date = ? "
                    "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                    "AND (exit_reason IS NULL OR exit_reason != 'PHANTOM_RECONCILE_VOID')",
                    (date,),
                ).fetchall()
                instruction_rows = conn.execute(
                    "SELECT id, decision_id, date, symbol, direction FROM playbook_instructions"
                ).fetchall()
                trades = [dict(r) for r in rows]
                identities = [dict(r) for r in instruction_rows]
                _attr_sources: list = []
                resolved_ids = resolve_trade_instruction_ids(
                    trades, identities, sources=_attr_sources)
                for trade, instruction_id, _src in zip(trades, resolved_ids, _attr_sources):
                    trade["_resolved_instruction_id"] = instruction_id
                    trade["_attribution_source"] = _src
                return trades
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning(f"[PlanEffectiveness] 查询 trade_records 失败: {e}")
            return []

    def _evaluate_target(self, target: Dict[str, Any], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """评估单个计划目标: 是否交易、入场区间命中、方向一致、PnL 贡献。"""
        plan_symbol = target.get("symbol", "")
        plan_direction = str(target.get("direction", "")).upper()
        canonical_direction = canonical_plan_direction(plan_direction)
        entry_zone = target.get("entry_zone") or []

        ev: Dict[str, Any] = {
            "symbol": plan_symbol,
            "plan_direction": plan_direction,
            "entry_zone": list(entry_zone) if isinstance(entry_zone, (list, tuple)) else [],
            "status": target.get("status", ""),
            "skip_reason": target.get("skip_reason", ""),
            "instruction_id": target.get("instruction_id"),
            "traded": False,
            "entry_zone_hit": False,
            "direction_match": False,
            "pnl_contribution": 0.0,
            "matched_trades": 0,
            "actual_entry_prices": [],
            "actual_directions": [],
        }

        # 解析 entry_zone
        zone_low: Optional[float] = None
        zone_high: Optional[float] = None
        if isinstance(entry_zone, (list, tuple)) and len(entry_zone) == 2:
            try:
                zone_low = float(entry_zone[0])
                zone_high = float(entry_zone[1])
                if zone_low > zone_high:
                    zone_low, zone_high = zone_high, zone_low
            except (TypeError, ValueError):
                zone_low = zone_high = None

        matched: List[Dict[str, Any]] = []
        matched_indexes: List[int] = []
        for index, tr in enumerate(trades):
            if tr.get("_resolved_instruction_id") != target.get("instruction_id"):
                continue
            tr_dir = canonical_plan_direction(tr.get("direction"))
            if tr_dir:
                ev["actual_directions"].append(str(tr.get("direction", "")).upper())
            if canonical_direction not in ("", "ANY") and tr_dir != canonical_direction:
                continue
            matched.append(tr)
            matched_indexes.append(index)

        ev["_matched_trade_indexes"] = matched_indexes

        if not matched:
            return ev

        ev["traded"] = True
        ev["matched_trades"] = len(matched)

        total_pnl = 0.0
        any_zone_hit = False
        any_direction_match = False

        for tr in matched:
            pnl = tr.get("pnl_net")
            if pnl is None:
                pnl = tr.get("realized_pnl")
            if pnl is None:
                pnl = tr.get("pnl")
            try:
                pnl_f = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                pnl_f = 0.0
            total_pnl += pnl_f

            # entry price
            ep = tr.get("entry_price")
            try:
                ep_f = float(ep) if ep is not None else None
            except (TypeError, ValueError):
                ep_f = None
            if ep_f is not None:
                ev["actual_entry_prices"].append(ep_f)
                if zone_low is not None and zone_high is not None:
                    if zone_low <= ep_f <= zone_high:
                        any_zone_hit = True

            # direction
            tr_dir = canonical_plan_direction(tr.get("direction"))
            if tr_dir:
                if canonical_direction in ("", "ANY") or tr_dir == canonical_direction:
                    any_direction_match = True

        ev["pnl_contribution"] = total_pnl
        ev["entry_zone_hit"] = any_zone_hit
        ev["direction_match"] = any_direction_match
        return ev

    def _save_report(self, report: Dict[str, Any], date: str) -> None:
        """保存 daily_YYYY-MM-DD.json 到 output_dir。"""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        out_path = self.output_dir / f"daily_{date}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"[PlanEffectiveness] 报告已保存: {out_path}")


__all__ = ["PlanEffectivenessTracker"]
