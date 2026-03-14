"""
FTSeek 月报生成器

生成月度绩效报告，包含:
- 月度交易汇总
- 收益率曲线
- 最大回撤
- 夏普比率/索提诺比率
- 策略归因分析
- 下月计划

推送时间：每月 1 日 09:00
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from app.storage.database import get_database
from app.risk.metrics_calculator import MetricsCalculator

logger = logging.getLogger("ftseek.reporters.monthly")


@dataclass
class MonthlyReport:
    """月报数据结构"""
    # 周期信息
    month: str = ""                   # 月份 (2026-03)
    start_date: str = ""              # 开始日期
    end_date: str = ""                # 结束日期
    trading_days: int = 0             # 交易天数
    
    # 交易汇总
    total_trades: int = 0             # 总交易次数
    wins: int = 0                     # 盈利交易数
    losses: int = 0                   # 亏损交易数
    win_rate: float = 0.0             # 胜率
    profit_loss_ratio: float = 0.0    # 盈亏比
    
    # 收益指标
    pnl: float = 0.0                  # 总盈亏 (港币)
    pnl_pct: float = 0.0              # 收益率 (%)
    initial_capital: float = 0.0      # 期初资金
    final_capital: float = 0.0        # 期末资金
    
    # 风险指标
    max_drawdown: float = 0.0         # 最大回撤 (%)
    sharpe_ratio: float = 0.0         # 夏普比率
    sortino_ratio: float = 0.0        # 索提诺比率
    volatility: float = 0.0           # 波动率 (%)
    
    # 收益率曲线数据
    equity_curve: List[float] = field(default_factory=list)
    curve_dates: List[str] = field(default_factory=list)
    
    # 策略归因
    strategies: Dict[str, Dict] = field(default_factory=dict)
    
    # 计划
    plan: str = "继续优化策略，控制风险"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "month": self.month,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "trading_days": self.trading_days,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "profit_loss_ratio": self.profit_loss_ratio,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio,
            "volatility": self.volatility,
            "strategies": self.strategies,
            "plan": self.plan,
        }


class MonthlyReportGenerator:
    """
    月报生成器
    
    从数据库获取交易数据，生成月度绩效报告
    """
    
    def __init__(self):
        self.db = get_database()
        self.metrics_calc = MetricsCalculator()
        self.initial_capital = 1_000_000  # 100 万港币
    
    def get_month_range(self, date: datetime = None) -> tuple:
        """
        获取指定日期所在月的起止日期
        
        Args:
            date: 指定日期，默认今天
            
        Returns:
            (start_date, end_date) 元组
        """
        if date is None:
            date = datetime.now()
        
        # 当月 1 日
        first_day = date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # 下月 1 日 - 1 秒 = 当月最后一天 23:59:59
        if date.month == 12:
            next_month = date.replace(year=date.year + 1, month=1, day=1)
        else:
            next_month = date.replace(month=date.month + 1, day=1)
        
        last_day = next_month - timedelta(seconds=1)
        
        return first_day, last_day
    
    def get_previous_month_range(self) -> tuple:
        """
        获取上一月的起止日期
        
        Returns:
            (start_date, end_date) 元组
        """
        # 本月 1 日
        today = datetime.now()
        first_day_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # 上月最后一天 = 本月 1 日 - 1 秒
        last_day_prev_month = first_day_this_month - timedelta(seconds=1)
        
        # 上月 1 日
        if last_day_prev_month.month == 12:
            first_day_prev_month = last_day_prev_month.replace(
                year=last_day_prev_month.year - 1, 
                month=1, 
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            first_day_prev_month = last_day_prev_month.replace(
                day=1, 
                hour=0, minute=0, second=0, microsecond=0
            )
        
        return first_day_prev_month, last_day_prev_month
    
    def generate(self, date: datetime = None) -> MonthlyReport:
        """
        生成月报
        
        Args:
            date: 指定日期，默认今天 (用于生成上一月的报告)
            
        Returns:
            MonthlyReport 对象
        """
        # 获取上一月的日期范围
        start_dt, end_dt = self.get_previous_month_range()
        start_date = start_dt.isoformat()
        end_date = end_dt.isoformat()
        
        # 获取月份字符串
        month_str = start_dt.strftime("%Y-%m")
        
        logger.info(f"生成月报：{month_str} ({start_dt.date()} ~ {end_dt.date()})")
        
        # 获取交易数据
        trades = self.db.get_trades_by_period(start_date, end_date)
        closed_trades = [t for t in trades if t.get('status') == 'closed']
        
        # 初始化报告
        report = MonthlyReport(
            month=month_str,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=end_dt.strftime("%Y-%m-%d"),
            initial_capital=self.initial_capital,
        )
        
        if len(closed_trades) == 0:
            report.plan = "上月无交易，本月关注市场机会"
            return report
        
        # 计算基础统计
        report.total_trades = len(closed_trades)
        report.wins = sum(1 for t in closed_trades if t.get('pnl', 0) > 0)
        report.losses = sum(1 for t in closed_trades if t.get('pnl', 0) < 0)
        report.win_rate = report.wins / report.total_trades if report.total_trades > 0 else 0
        report.pnl = sum(t.get('pnl', 0) for t in closed_trades)
        report.final_capital = self.initial_capital + report.pnl
        report.pnl_pct = (report.pnl / self.initial_capital) * 100
        
        # 计算盈亏比
        winning_trades = [t for t in closed_trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in closed_trades if t.get('pnl', 0) < 0]
        
        if winning_trades and losing_trades:
            avg_win = sum(t['pnl'] for t in winning_trades) / len(winning_trades)
            avg_loss = abs(sum(t['pnl'] for t in losing_trades) / len(losing_trades))
            report.profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        elif winning_trades:
            report.profit_loss_ratio = float('inf')
        else:
            report.profit_loss_ratio = 0
        
        # 构建资金曲线和计算风险指标
        report.equity_curve, report.curve_dates = self._build_equity_curve(closed_trades)
        
        if report.equity_curve:
            # 最大回撤
            report.max_drawdown = self.metrics_calc.max_drawdown(report.equity_curve) * 100
            
            # 夏普比率
            report.sharpe_ratio = self.metrics_calc.sharpe_ratio(equity_curve=report.equity_curve)
            
            # 索提诺比率
            report.sortino_ratio = self.metrics_calc.sortino_ratio(equity_curve=report.equity_curve)
            
            # 波动率
            report.volatility = self.metrics_calc.volatility(equity_curve=report.equity_curve) * 100
        
        # 计算交易天数
        trade_dates = set()
        for trade in closed_trades:
            entry_time = trade.get('entry_time', '')
            if entry_time:
                trade_date = entry_time[:10]  # YYYY-MM-DD
                trade_dates.add(trade_date)
        report.trading_days = len(trade_dates)
        
        # 策略归因分析
        report.strategies = self._analyze_strategies(closed_trades)
        
        # 生成下月计划
        report.plan = self._generate_plan(report)
        
        logger.info(f"月报生成完成：{report.total_trades} 笔交易，收益率 {report.pnl_pct:.2f}%")
        
        return report
    
    def _build_equity_curve(self, trades: List[Dict]) -> Tuple[List[float], List[str]]:
        """
        构建资金曲线
        
        Args:
            trades: 交易列表
            
        Returns:
            (equity_curve, dates) 元组
        """
        if not trades:
            return [], []
        
        # 按退出时间排序
        sorted_trades = sorted(trades, key=lambda t: t.get('exit_time', '') or t.get('entry_time', ''))
        
        equity = self.initial_capital
        equity_curve = [equity]
        dates = [sorted_trades[0].get('entry_time', '')[:10] if sorted_trades else '']
        
        for trade in sorted_trades:
            equity += trade.get('pnl', 0)
            equity_curve.append(equity)
            exit_time = trade.get('exit_time', '') or trade.get('entry_time', '')
            dates.append(exit_time[:10] if exit_time else '')
        
        return equity_curve, dates
    
    def _analyze_strategies(self, trades: List[Dict]) -> Dict[str, Dict]:
        """
        策略归因分析
        
        Args:
            trades: 交易列表
            
        Returns:
            {strategy_name: {count, wins, losses, pnl, win_rate, contribution}, ...}
        """
        strategies = {}
        total_pnl = sum(t.get('pnl', 0) for t in trades)
        
        for trade in trades:
            strategy = trade.get('strategy', 'unknown')
            
            if strategy not in strategies:
                strategies[strategy] = {
                    'count': 0,
                    'wins': 0,
                    'losses': 0,
                    'pnl': 0,
                }
            
            strategies[strategy]['count'] += 1
            strategies[strategy]['pnl'] += trade.get('pnl', 0)
            
            if trade.get('pnl', 0) > 0:
                strategies[strategy]['wins'] += 1
            elif trade.get('pnl', 0) < 0:
                strategies[strategy]['losses'] += 1
        
        # 计算胜率和贡献度
        for strategy, data in strategies.items():
            total = data['wins'] + data['losses']
            data['win_rate'] = data['wins'] / total if total > 0 else 0
            
            # 策略对总收益的贡献度
            if total_pnl != 0:
                data['contribution'] = data['pnl'] / total_pnl
            else:
                data['contribution'] = 0
        
        return strategies
    
    def _generate_plan(self, report: MonthlyReport) -> str:
        """
        生成下月计划
        
        Args:
            report: 月报数据
            
        Returns:
            计划文本
        """
        if report.total_trades == 0:
            return "上月无交易，本月关注市场机会"
        
        # 基于月度表现给出建议
        if report.pnl > 0 and report.win_rate >= 0.6:
            if report.sharpe_ratio >= 1.5:
                return "本月表现优异，夏普比率良好，继续执行当前策略"
            else:
                return "本月盈利但波动较大，下月注意风险控制"
        elif report.pnl > 0 and report.win_rate < 0.6:
            return "本月盈利但胜率偏低，下月优化入场信号质量"
        elif report.pnl < 0 and report.win_rate >= 0.5:
            return "本月胜率尚可但亏损，注意止盈止损策略"
        elif report.pnl < 0 and report.win_rate < 0.5:
            if report.max_drawdown > 10:
                return "本月表现不佳且回撤较大，建议降低仓位，复盘策略"
            else:
                return "本月表现不佳，建议复盘策略，谨慎交易"
        else:
            return "继续优化策略，控制风险"


# 全局单例
_reporter: Optional[MonthlyReportGenerator] = None


def get_reporter() -> MonthlyReportGenerator:
    """获取月报生成器单例"""
    global _reporter
    if _reporter is None:
        _reporter = MonthlyReportGenerator()
    return _reporter


def generate_monthly_report(date: datetime = None) -> MonthlyReport:
    """
    生成月报 (便捷函数)
    
    Args:
        date: 指定日期，默认今天
        
    Returns:
        MonthlyReport 对象
    """
    reporter = get_reporter()
    return reporter.generate(date)
