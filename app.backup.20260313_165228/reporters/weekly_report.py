"""
FTSeek 周报生成器

生成周度绩效报告，包含:
- 本周交易汇总
- 胜率 / 盈亏比
- 最佳/最差交易
- 各策略表现
- 下周展望

推送时间：周一 09:00
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from app.storage.database import get_database
from app.risk.metrics_calculator import MetricsCalculator

logger = logging.getLogger("ftseek.reporters.weekly")


@dataclass
class WeeklyReport:
    """周报数据结构"""
    # 周期信息
    week: str = ""                    # 周数 (2026-W09)
    start_date: str = ""              # 开始日期
    end_date: str = ""                # 结束日期
    
    # 交易汇总
    total_trades: int = 0             # 总交易次数
    wins: int = 0                     # 盈利交易数
    losses: int = 0                   # 亏损交易数
    win_rate: float = 0.0             # 胜率
    profit_loss_ratio: float = 0.0    # 盈亏比
    
    # 收益指标
    pnl: float = 0.0                  # 总盈亏 (港币)
    pnl_pct: float = 0.0              # 收益率 (%)
    max_drawdown: float = 0.0         # 最大回撤 (%)
    
    # 策略表现
    strategies: Dict[str, Dict] = field(default_factory=dict)
    
    # 最佳/最差交易
    best_trade: Dict[str, Any] = field(default_factory=dict)
    worst_trade: Dict[str, Any] = field(default_factory=dict)
    
    # 展望
    outlook: str = "关注市场动态，谨慎交易"
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "week": self.week,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "profit_loss_ratio": self.profit_loss_ratio,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "max_drawdown": self.max_drawdown,
            "strategies": self.strategies,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "outlook": self.outlook,
        }


class WeeklyReportGenerator:
    """
    周报生成器
    
    从数据库获取交易数据，生成周度绩效报告
    """
    
    def __init__(self):
        self.db = get_database()
        self.metrics_calc = MetricsCalculator()
    
    def get_week_range(self, date: datetime = None) -> tuple:
        """
        获取指定日期所在周的起止日期 (周一到周日)
        
        Args:
            date: 指定日期，默认今天
            
        Returns:
            (start_date, end_date) 元组
        """
        if date is None:
            date = datetime.now()
        
        # 获取周一 (weekday() 返回 0=周一, 6=周日)
        monday = date - timedelta(days=date.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 获取周日
        sunday = monday + timedelta(days=6)
        sunday = sunday.replace(hour=23, minute=59, second=59)
        
        return monday, sunday
    
    def get_previous_week_range(self) -> tuple:
        """
        获取上一周的起止日期
        
        Returns:
            (start_date, end_date) 元组
        """
        # 获取本周一
        today = datetime.now()
        this_monday = today - timedelta(days=today.weekday())
        
        # 上周一 = 本周一 - 7 天
        last_monday = this_monday - timedelta(days=7)
        last_monday = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 上周日 = 上周一 + 6 天
        last_sunday = last_monday + timedelta(days=6)
        last_sunday = last_sunday.replace(hour=23, minute=59, second=59)
        
        return last_monday, last_sunday
    
    def generate(self, date: datetime = None) -> WeeklyReport:
        """
        生成周报
        
        Args:
            date: 指定日期，默认今天 (用于生成上一周的报告)
            
        Returns:
            WeeklyReport 对象
        """
        # 获取上一周的日期范围
        start_dt, end_dt = self.get_previous_week_range()
        start_date = start_dt.isoformat()
        end_date = end_dt.isoformat()
        
        # 获取周数
        week_num = start_dt.isocalendar()[1]
        week_str = f"{start_dt.year}-W{week_num:02d}"
        
        logger.info(f"生成周报：{week_str} ({start_dt.date()} ~ {end_dt.date()})")
        
        # 获取交易数据
        trades = self.db.get_trades_by_period(start_date, end_date)
        closed_trades = [t for t in trades if t.get('status') == 'closed']
        
        # 初始化报告
        report = WeeklyReport(
            week=week_str,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=end_dt.strftime("%Y-%m-%d"),
        )
        
        if len(closed_trades) == 0:
            report.outlook = "上周无交易，本周关注市场机会"
            return report
        
        # 计算基础统计
        report.total_trades = len(closed_trades)
        report.wins = sum(1 for t in closed_trades if t.get('pnl', 0) > 0)
        report.losses = sum(1 for t in closed_trades if t.get('pnl', 0) < 0)
        report.win_rate = report.wins / report.total_trades if report.total_trades > 0 else 0
        report.pnl = sum(t.get('pnl', 0) for t in closed_trades)
        
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
        
        # 计算收益率 (基于初始资金 100 万)
        initial_capital = 1_000_000
        report.pnl_pct = (report.pnl / initial_capital) * 100
        
        # 计算最大回撤
        if closed_trades:
            # 按时间排序
            sorted_trades = sorted(closed_trades, key=lambda t: t.get('entry_time', ''))
            
            # 构建资金曲线
            equity = initial_capital
            equity_curve = [equity]
            for trade in sorted_trades:
                equity += trade.get('pnl', 0)
                equity_curve.append(equity)
            
            report.max_drawdown = self.metrics_calc.max_drawdown(equity_curve) * 100
        
        # 策略表现分析
        report.strategies = self._analyze_strategies(closed_trades)
        
        # 最佳/最差交易
        report.best_trade, report.worst_trade = self._find_extreme_trades(closed_trades)
        
        # 生成下周展望
        report.outlook = self._generate_outlook(report)
        
        logger.info(f"周报生成完成：{report.total_trades} 笔交易，胜率 {report.win_rate:.1%}")
        
        return report
    
    def _analyze_strategies(self, trades: List[Dict]) -> Dict[str, Dict]:
        """
        分析各策略表现
        
        Args:
            trades: 交易列表
            
        Returns:
            {strategy_name: {count, wins, win_rate, pnl}, ...}
        """
        strategies = {}
        
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
        
        # 计算胜率
        for strategy, data in strategies.items():
            total = data['wins'] + data['losses']
            data['win_rate'] = data['wins'] / total if total > 0 else 0
        
        return strategies
    
    def _find_extreme_trades(self, trades: List[Dict]) -> tuple:
        """
        找出最佳和最差交易
        
        Args:
            trades: 交易列表
            
        Returns:
            (best_trade, worst_trade) 元组
        """
        if not trades:
            return {}, {}
        
        # 按 pnl_percent 排序
        sorted_by_pct = sorted(trades, key=lambda t: t.get('pnl_percent', 0) or 0)
        
        worst = sorted_by_pct[0]
        best = sorted_by_pct[-1]
        
        best_trade = {
            'code': best.get('code', ''),
            'name': best.get('name', ''),
            'pnl': best.get('pnl', 0),
            'pnl_pct': best.get('pnl_percent', 0),
            'action': best.get('action', ''),
            'exit_time': best.get('exit_time', ''),
        }
        
        worst_trade = {
            'code': worst.get('code', ''),
            'name': worst.get('name', ''),
            'pnl': worst.get('pnl', 0),
            'pnl_pct': worst.get('pnl_percent', 0),
            'action': worst.get('action', ''),
            'exit_time': worst.get('exit_time', ''),
        }
        
        return best_trade, worst_trade
    
    def _generate_outlook(self, report: WeeklyReport) -> str:
        """
        生成下周展望
        
        Args:
            report: 周报数据
            
        Returns:
            展望文本
        """
        # 基于本周表现给出建议
        if report.total_trades == 0:
            return "上周无交易，本周关注市场机会"
        
        if report.pnl > 0 and report.win_rate >= 0.6:
            return "本周表现优异，继续保持当前策略，注意止盈止损"
        elif report.pnl > 0 and report.win_rate < 0.6:
            return "本周盈利但胜率偏低，建议优化入场时机"
        elif report.pnl < 0 and report.win_rate >= 0.5:
            return "本周胜率尚可但亏损，注意单笔亏损控制"
        elif report.pnl < 0 and report.win_rate < 0.5:
            return "本周表现不佳，建议复盘策略，谨慎交易"
        else:
            return "关注市场动态，谨慎交易"


# 全局单例
_reporter: Optional[WeeklyReportGenerator] = None


def get_reporter() -> WeeklyReportGenerator:
    """获取周报生成器单例"""
    global _reporter
    if _reporter is None:
        _reporter = WeeklyReportGenerator()
    return _reporter


def generate_weekly_report(date: datetime = None) -> WeeklyReport:
    """
    生成周报 (便捷函数)
    
    Args:
        date: 指定日期，默认今天
        
    Returns:
        WeeklyReport 对象
    """
    reporter = get_reporter()
    return reporter.generate(date)
