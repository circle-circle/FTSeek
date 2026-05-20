#!/usr/bin/env python3
"""
因子衰减监控器 - 检测策略因子的有效性变化

功能：
1. 追踪每个因子的胜率随时间的变化
2. 检测因子是否失效（胜率显著下降）
3. 识别新出现的有效因子
4. 生成因子健康度报告

使用方法:
    python -m app.analytics.factor_decay_monitor --days 30
    python -m app.analytics.factor_decay_monitor --alert-threshold 0.45

输出:
    - 因子胜率趋势图（文本）
    - 失效因子警告
    - 新发现的有效因子
"""

import sqlite3
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger("ftseek.analytics.factor_decay")

# 配置
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")
REPORT_DIR = Path("/opt/ftseek/FTSeek/data/analysis_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class FactorHealth:
    """因子健康度"""
    factor_name: str
    recent_win_rate: float       # 最近7天胜率
    historical_win_rate: float     # 历史胜率
    win_rate_change: float        # 胜率变化
    recent_signals: int           # 最近信号数
    historical_signals: int       # 历史信号数
    status: str                   # healthy / warning / critical
    trend: str                    # improving / stable / declining


class FactorDecayMonitor:
    """因子衰减监控器"""

    def __init__(self, days: int = 30):
        self.days = days
        self.conn = self._get_db_connection()

    def _get_db_connection(self):
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def analyze_factor_trends(self) -> Dict[str, FactorHealth]:
        """
        分析所有因子的胜率趋势

        返回:
            Dict[str, FactorHealth]: 因子名 -> 健康度
        """
        # 获取所有信号数据
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT factors, pnl_pct, created_at
            FROM signal_details
            WHERE created_at >= ? AND exit_price IS NOT NULL
            ORDER BY created_at DESC
        """, (since,))

        # 按周分组
        recent_week = defaultdict(lambda: {"total": 0, "wins": 0})  # 最近7天
        historical = defaultdict(lambda: {"total": 0, "wins": 0})  # 7-30天前

        now = datetime.now()
        for row in cursor.fetchall():
            factors_json = row[0]
            pnl_pct = row[1] or 0
            created_at = datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")

            if not factors_json:
                continue

            try:
                factors = json.loads(factors_json)
            except Exception:
                continue

            # 判断因子
            for factor_name in factors.keys():
                if factor_name in ("bullish_score", "bearish_score"):
                    continue

                # 按时间分组
                days_ago = (now - created_at).days
                if days_ago <= 7:
                    stats = recent_week[factor_name]
                else:
                    stats = historical[factor_name]

                stats["total"] += 1
                if pnl_pct > 0:
                    stats["wins"] += 1

        # 计算健康度
        all_factors = set(recent_week.keys()) | set(historical.keys())
        health_results = {}

        for factor in all_factors:
            recent = recent_week.get(factor, {"total": 0, "wins": 0})
            hist = historical.get(factor, {"total": 0, "wins": 0})

            recent_total = recent["total"]
            recent_wins = recent["wins"]
            hist_total = hist["total"]
            hist_wins = hist["wins"]

            # 计算胜率
            recent_wr = recent_wins / recent_total if recent_total > 0 else 0
            hist_wr = hist_wins / hist_total if hist_total > 0 else 0
            change = recent_wr - hist_wr

            # 判断状态
            if recent_total < 5:
                status = "insufficient_data"
                trend = "unknown"
            elif recent_wr >= 0.55 and change >= -0.05:
                status = "healthy"
                trend = "improving" if change > 0.05 else "stable"
            elif recent_wr >= 0.45:
                status = "warning"
                trend = "declining" if change < -0.05 else "stable"
            else:
                status = "critical"
                trend = "declining"

            health_results[factor] = FactorHealth(
                factor_name=factor,
                recent_win_rate=recent_wr,
                historical_win_rate=hist_wr,
                win_rate_change=change,
                recent_signals=recent_total,
                historical_signals=hist_total,
                status=status,
                trend=trend
            )

        return health_results

    def detect_decay_patterns(self, health_results: Dict[str, FactorHealth]) -> Dict:
        """
        检测衰减模式

        返回:
            包含各类发现的字典
        """
        decayed = []        # 衰减因子
        emerging = []       # 新兴有效因子
        stable = []         # 稳定因子

        for factor, health in health_results.items():
            if health.status == "critical":
                decayed.append(health)
            elif health.status == "healthy" and health.recent_signals >= 10:
                if health.win_rate_change > 0.05:
                    emerging.append(health)
                else:
                    stable.append(health)
            elif health.status == "warning":
                # 警告因子
                pass

        return {
            "decayed": decayed,
            "emerging": emerging,
            "stable": stable
        }

    def generate_report(self) -> str:
        """生成因子衰减监控报告"""
        lines = []
        lines.append("=" * 80)
        lines.append(f"FTSeek 因子衰减监控报告 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        lines.append(f"分析周期: 最近 {self.days} 天")
        lines.append("=" * 80)

        health_results = self.analyze_factor_trends()

        if not health_results:
            lines.append("\n[数据不足]")
            lines.append("  没有足够的信号数据进行分析")
            return "\n".join(lines)

        # 按状态分组
        healthy_factors = [h for h in health_results.values() if h.status == "healthy"]
        warning_factors = [h for h in health_results.values() if h.status == "warning"]
        critical_factors = [h for h in health_results.values() if h.status == "critical"]
        insufficient_factors = [h for h in health_results.values() if h.status == "insufficient_data"]

        # 健康因子
        lines.append(f"\n[健康因子 ({len(healthy_factors)})]")
        if healthy_factors:
            lines.append(f"{'因子':<15} {'近期胜率':<10} {'历史胜率':<10} {'变化':<10} {'信号数':<8}")
            lines.append("-" * 60)
            for h in sorted(healthy_factors, key=lambda x: x.recent_win_rate, reverse=True):
                lines.append(f"{h.factor_name:<15} {h.recent_win_rate:<10.1%} {h.historical_win_rate:<10.1%} "
                            f"{h.win_rate_change:+.1%}   {h.recent_signals:<8}")
        else:
            lines.append("  无")

        # 警告因子
        lines.append(f"\n[警告因子 ({len(warning_factors)})]")
        if warning_factors:
            lines.append(f"{'因子':<15} {'近期胜率':<10} {'历史胜率':<10} {'变化':<10} {'信号数':<8}")
            lines.append("-" * 60)
            for h in sorted(warning_factors, key=lambda x: x.recent_win_rate):
                lines.append(f"{h.factor_name:<15} {h.recent_win_rate:<10.1%} {h.historical_win_rate:<10.1%} "
                            f"{h.win_rate_change:+.1%}   {h.recent_signals:<8}")
        else:
            lines.append("  无")

        # 严重衰减因子
        lines.append(f"\n[严重衰减因子 ({len(critical_factors)}) - 需要立即处理]")
        if critical_factors:
            lines.append(f"{'因子':<15} {'近期胜率':<10} {'历史胜率':<10} {'变化':<10} {'信号数':<8}")
            lines.append("-" * 60)
            for h in sorted(critical_factors, key=lambda x: x.recent_win_rate):
                lines.append(f"{h.factor_name:<15} {h.recent_win_rate:<10.1%} {h.historical_win_rate:<10.1%} "
                            f"{h.win_rate_change:+.1%}   {h.recent_signals:<8}")
        else:
            lines.append("  无")

        # 数据不足
        lines.append(f"\n[数据不足因子 ({len(insufficient_factors)})]")
        if insufficient_factors:
            for h in insufficient_factors:
                lines.append(f"  {h.factor_name}: 近期信号数={h.recent_signals} (需要>=5)")
        else:
            lines.append("  无")

        # 衰减模式分析
        patterns = self.detect_decay_patterns(health_results)
        lines.append(f"\n[衰减模式分析]")
        lines.append(f"  衰减因子: {len(patterns['decayed'])} 个")
        lines.append(f"  新兴有效因子: {len(patterns['emerging'])} 个")
        lines.append(f"  稳定因子: {len(patterns['stable'])} 个")

        # 建议
        lines.append(f"\n[操作建议]")
        if critical_factors:
            lines.append(f"  ⚠️  以下因子近期胜率低于45%，建议降低权重或暂时禁用:")
            for h in critical_factors:
                lines.append(f"     - {h.factor_name} (胜率{h.recent_win_rate:.1%}, 变化{h.win_rate_change:+.1%})")
        if warning_factors:
            lines.append(f"  ⚡ 以下因子胜率在45%-55%之间，建议密切关注:")
            for h in warning_factors:
                lines.append(f"     - {h.factor_name} (胜率{h.recent_win_rate:.1%})")

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"factor_decay_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"因子衰减报告已保存到: {report_file}")

        return report


def main():
    parser = argparse.ArgumentParser(description="因子衰减监控")
    parser.add_argument("--days", type=int, default=30, help="分析天数")
    parser.add_argument("--alert-threshold", type=float, default=0.45, help="胜率警告阈值")
    args = parser.parse_args()

    monitor = FactorDecayMonitor(days=args.days)
    report = monitor.generate_report()
    print(report)


if __name__ == "__main__":
    main()
