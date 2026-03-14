"""
回测对比模块

核心功能：
- 实盘收益率 vs 回测收益率对比
- 实盘胜率 vs 回测胜率对比
- 交易次数对比
- 平均收益对比

模拟盘模式：使用实盘交易数据对比历史回测数据
"""
import sqlite3
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ftseek.performance.backtest_compare")


class BacktestComparer:
    """
    回测对比分析器

    功能：
    - compare_returns: 收益率对比
    - compare_win_rates: 胜率对比
    - compare_trades: 交易次数对比
    - generate_report: 生成对比报告
    """

    def __init__(self, db_path: str = None):
        """
        初始化回测对比器

        Args:
            db_path: 数据库路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
        """
        if db_path is None:
            db_path = '/opt/ftseek/FTSeek/data/ftseek.db'

        self.db_path = db_path
        self.live_db_path = db_path
        logger.info(f"BacktestComparer initialized with db: {db_path}")

    def _get_connection(self, db_path: str = None) -> sqlite3.Connection:
        """获取数据库连接"""
        path = db_path or self.db_path
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def _get_period_cutoff(self, period: str) -> str:
        """
        根据时间段计算截止时间

        Args:
            period: 时间段，如 '7d', '30d', '90d'

        Returns:
            ISO 格式的时间字符串
        """
        # 解析时间段
        if period.endswith('d'):
            days = int(period[:-1])
        elif period.endswith('w'):
            days = int(period[:-1]) * 7
        elif period.endswith('m'):
            days = int(period[:-1]) * 30
        else:
            days = 30  # 默认 30 天

        cutoff = datetime.now() - timedelta(days=days)
        return cutoff.isoformat()

    def _get_live_trades(self, period: str = '30d', code: str = None,
                         strategy: str = None) -> List[Dict]:
        """
        获取实盘交易数据

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            交易列表
        """
        cutoff = self._get_period_cutoff(period)
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 从 trade_pairs 表获取实盘交易
            query = """
                SELECT id, code, buy_price, sell_price, quantity,
                       return_rate, profit, open_time, close_time, status
                FROM trade_pairs
                WHERE status = 'closed'
                AND close_time > ?
            """
            params = [cutoff]

            if code:
                query += " AND code = ?"
                params.append(code)

            # 如果需要按策略过滤，需要关联 signals 表
            if strategy:
                query += """
                    AND EXISTS (
                        SELECT 1 FROM signals s
                        WHERE s.id = buy_signal_id
                        AND s.strategy = ?
                    )
                """
                params.append(strategy)

            query += " ORDER BY close_time DESC"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Failed to get live trades: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def _get_backtest_trades(self, period: str = '30d', code: str = None,
                             strategy: str = None) -> List[Dict]:
        """
        获取回测交易数据

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            交易列表
        """
        cutoff = self._get_period_cutoff(period)
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 从 backtest_trades 表获取回测数据
            query = """
                SELECT id, code, strategy, buy_price, sell_price, quantity,
                       return_rate, profit, open_time, close_time, status
                FROM backtest_trades
                WHERE status = 'closed'
                AND close_time > ?
            """
            params = [cutoff]

            if code:
                query += " AND code = ?"
                params.append(code)

            if strategy:
                query += " AND strategy = ?"
                params.append(strategy)

            query += " ORDER BY close_time DESC"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Failed to get backtest trades: {e}")
            # 如果表不存在，返回空列表
            if 'no such table' in str(e):
                logger.warning("backtest_trades table does not exist")
                return []
            return []
        finally:
            if conn:
                conn.close()

    def _calculate_avg_return(self, trades: List[Dict]) -> float:
        """
        计算平均收益率

        Args:
            trades: 交易列表

        Returns:
            平均收益率 (%)
        """
        if not trades:
            return 0.0

        total_return = sum(t.get('return_rate', 0) for t in trades)
        return total_return / len(trades)

    def _calculate_win_rate(self, trades: List[Dict]) -> float:
        """
        计算胜率

        Args:
            trades: 交易列表

        Returns:
            胜率 (0-1)
        """
        if not trades:
            return 0.0

        winning_trades = sum(1 for t in trades if t.get('return_rate', 0) > 0)
        return winning_trades / len(trades)

    def compare_returns(self, period: str = '30d', code: str = None,
                        strategy: str = None) -> Dict:
        """
        收益率对比

        Args:
            period: 时间段，如 '7d', '30d', '90d'
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            {
                'live_return': float,      # 实盘平均收益率 (%)
                'backtest_return': float,  # 回测平均收益率 (%)
                'difference': float        # 差异 (%)
            }
        """
        # 获取实盘和回测交易
        live_trades = self._get_live_trades(period=period, code=code,
                                            strategy=strategy)
        backtest_trades = self._get_backtest_trades(period=period, code=code,
                                                    strategy=strategy)

        # 计算平均收益率
        live_return = self._calculate_avg_return(live_trades)
        backtest_return = self._calculate_avg_return(backtest_trades)

        # 计算差异
        difference = live_return - backtest_return

        logger.info(f"Returns comparison - Live: {live_return:.2f}%, "
                    f"Backtest: {backtest_return:.2f}%, "
                    f"Difference: {difference:.2f}%")

        return {
            'live_return': live_return,
            'backtest_return': backtest_return,
            'difference': difference
        }

    def compare_win_rates(self, period: str = '30d', code: str = None,
                          strategy: str = None) -> Dict:
        """
        胜率对比

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            {
                'live_win_rate': float,      # 实盘胜率 (0-1)
                'backtest_win_rate': float,  # 回测胜率 (0-1)
                'difference': float          # 差异 (0-1)
            }
        """
        # 获取实盘和回测交易
        live_trades = self._get_live_trades(period=period, code=code,
                                            strategy=strategy)
        backtest_trades = self._get_backtest_trades(period=period, code=code,
                                                    strategy=strategy)

        # 计算胜率
        live_win_rate = self._calculate_win_rate(live_trades)
        backtest_win_rate = self._calculate_win_rate(backtest_trades)

        # 计算差异
        difference = live_win_rate - backtest_win_rate

        logger.info(f"Win rate comparison - Live: {live_win_rate:.2%}, "
                    f"Backtest: {backtest_win_rate:.2%}, "
                    f"Difference: {difference:.2%}")

        return {
            'live_win_rate': live_win_rate,
            'backtest_win_rate': backtest_win_rate,
            'difference': difference
        }

    def compare_trades(self, period: str = '30d', code: str = None,
                       strategy: str = None) -> Dict:
        """
        交易次数对比

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            {
                'live_trades': int,      # 实盘交易次数
                'backtest_trades': int,  # 回测交易次数
                'difference': int        # 差异
            }
        """
        # 获取实盘和回测交易
        live_trades = self._get_live_trades(period=period, code=code,
                                            strategy=strategy)
        backtest_trades = self._get_backtest_trades(period=period, code=code,
                                                    strategy=strategy)

        # 计算交易次数
        live_count = len(live_trades)
        backtest_count = len(backtest_trades)

        # 计算差异
        difference = live_count - backtest_count

        logger.info(f"Trades comparison - Live: {live_count}, "
                    f"Backtest: {backtest_count}, "
                    f"Difference: {difference}")

        return {
            'live_trades': live_count,
            'backtest_trades': backtest_count,
            'difference': difference
        }

    def compare_avg_profits(self, period: str = '30d', code: str = None,
                            strategy: str = None) -> Dict:
        """
        平均收益对比

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            {
                'live_avg_profit': float,      # 实盘平均收益
                'backtest_avg_profit': float,  # 回测平均收益
                'difference': float            # 差异
            }
        """
        # 获取实盘和回测交易
        live_trades = self._get_live_trades(period=period, code=code,
                                            strategy=strategy)
        backtest_trades = self._get_backtest_trades(period=period, code=code,
                                                    strategy=strategy)

        # 计算平均收益
        live_avg_profit = self._calculate_avg_profit(live_trades)
        backtest_avg_profit = self._calculate_avg_profit(backtest_trades)

        # 计算差异
        difference = live_avg_profit - backtest_avg_profit

        logger.info(f"Avg profit comparison - Live: {live_avg_profit:.2f}, "
                    f"Backtest: {backtest_avg_profit:.2f}, "
                    f"Difference: {difference:.2f}")

        return {
            'live_avg_profit': live_avg_profit,
            'backtest_avg_profit': backtest_avg_profit,
            'difference': difference
        }

    def _calculate_avg_profit(self, trades: List[Dict]) -> float:
        """
        计算平均收益金额

        Args:
            trades: 交易列表

        Returns:
            平均收益金额
        """
        if not trades:
            return 0.0

        total_profit = sum(t.get('profit', 0) for t in trades)
        return total_profit / len(trades)

    def generate_report(self, period: str = '30d', code: str = None,
                        strategy: str = None) -> Dict:
        """
        生成对比报告

        Args:
            period: 时间段
            code: 股票代码，可选
            strategy: 策略名称，可选

        Returns:
            {
                'period': str,
                'code': str,
                'strategy': str,
                'returns': Dict,
                'win_rates': Dict,
                'trades': Dict,
                'avg_profits': Dict,
                'summary': str
            }
        """
        # 获取各项对比数据
        returns = self.compare_returns(period=period, code=code,
                                       strategy=strategy)
        win_rates = self.compare_win_rates(period=period, code=code,
                                           strategy=strategy)
        trades = self.compare_trades(period=period, code=code,
                                     strategy=strategy)
        avg_profits = self.compare_avg_profits(period=period, code=code,
                                               strategy=strategy)

        # 生成总结
        summary = self._generate_summary(returns, win_rates, trades, avg_profits)

        report = {
            'period': period,
            'code': code or 'all',
            'strategy': strategy or 'all',
            'returns': returns,
            'win_rates': win_rates,
            'trades': trades,
            'avg_profits': avg_profits,
            'summary': summary
        }

        logger.info(f"Generated backtest comparison report for {period}")
        return report

    def _generate_summary(self, returns: Dict, win_rates: Dict,
                          trades: Dict, avg_profits: Dict) -> str:
        """
        生成对比总结文本

        Args:
            returns: 收益率对比数据
            win_rates: 胜率对比数据
            trades: 交易对比数据
            avg_profits: 平均收益对比数据

        Returns:
            总结文本
        """
        lines = []

        # 收益率总结
        if returns['difference'] > 0:
            lines.append(f"✓ 实盘收益率 ({returns['live_return']:.2f}%) 优于回测 "
                        f"({returns['backtest_return']:.2f}%)")
        elif returns['difference'] < 0:
            lines.append(f"✗ 实盘收益率 ({returns['live_return']:.2f}%) 落后回测 "
                        f"({returns['backtest_return']:.2f}%)")
        else:
            lines.append(f"= 实盘与回测收益率持平 ({returns['live_return']:.2f}%)")

        # 胜率总结
        if win_rates['difference'] > 0.05:  # 差异超过 5%
            lines.append(f"✓ 实盘胜率 ({win_rates['live_win_rate']:.1%}) 显著优于回测 "
                        f"({win_rates['backtest_win_rate']:.1%})")
        elif win_rates['difference'] < -0.05:
            lines.append(f"✗ 实盘胜率 ({win_rates['live_win_rate']:.1%}) 显著落后回测 "
                        f"({win_rates['backtest_win_rate']:.1%})")
        else:
            lines.append(f"= 实盘与回测胜率相近 "
                        f"({win_rates['live_win_rate']:.1%} vs "
                        f"{win_rates['backtest_win_rate']:.1%})")

        # 交易次数总结
        if trades['difference'] > 0:
            lines.append(f"ℹ 实盘交易次数 ({trades['live_trades']}) 多于回测 "
                        f"({trades['backtest_trades']})")
        elif trades['difference'] < 0:
            lines.append(f"ℹ 实盘交易次数 ({trades['live_trades']}) 少于回测 "
                        f"({trades['backtest_trades']})")
        else:
            lines.append(f"= 实盘与回测交易次数相同 ({trades['live_trades']})")

        return " | ".join(lines)

    def push_to_dingtalk(self, report: Dict, webhook_url: str = None) -> bool:
        """
        推送到钉钉

        Args:
            report: generate_report() 返回的报告字典
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
                'code': report['code'],
                'strategy': report['strategy'],
                'returns': report['returns'],
                'win_rates': report['win_rates'],
                'trades': report['trades'],
                'avg_profits': report['avg_profits'],
                'summary': report['summary'],
            }
            
            # 推送
            success = notifier.send_backtest_compare_report(report_data)
            
            if success:
                logger.info(f"Backtest comparison report pushed to DingTalk: {report['period']}")
            else:
                logger.warning(f"Backtest comparison report push failed: {report['period']}")
            
            return success
            
        except Exception as e:
            logger.error(f"Backtest comparison report push error: {e}")
            # 推送失败不影响主流程
            return False


# 全局单例
_comparer: Optional[BacktestComparer] = None


def get_backtest_comparer(db_path: str = None) -> BacktestComparer:
    """
    获取回测对比器单例

    Args:
        db_path: 数据库路径，可选

    Returns:
        BacktestComparer 实例
    """
    global _comparer
    if _comparer is None:
        _comparer = BacktestComparer(db_path=db_path)
    return _comparer
