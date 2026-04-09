#!/usr/bin/env python3
"""
绩效归因分析脚本

功能:
1. 因子贡献度分析 - 分析各因子对盈利的贡献
2. 策略贡献度分析 - 分析各策略的收益贡献
3. 标的贡献度分析 - 识别盈利/亏损最多的股票
4. 市场状态分析 - 不同市场状态下的表现
5. 时间分析 - 月度/周度表现趋势

使用方法:
    python scripts/performance_attribution.py --period 2026-01-01:2026-04-09
    python scripts/performance_attribution.py --days 30  # 最近 30 天
"""

import argparse
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from dataclasses import dataclass

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.attribution")

# 导入项目模块
from app.storage.database import get_database
from app.performance.stats import PerformanceCalculator, PerformanceReporter


@dataclass
class FactorContribution:
    """因子贡献度数据"""
    factor_name: str
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    win_pnl: float = 0.0
    loss_pnl: float = 0.0
    avg_quality_score: float = 0.0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0

    @property
    def net_pnl(self) -> float:
        return self.win_pnl - self.loss_pnl

    @property
    def profit_factor(self) -> float:
        return self.win_pnl / self.loss_pnl if self.loss_pnl > 0 else float('inf')


@dataclass
class StockContribution:
    """标的贡献度数据"""
    code: str = ""
    name: str = ""
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_pnl: float = 0.0
    avg_pnl_pct: float = 0.0
    max_win: float = 0.0
    max_loss: float = 0.0

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0


class PerformanceAttribution:
    """绩效归因分析器"""

    def __init__(self, db=None):
        self.db = db or get_database()
        self.calculator = PerformanceCalculator()

    def analyze_factor_contribution(
        self, start_date: str, end_date: str
    ) -> Dict[str, FactorContribution]:
        """
        分析因子贡献度

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            {factor_name: FactorContribution}
        """
        logger.info(f"分析因子贡献度：{start_date} ~ {end_date}")

        # 获取交易记录
        trades = self.db.get_trades_by_period(
            f"{start_date} 00:00:00",
            f"{end_date} 23:59:59"
        )

        # 只统计已平仓交易
        closed_trades = [t for t in trades if t.get('status') == 'closed']

        if not closed_trades:
            logger.warning("没有找到已平仓交易")
            return {}

        # 获取对应的信号
        factor_pnl = defaultdict(FactorContribution)

        for trade in closed_trades:
            signal_id = trade.get('signal_id')
            if not signal_id:
                continue

            signal = self.db.get_signal(signal_id)
            if not signal:
                continue

            pnl = trade.get('pnl', 0)
            pnl_pct = trade.get('pnl_percent', 0)

            # 从 signal extra 中获取因子信息
            extra = signal.get('extra', {})
            factors = extra.get('factors', {}) if isinstance(extra, dict) else {}

            # 如果没有因子信息，使用 strategy 作为因子
            if not factors:
                strategy = signal.get('strategy', 'unknown')
                factors = {strategy: {'direction': 1}}

            # 分摊 PnL 到各因子 (简单平均分摊)
            active_factors = [f for f, data in factors.items()
                            if isinstance(data, dict) and data.get('direction', 0) != 0]

            if not active_factors:
                continue

            pnl_per_factor = pnl / len(active_factors)
            pnl_pct_per_factor = pnl_pct / len(active_factors)

            # 更新各因子的贡献
            for factor_name in active_factors:
                fc = factor_pnl[factor_name]
                fc.factor_name = factor_name
                fc.trade_count = getattr(fc, 'trade_count', 0) + 1

                if pnl > 0:
                    fc.win_count += 1
                    fc.win_pnl += abs(pnl_per_factor)
                elif pnl < 0:
                    fc.loss_count += 1
                    fc.loss_pnl += abs(pnl_per_factor)

                fc.total_pnl += pnl_per_factor

        return dict(factor_pnl)

    def analyze_stock_contribution(
        self, start_date: str, end_date: str
    ) -> Dict[str, StockContribution]:
        """
        分析标的贡献度

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            {code: StockContribution}
        """
        logger.info(f"分析标的贡献度：{start_date} ~ {end_date}")

        trades = self.db.get_trades_by_period(
            f"{start_date} 00:00:00",
            f"{end_date} 23:59:59"
        )

        closed_trades = [t for t in trades if t.get('status') == 'closed']

        if not closed_trades:
            return {}

        stock_pnl = defaultdict(StockContribution)

        for trade in closed_trades:
            code = trade.get('code', '')
            pnl = trade.get('pnl', 0)
            pnl_pct = trade.get('pnl_percent', 0)
            name = trade.get('name', '')

            sc = stock_pnl[code]
            sc.code = code
            sc.name = name
            sc.trade_count += 1
            sc.total_pnl += pnl

            if pnl > 0:
                sc.win_count += 1
                sc.max_win = max(sc.max_win, pnl)
            elif pnl < 0:
                sc.loss_count += 1
                sc.max_loss = min(sc.max_loss, pnl)

        # 计算平均盈亏百分比
        for sc in stock_pnl.values():
            if sc.trade_count > 0:
                sc.avg_pnl_pct = sc.total_pnl / sc.trade_count

        return dict(stock_pnl)

    def analyze_regime_performance(
        self, start_date: str, end_date: str
    ) -> Dict:
        """
        分析不同市场状态下的表现

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            市场状态绩效 Dict
        """
        logger.info(f"分析市场状态表现：{start_date} ~ {end_date}")

        outcomes = self.db.get_signal_outcomes_summary(
            days=90,  # 使用最近 90 天数据
            group_by='market_regime'
        )

        return outcomes.get('data', [])

    def analyze_strategy_performance(
        self, start_date: str, end_date: str
    ) -> Dict:
        """
        分析策略表现

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            策略绩效 Dict
        """
        logger.info(f"分析策略表现：{start_date} ~ {end_date}")

        outcomes = self.db.get_signal_outcomes_summary(
            days=90,
            group_by='strategy'
        )

        return outcomes.get('data', [])

    def generate_report(
        self, start_date: str, end_date: str,
        top_n: int = 10
    ) -> str:
        """
        生成绩效归因报告

        Args:
            start_date: 开始日期
            end_date: 结束日期
            top_n: 展示前 N 个

        Returns:
            格式化报告文本
        """
        lines = [
            "=" * 70,
            "📊 FTSeek 绩效归因分析报告",
            "=" * 70,
            f"分析周期：{start_date} ~ {end_date}",
            f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]

        # 1. 因子贡献度
        lines.append("-" * 70)
        lines.append("🔍 因子贡献度分析")
        lines.append("-" * 70)

        factor_contrib = self.analyze_factor_contribution(start_date, end_date)

        if factor_contrib:
            # 按净盈利排序
            sorted_factors = sorted(
                factor_contrib.items(),
                key=lambda x: x[1].net_pnl,
                reverse=True
            )

            lines.append(f"{'因子':<15} {'胜率':<8} {'净盈利':<12} {'盈亏比':<10} {'交易数':<8}")
            lines.append("-" * 55)

            for factor_name, fc in sorted_factors[:top_n]:
                pf = fc.profit_factor
                pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"
                lines.append(
                    f"{factor_name:<15} {fc.win_rate:>6.1%}   "
                    f"HK${fc.net_pnl:>8.0f}    {pf_str:<10} {fc.trade_count:<8}"
                )

            # 找出最佳和最差因子
            if sorted_factors:
                best = sorted_factors[0][1]
                worst = sorted_factors[-1][1]
                lines.append("")
                lines.append(f"✅ 最佳因子：{best.factor_name} (HK${best.net_pnl:+.0f})")
                if worst.net_pnl < 0:
                    lines.append(f"⚠️  拖累因子：{worst.factor_name} (HK${worst.net_pnl:+.0f})")
        else:
            lines.append("⚠️  无足够因子数据")

        lines.append("")

        # 2. 标的贡献度
        lines.append("-" * 70)
        lines.append("📈 标的贡献度分析")
        lines.append("-" * 70)

        stock_contrib = self.analyze_stock_contribution(start_date, end_date)

        if stock_contrib:
            # 按总盈利排序
            sorted_stocks = sorted(
                stock_contrib.items(),
                key=lambda x: x[1].total_pnl,
                reverse=True
            )

            lines.append(f"{'代码':<12} {'名称':<10} {'胜率':<8} {'总盈利':<12} {'交易数':<8}")
            lines.append("-" * 55)

            for code, sc in sorted_stocks[:top_n]:
                name = sc.name[:10] if sc.name else code
                lines.append(
                    f"{code:<12} {name:<10} {sc.win_rate:>6.1%}   "
                    f"HK${sc.total_pnl:>8.0f}    {sc.trade_count:<8}"
                )

            # 最佳和最差标的
            if sorted_stocks:
                best = sorted_stocks[0][1]
                worst = sorted_stocks[-1][1]
                lines.append("")
                lines.append(f"✅ 最佳标的：{best.code} ({best.name}) HK${best.total_pnl:+.0f}")
                if worst.total_pnl < 0:
                    lines.append(f"⚠️  最差标的：{worst.code} ({worst.name}) HK${worst.total_pnl:+.0f}")
        else:
            lines.append("⚠️  无足够标的数据")

        lines.append("")

        # 3. 策略表现
        lines.append("-" * 70)
        lines.append("🎯 策略表现分析")
        lines.append("-" * 70)

        strategy_perf = self.analyze_strategy_performance(start_date, end_date)

        if strategy_perf:
            lines.append(f"{'策略':<20} {'交易数':<8} {'胜率':<10} {'平均 PnL%':<12}")
            lines.append("-" * 55)

            for sp in strategy_perf[:top_n]:
                strategy = sp.get('strategy', 'unknown')[:20]
                total = sp.get('total_trades', 0)
                win_rate = sp.get('win_rate', 0)
                avg_pnl = sp.get('avg_pnl_pct', 0)
                lines.append(
                    f"{strategy:<20} {total:<8} {win_rate:>6.1%}     {avg_pnl:>+6.2f}%"
                )
        else:
            lines.append("⚠️  无足够策略数据")

        lines.append("")

        # 4. 市场状态表现
        lines.append("-" * 70)
        lines.append("🌡️  市场状态表现")
        lines.append("-" * 70)

        regime_perf = self.analyze_regime_performance(start_date, end_date)

        if regime_perf:
            lines.append(f"{'市场状态':<15} {'交易数':<8} {'胜率':<10} {'平均 PnL%':<12}")
            lines.append("-" * 55)

            for rp in regime_perf[:top_n]:
                regime = rp.get('market_regime', 'unknown')[:15]
                total = rp.get('total_trades', 0)
                win_rate = rp.get('win_rate', 0)
                avg_pnl = rp.get('avg_pnl_pct', 0)
                lines.append(
                    f"{regime:<15} {total:<8} {win_rate:>6.1%}     {avg_pnl:>+6.2f}%"
                )
        else:
            lines.append("⚠️  无足够市场状态数据")

        lines.append("")
        lines.append("=" * 70)
        lines.append("报告结束")
        lines.append("=" * 70)

        return "\n".join(lines)


def parse_date_range(args) -> tuple:
    """解析日期范围参数"""
    if hasattr(args, 'period') and args.period:
        start, end = args.period.split(':')
        return start.strip(), end.strip()
    elif hasattr(args, 'days') and args.days:
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')
        return start, end
    else:
        # 默认最近 30 天
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        return start, end


def main():
    parser = argparse.ArgumentParser(description='绩效归因分析')
    parser.add_argument('--period', type=str, help='分析周期 (YYYY-MM-DD:YYYY-MM-DD)')
    parser.add_argument('--days', type=int, help='分析最近 N 天')
    parser.add_argument('--top', type=int, default=10, help='展示前 N 个')
    parser.add_argument('--output', type=str, help='输出文件路径')
    parser.add_argument('--verbose', '-v', action='store_true', help='详细输出')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    start_date, end_date = parse_date_range(args)

    logger.info(f"开始绩效归因分析：{start_date} ~ {end_date}")

    attribution = PerformanceAttribution()
    report = attribution.generate_report(start_date, end_date, top_n=args.top)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
        logger.info(f"报告已保存到：{args.output}")
    else:
        print("\n" + report)


if __name__ == '__main__':
    main()
