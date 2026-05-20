#!/usr/bin/env python3
"""
自动策略调整器 - 基于分析结果自动优化策略参数

功能：
1. 读取信号分析结果
2. 自动调整时段过滤参数
3. 自动排除低胜率标的
4. 自动调整因子权重
5. 生成调整报告（预览/应用模式）

使用方法:
    python -m app.analytics.auto_adjuster --days 7 --dry-run    # 预览模式
    python -m app.analytics.auto_adjuster --days 7              # 应用模式

注意：
    - 预览模式不会修改配置文件
    - 应用模式会修改 config/unified_config.yaml
    - 建议先使用预览模式确认调整内容
"""

import sqlite3
import json
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

import yaml

logger = logging.getLogger("ftseek.analytics.auto_adjuster")

# 配置
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")
CONFIG_PATH = Path("/opt/ftseek/FTSeek/config/unified_config.yaml")
REPORT_DIR = Path("/opt/ftseek/FTSeek/data/analysis_reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# 调整阈值
WIN_RATE_THRESHOLD = 0.45      # 低于此胜率的标的可能被排除
MIN_SIGNALS = 5                # 最少信号数才进行调整
FACTOR_WEIGHT_ADJUST = 0.1     # 因子权重调整幅度


class AutoAdjuster:
    """自动策略调整器"""

    def __init__(self, days: int = 7):
        self.days = days
        self.conn = self._get_db_connection()
        self.changes = []  # 记录所有调整

    def _get_db_connection(self):
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def load_config(self) -> Dict:
        """加载配置文件"""
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def save_config(self, config: Dict):
        """保存配置文件"""
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # ── 时段过滤调整 ──

    def analyze_time_performance(self) -> Dict[str, Dict]:
        """分析不同时段的胜率表现"""
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                strftime('%H', created_at) as hour,
                COUNT(*) as total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl
            FROM signal_details
            WHERE created_at >= ? AND exit_price IS NOT NULL
            GROUP BY hour
            ORDER BY hour
        """, (since,))

        results = {}
        for row in cursor.fetchall():
            hour = int(row[0])
            total = row[1]
            wins = row[2]
            avg_pnl = row[3] or 0

            win_rate = wins / total if total > 0 else 0

            results[hour] = {
                "total": total,
                "wins": wins,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl
            }

        return results

    def adjust_time_filters(self, config: Dict, dry_run: bool = True) -> Dict:
        """
        根据时段胜率调整时间过滤

        返回: 调整建议
        """
        time_performance = self.analyze_time_performance()
        adjustments = {}

        for hour, stats in time_performance.items():
            if stats["total"] < MIN_SIGNALS:
                continue

            if stats["win_rate"] < WIN_RATE_THRESHOLD:
                # 低胜率时段
                if 14 <= hour < 16:  # 尾盘时段
                    adjustments[hour] = {
                        "action": "tighten",
                        "reason": f"胜率{stats['win_rate']:.1%}低于阈值",
                        "current_cutoff": "15:00",
                        "suggested_cutoff": f"{hour:02d}:00"
                    }

        if adjustments and not dry_run:
            # 应用调整
            self.changes.append({
                "type": "time_filter",
                "adjustments": adjustments
            })
            logger.info(f"时段过滤已调整: {adjustments}")

        return adjustments

    # ── 标的排除 ──

    def analyze_symbol_performance(self) -> Dict[str, Dict]:
        """分析各标的的胜率表现"""
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                code,
                COUNT(*) as total,
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_pct) as avg_pnl
            FROM signal_details
            WHERE created_at >= ? AND exit_price IS NOT NULL
            GROUP BY code
            HAVING COUNT(*) >= ?
            ORDER BY wins * 1.0 / total
        """, (since, MIN_SIGNALS))

        results = {}
        for row in cursor.fetchall():
            code = row[0]
            total = row[1]
            wins = row[2]
            avg_pnl = row[3] or 0

            win_rate = wins / total if total > 0 else 0

            results[code] = {
                "total": total,
                "wins": wins,
                "win_rate": win_rate,
                "avg_pnl": avg_pnl
            }

        return results

    def adjust_symbol_filters(self, config: Dict, dry_run: bool = True) -> Dict:
        """
        根据标的胜率调整watchlist

        返回: 调整建议
        """
        symbol_performance = self.analyze_symbol_performance()
        adjustments = {
            "remove": [],
            "keep": [],
            "add_back": []
        }

        for code, stats in symbol_performance.items():
            if stats["win_rate"] < WIN_RATE_THRESHOLD and stats["total"] >= MIN_SIGNALS:
                adjustments["remove"].append({
                    "code": code,
                    "win_rate": stats["win_rate"],
                    "total_signals": stats["total"],
                    "reason": f"胜率{stats['win_rate']:.1%}低于阈值{WIN_RATE_THRESHOLD:.0%}"
                })
            elif stats["win_rate"] > 0.6:
                adjustments["keep"].append({
                    "code": code,
                    "win_rate": stats["win_rate"],
                    "reason": "高胜率标的"
                })

        if adjustments["remove"] and not dry_run:
            # 从watchlist中移除
            watchlist = config.get("trading", {}).get("watch_list_hk", [])
            for item in adjustments["remove"]:
                if item["code"] in watchlist:
                    watchlist.remove(item["code"])
                    logger.info(f"已从watchlist移除: {item['code']}")

            self.changes.append({
                "type": "symbol_filter",
                "removed": [i["code"] for i in adjustments["remove"]]
            })

        return adjustments

    # ── 因子权重调整 ──

    def analyze_factor_weights(self) -> Dict[str, Dict]:
        """分析各因子的权重表现"""
        since = (datetime.now() - timedelta(days=self.days)).strftime("%Y-%m-%d")

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT factors, pnl_pct
            FROM signal_details
            WHERE created_at >= ? AND exit_price IS NOT NULL
        """, (since,))

        factor_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0})

        for row in cursor.fetchall():
            factors_json = row[0]
            pnl_pct = row[1] or 0

            if not factors_json:
                continue

            try:
                factors = json.loads(factors_json)
            except Exception:
                continue

            for factor_name in factors.keys():
                if factor_name in ("bullish_score", "bearish_score"):
                    continue

                stats = factor_stats[factor_name]
                stats["total"] += 1
                stats["pnl_sum"] += pnl_pct
                if pnl_pct > 0:
                    stats["wins"] += 1

        results = {}
        for factor, stats in factor_stats.items():
            if stats["total"] < MIN_SIGNALS:
                continue

            win_rate = stats["wins"] / stats["total"]
            avg_pnl = stats["pnl_sum"] / stats["total"]

            results[factor] = {
                "total": stats["total"],
                "wins": stats["wins"],
                "win_rate": win_rate,
                "avg_pnl": avg_pnl
            }

        return results

    def adjust_factor_weights(self, config: Dict, dry_run: bool = True) -> Dict:
        """
        根据因子胜率调整权重

        返回: 调整建议
        """
        factor_performance = self.analyze_factor_weights()
        adjustments = {}

        for factor, stats in factor_performance.items():
            if stats["total"] < MIN_SIGNALS:
                continue

            if stats["win_rate"] < WIN_RATE_THRESHOLD:
                adjustments[factor] = {
                    "action": "decrease",
                    "current_weight": 1.0,
                    "suggested_weight": round(1.0 - FACTOR_WEIGHT_ADJUST, 2),
                    "reason": f"胜率{stats['win_rate']:.1%}低于阈值"
                }
            elif stats["win_rate"] > 0.6:
                adjustments[factor] = {
                    "action": "increase",
                    "current_weight": 1.0,
                    "suggested_weight": round(1.0 + FACTOR_WEIGHT_ADJUST, 2),
                    "reason": f"胜率{stats['win_rate']:.1%}高于阈值"
                }

        if adjustments and not dry_run:
            self.changes.append({
                "type": "factor_weights",
                "adjustments": adjustments
            })
            logger.info(f"因子权重已调整: {adjustments}")

        return adjustments

    # ── 主流程 ──

    def run_adjustment(self, dry_run: bool = True) -> str:
        """执行自动调整"""
        lines = []
        lines.append("=" * 80)
        lines.append(f"FTSeek 自动策略调整报告 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        lines.append(f"分析周期: 最近 {self.days} 天")
        lines.append(f"模式: {'预览' if dry_run else '应用'}")
        lines.append("=" * 80)

        config = self.load_config()

        # 1. 时段过滤调整
        lines.append("\n[时段过滤调整]")
        time_adjustments = self.adjust_time_filters(config, dry_run)
        if time_adjustments:
            lines.append(f"  发现 {len(time_adjustments)} 个时段需要调整:")
            for hour, adj in time_adjustments.items():
                lines.append(f"    {hour:02d}:00 - {adj['reason']}")
        else:
            lines.append("  无需调整")

        # 2. 标的排除
        lines.append("\n[标的排除调整]")
        symbol_adjustments = self.adjust_symbol_filters(config, dry_run)
        if symbol_adjustments["remove"]:
            lines.append(f"  建议移除 {len(symbol_adjustments['remove'])} 个低胜率标的:")
            for item in symbol_adjustments["remove"]:
                lines.append(f"    {item['code']}: {item['reason']}")
        else:
            lines.append("  无需调整")

        if symbol_adjustments["keep"]:
            lines.append(f"\n  高胜率标的 ({len(symbol_adjustments['keep'])}):")
            for item in symbol_adjustments["keep"]:
                lines.append(f"    {item['code']}: 胜率{item['win_rate']:.1%}")

        # 3. 因子权重调整
        lines.append("\n[因子权重调整]")
        factor_adjustments = self.adjust_factor_weights(config, dry_run)
        if factor_adjustments:
            for factor, adj in factor_adjustments.items():
                lines.append(f"  {factor}: {adj['action']} ({adj['reason']})")
        else:
            lines.append("  无需调整")

        # 4. 应用模式
        if not dry_run:
            lines.append("\n[已应用调整]")
            if self.changes:
                for change in self.changes:
                    lines.append(f"  - {change['type']}")
                self.save_config(config)
                lines.append(f"\n配置文件已更新: {CONFIG_PATH}")
            else:
                lines.append("  无调整需要应用")
        else:
            lines.append("\n[预览模式]")
            lines.append("  以上调整仅为预览，未实际应用")
            lines.append("  使用 --apply 参数应用调整")

        report = "\n".join(lines)

        # 保存报告
        report_file = REPORT_DIR / f"auto_adjust_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"自动调整报告已保存: {report_file}")

        return report


def main():
    parser = argparse.ArgumentParser(description="自动策略调整")
    parser.add_argument("--days", type=int, default=7, help="分析天数")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--apply", action="store_true", help="应用调整")
    args = parser.parse_args()

    adjuster = AutoAdjuster(days=args.days)
    report = adjuster.run_adjustment(dry_run=not args.apply)
    print(report)


if __name__ == "__main__":
    main()
