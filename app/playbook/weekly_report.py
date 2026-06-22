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
            trade_date = date.today().isoformat()

        # 1. 更新因子统计
        update_result = self.analyzer.update_all_stats(days_back=90)

        # 2. 获取因子报告
        factor_report = self.analyzer.generate_factor_report()

        # 3. 本周交易汇总
        week_stats = self._get_week_stats(trade_date)

        # 4. 自动权重调整
        adjustments = self.adjuster.adjust(factor_report)

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
        lines = [
            f"📊 周度因子复盘 ({date_str})",
            "━" * 20,
            "",
            f"本周交易: {_num(week, 'total', 0)} 笔 | "
            f"胜率 {_num(week, 'win_rate', 0):.0f}% | "
            f"净盈亏 {'+' if _num(week, 'net_pnl', 0) >= 0 else ''}${_num(week, 'net_pnl', 0):.0f}",
            "",
        ]

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

    def _get_week_stats(self, trade_date: str) -> Dict[str, Any]:
        """本周交易汇总"""
        try:
            dt = date.fromisoformat(trade_date)
        except ValueError:
            dt = date.today()

        # 本周一
        monday = dt - timedelta(days=dt.weekday())
        total = 0
        wins = 0
        net_pnl = 0.0

        for i in range(7):
            day = (monday + timedelta(days=i)).isoformat()
            instructions = self.db.get_instructions_by_date(day)
            for inst in instructions:
                if inst.get("executed") and inst.get("outcome") in ("win", "loss"):
                    total += 1
                    if inst.get("outcome") == "win":
                        wins += 1
                    net_pnl += inst.get("pnl", 0) or 0

        win_rate = (wins / total * 100) if total > 0 else 0
        return {
            "total": total,
            "wins": wins,
            "win_rate": round(win_rate, 1),
            "net_pnl": round(net_pnl, 2),
        }

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
