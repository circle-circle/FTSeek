"""
Playbook Weekly Report — 周度因子分析报告 (spec 6.4)

功能:
  1. 汇总本周因子统计
  2. 识别正/负收益因子
  3. 生成结构化报告 (可选 LLM 增强)
  4. 推送到飞书

Usage:
  from app.playbook.weekly_report import WeeklyReportGenerator

  gen = WeeklyReportGenerator(playbook_db, factor_analyzer)
  report = gen.generate("2026-06-18")
  gen.push_report(report)
"""
import logging
from typing import Dict, Any, Optional
from datetime import date, timedelta

from app.utils.timezone import _et_trade_date, now_et

from app.playbook.db import PlaybookDB
from app.playbook.factor_analyzer import FactorAnalyzer
from app.playbook.weight_adjuster import WeightAdjuster
from app.playbook.feishu_push import PlaybookPusher

logger = logging.getLogger(__name__)


class WeeklyReportGenerator:
    """周度因子分析报告生成器"""

    def __init__(
        self,
        playbook_db: PlaybookDB = None,
        factor_analyzer: FactorAnalyzer = None,
        weight_adjuster: WeightAdjuster = None,
        feishu_pusher: PlaybookPusher = None,
    ):
        self.db = playbook_db or PlaybookDB()
        self.analyzer = factor_analyzer or FactorAnalyzer(self.db)
        self.adjuster = weight_adjuster or WeightAdjuster()
        self.pusher = feishu_pusher or PlaybookPusher(self.db)

    def generate(self, trade_date: str = None) -> Dict[str, Any]:
        """
        生成周度报告

        Args:
            trade_date: 报告日期 (默认今天)

        Returns:
            结构化报告 dict
        """
        if trade_date is None:
            trade_date = _et_trade_date(now_et())

        # 1. 更新因子统计
        update_result = self.analyzer.update_all_stats(days_back=90)

        # 2. 获取因子报告
        factor_report = self.analyzer.generate_factor_report()

        # 3. 本周交易汇总
        week_stats = self._get_week_stats(trade_date)

        # 4. 自动权重调整
        # 2026-07-31 议题⑤ F1 (DEB-cleanup-remaining, 用户裁"整体执行"): 停用 —
        #   本调用写 playbook_screening.yaml 基础权重, 与 screener 运行时 factor_ic
        #   日更加成对同一 IC 信号双重计权。07-04 已识别此病并撤销 pipeline 侧接线
        #   (pipeline.py L2293), 本处是漏掉的另一条腿, 补全历史裁决。回滚: 恢复 adjust 调用。
        adjustments = []

        # 5. 用户跳过分析
        skip_analysis = self._analyze_user_skips(trade_date)

        report = {
            "date": trade_date,
            "week_stats": week_stats,
            "factor_report": factor_report,
            "adjustments": adjustments,
            "skip_analysis": skip_analysis,
            "update_result": update_result,
        }

        return report

    def format_report(self, report: Dict[str, Any]) -> str:
        """格式化周报为文本"""
        def _num(d, k, default=0):
            # .get(k, default) 在 key 存在但值为 None 时返回 None → {None:.0%} 报错.
            # 因子统计字段在样本不足时可能为 DB NULL, 统一兜底.
            v = d.get(k, default) if isinstance(d, dict) else default
            return v if isinstance(v, (int, float)) else default

        week = report.get("week_stats", {}) or {}
        factors = report.get("factor_report", {}) or {}
        adjustments = report.get("adjustments", []) or []
        skip_analysis = report.get("skip_analysis", {}) or {}

        date_str = report.get("date", "")
        _unified = week.get("unified", {}) if isinstance(week, dict) else {}
        _legacy = week.get("legacy", {}) if isinstance(week, dict) else {}
        lines = [
            f"📊 周度因子复盘 ({date_str})",
            "━" * 20,
            "",
            f"本周已平仓: {_num(week, 'total', 0)} 笔 | "
            f"胜率 {_num(week, 'win_rate', 0):.0f}% | "
            f"净盈亏 {'+' if _num(week, 'net_pnl', 0) >= 0 else ''}${_num(week, 'net_pnl', 0):.0f} | "
            f"PF {_num(week, 'profit_factor', 0):.2f}",
        ]
        # playbook 统一管线 (当前系统真实表现, 旧管线 7/16 起已禁用)
        if _num(_unified, "total", 0) > 0:
            lines.append(
                f"  └ 统一管线: {_num(_unified, 'total', 0)}笔 | "
                f"胜率 {_num(_unified, 'win_rate', 0):.0f}% | "
                f"净 {'+' if _num(_unified, 'net_pnl', 0) >= 0 else ''}${_num(_unified, 'net_pnl', 0):.0f} | "
                f"PF {_num(_unified, 'profit_factor', 0):.2f}"
            )
        # 旧管线残留 (已禁用, 仅历史残留; 不代表当前系统)
        if _num(_legacy, "total", 0) > 0:
            lines.append(
                f"  └ 旧管线残留(已禁用): {_num(_legacy, 'total', 0)}笔 | "
                f"净 {'+' if _num(_legacy, 'net_pnl', 0) >= 0 else ''}${_num(_legacy, 'net_pnl', 0):.0f} "
                f"— 不计入当前系统评价"
            )
        lines.append("")

        # 正收益单因子
        pos_singles = factors.get("positive_singles", [])[:5]
        if pos_singles:
            lines.append("🟢 正收益单因子 (强化):")
            for f in pos_singles:
                wr = _num(f, "bayesian_win_rate")
                lower = _num(f, "bayesian_lower")
                upper = _num(f, "bayesian_upper")
                avg = _num(f, "avg_pnl")
                total = _num(f, "total_trades")
                lines.append(f"   {f.get('factor_key', '?')}: {total}笔, "
                             f"贝叶斯胜率{wr:.0%} [{lower:.0%}-{upper:.0%}], "
                             f"avg {'+' if avg >= 0 else ''}${avg:.0f}")
            lines.append("")

        # 正收益条件对
        pos_pairs = factors.get("positive_pairs", [])[:5]
        if pos_pairs:
            lines.append("🟢 正收益条件对:")
            for f in pos_pairs:
                wr = _num(f, "bayesian_win_rate")
                avg = _num(f, "avg_pnl")
                total = _num(f, "total_trades")
                lines.append(f"   {f.get('factor_key', '?')}: {total}笔, 胜率{wr:.0%}, "
                             f"avg {'+' if avg >= 0 else ''}${avg:.0f}")
            lines.append("")

        # 负收益因子
        neg_singles = factors.get("negative_singles", [])[:5]
        if neg_singles:
            lines.append("🔴 负收益单因子 (弱化):")
            for f in neg_singles:
                wr = _num(f, "bayesian_win_rate")
                avg = _num(f, "avg_pnl")
                lines.append(f"   {f.get('factor_key', '?')}: 胜率{wr:.0%}, "
                             f"avg {'+' if avg >= 0 else ''}${avg:.0f}")
            lines.append("")

        # 观察中
        observing = factors.get("observing_singles", [])[:3]
        if observing:
            lines.append("⚠️ 观察中 (样本不足):")
            for f in observing:
                lower = _num(f, "bayesian_lower")
                upper = _num(f, "bayesian_upper")
                lines.append(f"   {f.get('factor_key', '?')}: {_num(f, 'total_trades')}笔, "
                             f"贝叶斯CI [{lower:.0%}-{upper:.0%}]")
            lines.append("")

        # 权重调整
        if adjustments:
            lines.append("📝 权重自动调整:")
            for adj in adjustments:
                lines.append(f"   {adj['factor']}: {adj['old_weight']} → {adj['new_weight']} "
                             f"({'+' if adj['delta'] > 0 else ''}{adj['delta']})")
            lines.append("")

        # 用户跳过分析
        if skip_analysis.get("total_skips", 0) > 0:
            lines.append("👤 用户跳过分析:")
            lines.append(f"   本周跳过 {skip_analysis['total_skips']} 条, "
                         f"其中 {skip_analysis.get('profitable_skips', 0)} 条 shadow_pnl 为正")
            if skip_analysis.get("profitable_skips", 0) > 0:
                lines.append("   → 建议: 可考虑更积极地执行被跳过的信号")
            lines.append("")

        return "\n".join(lines)

    def push_report(self, report: Dict[str, Any]) -> bool:
        """推送周报到飞书"""
        text = self.format_report(report)
        return self.pusher._send(text)

    # 旧管线退出标签 (2026-07-16 commit 9302339a 起 ExitDispatcher/dynamic_exit 全禁).
    #   这些 exit_reason 只会出现在禁用前的残留平仓中, 不再由 playbook 统一管线产生.
    #   周报按此分列, 避免旧管线亏损污染对当前系统的评价.
    _LEGACY_EXIT_REASONS = frozenset({
        "TRAILING_STOP", "TRAILING_STOP_BY_MAIN", "SL_FIXED_PCT",
    })

    def _get_week_stats(self, trade_date: str) -> Dict[str, Any]:
        """本周交易汇总 — 分列'旧管线残留' vs 'playbook 统一管线'."""
        try:
            dt = date.fromisoformat(trade_date)
        except ValueError:
            dt = date.today()

        # 本周一
        monday = dt - timedelta(days=dt.weekday())

        unified_rows = []
        legacy_rows = []
        for i in range(7):
            day = (monday + timedelta(days=i)).isoformat()
            instructions = self.db.get_instructions_by_date(day)
            for inst in instructions:
                if inst.get("executed") and inst.get("outcome") in ("win", "loss"):
                    _reason = (inst.get("exit_reason") or "").upper()
                    _row = (inst.get("outcome"), inst.get("pnl", 0) or 0)
                    if _reason in self._LEGACY_EXIT_REASONS:
                        legacy_rows.append(_row)
                    else:
                        unified_rows.append(_row)

        def _agg(rows):
            n = len(rows)
            wins = sum(1 for o, _ in rows if o == "win")
            net = sum(p for _, p in rows)
            gross_profit = sum(p for _, p in rows if p > 0)
            gross_loss = abs(sum(p for _, p in rows if p < 0))
            pf = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
            return {
                "total": n,
                "wins": wins,
                "win_rate": round((wins / n * 100) if n > 0 else 0, 1),
                "net_pnl": round(net, 2),
                "profit_factor": round(pf, 2),
            }

        all_rows = unified_rows + legacy_rows
        combined = _agg(all_rows)
        # 顶层键保持向后兼容 (total/wins/win_rate/net_pnl 指全部已平仓单)
        combined["profit_factor"] = combined.get("profit_factor", 0.0)
        combined["unified"] = _agg(unified_rows)
        combined["legacy"] = _agg(legacy_rows)
        return combined

    def _analyze_user_skips(self, trade_date: str) -> Dict[str, Any]:
        """分析用户跳过的指令"""
        try:
            dt = date.fromisoformat(trade_date)
        except ValueError:
            dt = date.today()

        monday = dt - timedelta(days=dt.weekday())
        total_skips = 0
        profitable_skips = 0

        for i in range(7):
            day = (monday + timedelta(days=i)).isoformat()
            instructions = self.db.get_instructions_by_date(day)
            for inst in instructions:
                if inst.get("direction") == "SKIP" or inst.get("outcome") == "skipped":
                    total_skips += 1
                    shadow = inst.get("shadow_pnl", 0) or 0
                    if shadow > 0:
                        profitable_skips += 1

        return {
            "total_skips": total_skips,
            "profitable_skips": profitable_skips,
        }
