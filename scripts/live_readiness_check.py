#!/usr/bin/env python3
"""
实盘就绪检查 - 7 项准入条件必须全部满足才允许切换到实盘

使用:
    python scripts/live_readiness_check.py

返回值:
    0  全部条件通过
    1  有任意条件未通过
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))


class LiveReadinessChecker:
    """实盘就绪检查器 — 基于模拟盘交易记录的 7 项准入条件"""

    # 7 项准入条件阈值
    CRITERIA: Dict[str, Any] = {
        "min_running_days": 14,          # 至少运行 14 天
        "min_trade_count": 50,           # 至少 50 笔交易
        "min_win_rate": 0.45,            # 胜率 >= 45%
        "min_profit_factor": 1.2,        # 盈亏比 >= 1.2
        "max_drawdown_pct": 15.0,        # 最大回撤 <= 15%
        "min_sharpe_ratio": 0.8,         # Sharpe >= 0.8
        "max_consecutive_losses": 8,     # 连续亏损 <= 8 笔
    }

    # 实际数据库使用 mode 列，模拟盘对应 'sandbox' 或 'simulate'
    SIMULATE_MODE_FILTER = "mode IN ('sandbox', 'simulate')"

    INITIAL_CAPITAL = 1_000_000  # $1M 模拟资金（用于回撤百分比换算）

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "data" / "ftseek_us.db"
        self.db_path = str(db_path)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def check_all(self) -> Dict[str, Any]:
        """运行所有检查，返回结果"""
        results: Dict[str, Any] = {}
        if not Path(self.db_path).exists():
            for key in (
                "running_days",
                "trade_count",
                "win_rate",
                "profit_factor",
                "max_drawdown",
                "sharpe_ratio",
                "consecutive_losses",
            ):
                results[key] = {
                    "passed": False,
                    "value": 0,
                    "threshold": list(self.CRITERIA.values())[0],
                    "reason": f"数据库不存在: {self.db_path}",
                }
            results["all_passed"] = False
            return results

        conn = sqlite3.connect(self.db_path)
        try:
            results["running_days"] = self._check_running_days(conn)
            results["trade_count"] = self._check_trade_count(conn)
            results["win_rate"] = self._check_win_rate(conn)
            results["profit_factor"] = self._check_profit_factor(conn)
            results["max_drawdown"] = self._check_max_drawdown(conn)
            results["sharpe_ratio"] = self._check_sharpe_ratio(conn)
            results["consecutive_losses"] = self._check_consecutive_losses(conn)
        finally:
            conn.close()

        results["all_passed"] = all(
            isinstance(r, dict) and r.get("passed", False) for r in results.values()
        )
        return results

    # ------------------------------------------------------------------ #
    # 单项检查
    # ------------------------------------------------------------------ #
    def _check_running_days(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"SELECT MIN(trade_date), MAX(trade_date) FROM trade_records "
            f"WHERE {self.SIMULATE_MODE_FILTER}"
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return {
                "passed": False,
                "value": 0,
                "threshold": self.CRITERIA["min_running_days"],
                "reason": "无模拟交易记录",
            }
        first_date = datetime.strptime(row[0], "%Y-%m-%d")
        last_date = datetime.strptime(row[1], "%Y-%m-%d")
        days = (last_date - first_date).days
        return {
            "passed": days >= self.CRITERIA["min_running_days"],
            "value": days,
            "threshold": self.CRITERIA["min_running_days"],
        }

    def _check_trade_count(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"SELECT COUNT(*) FROM trade_records WHERE {self.SIMULATE_MODE_FILTER}"
        )
        count = cursor.fetchone()[0] or 0
        return {
            "passed": count >= self.CRITERIA["min_trade_count"],
            "value": count,
            "threshold": self.CRITERIA["min_trade_count"],
        }

    def _check_win_rate(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"""
            SELECT
                COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) AS wins,
                COUNT(*) AS total
            FROM trade_records
            WHERE {self.SIMULATE_MODE_FILTER} AND realized_pnl IS NOT NULL
            """
        )
        row = cursor.fetchone()
        if not row or not row[1]:
            return {
                "passed": False,
                "value": 0,
                "threshold": self.CRITERIA["min_win_rate"],
            }
        win_rate = row[0] / row[1]
        return {
            "passed": win_rate >= self.CRITERIA["min_win_rate"],
            "value": round(win_rate, 4),
            "threshold": self.CRITERIA["min_win_rate"],
        }

    def _check_profit_factor(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0) AS gross_profit,
                COALESCE(ABS(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl END)), 0) AS gross_loss
            FROM trade_records
            WHERE {self.SIMULATE_MODE_FILTER} AND realized_pnl IS NOT NULL
            """
        )
        row = cursor.fetchone() or (0, 0)
        gross_profit, gross_loss = row[0] or 0, row[1] or 0
        pf = gross_profit / max(gross_loss, 0.01)
        return {
            "passed": pf >= self.CRITERIA["min_profit_factor"],
            "value": round(pf, 3),
            "threshold": self.CRITERIA["min_profit_factor"],
        }

    def _check_max_drawdown(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"""
            SELECT realized_pnl FROM trade_records
            WHERE {self.SIMULATE_MODE_FILTER} AND realized_pnl IS NOT NULL
            ORDER BY exit_time
            """
        )
        pnls = [row[0] for row in cursor.fetchall()]
        if not pnls:
            return {
                "passed": False,
                "value": 0,
                "threshold": self.CRITERIA["max_drawdown_pct"],
            }
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in pnls:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / self.INITIAL_CAPITAL * 100
            if dd > max_dd:
                max_dd = dd
        return {
            "passed": max_dd <= self.CRITERIA["max_drawdown_pct"],
            "value": round(max_dd, 2),
            "threshold": self.CRITERIA["max_drawdown_pct"],
        }

    def _check_sharpe_ratio(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"""
            SELECT realized_pnl FROM trade_records
            WHERE {self.SIMULATE_MODE_FILTER} AND realized_pnl IS NOT NULL
            ORDER BY exit_time
            """
        )
        pnls = [row[0] for row in cursor.fetchall()]
        if len(pnls) < 10:
            return {
                "passed": False,
                "value": 0,
                "threshold": self.CRITERIA["min_sharpe_ratio"],
            }
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        if std_pnl == 0:
            return {
                "passed": False,
                "value": 0,
                "threshold": self.CRITERIA["min_sharpe_ratio"],
            }
        # 年化 Sharpe（假设每年 252 交易日，每日 ~2 笔）
        sharpe = (mean_pnl / std_pnl) * (252 * 2) ** 0.5
        return {
            "passed": sharpe >= self.CRITERIA["min_sharpe_ratio"],
            "value": round(sharpe, 3),
            "threshold": self.CRITERIA["min_sharpe_ratio"],
        }

    def _check_consecutive_losses(self, conn) -> Dict[str, Any]:
        cursor = conn.execute(
            f"""
            SELECT realized_pnl FROM trade_records
            WHERE {self.SIMULATE_MODE_FILTER} AND realized_pnl IS NOT NULL
            ORDER BY exit_time
            """
        )
        pnls = [row[0] for row in cursor.fetchall()]
        max_consecutive = 0
        current_streak = 0
        for pnl in pnls:
            if pnl < 0:
                current_streak += 1
                max_consecutive = max(max_consecutive, current_streak)
            else:
                current_streak = 0
        return {
            "passed": max_consecutive <= self.CRITERIA["max_consecutive_losses"],
            "value": max_consecutive,
            "threshold": self.CRITERIA["max_consecutive_losses"],
        }

    # ------------------------------------------------------------------ #
    # 报告
    # ------------------------------------------------------------------ #
    def print_report(self) -> bool:
        """打印格式化的检查报告，返回是否全部通过"""
        results = self.check_all()

        print("\n" + "=" * 60)
        print("  FTSeek-US 实盘就绪检查报告")
        print("=" * 60)
        print(f"  检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  数据库  : {self.db_path}")
        print("-" * 60)

        checks = [
            ("running_days", "运行天数", "天"),
            ("trade_count", "交易笔数", "笔"),
            ("win_rate", "胜率", ""),
            ("profit_factor", "盈亏比", ""),
            ("max_drawdown", "最大回撤", "%"),
            ("sharpe_ratio", "Sharpe比率", ""),
            ("consecutive_losses", "最大连亏", "笔"),
        ]

        for key, name, unit in checks:
            r = results[key]
            status = "✅ PASS" if r.get("passed") else "❌ FAIL"
            extra = f"  原因: {r['reason']}" if r.get("reason") else ""
            print(
                f"  {status}  {name}: {r['value']}{unit} "
                f"(阈值: {r['threshold']}{unit}){extra}"
            )

        print("-" * 60)
        if results["all_passed"]:
            print("  🟢 所有条件满足，可以切换到实盘")
        else:
            print("  🔴 存在未满足条件，不建议切换到实盘")
        print("=" * 60 + "\n")

        return bool(results["all_passed"])


if __name__ == "__main__":
    checker = LiveReadinessChecker()
    is_ready = checker.print_report()
    sys.exit(0 if is_ready else 1)
