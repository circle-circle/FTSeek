"""
周报复盘模块

核心功能：
- 生成周度绩效报告
- 分析各策略表现
- 基准对比（恒生指数等）
- 生成改进建议

模拟盘模式：只读数据，不修改数据库
"""
import sqlite3
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ftseek.performance.weekly_review")


class WeeklyReviewer:
    """
    周报复盘器

    功能：
    - generate_report: 生成周报数据
    - analyze_strategies: 分析各策略表现
    - compare_benchmark: 基准对比
    - generate_recommendations: 生成改进建议
    """

    # 默认基准收益率（恒生指数周收益率假设）
    DEFAULT_BENCHMARK_RETURN = 2.0

    def __init__(self, db_path: str = None):
        """
        初始化周报复盘器

        Args:
            db_path: 数据库路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
        """
        if db_path is None:
            db_path = '/opt/ftseek/FTSeek/data/ftseek.db'

        self.db_path = db_path
        logger.info(f"WeeklyReviewer initialized with db: {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def _get_trades_in_period(self, week_start: str, week_end: str) -> List[Dict]:
        """
        获取指定时间段内的交易配对

        Args:
            week_start: 周期开始日期 (YYYY-MM-DD)
            week_end: 周期结束日期 (YYYY-MM-DD)

        Returns:
            交易配对列表
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 查询 trade_pairs 表，JOIN signals 获取 strategy
            query = """
                SELECT tp.id, tp.buy_signal_id, tp.sell_signal_id, tp.code,
                       tp.buy_price, tp.sell_price, tp.quantity, tp.return_rate,
                       tp.profit, tp.open_time, tp.close_time, tp.status,
                       s.strategy
                FROM trade_pairs tp
                LEFT JOIN signals s ON tp.buy_signal_id = s.id
                WHERE tp.status = 'closed'
                  AND date(tp.close_time) BETWEEN date(?) AND date(?)
                ORDER BY tp.close_time ASC
            """

            cursor.execute(query, (week_start, week_end))
            rows = cursor.fetchall()

            trades = []
            for row in rows:
                trade = dict(row)
                # 如果没有 strategy，使用 UNKNOWN
                if trade.get('strategy') is None:
                    trade['strategy'] = 'UNKNOWN'
                trades.append(trade)

            logger.info(f"Found {len(trades)} trades in period {week_start} to {week_end}")
            return trades

        except sqlite3.Error as e:
            logger.error(f"Failed to get trades: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def generate_report(self, week_start: str, week_end: str) -> Dict:
        """
        生成周报数据

        Args:
            week_start: 周期开始日期 (YYYY-MM-DD)
            week_end: 周期结束日期 (YYYY-MM-DD)

        Returns:
            {
                'period': str,              # 报告周期
                'total_trades': int,        # 总交易数
                'win_rate': float,          # 胜率 (0-1)
                'total_return': float,      # 总收益率 (%)
                'by_strategy': Dict,        # 按策略分组
                'by_stock': Dict,           # 按股票分组
                'recommendations': List[str] # 改进建议
            }
        """
        logger.info(f"Generating weekly report from {week_start} to {week_end}")

        # 获取时间段内的交易
        trades = self._get_trades_in_period(week_start, week_end)

        # 计算基础指标
        total_trades = len(trades)

        if total_trades == 0:
            return {
                'period': f"{week_start} to {week_end}",
                'total_trades': 0,
                'win_rate': 0.0,
                'total_return': 0.0,
                'by_strategy': {},
                'by_stock': {},
                'recommendations': ['本周无交易数据，建议检查信号生成策略']
            }

        # 计算胜率
        winning_trades = sum(1 for t in trades if t['return_rate'] > 0)
        win_rate = winning_trades / total_trades

        # 计算总收益率
        total_return = sum(t['return_rate'] for t in trades)

        # 按策略分析
        by_strategy = self.analyze_strategies(trades)

        # 按股票分析
        by_stock = self._analyze_by_stock(trades)

        # 生成汇总指标用于建议生成
        metrics = {
            'win_rate': win_rate,
            'total_return': total_return,
            'total_trades': total_trades,
            'avg_return': total_return / total_trades if total_trades > 0 else 0.0
        }

        # 生成改进建议
        recommendations = self.generate_recommendations(metrics)

        report = {
            'period': f"{week_start} to {week_end}",
            'total_trades': total_trades,
            'win_rate': win_rate,
            'total_return': total_return,
            'by_strategy': by_strategy,
            'by_stock': by_stock,
            'recommendations': recommendations
        }

        logger.info(f"Report generated: {total_trades} trades, win_rate={win_rate:.2%}")
        return report

    def analyze_strategies(self, trades: List[Dict]) -> Dict:
        """
        分析各策略表现

        Args:
            trades: 交易列表

        Returns:
            {
                'strategy_name': {
                    'count': int,           # 交易数量
                    'win_count': int,       # 盈利数量
                    'avg_return': float,    # 平均收益率
                    'total_profit': float,  # 总收益金额
                    'win_rate': float       # 胜率
                }
            }
        """
        if not trades:
            return {}

        # 按策略分组
        strategy_groups = {}

        for trade in trades:
            strategy = trade.get('strategy', 'UNKNOWN')

            if strategy not in strategy_groups:
                strategy_groups[strategy] = []

            strategy_groups[strategy].append(trade)

        # 分析每个策略
        result = {}

        for strategy, strategy_trades in strategy_groups.items():
            count = len(strategy_trades)
            win_count = sum(1 for t in strategy_trades if t['return_rate'] > 0)
            total_return = sum(t['return_rate'] for t in strategy_trades)
            total_profit = sum(t['profit'] for t in strategy_trades)

            result[strategy] = {
                'count': count,
                'win_count': win_count,
                'avg_return': total_return / count if count > 0 else 0.0,
                'total_profit': total_profit,
                'win_rate': win_count / count if count > 0 else 0.0
            }

        logger.info(f"Analyzed {len(result)} strategies")
        return result

    def _analyze_by_stock(self, trades: List[Dict]) -> Dict:
        """
        按股票分析表现

        Args:
            trades: 交易列表

        Returns:
            {
                'stock_code': {
                    'count': int,
                    'win_count': int,
                    'avg_return': float,
                    'total_profit': float,
                    'win_rate': float
                }
            }
        """
        if not trades:
            return {}

        # 按股票分组
        stock_groups = {}

        for trade in trades:
            code = trade.get('code', 'UNKNOWN')

            if code not in stock_groups:
                stock_groups[code] = []

            stock_groups[code].append(trade)

        # 分析每个股票
        result = {}

        for code, stock_trades in stock_groups.items():
            count = len(stock_trades)
            win_count = sum(1 for t in stock_trades if t['return_rate'] > 0)
            total_return = sum(t['return_rate'] for t in stock_trades)
            total_profit = sum(t['profit'] for t in stock_trades)

            result[code] = {
                'count': count,
                'win_count': win_count,
                'avg_return': total_return / count if count > 0 else 0.0,
                'total_profit': total_profit,
                'win_rate': win_count / count if count > 0 else 0.0
            }

        logger.info(f"Analyzed {len(result)} stocks")
        return result

    def compare_benchmark(self, return_rate: float,
                          benchmark_return: float = None) -> Dict:
        """
        基准对比

        Args:
            return_rate: 策略收益率 (%)
            benchmark_return: 基准收益率 (%)，默认使用 DEFAULT_BENCHMARK_RETURN

        Returns:
            {
                'strategy_return': float,   # 策略收益率
                'benchmark_return': float,  # 基准收益率
                'excess_return': float,     # 超额收益
                'outperform': bool          # 是否跑赢基准
            }
        """
        if benchmark_return is None:
            benchmark_return = self.DEFAULT_BENCHMARK_RETURN

        excess_return = return_rate - benchmark_return
        outperform = excess_return > 0

        result = {
            'strategy_return': return_rate,
            'benchmark_return': benchmark_return,
            'excess_return': excess_return,
            'outperform': outperform
        }

        logger.info(f"Benchmark comparison: strategy={return_rate:.2f}%, "
                    f"benchmark={benchmark_return:.2f}%, excess={excess_return:.2f}%")
        return result

    def generate_recommendations(self, metrics: Dict) -> List[str]:
        """
        生成改进建议

        Args:
            metrics: 绩效指标字典
                - win_rate: 胜率 (0-1)
                - total_return: 总收益率 (%)
                - total_trades: 交易数量
                - avg_return: 平均收益率 (%)

        Returns:
            建议列表
        """
        recommendations = []

        win_rate = metrics.get('win_rate', 0.0)
        total_return = metrics.get('total_return', 0.0)
        total_trades = metrics.get('total_trades', 0)
        avg_return = metrics.get('avg_return', 0.0)

        # 胜率相关建议
        if win_rate < 0.4:
            recommendations.append(
                '胜率较低（{:.0%}），建议优化入场信号或加强止损策略'.format(win_rate)
            )
        elif win_rate >= 0.7:
            recommendations.append(
                '胜率优秀（{:.0%}），可考虑适当增加仓位'.format(win_rate)
            )

        # 收益率相关建议
        if total_return < 0:
            recommendations.append(
                '本周收益为负（{:.2f}%），建议复盘亏损交易，调整策略参数'.format(total_return)
            )
        elif total_return > 10:
            recommendations.append(
                '本周收益表现优异（{:.2f}%），建议总结成功经验并保持一致性'.format(total_return)
            )

        # 交易数量相关建议
        if total_trades == 0:
            recommendations.append('本周无交易，建议检查信号生成条件是否过于严格')
        elif total_trades < 3:
            recommendations.append(
                '交易数量较少（{}笔），统计样本不足，建议观察更多交易后再评估策略效果'.format(total_trades)
            )
        elif total_trades > 20:
            recommendations.append(
                '交易频率较高（{}笔），注意控制交易成本和过度交易风险'.format(total_trades)
            )

        # 平均收益相关建议
        if avg_return < 1.0 and total_trades > 0:
            recommendations.append(
                '平均收益率较低（{:.2f}%），建议优化止盈策略或选择波动性更高的标的'.format(avg_return)
            )

        # 如果没有特别建议，给出通用建议
        if not recommendations:
            recommendations.append('本周表现稳定，建议继续保持当前策略')

        logger.info(f"Generated {len(recommendations)} recommendations")
        return recommendations

    def get_week_range(self, week_type: str = 'this') -> tuple:
        """
        获取周范围日期

        Args:
            week_type: 'this' (本周), 'last' (上周)

        Returns:
            (week_start, week_end) 日期元组 (YYYY-MM-DD 格式)
        """
        now = datetime.now()

        if week_type == 'this':
            # 本周一到周日
            # 计算本周一（weekday 0 = Monday）
            monday = now - timedelta(days=now.weekday())
            sunday = monday + timedelta(days=6)
        elif week_type == 'last':
            # 上周一到上周日
            last_week = now - timedelta(weeks=1)
            monday = last_week - timedelta(days=last_week.weekday())
            sunday = monday + timedelta(days=6)
        else:
            raise ValueError(f"Invalid week_type: {week_type}")

        return monday.strftime('%Y-%m-%d'), sunday.strftime('%Y-%m-%d')

    def format_report_text(self, report: Dict) -> str:
        """
        格式化报告为文本

        Args:
            report: generate_report() 返回的报告字典

        Returns:
            格式化的文本报告
        """
        lines = []
        lines.append("=" * 60)
        lines.append("📊 FTSeek 周报复盘")
        lines.append("=" * 60)
        lines.append(f"周期：{report['period']}")
        lines.append("")

        # 交易汇总
        lines.append("📈 交易汇总")
        lines.append("-" * 40)
        lines.append(f"总交易数：{report['total_trades']} 笔")
        lines.append(f"胜率：{report['win_rate']:.2%}")
        lines.append(f"总收益率：{report['total_return']:.2f}%")
        lines.append("")

        # 策略表现
        if report['by_strategy']:
            lines.append("🎯 策略表现")
            lines.append("-" * 40)
            for strategy, metrics in report['by_strategy'].items():
                lines.append(f"{strategy}:")
                lines.append(f"  交易数：{metrics['count']} | "
                            f"胜率：{metrics['win_rate']:.2%} | "
                            f"平均收益：{metrics['avg_return']:.2f}%")
            lines.append("")

        # 股票表现
        if report['by_stock']:
            lines.append("📦 股票表现")
            lines.append("-" * 40)
            for stock, metrics in report['by_stock'].items():
                lines.append(f"{stock}:")
                lines.append(f"  交易数：{metrics['count']} | "
                            f"胜率：{metrics['win_rate']:.2%} | "
                            f"总盈利：{metrics['total_profit']:.2f}")
            lines.append("")

        # 基准对比
        benchmark_result = self.compare_benchmark(report['total_return'])
        lines.append("📊 基准对比")
        lines.append("-" * 40)
        lines.append(f"策略收益：{benchmark_result['strategy_return']:.2f}%")
        lines.append(f"基准收益：{benchmark_result['benchmark_return']:.2f}%")
        lines.append(f"超额收益：{benchmark_result['excess_return']:.2f}%")
        lines.append(f"表现：{'✅ 跑赢基准' if benchmark_result['outperform'] else '❌ 跑输基准'}")
        lines.append("")

        # 改进建议
        lines.append("💡 改进建议")
        lines.append("-" * 40)
        for i, rec in enumerate(report['recommendations'], 1):
            lines.append(f"{i}. {rec}")
        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    def push_to_dingtalk(self, report: Dict, webhook_url: str = None) -> bool:
        """
        推送到钉钉

        Args:
            report: 报告数据
            webhook_url: 钉钉 webhook URL (可选，使用配置的默认值)

        Returns:
            bool: 推送是否成功
        """
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            
            # 创建推送实例
            notifier = DingTalkNotifier(webhook_url=webhook_url)
            
            # 准备推送数据
            report_data = {
                'period': report['period'],
                'total_trades': report['total_trades'],
                'win_rate': report['win_rate'],
                'total_return': report['total_return'],
                'by_strategy': report['by_strategy'],
                'by_stock': report['by_stock'],
                'recommendations': report['recommendations'],
            }
            
            # 添加基准对比数据
            benchmark_result = self.compare_benchmark(report['total_return'])
            report_data['benchmark_comparison'] = benchmark_result
            
            # 推送
            success = notifier.send_weekly_report(report_data)
            
            if success:
                logger.info(f"Weekly report pushed to DingTalk: {report['period']}")
            else:
                logger.warning(f"Weekly report push failed: {report['period']}")
            
            return success
            
        except Exception as e:
            logger.error(f"Weekly report push error: {e}")
            # 推送失败不影响主流程
            return False


# 全局单例
_reviewer: Optional[WeeklyReviewer] = None


def get_weekly_reviewer(db_path: str = None) -> WeeklyReviewer:
    """
    获取周报复盘器单例

    Args:
        db_path: 数据库路径，可选

    Returns:
        WeeklyReviewer 实例
    """
    global _reviewer
    if _reviewer is None:
        _reviewer = WeeklyReviewer(db_path=db_path)
    return _reviewer
