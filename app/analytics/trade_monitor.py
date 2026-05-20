#!/usr/bin/env python3
"""
交易质量监控器 - 每日自动运行

功能：
1. 每日收盘后自动记录交易质量指标
2. 检测异常交易（幽灵交易、连续亏损等）
3. 记录胜率、盈亏比趋势
4. 发送异常警报

使用方法:
    python -m app.analytics.trade_monitor --today        # 分析今日交易
    python -m app.analytics.trade_monitor --week        # 分析本周
    python -m app.analytics.trade_monitor --alert       # 检测异常并发送警报
"""

import sqlite3
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from collections import defaultdict

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.analytics.trade_monitor")

# 路径配置
PROJECT_ROOT = Path("/opt/ftseek/FTSeek")
DB_PATH = PROJECT_ROOT / "data/ftseek.db"
REPORT_DIR = PROJECT_ROOT / "data/analysis_reports"
MONITOR_LOG = PROJECT_ROOT / "data/trade_monitor.log"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
#  数据模型
# ============================================================

class TradeMonitor:
    """交易质量监控器"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH), timeout=30)
        self.conn.row_factory = sqlite3.Row

    def get_today_trades(self) -> List[Dict]:
        """获取今日交易"""
        today = datetime.now().strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trades
            WHERE DATE(entry_time) = ? AND pnl IS NOT NULL
            ORDER BY entry_time DESC
        """, (today,))
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_trades(self, days: int = 7) -> List[Dict]:
        """获取最近N天的交易"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trades
            WHERE entry_time >= ? AND pnl IS NOT NULL
            ORDER BY entry_time DESC
        """, (since,))
        return [dict(row) for row in cursor.fetchall()]

    def detect_ghost_trades(self, trades: List[Dict]) -> List[Dict]:
        """
        检测幽灵交易（同一秒买卖）

        Returns:
            幽灵交易列表
        """
        ghost_trades = []
        for trade in trades:
            entry_time = trade.get("entry_time", "")
            exit_time = trade.get("exit_time", "")

            if entry_time and exit_time:
                # 比较时间部分（忽略日期）
                try:
                    entry_dt = datetime.strptime(str(entry_time), "%Y-%m-%d %H:%M:%S")
                    exit_dt = datetime.strptime(str(exit_time).split(".")[0].replace("T", " "), "%Y-%m-%d %H:%M:%S")

                    # 如果差值小于60秒，认为是幽灵交易
                    if abs((exit_dt - entry_dt).total_seconds()) < 60:
                        ghost_trades.append(trade)
                except Exception:
                    pass

        return ghost_trades

    def detect_consecutive_losses(self, trades: List[Dict], threshold: int = 3) -> List[Dict]:
        """
        检测连续亏损

        Args:
            trades: 交易列表
            threshold: 连续亏损阈值

        Returns:
            连续亏损记录
        """
        if not trades:
            return []

        # 按时间排序
        sorted_trades = sorted(trades, key=lambda x: x.get("entry_time", ""))

        consecutive_losses = []
        current_streak = []

        for trade in sorted_trades:
            pnl = trade.get("pnl_percent") or trade.get("pnl", 0)
            if pnl is not None and pnl <= 0:
                current_streak.append(trade)
            else:
                if len(current_streak) >= threshold:
                    consecutive_losses.extend(current_streak)
                current_streak = []

        # 检查最后的 streak
        if len(current_streak) >= threshold:
            consecutive_losses.extend(current_streak)

        return consecutive_losses

    def calculate_daily_metrics(self, trades: List[Dict]) -> Dict:
        """
        计算每日指标

        Returns:
            指标字典
        """
        if not trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
                "max_win": 0,
                "max_loss": 0,
                "ghost_trades": 0
            }

        total = len(trades)
        # 使用 pnl (实际金额) 判断盈亏
        wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        losses = total - wins

        # 计算盈亏百分比（基于entry_price和exit_price）
        pnls = []
        for t in trades:
            entry_price = t.get("entry_price", 0)
            exit_price = t.get("exit_price", 0)
            if entry_price and entry_price > 0 and exit_price:
                # 计算百分比变化
                if t.get("action") == "SELL":
                    # 做空：entry_price 是卖出价，exit_price 是买入价
                    pnl_pct = (exit_price - entry_price) / entry_price
                else:
                    # 做多
                    pnl_pct = (exit_price - entry_price) / entry_price
                pnls.append(pnl_pct)
            else:
                pnls.append(0)

        total_pnl = sum(pnls)
        avg_pnl = total_pnl / total if total > 0 else 0

        ghost_trades = self.detect_ghost_trades(trades)

        return {
            "total_trades": total,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": wins / total if total > 0 else 0,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "max_win": max(pnls) if pnls else 0,
            "max_loss": min(pnls) if pnls else 0,
            "ghost_trades": len(ghost_trades)
        }

    def save_daily_metrics(self, metrics: Dict):
        """保存每日指标到数据库"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_monitor_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL UNIQUE,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    avg_pnl REAL DEFAULT 0,
                    max_win REAL DEFAULT 0,
                    max_loss REAL DEFAULT 0,
                    ghost_trades INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("""
                INSERT OR REPLACE INTO trade_monitor_daily
                (date, total_trades, winning_trades, losing_trades, win_rate,
                 total_pnl, avg_pnl, max_win, max_loss, ghost_trades)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                today, metrics["total_trades"], metrics["winning_trades"],
                metrics["losing_trades"], metrics["win_rate"], metrics["total_pnl"],
                metrics["avg_pnl"], metrics["max_win"], metrics["max_loss"],
                metrics["ghost_trades"]
            ))

            self.conn.commit()
            logger.info(f"每日指标已保存: {today}")

        except Exception as e:
            logger.error(f"保存每日指标失败: {e}")

    def get_metrics_trend(self, days: int = 14) -> List[Dict]:
        """
        获取指标趋势

        Returns:
            趋势数据列表
        """
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM trade_monitor_daily
            WHERE date >= ?
            ORDER BY date DESC
        """, (since,))

        return [dict(row) for row in cursor.fetchall()]

    def generate_daily_report(self) -> str:
        """生成每日监控报告"""
        trades = self.get_today_trades()
        metrics = self.calculate_daily_metrics(trades)

        lines = []
        lines.append("=" * 60)
        lines.append(f"FTSeek 交易质量监控报告 ({datetime.now().strftime('%Y-%m-%d')})")
        lines.append("=" * 60)

        lines.append(f"\n[今日交易]")
        lines.append(f"  总交易数: {metrics['total_trades']}")
        lines.append(f"  盈利: {metrics['winning_trades']} | 亏损: {metrics['losing_trades']}")
        lines.append(f"  胜率: {metrics['win_rate']:.1%}")
        lines.append(f"  总盈亏: {metrics['total_pnl']:+.2%}")
        lines.append(f"  平均盈亏: {metrics['avg_pnl']:+.2%}")

        if metrics['ghost_trades'] > 0:
            lines.append(f"\n⚠️  幽灵交易: {metrics['ghost_trades']} 笔（需关注）")

        # 异常检测
        alerts = []
        if metrics['total_trades'] > 0 and metrics['win_rate'] < 0.3:
            alerts.append("胜率低于30%，策略可能失效")
        if metrics['ghost_trades'] > 0:
            alerts.append("存在幽灵交易，止损设置可能过近")
        if metrics['total_pnl'] < -0.05:
            alerts.append("日亏损超过5%，建议暂停交易")

        if alerts:
            lines.append(f"\n[异常警报]")
            for alert in alerts:
                lines.append(f"  🚨 {alert}")

        # 保存指标
        self.save_daily_metrics(metrics)

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"daily_monitor_{datetime.now().strftime('%Y%m%d')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)

        return report

    def generate_weekly_report(self) -> str:
        """生成每周综合分析报告"""
        trades = self.get_recent_trades(days=7)
        metrics = self.calculate_daily_metrics(trades)

        # 获取趋势数据
        trend = self.get_metrics_trend(days=14)

        lines = []
        lines.append("=" * 60)
        lines.append(f"FTSeek 每周综合分析报告 ({datetime.now().strftime('%Y-%m-%d')})")
        lines.append("=" * 60)

        lines.append(f"\n[本周表现]")
        lines.append(f"  总交易数: {metrics['total_trades']}")
        lines.append(f"  胜率: {metrics['win_rate']:.1%}")
        lines.append(f"  总盈亏: {metrics['total_pnl']:+.2%}")
        lines.append(f"  平均盈亏: {metrics['avg_pnl']:+.2%}")

        # 趋势分析
        if len(trend) >= 7:
            recent_win_rates = [d['win_rate'] for d in trend[:7]]
            avg_wr = sum(recent_win_rates) / len(recent_win_rates)

            lines.append(f"\n[趋势分析]")
            lines.append(f"  近7日平均胜率: {avg_wr:.1%}")

            # 胜率趋势
            if len(recent_win_rates) >= 3:
                if recent_win_rates[0] > recent_win_rates[-1]:
                    lines.append(f"  胜率趋势: 下降 ⚠️")
                else:
                    lines.append(f"  胜率趋势: 上升 ✅")

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"weekly_report_{datetime.now().strftime('%Y%m%d')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)

        return report


def main():
    parser = argparse.ArgumentParser(description="交易质量监控")
    parser.add_argument("--today", action="store_true", help="分析今日交易")
    parser.add_argument("--week", action="store_true", help="分析本周")
    parser.add_argument("--alert", action="store_true", help="检测异常")
    args = parser.parse_args()

    monitor = TradeMonitor()

    if args.today:
        report = monitor.generate_daily_report()
        print(report)
    elif args.week:
        report = monitor.generate_weekly_report()
        print(report)
    elif args.alert:
        trades = monitor.get_today_trades()
        ghost_trades = monitor.detect_ghost_trades(trades)
        if ghost_trades:
            print(f"⚠️  发现 {len(ghost_trades)} 笔幽灵交易")
            for t in ghost_trades:
                print(f"  - {t['code']}: {t['entry_time']} -> {t['exit_time']}")
        else:
            print("✅ 未发现幽灵交易")
    else:
        # 默认：运行今日分析
        report = monitor.generate_daily_report()
        print(report)


if __name__ == "__main__":
    main()
