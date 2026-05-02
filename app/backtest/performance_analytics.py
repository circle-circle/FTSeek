"""
Performance Analytics Engine — 基于实盘数据的绩效分析

功能:
1. 从 ftseek.db 加载真实交易记录
2. 计算 Sharpe / Sortino / Calmar / Max Drawdown
3. 按策略/市场状态/标的维度分解
4. 生成性能报告

使用:
    from app.backtest.performance_analytics import PerformanceAnalytics
    analytics = PerformanceAnalytics()
    report = analytics.analyze()
    print(report.summary())
"""

import logging
import sqlite3
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
import numpy as np

logger = logging.getLogger("ftseek.perf_analytics")

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"


@dataclass
class PerfMetrics:
    """绩效指标"""
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_days: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    avg_hold_days: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0


@dataclass
class ClosedTrade:
    """已平仓交易"""
    code: str
    action: str
    entry_price: float
    exit_price: float
    quantity: int
    realized_pnl: float
    pnl_pct: float
    entry_time: str
    exit_time: str
    strategy: str = ""
    market_regime: str = ""
    exit_reason: str = ""


@dataclass
class PerfReport:
    """完整绩效报告"""
    generated_at: str
    db_path: str
    overall: PerfMetrics = field(default_factory=PerfMetrics)
    by_strategy: Dict[str, PerfMetrics] = field(default_factory=dict)
    by_regime: Dict[str, PerfMetrics] = field(default_factory=dict)
    by_code: Dict[str, PerfMetrics] = field(default_factory=dict)
    by_direction: Dict[str, PerfMetrics] = field(default_factory=dict)  # P3: LONG vs SHORT
    monthly_returns: Dict[str, float] = field(default_factory=dict)
    equity_curve: List[float] = field(default_factory=list)
    trades: List[ClosedTrade] = field(default_factory=list)

    def summary(self) -> str:
        m = self.overall
        lines = [
            "=" * 64,
            "  FTSeek 绩效分析报告",
            "=" * 64,
            f"  生成时间: {self.generated_at}",
            f"  数据来源: {self.db_path}",
            "",
            "  ── 收益指标 ──",
            f"  总收益率:   {m.total_return_pct:+.2%}",
            f"  年化收益:   {m.annualized_return_pct:+.2%}",
            "",
            "  ── 风险指标 ──",
            f"  Sharpe:     {m.sharpe_ratio:.3f}",
            f"  Sortino:    {m.sortino_ratio:.3f}",
            f"  Calmar:     {m.calmar_ratio:.3f}",
            f"  最大回撤:   {m.max_drawdown_pct:.2%} ({m.max_drawdown_days}d)",
            "",
            "  ── 交易统计 ──",
            f"  总交易:     {m.total_trades}",
            f"  胜率:       {m.win_rate:.1%} ({m.winning_trades}W / {m.losing_trades}L)",
            f"  盈亏比:     {m.profit_factor:.2f}",
            f"  期望值:     {m.expectancy_pct:+.2%}",
            f"  均盈:       {m.avg_win_pct:+.2%}  均亏: {m.avg_loss_pct:+.2%}",
            f"  最佳:       {m.best_trade_pct:+.2%}  最差: {m.worst_trade_pct:+.2%}",
            f"  均持:       {m.avg_hold_days:.1f}d",
        ]

        if self.by_strategy:
            lines.extend(["", "  ── 策略分解 ──"])
            for name, pm in sorted(self.by_strategy.items()):
                lines.append(
                    f"  {name:20s} trades={pm.total_trades:2d}  "
                    f"ret={pm.total_return_pct:+.2%}  "
                    f"SR={pm.sharpe_ratio:.2f}  WR={pm.win_rate:.1%}"
                )

        if self.by_regime:
            lines.extend(["", "  ── 市场状态分解 ──"])
            for name, pm in sorted(self.by_regime.items()):
                lines.append(
                    f"  {name:20s} trades={pm.total_trades:2d}  "
                    f"ret={pm.total_return_pct:+.2%}  WR={pm.win_rate:.1%}"
                )

        # P3 修复 (2026-04-28): 显示做多/做空分解
        if self.by_direction:
            lines.extend(["", "  ── 方向分解 ──"])
            for name, pm in sorted(self.by_direction.items()):
                direction_icon = "📈" if name == "LONG" else "📉"
                lines.append(
                    f"  {direction_icon} {name:20s} trades={pm.total_trades:2d}  "
                    f"ret={pm.total_return_pct:+.2%}  "
                    f"SR={pm.sharpe_ratio:.2f}  WR={pm.win_rate:.1%}"
                )

        if self.monthly_returns:
            lines.extend(["", "  ── 月度收益 ──"])
            for month, ret in sorted(self.monthly_returns.items()):
                lines.append(f"  {month}: {ret:+.2%}")

        lines.append("=" * 64)
        return "\n".join(lines)


class PerformanceAnalytics:
    """实盘绩效分析引擎"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def analyze(
        self,
        strategy_filter: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> PerfReport:
        """执行完整绩效分析"""
        trades = self._load_closed_positions(strategy_filter, start_date, end_date)
        if not trades:
            logger.warning("[PerfAnalytics] 无已平仓交易数据")
            return PerfReport(
                generated_at=datetime.now().isoformat(), db_path=self.db_path
            )

        equity = self._build_equity_curve(trades)
        overall = self._compute_metrics(trades, equity)
        by_strategy = self._group_metrics(trades, lambda t: t.strategy or "unknown")
        by_regime = self._group_metrics(trades, lambda t: t.market_regime or "unknown")
        by_code = self._group_metrics(trades, lambda t: t.code)
        # P3 修复 (2026-04-28): 按交易方向分组 (LONG vs SHORT)
        by_direction = self._group_metrics(
            trades, lambda t: "SHORT" if t.action == "SELL" else "LONG"
        )
        monthly = self._monthly_returns(trades)

        return PerfReport(
            generated_at=datetime.now().isoformat(),
            db_path=self.db_path,
            overall=overall,
            by_strategy=by_strategy,
            by_regime=by_regime,
            by_code=by_code,
            by_direction=by_direction,
            monthly_returns=monthly,
            equity_curve=equity,
            trades=trades,
        )

    # ── 内部方法 ──────────────────────────────────────────────────────

    def _load_closed_positions(
        self, strategy_filter: str = None,
        start_date: str = None, end_date: str = None
    ) -> List[ClosedTrade]:
        """从 positions 表加载已平仓记录"""
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        cur = conn.cursor()

        query = """
            SELECT code, action, entry_price, exit_price, quantity,
                   realized_pnl, entry_time, exit_time, strategy
            FROM positions
            WHERE status = 'CLOSED' AND exit_price IS NOT NULL
              AND entry_price > 0 AND quantity > 0
        """
        params = []
        if start_date:
            query += " AND entry_time >= ?"
            params.append(start_date)
        if end_date:
            query += " AND entry_time <= ?"
            params.append(end_date)

        cur.execute(query, params)
        rows = cur.fetchall()

        trades = []
        for row in rows:
            code, action, ep, xp, qty, pnl, et, xt, strategy = row
            if ep <= 0:
                continue
            pnl_pct = (xp - ep) / ep if action == "BUY" else (ep - xp) / ep

            trades.append(ClosedTrade(
                code=code, action=action or "BUY",
                entry_price=ep, exit_price=xp,
                quantity=qty, realized_pnl=pnl or 0.0,
                pnl_pct=pnl_pct, entry_time=et or "", exit_time=xt or "",
                strategy=strategy or "",
            ))

        conn.close()

        if strategy_filter:
            trades = [t for t in trades if strategy_filter.lower() in t.strategy.lower()]

        trades.sort(key=lambda t: t.entry_time)
        logger.info(f"[PerfAnalytics] 加载 {len(trades)} 条已平仓记录")
        return trades

    def _build_equity_curve(self, trades: List[ClosedTrade], initial: float = 100000.0) -> List[float]:
        """构建权益曲线"""
        curve = [initial]
        for t in sorted(trades, key=lambda x: x.exit_time or x.entry_time):
            curve.append(curve[-1] + t.realized_pnl)
        return curve

    def _compute_metrics(
        self, trades: List[ClosedTrade], equity_curve: List[float]
    ) -> PerfMetrics:
        """计算完整绩效指标"""
        m = PerfMetrics()
        m.total_trades = len(trades)
        if not trades:
            return m

        wins = [t for t in trades if t.pnl_pct > 0]
        losses = [t for t in trades if t.pnl_pct <= 0]
        m.winning_trades = len(wins)
        m.losing_trades = len(losses)
        m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0

        # 收益率
        returns = [t.pnl_pct for t in trades]
        m.total_return_pct = sum(t.realized_pnl for t in trades) / (equity_curve[0] if equity_curve else 100000)

        # 年化 (简化: 从首笔到末笔)
        if len(trades) >= 2:
            try:
                t0 = datetime.fromisoformat(trades[0].entry_time.replace(" ", "T"))
                t1 = datetime.fromisoformat(trades[-1].exit_time.replace(" ", "T"))
                years = max((t1 - t0).days / 365.25, 0.02)
                m.annualized_return_pct = (1 + m.total_return_pct) ** (1 / years) - 1
            except (ValueError, OSError):
                m.annualized_return_pct = m.total_return_pct

        # 平均盈亏
        m.avg_win_pct = np.mean([t.pnl_pct for t in wins]) if wins else 0
        m.avg_loss_pct = np.mean([t.pnl_pct for t in losses]) if losses else 0
        m.best_trade_pct = max(returns) if returns else 0
        m.worst_trade_pct = min(returns) if returns else 0

        # 盈亏比
        gross_profit = sum(t.realized_pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.realized_pnl for t in losses)) if losses else 1
        m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # 期望值
        m.expectancy_pct = np.mean(returns) if returns else 0

        # 持仓天数
        hold_days = []
        for t in trades:
            try:
                et = datetime.fromisoformat(t.entry_time.replace(" ", "T"))
                xt = datetime.fromisoformat(t.exit_time.replace(" ", "T"))
                hold_days.append((xt - et).days)
            except (ValueError, OSError):
                pass
        m.avg_hold_days = np.mean(hold_days) if hold_days else 0

        # 最大回撤
        if equity_curve:
            peak = equity_curve[0]
            max_dd = 0.0
            peak_idx = 0
            dd_start = 0
            for i, eq in enumerate(equity_curve):
                if eq > peak:
                    peak = eq
                    peak_idx = i
                dd = (peak - eq) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                    dd_start = peak_idx
            m.max_drawdown_pct = max_dd
            m.max_drawdown_days = len(equity_curve) - dd_start - 1 if dd_start < len(equity_curve) else 0

        # Sharpe / Sortino / Calmar
        if len(returns) >= 2:
            ret_arr = np.array(returns)
            avg_ret = np.mean(ret_arr)
            std_ret = np.std(ret_arr, ddof=1)
            rf_annual = 0.02
            rf_daily = rf_annual / 252

            # 用日化近似
            if std_ret > 0:
                m.sharpe_ratio = (avg_ret - rf_daily) / std_ret * np.sqrt(252)
            else:
                m.sharpe_ratio = 0.0 if avg_ret <= 0 else 9.99

            neg_returns = ret_arr[ret_arr < 0]
            if len(neg_returns) > 1:
                down_std = np.std(neg_returns, ddof=1)
                m.sortino_ratio = (avg_ret - rf_daily) / down_std * np.sqrt(252) if down_std > 0 else 0
            elif avg_ret > 0:
                m.sortino_ratio = 9.99

            if m.max_drawdown_pct > 0 and m.annualized_return_pct != 0:
                m.calmar_ratio = m.annualized_return_pct / m.max_drawdown_pct

        return m

    def _group_metrics(
        self, trades: List[ClosedTrade], key_fn
    ) -> Dict[str, PerfMetrics]:
        """按维度分组计算指标"""
        groups = defaultdict(list)
        for t in trades:
            groups[key_fn(t)].append(t)
        result = {}
        for key, group_trades in groups.items():
            eq = self._build_equity_curve(group_trades)
            result[key] = self._compute_metrics(group_trades, eq)
        return result

    def _monthly_returns(self, trades: List[ClosedTrade]) -> Dict[str, float]:
        """
        按月度汇总收益率

        P3 修复 (2026-04-28): 改用权益曲线端点计算月度收益，
        而非简单累加 pnl_pct (复利效应下累加不等于真实收益)
        """
        # 按月份分组交易
        monthly_trades = defaultdict(list)
        for t in trades:
            try:
                dt = datetime.fromisoformat(t.exit_time.replace(" ", "T"))
                month_key = dt.strftime("%Y-%m")
                monthly_trades[month_key].append(t)
            except (ValueError, OSError):
                pass

        # 使用每组的权益曲线端点计算月度收益
        result = {}
        initial = 100000.0
        cumulative = initial
        for month_key in sorted(monthly_trades.keys()):
            group = monthly_trades[month_key]
            eq = self._build_equity_curve(group, initial=cumulative)
            month_end = eq[-1] if eq else cumulative
            month_ret = (month_end - cumulative) / cumulative if cumulative > 0 else 0
            result[month_key] = round(month_ret, 6)
            cumulative = month_end

        return result

    def compare_params(
        self, param_field: str = "strategy"
    ) -> Dict[str, PerfMetrics]:
        """
        参数对比: 按指定字段分组比较策略表现.
        可用于对比不同止损/止盈/仓位参数的效果.
        """
        trades = self._load_closed_positions()
        if param_field == "strategy":
            return self._group_metrics(trades, lambda t: t.strategy or "unknown")
        elif param_field == "regime":
            return self._group_metrics(trades, lambda t: t.market_regime or "unknown")
        elif param_field == "code":
            return self._group_metrics(trades, lambda t: t.code)
        else:
            raise ValueError(f"不支持的字段: {param_field}")


def run_analysis(db_path: str = DB_PATH) -> PerfReport:
    """便捷函数: 运行完整分析并打印报告"""
    analytics = PerformanceAnalytics(db_path)
    report = analytics.analyze()
    logger.info("\n" + report.summary())
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_analysis()
