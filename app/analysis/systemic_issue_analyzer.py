"""
系统性问题归纳器 (Systemic Issue Analyzer)

Task #30 (2026-05-27)

目标:
  - 从 trade_attribution 表聚合系统性问题
  - 按 systemic_pattern 聚类
  - 生成优先修复建议
  - 每周日自动触发

数据源:
  ftseek_us.db -> trade_attribution (is_systemic_issue, systemic_pattern)

  注: is_systemic_issue / systemic_pattern 是可选列。若不存在则降级使用
  failure_factors / pnl_category / exit_reason 推导, 不强制依赖 schema 升级。

输出:
  data/systemic_issues/weekly_YYYY-Wxx.json
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
)
DEFAULT_OUTPUT_DIR = str(
    Path(__file__).resolve().parent.parent.parent / "data" / "systemic_issues"
)


class SystemicIssueAnalyzer:
    """系统性问题归纳器"""

    def __init__(self, db_path: Optional[str] = None, output_dir: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.output_dir = Path(output_dir or DEFAULT_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────────────────
    #  Schema 探测 — 兼容 is_systemic_issue / systemic_pattern 缺失
    # ──────────────────────────────────────────────────────────────────
    def _get_attribution_columns(self, conn: sqlite3.Connection) -> set:
        try:
            rows = conn.execute("PRAGMA table_info(trade_attribution)").fetchall()
            return {r[1] for r in rows}
        except Exception:
            return set()

    def _derive_pattern_from_row(self, row: Dict[str, Any]) -> Optional[str]:
        """缺乏 systemic_pattern 列时, 从 failure_factors / exit_reason / pnl 推导模式"""
        # 1. 优先使用 failure_factors 中的标签
        ff = row.get("failure_factors") or ""
        if isinstance(ff, str) and ff:
            ff_lower = ff.lower()
            if "premature" in ff_lower or "early_exit" in ff_lower or "过早" in ff:
                return "premature_exit"
            if "wrong_direction" in ff_lower or "逆势" in ff or "反向" in ff:
                return "wrong_direction"
            if "stop_loss_too_tight" in ff_lower or "止损过紧" in ff:
                return "stop_loss_too_tight"
            if "stop_loss_too_wide" in ff_lower or "止损过宽" in ff:
                return "stop_loss_too_wide"
            if "overtrading" in ff_lower or "过度交易" in ff:
                return "overtrading"
            if "news" in ff_lower:
                return "news_driven_loss"

        # 2. 从 exit_reason 推导
        exit_reason = (row.get("exit_reason") or "").lower()
        pnl = row.get("pnl") or row.get("realized_pnl") or 0
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            pnl = 0.0

        if pnl < 0:
            if "stop_loss" in exit_reason or "stop loss" in exit_reason:
                return "stop_loss_too_tight"
            if "trailing" in exit_reason:
                return "premature_exit"
            if "time_exit" in exit_reason or "eod" in exit_reason:
                return "entry_timing_poor"

        return None

    # ──────────────────────────────────────────────────────────────────
    #  主入口
    # ──────────────────────────────────────────────────────────────────
    def analyze_weekly(self, week_date: Optional[str] = None) -> Dict[str, Any]:
        """
        周度系统性问题汇总

        Args:
            week_date: 周起始日期 (默认上周一)

        Returns:
            周度系统性问题分析报告
        """
        if week_date is None:
            today = datetime.now()
            last_monday = today - timedelta(days=today.weekday() + 7)
            week_date = last_monday.strftime("%Y-%m-%d")

        start_date = week_date
        end_date = (
            datetime.strptime(week_date, "%Y-%m-%d") + timedelta(days=7)
        ).strftime("%Y-%m-%d")
        week_label = datetime.strptime(week_date, "%Y-%m-%d").strftime("%Y-W%W")

        rows: List[Dict[str, Any]] = []
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cols = self._get_attribution_columns(conn)

            has_systemic_flag = "is_systemic_issue" in cols
            has_pattern_col = "systemic_pattern" in cols

            # 构造查询: 优先利用列 (若存在), 否则降级 (按 pnl<0 拉所有亏损)
            if has_systemic_flag:
                base_sql = """
                    SELECT * FROM trade_attribution
                    WHERE is_systemic_issue = 1
                      AND trade_date >= ? AND trade_date < ?
                """
            else:
                base_sql = """
                    SELECT * FROM trade_attribution
                    WHERE trade_date >= ? AND trade_date < ?
                      AND (
                        (realized_pnl IS NOT NULL AND realized_pnl < 0)
                        OR (pnl IS NOT NULL AND pnl < 0)
                      )
                """

            try:
                rows_raw = conn.execute(base_sql, (start_date, end_date)).fetchall()
            except sqlite3.OperationalError as oe:
                logger.warning(f"[SystemicAnalyzer] 主查询失败, 降级: {oe}")
                rows_raw = conn.execute(
                    "SELECT * FROM trade_attribution "
                    "WHERE trade_date >= ? AND trade_date < ?",
                    (start_date, end_date),
                ).fetchall()

            # 与 trade_records 关联补全 pnl/symbol/direction (容错)
            for r in rows_raw:
                d = dict(r)
                if not d.get("pnl"):
                    d["pnl"] = d.get("realized_pnl") or 0
                if has_pattern_col and d.get("systemic_pattern"):
                    pass
                else:
                    d["systemic_pattern"] = self._derive_pattern_from_row(d) or "unknown"
                rows.append(d)
            conn.close()
        except Exception as e:
            logger.warning(f"[SystemicAnalyzer] DB 查询失败: {e}")
            rows = []

        if not rows:
            result = {
                "week": week_label,
                "period_start": start_date,
                "period_end": end_date,
                "total_systemic_issues": 0,
                "total_affected_trades": 0,
                "patterns": {},
                "priority_fixes": [],
                "generated_at": datetime.now().isoformat(),
            }
            self._save_report(result, week_label)
            return result

        # 按 systemic_pattern 聚类
        patterns: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            pattern = row.get("systemic_pattern") or "unknown"
            patterns[pattern].append(row)

        # 分析每个模式
        pattern_analysis: Dict[str, Dict[str, Any]] = {}
        for pattern, trades in patterns.items():
            losses = [float(t.get("pnl", 0) or 0) for t in trades]
            total_loss = sum(p for p in losses if p < 0)
            affected_symbols = sorted({t.get("symbol", "") for t in trades if t.get("symbol")})
            trade_dates = [t.get("trade_date", "") for t in trades if t.get("trade_date")]

            # 严重度判断
            if len(trades) >= 5 or total_loss < -2000:
                severity = "critical"
            elif len(trades) >= 3 or total_loss < -1000:
                severity = "high"
            elif len(trades) >= 2:
                severity = "medium"
            else:
                severity = "low"

            pattern_analysis[pattern] = {
                "count": len(trades),
                "total_loss": round(total_loss, 2),
                "affected_symbols": affected_symbols[:10],
                "severity": severity,
                "first_occurrence": min(trade_dates) if trade_dates else "",
                "last_occurrence": max(trade_dates) if trade_dates else "",
                "recommendation": self._generate_recommendation(pattern, trades),
            }

        # 优先修复排序 (按累计亏损升序, 最亏在前)
        priority_fixes = sorted(
            [{"pattern": p, **data} for p, data in pattern_analysis.items()],
            key=lambda x: x["total_loss"],
        )[:5]

        result = {
            "week": week_label,
            "period_start": start_date,
            "period_end": end_date,
            "total_systemic_issues": len(rows),
            "total_affected_trades": len({r.get("trace_id", "") for r in rows if r.get("trace_id")}) or len(rows),
            "patterns": pattern_analysis,
            "priority_fixes": priority_fixes,
            "generated_at": datetime.now().isoformat(),
        }

        self._save_report(result, week_label)
        return result

    def _generate_recommendation(self, pattern: str, trades: List[Dict]) -> str:
        """根据模式生成修复建议"""
        recommendations = {
            "premature_exit": "建议: 检查退出规则灵敏度, 考虑放宽 trailing_stop 触发条件",
            "wrong_direction": "建议: 加强大盘共振检查, VIX>25 时减少逆势交易",
            "position_too_large": "建议: 降低单笔最大仓位比例, 加强 ATR 动态调仓",
            "stop_loss_too_tight": "建议: 基于 ATR 动态计算止损, 当前止损可能过紧",
            "stop_loss_too_wide": "建议: 收紧止损宽度, 限制单笔最大亏损",
            "entry_timing_poor": "建议: 优化入场时机, 避免开盘前 5 分钟冲动入场",
            "overtrading": "建议: 减少日内交易频率, 提高信号质量门槛",
            "news_driven_loss": "建议: 事件驱动交易需更严格的止损和更短的持有期",
        }
        return recommendations.get(
            pattern, f'建议: 深入分析 "{pattern}" 模式的根因 ({len(trades)} 笔受影响)'
        )

    def _save_report(self, report: Dict, week_label: str) -> None:
        """保存周度报告"""
        try:
            filename = f"weekly_{week_label}.json"
            filepath = self.output_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info(f"[SystemicAnalyzer] 报告已保存: {filepath}")
        except Exception as e:
            logger.debug(f"[SystemicAnalyzer] 保存失败: {e}")

    def get_latest_report(self) -> Optional[Dict]:
        """获取最近一份报告"""
        try:
            files = sorted(self.output_dir.glob("weekly_*.json"), reverse=True)
            if files:
                with open(files[0], "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None
