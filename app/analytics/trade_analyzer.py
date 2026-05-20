#!/usr/bin/env python3
"""
真实交易数据分析器 - 基于现有 trades 表数据

分析您当前模拟环境中的真实交易数据，
生成优化建议，直接用于调整策略。

使用方法:
    python -m app.analytics.trade_analyzer --days 30
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger("ftseek.analytics.trade_analyzer")

# 配置
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")
REPORT_DIR = Path("/opt/ftseek/FTSeek/data/analysis_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


class TradeAnalyzer:
    """真实交易数据分析器"""

    def __init__(self, days: int = 30):
        self.days = days
        self.conn = sqlite3.connect(str(DB_PATH), timeout=30)
        self.conn.row_factory = sqlite3.Row

    def get_trades(self) -> List[Dict]:
        """获取指定时间范围内的交易数据"""
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                t.code,
                t.action,
                t.entry_price,
                t.exit_price,
                t.pnl,
                t.pnl_percent,
                t.entry_time,
                t.exit_time,
                t.exit_reason,
                t.strategy,
                s.confidence,
                s.reason
            FROM trades t
            LEFT JOIN signals s ON t.signal_id = s.id
            WHERE t.pnl IS NOT NULL
              AND t.entry_time >= ?
            ORDER BY t.entry_time DESC
        """, (since,))

        trades = []
        for row in cursor.fetchall():
            trades.append({
                "code": row[0],
                "action": row[1],
                "entry_price": row[2],
                "exit_price": row[3],
                "pnl": row[4],
                "pnl_percent": row[5] or 0,
                "entry_time": row[6],
                "exit_time": row[7],
                "exit_reason": row[8],
                "strategy": row[9],
                "confidence": row[10] or 0,
                "reason": row[11]
            })

        return trades

    def analyze_symbol_performance(self, trades: List[Dict]) -> List[Dict]:
        """分析各标的的胜率"""
        stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for trade in trades:
            code = trade["code"]
            stats[code]["total"] += 1
            stats[code]["pnl_sum"] += trade["pnl_percent"]
            if trade["pnl_percent"] > 0:
                stats[code]["wins"] += 1

        results = []
        for code, s in stats.items():
            if s["total"] < 1:
                continue
            results.append({
                "code": code,
                "total": s["total"],
                "wins": s["wins"],
                "win_rate": s["wins"] / s["total"],
                "avg_pnl": s["pnl_sum"] / s["total"]
            })

        return sorted(results, key=lambda x: x["win_rate"], reverse=True)

    def analyze_time_performance(self, trades: List[Dict]) -> List[Dict]:
        """分析不同时段的胜率"""
        stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for trade in trades:
            try:
                hour = int(trade["entry_time"].split()[1].split(":")[0])
            except Exception:
                continue

            if 9 <= hour < 10:
                bucket = "09:30-10:00"
            elif 10 <= hour < 11:
                bucket = "10:00-11:00"
            elif 11 <= hour < 12:
                bucket = "11:00-12:00"
            elif 13 <= hour < 14:
                bucket = "13:00-14:00"
            elif 14 <= hour < 15:
                bucket = "14:00-15:00"
            else:
                bucket = "15:00-16:00"

            stats[bucket]["total"] += 1
            stats[bucket]["pnl_sum"] += trade["pnl_percent"]
            if trade["pnl_percent"] > 0:
                stats[bucket]["wins"] += 1

        results = []
        for bucket, s in stats.items():
            results.append({
                "bucket": bucket,
                "total": s["total"],
                "wins": s["wins"],
                "win_rate": s["wins"] / s["total"] if s["total"] > 0 else 0,
                "avg_pnl": s["pnl_sum"] / s["total"] if s["total"] > 0 else 0
            })

        return sorted(results, key=lambda x: ["09:30-10:00", "10:00-11:00", "11:00-12:00",
                                               "13:00-14:00", "14:00-15:00", "15:00-16:00"].index(x["bucket"]))

    def analyze_exit_reasons(self, trades: List[Dict]) -> List[Dict]:
        """分析退出原因"""
        stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for trade in trades:
            reason = trade["exit_reason"] or "unknown"
            stats[reason]["total"] += 1
            stats[reason]["pnl_sum"] += trade["pnl_percent"]
            if trade["pnl_percent"] > 0:
                stats[reason]["wins"] += 1

        results = []
        for reason, s in stats.items():
            results.append({
                "reason": reason,
                "total": s["total"],
                "wins": s["wins"],
                "win_rate": s["wins"] / s["total"] if s["total"] > 0 else 0,
                "avg_pnl": s["pnl_sum"] / s["total"] if s["total"] > 0 else 0
            })

        return sorted(results, key=lambda x: x["win_rate"], reverse=True)

    def analyze_holding_period(self, trades: List[Dict]) -> List[Dict]:
        """分析持仓周期与胜率的关系"""
        stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for trade in trades:
            try:
                entry = datetime.strptime(trade["entry_time"], "%Y-%m-%d %H:%M:%S")
                exit_time = trade["exit_time"]
                if not exit_time:
                    continue
                # 处理带T的ISO格式
                if "T" in str(exit_time):
                    exit_time = str(exit_time).replace("T", " ").split(".")[0]
                exit_dt = datetime.strptime(str(exit_time), "%Y-%m-%d %H:%M:%S")
                hold_hours = (exit_dt - entry).total_seconds() / 3600

                if hold_hours < 1:
                    bucket = "< 1h"
                elif hold_hours < 4:
                    bucket = "1-4h"
                elif hold_hours < 24:
                    bucket = "4-24h"
                elif hold_hours < 48:
                    bucket = "1-2d"
                else:
                    bucket = "> 2d"

                stats[bucket]["total"] += 1
                stats[bucket]["pnl_sum"] += trade["pnl_percent"]
                if trade["pnl_percent"] > 0:
                    stats[bucket]["wins"] += 1
            except Exception as e:
                continue

        results = []
        for bucket, s in stats.items():
            results.append({
                "bucket": bucket,
                "total": s["total"],
                "wins": s["wins"],
                "win_rate": s["wins"] / s["total"] if s["total"] > 0 else 0,
                "avg_pnl": s["pnl_sum"] / s["total"] if s["total"] > 0 else 0
            })

        return sorted(results, key=lambda x: ["< 1h", "1-4h", "4-24h", "1-2d", "> 2d"].index(x["bucket"]) if x["bucket"] in ["< 1h", "1-4h", "4-24h", "1-2d", "> 2d"] else 99)

    def generate_report(self) -> str:
        """生成分析报告"""
        trades = self.get_trades()
        total = len(trades)
        wins = sum(1 for t in trades if t["pnl_percent"] > 0)
        win_rate = wins / total if total > 0 else 0
        total_pnl = sum(t["pnl_percent"] for t in trades)
        avg_pnl = total_pnl / total if total > 0 else 0

        lines = []
        lines.append("=" * 80)
        lines.append(f"FTSeek 真实交易数据分析报告 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        lines.append(f"分析周期: 最近 {self.days} 天")
        lines.append("=" * 80)

        # 整体统计
        lines.append(f"\n[整体表现]")
        lines.append(f"  总交易数: {total}")
        lines.append(f"  盈利: {wins} | 亏损: {total - wins}")
        lines.append(f"  胜率: {win_rate:.1%}")
        lines.append(f"  总盈亏: {total_pnl:.2%}")
        lines.append(f"  平均盈亏: {avg_pnl:.2%}")

        # 标的分析
        lines.append(f"\n[标的胜率排名]")
        symbol_results = self.analyze_symbol_performance(trades)
        if symbol_results:
            lines.append(f"{'标的':<15} {'交易数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for r in symbol_results:
                lines.append(f"{r['code']:<15} {r['total']:<8} {r['win_rate']:<8.1%} {r['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足")

        # 时段分析
        lines.append(f"\n[时段胜率分析]")
        time_results = self.analyze_time_performance(trades)
        if time_results:
            lines.append(f"{'时段':<15} {'交易数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for r in time_results:
                lines.append(f"{r['bucket']:<15} {r['total']:<8} {r['win_rate']:<8.1%} {r['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足")

        # 退出原因分析
        lines.append(f"\n[退出原因分析]")
        exit_results = self.analyze_exit_reasons(trades)
        if exit_results:
            lines.append(f"{'原因':<20} {'交易数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 50)
            for r in exit_results:
                lines.append(f"{r['reason']:<20} {r['total']:<8} {r['win_rate']:<8.1%} {r['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足")

        # 持仓周期分析
        lines.append(f"\n[持仓周期分析]")
        hold_results = self.analyze_holding_period(trades)
        if hold_results:
            lines.append(f"{'周期':<15} {'交易数':<8} {'胜率':<8} {'平均盈亏':<10}")
            lines.append("-" * 45)
            for r in hold_results:
                lines.append(f"{r['bucket']:<15} {r['total']:<8} {r['win_rate']:<8.1%} {r['avg_pnl']:<10.2%}")
        else:
            lines.append("  数据不足")

        # 建议
        lines.append(f"\n[优化建议]")
        recommendations = self._generate_recommendations(symbol_results, time_results, exit_results)
        for rec in recommendations:
            lines.append(f"  {rec}")

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"trade_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"分析报告已保存: {report_file}")

        return report

    def _generate_recommendations(self, symbol_results, time_results, exit_results) -> List[str]:
        """生成优化建议"""
        recommendations = []

        # 标的建议
        if symbol_results:
            good_symbols = [s for s in symbol_results if s["win_rate"] == 1.0 and s["total"] >= 1]
            bad_symbols = [s for s in symbol_results if s["win_rate"] == 0 and s["total"] >= 2]

            if good_symbols:
                recommendations.append(f"✅ 高胜率标的: {', '.join(s['code'] for s in good_symbols)}")
            if bad_symbols:
                recommendations.append(f"⚠️  低胜率标的(考虑排除): {', '.join(s['code'] for s in bad_symbols)}")

        # 时段建议
        if time_results:
            good_times = [t for t in time_results if t["win_rate"] > 0.5 and t["total"] >= 2]
            bad_times = [t for t in time_results if t["win_rate"] < 0.5 and t["total"] >= 3]

            if good_times:
                recommendations.append(f"✅ 最佳时段: {', '.join(t['bucket'] for t in good_times)}")
            if bad_times:
                recommendations.append(f"⚠️  避免时段: {', '.join(t['bucket'] for t in bad_times)}")

        # 退出原因建议
        if exit_results:
            bad_exits = [e for e in exit_results if e["win_rate"] < 0.4 and e["total"] >= 3]
            if bad_exits:
                recommendations.append(f"⚠️  低效退出原因: {', '.join(e['reason'] for e in bad_exits)}")

        if not recommendations:
            recommendations.append("数据积累中，建议继续运行模拟交易...")

        return recommendations


def main():
    import argparse
    parser = argparse.ArgumentParser(description="真实交易数据分析器")
    parser.add_argument("--days", type=int, default=30, help="分析天数")
    args = parser.parse_args()

    analyzer = TradeAnalyzer(days=args.days)
    report = analyzer.generate_report()
    print(report)


if __name__ == "__main__":
    main()
