"""
绩效统计模块

统计维度:
- 周度报告
- 月度报告

核心指标:
- 信号胜率
- 执行胜率
- 盈亏比
- 最大回撤
- 夏普比率
- 索提诺比率

目标: 模拟盘连续两个月胜率>60%
"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json

from app.storage.database import get_database

logger = logging.getLogger("ftseek.performance.stats")
logger.propagate = False  # 防止日志重复


@dataclass
class PerformanceMetrics:
    """绩效指标"""
    # 周期信息
    period: str = ""              # "weekly" | "monthly"
    start_date: str = ""
    end_date: str = ""
    
    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    
    # 胜率指标
    signal_win_rate: float = 0.0      # 信号胜率
    execution_win_rate: float = 0.0   # 执行胜率
    win_rate: float = 0.0             # 交易胜率
    
    # 盈亏指标
    total_pnl: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    profit_factor: float = 0.0        # 盈亏比
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_pnl: float = 0.0
    
    # 风险指标
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    
    # 交易质量
    largest_win: float = 0.0
    largest_loss: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    avg_holding_period: float = 0.0
    
    # 目标追踪
    target_win_rate: float = 0.60     # 60% 目标
    above_target: bool = False
    months_above_target: int = 0      # 连续达标月数
    
    def to_dict(self) -> Dict:
        return {
            'period': self.period,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': f"{self.win_rate:.2%}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'total_pnl': f"{self.total_pnl:.2f}",
            'max_drawdown': f"{self.max_drawdown:.2%}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'above_target': self.above_target,
        }
    
    def summary(self) -> str:
        """生成摘要文本"""
        status = "✅" if self.above_target else "❌"
        return f"""
{status} {self.period.upper()} 绩效报告 ({self.start_date} ~ {self.end_date})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
交易统计:
  总交易数：{self.total_trades}
  盈利：{self.winning_trades} | 亏损：{self.losing_trades}
  胜率：{self.win_rate:.2%} (目标：{self.target_win_rate:.0%})

盈亏指标:
  总盈亏：{self.total_pnl:.2f}
  盈亏比：{self.profit_factor:.2f}
  平均盈利：{self.avg_win:.2f} | 平均亏损：{self.avg_loss:.2f}

风险指标:
  最大回撤：{self.max_drawdown:.2%}
  夏普比率：{self.sharpe_ratio:.2f}

目标追踪:
  达标状态：{'是' if self.above_target else '否'}
  连续达标月数：{self.months_above_target}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


class PerformanceCalculator:
    """绩效计算器"""
    
    def __init__(self):
        self.db = get_database()
    
    def calculate_period_metrics(self, start_date: str, 
                                 end_date: str,
                                 period: str = "custom") -> PerformanceMetrics:
        """
        计算指定时间段的绩效指标
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            period: 周期类型 ("weekly" | "monthly" | "custom")
            
        Returns:
            PerformanceMetrics
        """
        metrics = PerformanceMetrics(
            period=period,
            start_date=start_date,
            end_date=end_date
        )
        
        # 获取交易记录
        trades = self.db.get_trades_by_period(
            f"{start_date} 00:00:00", 
            f"{end_date} 23:59:59"
        )
        
        if not trades:
            return metrics
        
        # 只统计已平仓交易
        closed_trades = [t for t in trades if t.get('status') == 'closed']
        
        if not closed_trades:
            return metrics
        
        metrics.total_trades = len(closed_trades)
        
        # 分类统计
        winning = [t for t in closed_trades if t.get('pnl', 0) > 0]
        losing = [t for t in closed_trades if t.get('pnl', 0) < 0]
        breakeven = [t for t in closed_trades if t.get('pnl', 0) == 0]
        
        metrics.winning_trades = len(winning)
        metrics.losing_trades = len(losing)
        metrics.breakeven_trades = len(breakeven)
        
        # 胜率
        if metrics.total_trades > 0:
            metrics.win_rate = metrics.winning_trades / metrics.total_trades
        
        # 盈亏统计
        metrics.total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
        metrics.total_profit = sum(t.get('pnl', 0) for t in winning)
        metrics.total_loss = abs(sum(t.get('pnl', 0) for t in losing))
        
        # 盈亏比
        if metrics.total_loss > 0:
            metrics.profit_factor = metrics.total_profit / metrics.total_loss
        else:
            metrics.profit_factor = float('inf') if metrics.total_profit > 0 else 0
        
        # 平均盈亏
        metrics.avg_win = metrics.total_profit / len(winning) if winning else 0
        metrics.avg_loss = metrics.total_loss / len(losing) if losing else 0
        metrics.avg_pnl = metrics.total_pnl / metrics.total_trades
        
        # 最大最小
        metrics.largest_win = max((t.get('pnl', 0) for t in winning), default=0)
        metrics.largest_loss = min((t.get('pnl', 0) for t in losing), default=0)
        
        # 连续盈亏
        metrics.consecutive_wins, metrics.consecutive_losses = \
            self._calculate_consecutive(closed_trades)
        
        # 最大回撤 (简化计算)
        metrics.max_drawdown = self._calculate_max_drawdown(closed_trades)
        
        # 夏普比率 (简化，假设无风险利率为 0)
        metrics.sharpe_ratio = self._calculate_sharpe_ratio(closed_trades)
        
        # 索提诺比率
        metrics.sortino_ratio = self._calculate_sortino_ratio(closed_trades)
        
        # 目标检查
        metrics.above_target = metrics.win_rate >= metrics.target_win_rate
        
        logger.info(f"绩效计算完成：{period} {start_date}~{end_date} "
                   f"胜率={metrics.win_rate:.2%} 盈亏比={metrics.profit_factor:.2f}")
        
        return metrics
    
    def _calculate_consecutive(self, trades: List[Dict]) -> Tuple[int, int]:
        """计算最大连续盈利/亏损"""
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        
        for trade in sorted(trades, key=lambda x: x.get('entry_time', '')):
            pnl = trade.get('pnl', 0)
            if pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            elif pnl < 0:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
            else:
                current_wins = 0
                current_losses = 0
        
        return max_wins, max_losses
    
    def _calculate_max_drawdown(self, trades: List[Dict]) -> float:
        """计算最大回撤"""
        if not trades:
            return 0.0
        
        # 按时间排序
        sorted_trades = sorted(trades, key=lambda x: x.get('entry_time', ''))
        
        peak = 0
        max_dd = 0
        
        cumulative = 0
        for trade in sorted_trades:
            cumulative += trade.get('pnl', 0)
            if cumulative > peak:
                peak = cumulative
            if peak > 0:
                dd = (peak - cumulative) / peak
                max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _calculate_sharpe_ratio(self, trades: List[Dict], 
                               risk_free_rate: float = 0.0) -> float:
        """计算夏普比率"""
        if len(trades) < 2:
            return 0.0
        
        returns = [t.get('pnl_percent', 0) / 100 for t in trades if t.get('pnl_percent')]
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        # 标准差
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = variance ** 0.5
        
        if std_dev == 0:
            return 0.0
        
        # 年化 (假设每日交易)
        annualized_return = avg_return * 252
        annualized_std = std_dev * (252 ** 0.5)
        
        return (annualized_return - risk_free_rate) / annualized_std
    
    def _calculate_sortino_ratio(self, trades: List[Dict],
                                risk_free_rate: float = 0.0) -> float:
        """计算索提诺比率 (只考虑下行波动)"""
        if len(trades) < 2:
            return 0.0
        
        returns = [t.get('pnl_percent', 0) / 100 for t in trades if t.get('pnl_percent')]
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        # 下行标准差
        downside_returns = [r for r in returns if r < 0]
        if not downside_returns:
            return float('inf') if avg_return > 0 else 0
        
        downside_variance = sum(r ** 2 for r in downside_returns) / len(downside_returns)
        downside_std = downside_variance ** 0.5
        
        if downside_std == 0:
            return 0.0
        
        # 年化
        annualized_return = avg_return * 252
        annualized_downside = downside_std * (252 ** 0.5)
        
        return (annualized_return - risk_free_rate) / annualized_downside
    
    def get_weekly_periods(self, weeks: int = 12) -> List[Tuple[str, str]]:
        """获取最近 N 周的日期范围"""
        periods = []
        today = datetime.now().date()
        
        for i in range(weeks):
            end = today - timedelta(weeks=i)
            start = end - timedelta(days=6)
            periods.append((
                start.strftime('%Y-%m-%d'),
                end.strftime('%Y-%m-%d')
            ))
        
        return periods
    
    def get_monthly_periods(self, months: int = 6) -> List[Tuple[str, str]]:
        """获取最近 N 个月的日期范围"""
        periods = []
        today = datetime.now().date()
        
        for i in range(months):
            # 月末
            if today.month == 1:
                end = today.replace(year=today.year - 1, month=12, day=28)
            else:
                end = today.replace(month=today.month - 1, day=28)
            
            # 月初
            if end.month == 1:
                start = end.replace(year=end.year - 1, month=12, day=1)
            else:
                start = end.replace(month=end.month - 1, day=1)
            
            periods.append((
                start.strftime('%Y-%m-%d'),
                end.strftime('%Y-%m-%d')
            ))
        
        return periods
    
    def save_snapshot(self, metrics: PerformanceMetrics) -> bool:
        """保存绩效快照到数据库"""
        snapshot = {
            'id': hashlib.md5(
                f"{metrics.period}{metrics.start_date}{metrics.end_date}".encode()
            ).hexdigest()[:16],
            'period': metrics.period,
            'start_date': metrics.start_date,
            'end_date': metrics.end_date,
            'total_trades': metrics.total_trades,
            'winning_trades': metrics.winning_trades,
            'losing_trades': metrics.losing_trades,
            'win_rate': metrics.win_rate,
            'profit_factor': metrics.profit_factor,
            'max_drawdown': metrics.max_drawdown,
            'sharpe_ratio': metrics.sharpe_ratio,
            'sortino_ratio': metrics.sortino_ratio,
            'total_return': metrics.total_pnl,
            'avg_win': metrics.avg_win,
            'avg_loss': metrics.avg_loss
        }
        return self.db.save_performance_snapshot(snapshot)


class PerformanceReporter:
    """绩效报告生成器"""
    
    def __init__(self):
        self.calculator = PerformanceCalculator()
        self.db = get_database()
    
    def generate_weekly_report(self, week_offset: int = 0) -> PerformanceMetrics:
        """
        生成周度报告
        
        Args:
            week_offset: 周偏移 (0=本周，1=上周，以此类推)
        """
        today = datetime.now().date()
        end = today - timedelta(weeks=week_offset)
        start = end - timedelta(days=6)
        
        metrics = self.calculator.calculate_period_metrics(
            start.strftime('%Y-%m-%d'),
            end.strftime('%Y-%m-%d'),
            period='weekly'
        )
        
        self.calculator.save_snapshot(metrics)
        return metrics
    
    def generate_monthly_report(self, month_offset: int = 0) -> PerformanceMetrics:
        """
        生成月度报告
        
        Args:
            month_offset: 月偏移 (0=本月，1=上月，以此类推)
        """
        today = datetime.now().date()
        
        # 计算上月末
        if today.month == 1:
            end = today.replace(year=today.year - 1, month=12, day=28)
        else:
            end = today.replace(month=today.month - 1, day=28)
        
        # 减去偏移
        for _ in range(month_offset):
            if end.month == 1:
                end = end.replace(year=end.year - 1, month=12, day=28)
            else:
                end = end.replace(month=end.month - 1, day=28)
        
        # 月初
        if end.month == 1:
            start = end.replace(year=end.year - 1, month=12, day=1)
        else:
            start = end.replace(month=end.month - 1, day=1)
        
        metrics = self.calculator.calculate_period_metrics(
            start.strftime('%Y-%m-%d'),
            end.strftime('%Y-%m-%d'),
            period='monthly'
        )
        
        self.calculator.save_snapshot(metrics)
        return metrics
    
    def get_target_tracking(self) -> Dict:
        """
        获取目标追踪状态
        
        Returns:
            {
                'current_streak': 连续达标月数,
                'last_6_months': [月胜率列表],
                'target': 0.60,
                'on_track': bool
            }
        """
        snapshots = self.db.get_performance_snapshots(period='monthly', limit=6)
        
        if not snapshots:
            return {
                'current_streak': 0,
                'last_6_months': [],
                'target': 0.60,
                'on_track': False
            }
        
        # 计算连续达标月数
        streak = 0
        win_rates = []
        
        for snap in snapshots:
            win_rate = snap.get('win_rate', 0)
            win_rates.append({
                'period': f"{snap.get('start_date')}~{snap.get('end_date')}",
                'win_rate': win_rate,
                'above_target': win_rate >= 0.60
            })
            
            if win_rate >= 0.60:
                streak += 1
            else:
                break
        
        return {
            'current_streak': streak,
            'last_6_months': win_rates,
            'target': 0.60,
            'on_track': streak >= 2
        }
    
    def generate_summary_report(self) -> str:
        """生成汇总报告文本"""
        tracking = self.get_target_tracking()
        
        lines = [
            "📊 绩效目标追踪报告",
            "=" * 40,
            f"目标胜率：{tracking['target']:.0%}",
            f"当前连续达标月数：{tracking['current_streak']}",
            f"达标状态：{'✅ 进行中' if tracking['on_track'] else '❌ 未达标'}",
            "",
            "最近 6 个月:",
        ]
        
        for month in tracking['last_6_months']:
            status = "✅" if month['above_target'] else "❌"
            lines.append(f"  {status} {month['period']}: {month['win_rate']:.2%}")
        
        return "\n".join(lines)


# 全局单例
_reporter: Optional[PerformanceReporter] = None


def get_performance_reporter() -> PerformanceReporter:
    """获取绩效报告器单例"""
    global _reporter
    if _reporter is None:
        _reporter = PerformanceReporter()
    return _reporter
