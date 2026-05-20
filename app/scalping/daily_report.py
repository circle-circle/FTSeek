"""
Scalping 日报生成 — 盘后汇总

生成内容:
  - 今日交易统计 (胜率、EV、最大回撤)
  - 连续盈亏状态
  - 资金使用情况
  - 最佳/最差交易
  - 建议 (基于今日表现)
"""

import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ScalpingDailyReport:
    """
    Scalping 日报生成器
    
    职责:
      1. 汇总今日交易统计
      2. 分析表现
      3. 生成建议
      4. 输出报告
    """

    def __init__(self, stats_tracker=None, budget=None, circuit_breaker=None):
        self.stats_tracker = stats_tracker
        self.budget = budget
        self.circuit_breaker = circuit_breaker

    def generate(self) -> str:
        """生成日报文本"""
        today_stats = self.stats_tracker.get_today_stats() if self.stats_tracker else {}
        total_stats = self.stats_tracker.get_stats() if self.stats_tracker else {}
        budget_state = self.budget.get_state() if self.budget else {}
        cb_state = self.circuit_breaker.get_state() if self.circuit_breaker else {}

        # 构建报告
        lines = [
            "=" * 50,
            "⚡ SCALPING 日报",
            f"日期: {datetime.now().strftime('%Y-%m-%d')}",
            "=" * 50,
            "",
            "── 今日统计 ──",
            f"交易次数: {today_stats.get('total_trades', 0)}",
            f"盈利: {today_stats.get('wins', 0)}",
            f"亏损: {today_stats.get('losses', 0)}",
            f"胜率: {today_stats.get('win_rate', 0):.1%}",
            f"总盈亏: {today_stats.get('total_pnl_pct', 0):.2%}",
            "",
            "── 累计统计 ──",
            f"总交易: {total_stats.get('total_trades', 0)}",
            f"总胜率: {total_stats.get('win_rate', 0):.1%}",
            f"期望值 (EV): {total_stats.get('expectancy', 0):.4f}",
            f"平均盈利: {total_stats.get('avg_win_pct', 0):.2%}",
            f"平均亏损: {total_stats.get('avg_loss_pct', 0):.2%}",
            f"最大回撤: {total_stats.get('max_drawdown_pct', 0):.2%}",
            "",
            "── 资金状态 ──",
            f"总预算: ${budget_state.get('total_budget', 0):,.0f}",
            f"已用: ${budget_state.get('used', 0):,.0f} ({budget_state.get('used_pct', 0):.1%})",
            f"日亏损: ${budget_state.get('daily_loss', 0):,.0f}",
            f"日亏损上限: ${budget_state.get('daily_loss_limit', 0):,.0f}",
            f"同时持仓: {budget_state.get('concurrent_positions', 0)}/{budget_state.get('max_concurrent', 0)}",
            "",
            "── 熔断状态 ──",
            f"状态: {cb_state.get('state', 'N/A')}",
            f"连续亏损: {cb_state.get('consecutive_losses', 0)}/{cb_state.get('max_consecutive_losses', 0)}",
            f"冷却剩余: {cb_state.get('remaining_minutes', 0):.0f} 分钟",
            "",
            "── 建议 ──",
        ]

        # 根据数据生成建议
        suggestions = self._generate_suggestions(today_stats, total_stats, budget_state, cb_state)
        lines.extend(f"  • {s}" for s in suggestions)

        # C5 归因 (2026-05-13): 附加跨策略归因数据
        try:
            from app.analysis.strategy_attribution import get_attribution
            attr_report = get_attribution().get_formatted_report()
            lines.append("")
            lines.append(attr_report)
        except Exception as e:
            logger.debug(f"[日报] 归因数据获取失败 (降级): {e}")

        lines.append("")
        lines.append("=" * 50)

        return "\n".join(lines)

    def _generate_suggestions(
        self,
        today_stats: Dict,
        total_stats: Dict,
        budget_state: Dict,
        cb_state: Dict,
    ) -> list:
        """根据表现生成建议"""
        suggestions = []

        # 胜率分析
        win_rate = today_stats.get("win_rate", 0)
        if win_rate > 0.60:
            suggestions.append("✅ 今日胜率优秀 (>60%)，保持当前策略")
        elif win_rate < 0.45 and today_stats.get("total_trades", 0) > 3:
            suggestions.append("⚠️ 今日胜率偏低 (<45%)，建议 review 信号质量")

        # 交易频率
        trades = today_stats.get("total_trades", 0)
        if trades > 15:
            suggestions.append("⚠️ 今日交易过多 (>15)，可能过度交易")
        elif trades == 0:
            suggestions.append("ℹ️ 今日无交易 (可能是时段过滤或趋势过滤)")

        # 期望值
        ev = total_stats.get("expectancy", 0)
        if ev > 0.003:
            suggestions.append("✅ 累计期望值为正，策略有效")
        elif ev < -0.001 and total_stats.get("total_trades", 0) > 10:
            suggestions.append("🚨 累计期望值为负，需要调整策略参数")

        # 资金
        daily_loss_pct = budget_state.get("daily_loss_pct", 0)
        if daily_loss_pct > 0.8:
            suggestions.append("⚠️ 日亏损接近上限，注意风控")

        # 熔断
        if cb_state.get("state") == "COOLDOWN":
            suggestions.append(f"🚨 熔断中，冷却剩余 {cb_state.get('remaining_minutes', 0):.0f} 分钟")

        # 总体
        if not suggestions:
            suggestions.append("ℹ️ 数据不足，继续观察")

        return suggestions

    def get_summary_dict(self) -> Dict:
        """获取摘要字典 (用于程序化消费)"""
        today_stats = self.stats_tracker.get_today_stats() if self.stats_tracker else {}
        total_stats = self.stats_tracker.get_stats() if self.stats_tracker else {}

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "today_trades": today_stats.get("total_trades", 0),
            "today_wins": today_stats.get("wins", 0),
            "today_losses": today_stats.get("losses", 0),
            "today_win_rate": today_stats.get("win_rate", 0),
            "today_pnl_pct": today_stats.get("total_pnl_pct", 0),
            "total_trades": total_stats.get("total_trades", 0),
            "total_win_rate": total_stats.get("win_rate", 0),
            "expectancy": total_stats.get("expectancy", 0),
            "max_drawdown_pct": total_stats.get("max_drawdown_pct", 0),
        }


def get_daily_report(
    stats_tracker=None, budget=None, circuit_breaker=None
) -> ScalpingDailyReport:
    """获取日报实例"""
    return ScalpingDailyReport(
        stats_tracker=stats_tracker,
        budget=budget,
        circuit_breaker=circuit_breaker,
    )
