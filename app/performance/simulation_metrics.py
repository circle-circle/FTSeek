"""
模拟盘绩效统计模块

功能:
- 胜率计算 (胜率 = 盈利交易数 / 总交易数)
- 收益统计 (累计收益、日均收益、收益率)
- 回撤计算 (当前回撤、最大回撤)
- 夏普比率计算
- 盈亏比计算
- 交易笔数统计
- 连续盈利/亏损统计

数据源：SQLite 数据库中的模拟盘交易记录
输出：可被 dashboard 调用的统计接口
"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math

from app.storage.simulation_tables import get_simulation_database

logger = logging.getLogger("ftseek.performance.simulation_metrics")
logger.propagate = False  # 防止日志重复


@dataclass
class SimulationMetrics:
    """
    模拟盘绩效指标
    
    Attributes:
        period: 统计周期
        start_date: 开始日期
        end_date: 结束日期
        
        # 交易统计
        total_trades: 总交易数
        winning_trades: 盈利交易数
        losing_trades: 亏损交易数
        breakeven_trades: 平手交易数
        
        # 胜率指标
        win_rate: 胜率
        signal_win_rate: 信号胜率
        execution_win_rate: 执行胜率
        
        # 盈亏指标
        total_pnl: 累计盈亏
        total_profit: 总盈利
        total_loss: 总亏损
        profit_factor: 盈亏比
        avg_win: 平均盈利
        avg_loss: 平均亏损
        avg_pnl: 平均盈亏
        daily_avg_pnl: 日均盈亏
        
        # 收益率指标
        total_return: 总收益率 (%)
        daily_avg_return: 日均收益率 (%)
        annualized_return: 年化收益率 (%)
        
        # 风险指标
        max_drawdown: 最大回撤 (%)
        current_drawdown: 当前回撤 (%)
        sharpe_ratio: 夏普比率
        sortino_ratio: 索提诺比率
        calmar_ratio: 卡尔玛比率
        
        # 交易质量
        largest_win: 最大单笔盈利
        largest_loss: 最大单笔亏损
        consecutive_wins: 最大连续盈利次数
        consecutive_losses: 最大连续亏损次数
        avg_holding_period: 平均持仓周期 (天)
        
        # 账户信息
        initial_capital: 初始资金
        current_capital: 当前资金
        available_cash: 可用资金
    """
    # 周期信息
    period: str = ""
    start_date: str = ""
    end_date: str = ""
    
    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    
    # 胜率指标
    win_rate: float = 0.0
    signal_win_rate: float = 0.0
    execution_win_rate: float = 0.0
    
    # 盈亏指标
    total_pnl: float = 0.0
    total_profit: float = 0.0
    total_loss: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_pnl: float = 0.0
    daily_avg_pnl: float = 0.0
    
    # 收益率指标
    total_return: float = 0.0
    daily_avg_return: float = 0.0
    annualized_return: float = 0.0
    
    # 风险指标
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    
    # 交易质量
    largest_win: float = 0.0
    largest_loss: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    avg_holding_period: float = 0.0
    
    # 账户信息
    initial_capital: float = 0.0
    current_capital: float = 0.0
    available_cash: float = 0.0
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'period': self.period,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': f"{self.win_rate:.2%}",
            'profit_factor': f"{self.profit_factor:.2f}" if self.profit_factor != float('inf') else "∞",
            'total_pnl': f"{self.total_pnl:.2f}",
            'total_return': f"{self.total_return:.2%}",
            'max_drawdown': f"{self.max_drawdown:.2%}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'sortino_ratio': f"{self.sortino_ratio:.2f}",
            'calmar_ratio': f"{self.calmar_ratio:.2f}",
            'initial_capital': f"{self.initial_capital:.2f}",
            'current_capital': f"{self.current_capital:.2f}",
            'annualized_return': f"{self.annualized_return:.2%}",
        }
    
    def _format_profit_factor(self) -> str:
        """格式化盈亏比"""
        if self.profit_factor == float('inf'):
            return "∞"
        return f"{self.profit_factor:.2f}"
    
    def summary(self) -> str:
        """生成摘要文本"""
        return f"""
📊 模拟盘绩效报告 ({self.period})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
周期：{self.start_date} ~ {self.end_date}

交易统计:
  总交易数：{self.total_trades}
  盈利：{self.winning_trades} | 亏损：{self.losing_trades} | 平手：{self.breakeven_trades}
  胜率：{self.win_rate:.2%}
  最大连续盈利：{self.consecutive_wins} | 最大连续亏损：{self.consecutive_losses}

盈亏指标:
  累计盈亏：{self.total_pnl:+.2f}
  总收益率：{self.total_return:+.2%}
  年化收益率：{self.annualized_return:+.2%}
  日均收益：{self.daily_avg_pnl:+.2f}
  盈亏比：{self._format_profit_factor()}
  平均盈利：{self.avg_win:.2f} | 平均亏损：{self.avg_loss:.2f}
  最大盈利：{self.largest_win:.2f} | 最大亏损：{self.largest_loss:.2f}

风险指标:
  最大回撤：{self.max_drawdown:.2%}
  当前回撤：{self.current_drawdown:.2%}
  夏普比率：{self.sharpe_ratio:.2f}
  索提诺比率：{self.sortino_ratio:.2f}
  卡尔玛比率：{self.calmar_ratio:.2f}

账户状态:
  初始资金：{self.initial_capital:,.2f}
  当前资金：{self.current_capital:,.2f}
  可用资金：{self.available_cash:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


class SimulationMetricsCalculator:
    """
    模拟盘绩效计算器
    
    功能:
    - 计算各类绩效指标
    - 支持不同时间周期统计
    - 提供 dashboard 调用接口
    """
    
    def __init__(self):
        """初始化计算器"""
        self.db = get_simulation_database()
        logger.info("SimulationMetricsCalculator 初始化完成")
    
    def calculate_metrics(self, days: int = 30, 
                          start_date: str = None,
                          end_date: str = None) -> SimulationMetrics:
        """
        计算模拟盘绩效指标
        
        Args:
            days: 统计天数 (默认 30 天)
            start_date: 开始日期 (YYYY-MM-DD)，与 days 互斥
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            SimulationMetrics: 绩效指标
        """
        metrics = SimulationMetrics()
        
        try:
            # 确定时间范围
            if start_date and end_date:
                metrics.start_date = start_date
                metrics.end_date = end_date
                period_days = (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days + 1
            else:
                metrics.end_date = datetime.now().date().isoformat()
                metrics.start_date = (datetime.now() - timedelta(days=days-1)).date().isoformat()
                period_days = days
            
            metrics.period = f"{days}天" if not start_date else "custom"
            
            # 获取账户信息
            account = self.db.get_simulation_account()
            if account:
                metrics.initial_capital = account.get('initial_capital', 0)
                metrics.current_capital = account.get('current_capital', 0)
                metrics.available_cash = account.get('available_cash', 0)
            
            # 获取已平仓交易
            trades = self.db.get_simulation_trades(status='closed', days=days)
            
            if not trades:
                logger.info(f"无交易记录，返回空指标")
                return metrics
            
            # 基本统计
            metrics.total_trades = len(trades)
            
            # 分类统计
            winning = [t for t in trades if (t.get('pnl') or 0) > 0]
            losing = [t for t in trades if (t.get('pnl') or 0) < 0]
            breakeven = [t for t in trades if (t.get('pnl') or 0) == 0]
            
            metrics.winning_trades = len(winning)
            metrics.losing_trades = len(losing)
            metrics.breakeven_trades = len(breakeven)
            
            # 胜率
            if metrics.total_trades > 0:
                metrics.win_rate = metrics.winning_trades / metrics.total_trades
            
            # 盈亏统计
            metrics.total_pnl = sum(t.get('pnl') or 0 for t in trades)
            metrics.total_profit = sum(t.get('pnl') or 0 for t in winning)
            metrics.total_loss = abs(sum(t.get('pnl') or 0 for t in losing))
            
            # 盈亏比
            if metrics.total_loss > 0:
                metrics.profit_factor = metrics.total_profit / metrics.total_loss
            else:
                metrics.profit_factor = float('inf') if metrics.total_profit > 0 else 0
            
            # 平均盈亏
            metrics.avg_win = metrics.total_profit / len(winning) if winning else 0
            metrics.avg_loss = metrics.total_loss / len(losing) if losing else 0
            metrics.avg_pnl = metrics.total_pnl / metrics.total_trades
            metrics.daily_avg_pnl = metrics.total_pnl / period_days
            
            # 最大最小
            metrics.largest_win = max((t.get('pnl') or 0 for t in winning), default=0)
            metrics.largest_loss = min((t.get('pnl') or 0 for t in losing), default=0)
            
            # 连续盈亏
            metrics.consecutive_wins, metrics.consecutive_losses = \
                self._calculate_consecutive(trades)
            
            # 收益率
            if metrics.initial_capital > 0:
                metrics.total_return = metrics.total_pnl / metrics.initial_capital
                metrics.daily_avg_return = metrics.total_return / period_days
                metrics.annualized_return = metrics.daily_avg_return * 252
            
            # 回撤
            metrics.max_drawdown = self._calculate_max_drawdown(trades)
            metrics.current_drawdown = self._calculate_current_drawdown(trades)
            
            # 夏普比率
            metrics.sharpe_ratio = self._calculate_sharpe_ratio(trades)
            
            # 索提诺比率
            metrics.sortino_ratio = self._calculate_sortino_ratio(trades)
            
            # 卡尔玛比率
            if metrics.max_drawdown > 0:
                metrics.calmar_ratio = metrics.annualized_return / metrics.max_drawdown
            
            # 平均持仓周期
            metrics.avg_holding_period = self._calculate_avg_holding_period(trades)
            
            logger.info(
                f"绩效计算完成：{metrics.period} "
                f"胜率={metrics.win_rate:.2%} 盈亏比={metrics.profit_factor:.2f} "
                f"夏普={metrics.sharpe_ratio:.2f} 回撤={metrics.max_drawdown:.2%}"
            )
            
        except Exception as e:
            logger.error(f"计算绩效指标失败：{e}", exc_info=True)
            raise
        
        return metrics
    
    def _calculate_consecutive(self, trades: List[Dict]) -> Tuple[int, int]:
        """
        计算最大连续盈利/亏损次数
        
        Args:
            trades: 交易记录列表
            
        Returns:
            Tuple[int, int]: (最大连续盈利次数，最大连续亏损次数)
        """
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        
        # 按时间排序
        sorted_trades = sorted(trades, key=lambda x: x.get('entry_time', ''))
        
        for trade in sorted_trades:
            pnl = trade.get('pnl') or 0
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
        """
        计算最大回撤
        
        Args:
            trades: 交易记录列表
            
        Returns:
            float: 最大回撤比例
        """
        if not trades:
            return 0.0
        
        # 按时间排序
        sorted_trades = sorted(trades, key=lambda x: x.get('entry_time', ''))
        
        peak = 0
        max_dd = 0
        
        cumulative = 0
        for trade in sorted_trades:
            cumulative += trade.get('pnl') or 0
            if cumulative > peak:
                peak = cumulative
            if peak > 0:
                dd = (peak - cumulative) / peak
                max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _calculate_current_drawdown(self, trades: List[Dict]) -> float:
        """
        计算当前回撤
        
        Args:
            trades: 交易记录列表
            
        Returns:
            float: 当前回撤比例
        """
        if not trades:
            return 0.0
        
        sorted_trades = sorted(trades, key=lambda x: x.get('entry_time', ''))
        
        peak = 0
        cumulative = 0
        
        for trade in sorted_trades:
            cumulative += trade.get('pnl') or 0
            if cumulative > peak:
                peak = cumulative
        
        if peak > 0:
            return (peak - cumulative) / peak
        return 0.0
    
    def _calculate_sharpe_ratio(self, trades: List[Dict],
                                risk_free_rate: float = 0.0) -> float:
        """
        计算夏普比率
        
        Args:
            trades: 交易记录列表
            risk_free_rate: 无风险利率 (默认 0)
            
        Returns:
            float: 夏普比率
        """
        if len(trades) < 2:
            return 0.0
        
        # 获取收益率序列
        returns = []
        for t in trades:
            pnl_pct = t.get('pnl_percent')
            if pnl_pct is not None:
                returns.append(pnl_pct / 100)
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        # 标准差
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 0
        
        if std_dev == 0:
            return 0.0
        
        # 年化 (假设每日交易)
        annualized_return = avg_return * 252
        annualized_std = std_dev * math.sqrt(252)
        
        return (annualized_return - risk_free_rate) / annualized_std
    
    def _calculate_sortino_ratio(self, trades: List[Dict],
                                 risk_free_rate: float = 0.0) -> float:
        """
        计算索提诺比率 (只考虑下行波动)
        
        Args:
            trades: 交易记录列表
            risk_free_rate: 无风险利率 (默认 0)
            
        Returns:
            float: 索提诺比率
        """
        if len(trades) < 2:
            return 0.0
        
        # 获取收益率序列
        returns = []
        for t in trades:
            pnl_pct = t.get('pnl_percent')
            if pnl_pct is not None:
                returns.append(pnl_pct / 100)
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        # 下行标准差
        downside_returns = [r for r in returns if r < 0]
        if not downside_returns:
            return float('inf') if avg_return > 0 else 0
        
        downside_variance = sum(r ** 2 for r in downside_returns) / len(downside_returns)
        downside_std = math.sqrt(downside_variance) if downside_variance > 0 else 0
        
        if downside_std == 0:
            return 0.0
        
        # 年化
        annualized_return = avg_return * 252
        annualized_downside = downside_std * math.sqrt(252)
        
        return (annualized_return - risk_free_rate) / annualized_downside
    
    def _calculate_avg_holding_period(self, trades: List[Dict]) -> float:
        """
        计算平均持仓周期
        
        Args:
            trades: 交易记录列表
            
        Returns:
            float: 平均持仓天数
        """
        holding_periods = []
        
        for trade in trades:
            entry_time = trade.get('entry_time')
            exit_time = trade.get('exit_time')
            
            if entry_time and exit_time:
                try:
                    entry = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                    exit = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
                    days = (exit - entry).days
                    if days >= 0:
                        holding_periods.append(days)
                except Exception:
                    pass
        
        if holding_periods:
            return sum(holding_periods) / len(holding_periods)
        return 0.0
    
    def get_daily_metrics(self, date: str = None) -> SimulationMetrics:
        """
        获取单日绩效指标
        
        Args:
            date: 日期 (YYYY-MM-DD)，默认今天
            
        Returns:
            SimulationMetrics: 单日绩效指标
        """
        if date is None:
            date = datetime.now().date().isoformat()
        
        return self.calculate_metrics(
            start_date=date,
            end_date=date
        )
    
    def get_weekly_metrics(self, week_offset: int = 0) -> SimulationMetrics:
        """
        获取周度绩效指标
        
        Args:
            week_offset: 周偏移 (0=本周，1=上周)
            
        Returns:
            SimulationMetrics: 周度绩效指标
        """
        today = datetime.now().date()
        end = today - timedelta(weeks=week_offset)
        start = end - timedelta(days=6)
        
        metrics = self.calculate_metrics(
            start_date=start.isoformat(),
            end_date=end.isoformat()
        )
        metrics.period = f"Week {week_offset}"
        
        return metrics
    
    def get_monthly_metrics(self, month_offset: int = 0) -> SimulationMetrics:
        """
        获取月度绩效指标
        
        Args:
            month_offset: 月偏移 (0=本月，1=上月)
            
        Returns:
            SimulationMetrics: 月度绩效指标
        """
        today = datetime.now().date()
        
        # 计算月初
        if today.month == 1:
            start = today.replace(year=today.year - 1, month=12, day=1)
        else:
            start = today.replace(month=today.month - 1, day=1)
        
        # 减去偏移
        for _ in range(month_offset):
            if start.month == 1:
                start = start.replace(year=start.year - 1, month=12, day=1)
            else:
                start = start.replace(month=start.month - 1, day=1)
        
        # 月末
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = start.replace(month=start.month + 1, day=1) - timedelta(days=1)
        
        metrics = self.calculate_metrics(
            start_date=start.isoformat(),
            end_date=end.isoformat()
        )
        metrics.period = f"Month {month_offset}"
        
        return metrics
    
    def get_all_time_metrics(self) -> SimulationMetrics:
        """
        获取历史总绩效指标
        
        Returns:
            SimulationMetrics: 历史总绩效指标
        """
        return self.calculate_metrics(days=3650)  # 10 年
    
    def get_comparison_metrics(self, days: int = 30) -> Dict:
        """
        获取对比指标 (本期 vs 上期)
        
        Args:
            days: 统计天数
            
        Returns:
            Dict: 对比指标
        """
        # 本期
        current = self.calculate_metrics(days=days)
        
        # 上期
        previous = self.calculate_metrics(
            start_date=(datetime.now() - timedelta(days=days*2)).date().isoformat(),
            end_date=(datetime.now() - timedelta(days=days)).date().isoformat()
        )
        
        # 计算变化
        def safe_pct_change(current_val, prev_val):
            if prev_val == 0:
                return 0 if current_val == 0 else float('inf')
            return (current_val - prev_val) / abs(prev_val)
        
        return {
            'current': current.to_dict(),
            'previous': previous.to_dict(),
            'changes': {
                'win_rate': safe_pct_change(current.win_rate, previous.win_rate),
                'total_pnl': safe_pct_change(current.total_pnl, previous.total_pnl),
                'profit_factor': safe_pct_change(current.profit_factor, previous.profit_factor),
                'sharpe_ratio': safe_pct_change(current.sharpe_ratio, previous.sharpe_ratio),
                'max_drawdown': safe_pct_change(current.max_drawdown, previous.max_drawdown),
            }
        }


# 全局单例
_calculator: Optional[SimulationMetricsCalculator] = None


def get_simulation_metrics_calculator() -> SimulationMetricsCalculator:
    """获取模拟盘绩效计算器单例"""
    global _calculator
    if _calculator is None:
        _calculator = SimulationMetricsCalculator()
    return _calculator


def get_simulation_metrics(days: int = 30) -> SimulationMetrics:
    """
    便捷函数：获取模拟盘绩效指标
    
    Args:
        days: 统计天数
        
    Returns:
        SimulationMetrics: 绩效指标
    """
    calculator = get_simulation_metrics_calculator()
    return calculator.calculate_metrics(days=days)


def reset_simulation_metrics_calculator():
    """重置计算器单例 (用于测试)"""
    global _calculator
    _calculator = None
