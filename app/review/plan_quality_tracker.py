"""
盘前计划质量追踪器 (Task #81)

功能:
1. 执行率追踪: plan_targets vs actual_trades
2. 质量评分: 按thesis_type/symbol分类统计
3. 周度复盘: 生成JSON摘要供LLM注入

注意: trade_records 实际使用 realized_pnl 字段（非 pnl_usd）；
target 的有效状态字段为 open_check_status（VALID/INVALID/缺省）。
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
DB_PATH = os.path.join(DATA_DIR, "ftseek_us.db")
PLAN_PATH = os.path.join(DATA_DIR, "daily_battle_plan_us.json")
QUALITY_REPORT_PATH = os.path.join(DATA_DIR, "plan_quality_weekly.json")


class PlanQualityTracker:
    """计划质量追踪器"""

    def __init__(self, db_path: str = DB_PATH, plan_path: str = PLAN_PATH):
        self.db_path = db_path
        self.plan_path = plan_path

    # =====================================================================
    # 1. 每日执行率追踪
    # =====================================================================
    def track_daily_execution(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """
        每日收盘后调用：统计今日计划标的执行情况

        Returns:
            {
                "date": "2026-05-29",
                "plan_targets": ["US.NVDA", "US.TSLA", ...],
                "executed_targets": ["US.NVDA"],
                "non_plan_trades": ["US.AMD"],
                "execution_rate": 0.33,
                "blocked_by": {"REGIME_HIGH_VOL": 2, "TIMING_LOW": 1}
            }
        """
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        result: Dict[str, Any] = {
            "date": date_str,
            "plan_targets": [],
            "executed_targets": [],
            "non_plan_trades": [],
            "execution_rate": 0.0,
            "blocked_by": {},
        }

        try:
            plan = self._load_plan()
            if not plan:
                return result

            plan_symbols = set()
            for t in plan.get("targets", []) or []:
                sym = t.get("symbol", "")
                if not sym:
                    continue
                status = t.get("open_check_status") or t.get("status")
                # VALID / ACTIVE / 未填均视作有效计划标的
                if status in ("VALID", "ACTIVE", None, ""):
                    plan_symbols.add(sym)
            result["plan_targets"] = sorted(plan_symbols)

            trades = self._query_trades(date_str)
            traded_symbols = {t["symbol"] for t in trades if t.get("symbol")}

            result["executed_targets"] = sorted(plan_symbols & traded_symbols)
            result["non_plan_trades"] = sorted(traded_symbols - plan_symbols)

            if plan_symbols:
                result["execution_rate"] = round(
                    len(result["executed_targets"]) / len(plan_symbols), 4
                )

        except Exception as e:
            logger.warning(f"[PlanQualityTracker] track_daily_execution failed: {e}")

        return result

    # =====================================================================
    # 2. 质量评分
    # =====================================================================
    def score_quality(self, days: int = 7) -> Dict[str, Any]:
        """
        质量评分：比较计划标的 vs 非计划标的的表现
        """
        result: Dict[str, Any] = {
            "period": f"{days}d",
            "plan_trades": {"count": 0, "win_rate": 0.0, "total_pnl": 0.0},
            "non_plan_trades": {"count": 0, "win_rate": 0.0, "total_pnl": 0.0},
            "by_thesis_type": {},
        }

        try:
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

            plan = self._load_plan()
            plan_symbols = set()
            if plan:
                for t in plan.get("targets", []) or []:
                    sym = t.get("symbol", "")
                    if sym:
                        plan_symbols.add(sym)

            trades = self._query_trades_range(start_date, end_date)

            plan_trades = [t for t in trades if t["symbol"] in plan_symbols]
            non_plan_trades = [t for t in trades if t["symbol"] not in plan_symbols]

            result["plan_trades"] = self._calc_stats(plan_trades)
            result["non_plan_trades"] = self._calc_stats(non_plan_trades)

            thesis_groups: Dict[str, List[Dict]] = {}
            for t in trades:
                thesis = self._extract_thesis_type(t)
                thesis_groups.setdefault(thesis, []).append(t)

            for thesis, group in thesis_groups.items():
                stats = self._calc_stats(group)
                if stats["count"] > 0:
                    stats["avg_pnl"] = round(stats["total_pnl"] / stats["count"], 2)
                result["by_thesis_type"][thesis] = stats

        except Exception as e:
            logger.warning(f"[PlanQualityTracker] score_quality failed: {e}")

        return result

    # =====================================================================
    # 3. 周度复盘
    # =====================================================================
    def generate_weekly_summary(self) -> Dict[str, Any]:
        """
        周度复盘：生成JSON摘要供下周盘前计划LLM注入
        输出保存到 data/plan_quality_weekly.json
        """
        summary: Dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "period": "last_7_days",
            "quality_score": self.score_quality(7),
            "recommendations": [],
        }

        try:
            quality = summary["quality_score"]

            plan_wr = quality["plan_trades"].get("win_rate", 0)
            non_plan_wr = quality["non_plan_trades"].get("win_rate", 0)

            if plan_wr > non_plan_wr:
                summary["recommendations"].append(
                    f"计划标的胜率({plan_wr:.0%})优于非计划({non_plan_wr:.0%})，继续加强计划驱动"
                )
            else:
                summary["recommendations"].append(
                    f"计划标的胜率({plan_wr:.0%})不及非计划({non_plan_wr:.0%})，需审视计划质量"
                )

            for thesis, stats in quality.get("by_thesis_type", {}).items():
                if thesis == "unknown":
                    continue
                wr = stats.get("win_rate", 0)
                cnt = stats.get("count", 0)
                if cnt >= 3:
                    if wr >= 0.5:
                        summary["recommendations"].append(
                            f"{thesis}策略表现优异(胜率{wr:.0%}/{cnt}笔)，建议增加权重"
                        )
                    elif wr < 0.25:
                        summary["recommendations"].append(
                            f"{thesis}策略表现不佳(胜率{wr:.0%}/{cnt}笔)，建议减少或暂停"
                        )

            try:
                os.makedirs(os.path.dirname(QUALITY_REPORT_PATH), exist_ok=True)
                with open(QUALITY_REPORT_PATH, "w", encoding="utf-8") as f:
                    json.dump(summary, f, indent=2, ensure_ascii=False)
                logger.info(f"[PlanQualityTracker] 周度复盘已保存: {QUALITY_REPORT_PATH}")
            except Exception as e:
                logger.warning(f"[PlanQualityTracker] 保存周度复盘失败: {e}")

        except Exception as e:
            logger.warning(f"[PlanQualityTracker] generate_weekly_summary failed: {e}")

        return summary

    # =====================================================================
    # 4. LLM注入文本
    # =====================================================================
    def get_injection_for_llm(self) -> str:
        """获取供盘前计划LLM注入的质量反馈文本"""
        try:
            if not os.path.exists(QUALITY_REPORT_PATH):
                return ""
            with open(QUALITY_REPORT_PATH, "r", encoding="utf-8") as f:
                report = json.load(f)

            lines = ["【上周计划执行复盘】"]
            quality = report.get("quality_score", {})
            plan_stats = quality.get("plan_trades", {}) or {}
            non_plan_stats = quality.get("non_plan_trades", {}) or {}

            lines.append(
                f"- 计划标的: {plan_stats.get('count', 0)}笔, "
                f"胜率{plan_stats.get('win_rate', 0):.0%}, "
                f"盈亏${plan_stats.get('total_pnl', 0):.0f}"
            )
            lines.append(
                f"- 非计划标的: {non_plan_stats.get('count', 0)}笔, "
                f"胜率{non_plan_stats.get('win_rate', 0):.0%}, "
                f"盈亏${non_plan_stats.get('total_pnl', 0):.0f}"
            )

            for rec in (report.get("recommendations") or [])[:3]:
                lines.append(f"- {rec}")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"[PlanQualityTracker] get_injection_for_llm failed: {e}")

        return ""

    # =====================================================================
    # Private helpers
    # =====================================================================
    def _load_plan(self) -> Optional[Dict]:
        try:
            if os.path.exists(self.plan_path):
                with open(self.plan_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"[PlanQualityTracker] _load_plan failed: {e}")
        return None

    def _query_trades(self, date_str: str) -> List[Dict]:
        """查询指定日期的交易（按 trade_date 优先，回退 entry/exit_time）"""
        sql = (
            "SELECT symbol, direction, realized_pnl, exit_reason, "
            "entry_time, exit_time, trade_date, entry_reason, "
            "metadata_json, owner_strategy, signal_details "
            "FROM trade_records "
            "WHERE trade_date = ? "
            "   OR date(entry_time) = ? "
            "   OR date(exit_time) = ?"
        )
        return self._execute_query(sql, (date_str, date_str, date_str))

    def _query_trades_range(self, start_date: str, end_date: str) -> List[Dict]:
        """查询日期范围内的交易（按 trade_date）"""
        sql = (
            "SELECT symbol, direction, realized_pnl, exit_reason, "
            "entry_time, exit_time, trade_date, entry_reason, "
            "metadata_json, owner_strategy, signal_details "
            "FROM trade_records "
            "WHERE (trade_date >= ? AND trade_date <= ?) "
            "   OR (date(exit_time) >= ? AND date(exit_time) <= ?)"
        )
        return self._execute_query(sql, (start_date, end_date, start_date, end_date))

    def _execute_query(self, sql: str, params: tuple) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql, params)
            rows = [dict(row) for row in cursor.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"[PlanQualityTracker] query failed: {e}")
            return []

    def _calc_stats(self, trades: List[Dict]) -> Dict[str, Any]:
        """计算交易统计（基于 realized_pnl）"""
        if not trades:
            return {"count": 0, "win_rate": 0.0, "total_pnl": 0.0}

        count = len(trades)
        wins = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
        total_pnl = sum((t.get("realized_pnl") or 0) for t in trades)

        return {
            "count": count,
            "win_rate": round(wins / count, 4) if count > 0 else 0.0,
            "total_pnl": round(float(total_pnl), 2),
        }

    def _extract_thesis_type(self, trade: Dict) -> str:
        """
        从 trade 记录中尽力解析 thesis_type：
        优先级 metadata_json.thesis_type > signal_details.thesis_type
              > owner_strategy > entry_reason 关键词 > unknown
        """
        try:
            meta_raw = trade.get("metadata_json")
            if meta_raw:
                try:
                    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                    if isinstance(meta, dict):
                        v = meta.get("thesis_type")
                        if v:
                            return str(v)
                except Exception:
                    pass

            sig_raw = trade.get("signal_details")
            if sig_raw:
                try:
                    sig = json.loads(sig_raw) if isinstance(sig_raw, str) else sig_raw
                    if isinstance(sig, dict):
                        v = sig.get("thesis_type")
                        if v:
                            return str(v)
                except Exception:
                    pass

            owner = trade.get("owner_strategy")
            if owner:
                return str(owner)

            er = (trade.get("entry_reason") or "").lower()
            if "trend" in er:
                return "trend_following"
            if "breakout" in er:
                return "breakout"
            if "reversal" in er or "mean_revert" in er:
                return "mean_reversion"
        except Exception:
            pass
        return "unknown"


# 模块级便捷函数（供脚本直接调用）
def track_daily_execution(date_str: Optional[str] = None) -> Dict[str, Any]:
    return PlanQualityTracker().track_daily_execution(date_str)


def generate_weekly_summary() -> Dict[str, Any]:
    return PlanQualityTracker().generate_weekly_summary()


def get_injection_for_llm() -> str:
    return PlanQualityTracker().get_injection_for_llm()
