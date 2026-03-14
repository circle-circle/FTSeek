"""
专业量化指标计算器 (Metrics Calculator)

提供夏普比率、索提诺比率、VaR、CVaR 等专业量化指标计算
"""
import numpy as np
import logging
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.metrics")


@dataclass
class PerformanceMetrics:
    """绩效指标"""
    # 收益指标
    total_return: float = 0.0           # 总收益率
    annualized_return: float = 0.0      # 年化收益率
    final_capital: float = 0.0          # 最终资金
    
    # 风险调整收益指标
    sharpe_ratio: float = 0.0           # 夏普比率
    sortino_ratio: float = 0.0          # 索提诺比率
    calmar_ratio: float = 0.0           # 卡玛比率
    information_ratio: float = 0.0      # 信息比率
    
    # 风险指标
    max_drawdown: float = 0.0           # 最大回撤
    current_drawdown: float = 0.0       # 当前回撤
    var_95: float = 0.0                 # 95% VaR
    cvar_95: float = 0.0                # 95% CVaR (条件 VaR)
    volatility: float = 0.0             # 年化波动率
    downside_volatility: float = 0.0    # 下行波动率
    
    # 交易质量指标
    win_rate: float = 0.0               # 胜率
    profit_factor: float = 0.0          # 盈利因子
    avg_win: float = 0.0                # 平均盈利
    avg_loss: float = 0.0               # 平均亏损
    profit_loss_ratio: float = 0.0      # 盈亏比
    expectancy: float = 0.0             # 期望值
    
    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_trade_duration: float = 0.0     # 平均持仓时间 (天)
    
    # 恢复能力
    recovery_time: int = 0              # 回撤恢复时间 (天)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            # 收益指标
            "total_return": f"{self.total_return:.2%}",
            "annualized_return": f"{self.annualized_return:.2%}",
            "final_capital": f"{self.final_capital:,.2f}",
            
            # 风险调整收益
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "sortino_ratio": round(self.sortino_ratio, 2),
            "calmar_ratio": round(self.calmar_ratio, 2),
            "information_ratio": round(self.information_ratio, 2),
            
            # 风险指标
            "max_drawdown": f"{self.max_drawdown:.2%}",
            "current_drawdown": f"{self.current_drawdown:.2%}",
            "var_95": f"{self.var_95:.2%}",
            "cvar_95": f"{self.cvar_95:.2%}",
            "volatility": f"{self.volatility:.2%}",
            "downside_volatility": f"{self.downside_volatility:.2%}",
            
            # 交易质量
            "win_rate": f"{self.win_rate:.2%}",
            "profit_factor": round(self.profit_factor, 2),
            "avg_win": f"{self.avg_win:.2f}",
            "avg_loss": f"{self.avg_loss:.2f}",
            "profit_loss_ratio": round(self.profit_loss_ratio, 2),
            "expectancy": f"{self.expectancy:.4f}",
            
            # 交易统计
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
            "avg_trade_duration": f"{self.avg_trade_duration:.1f}",
            
            # 恢复能力
            "recovery_time": self.recovery_time,
        }
    
    def summary(self) -> str:
        """简洁摘要"""
        return (
            f"总收益：{self.total_return:.2%} | "
            f"夏普：{self.sharpe_ratio:.2f} | "
            f"索提诺：{self.sortino_ratio:.2f} | "
            f"最大回撤：{self.max_drawdown:.2%} | "
            f"胜率：{self.win_rate:.2%} | "
            f"盈利因子：{self.profit_factor:.2f}"
        )


@dataclass
class PortfolioRiskMetrics:
    """组合风险指标"""
    beta: float = 0.0                   # Beta 系数
    correlation_matrix: Dict = field(default_factory=dict)  # 相关性矩阵
    concentration_top3: float = 0.0     # 前 3 大持仓集中度
    concentration_top5: float = 0.0     # 前 5 大持仓集中度
    liquidity_ratio: float = 0.0        # 流动性比率
    leverage: float = 1.0               # 杠杆率
    
    def to_dict(self) -> dict:
        return {
            "beta": round(self.beta, 2),
            "correlation_matrix": self.correlation_matrix,
            "concentration_top3": f"{self.concentration_top3:.1%}",
            "concentration_top5": f"{self.concentration_top5:.1%}",
            "liquidity_ratio": f"{self.liquidity_ratio:.1%}",
            "leverage": round(self.leverage, 2),
        }


class MetricsCalculator:
    """
    量化指标计算器
    
    提供完整的绩效和风险评估指标
    """
    
    def __init__(self, risk_free_rate: float = 0.02, trading_days: int = 252):
        """
        初始化
        
        Args:
            risk_free_rate: 无风险利率 (年化)
            trading_days: 年交易天数
        """
        self.risk_free_rate = risk_free_rate
        self.trading_days = trading_days
        self.daily_rf_rate = risk_free_rate / trading_days
    
    # ==================== 收益指标 ====================
    
    def total_return(self, equity_curve: List[float]) -> float:
        """
        计算总收益率
        
        Args:
            equity_curve: 资金曲线列表
            
        Returns:
            总收益率
        """
        if len(equity_curve) < 2:
            return 0.0
        return (equity_curve[-1] - equity_curve[0]) / equity_curve[0]
    
    def annualized_return(self, equity_curve: List[float], days: int = None) -> float:
        """
        计算年化收益率
        
        Args:
            equity_curve: 资金曲线列表
            days: 总天数 (默认使用曲线长度)
            
        Returns:
            年化收益率
        """
        if len(equity_curve) < 2:
            return 0.0
        
        total_ret = self.total_return(equity_curve)
        n_days = days if days else len(equity_curve)
        
        if n_days <= 0:
            return 0.0
        
        return (1 + total_ret) ** (self.trading_days / n_days) - 1
    
    def returns_series(self, equity_curve: List[float]) -> np.ndarray:
        """
        计算收益率序列
        
        Args:
            equity_curve: 资金曲线列表
            
        Returns:
            日收益率 numpy 数组
        """
        if len(equity_curve) < 2:
            return np.array([])
        
        equity = np.array(equity_curve)
        returns = np.diff(equity) / equity[:-1]
        return returns
    
    # ==================== 风险调整收益指标 ====================
    
    def sharpe_ratio(self, returns: np.ndarray = None, equity_curve: List[float] = None) -> float:
        """
        夏普比率 (Sharpe Ratio)
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线 (可选，如果 returns 未提供)
            
        Returns:
            夏普比率
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0 or np.std(returns) == 0:
            return 0.0
        
        excess_returns = returns - self.daily_rf_rate
        return np.sqrt(self.trading_days) * np.mean(excess_returns) / np.std(excess_returns)
    
    def sortino_ratio(self, returns: np.ndarray = None, equity_curve: List[float] = None) -> float:
        """
        索提诺比率 (Sortino Ratio) - 只考虑下行波动
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线
            
        Returns:
            索提诺比率
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0:
            return 0.0
        
        excess_returns = returns - self.daily_rf_rate
        downside_returns = excess_returns[excess_returns < 0]
        
        if len(downside_returns) == 0:
            return np.sqrt(self.trading_days) * np.mean(excess_returns) / 0.01
        
        downside_std = np.sqrt(np.mean(downside_returns ** 2))
        return np.sqrt(self.trading_days) * np.mean(excess_returns) / downside_std
    
    def calmar_ratio(self, equity_curve: List[float]) -> float:
        """
        卡玛比率 (Calmar Ratio) - 年化收益/最大回撤
        
        Args:
            equity_curve: 资金曲线列表
            
        Returns:
            卡玛比率
        """
        ann_return = self.annualized_return(equity_curve)
        max_dd = abs(self.max_drawdown(equity_curve))
        
        if max_dd == 0:
            return 0.0
        
        return ann_return / max_dd
    
    def information_ratio(
        self, 
        returns: np.ndarray, 
        benchmark_returns: np.ndarray
    ) -> float:
        """
        信息比率 (Information Ratio)
        
        Args:
            returns: 策略收益率
            benchmark_returns: 基准收益率
            
        Returns:
            信息比率
        """
        if len(returns) != len(benchmark_returns) or len(returns) == 0:
            return 0.0
        
        active_returns = returns - benchmark_returns
        tracking_error = np.std(active_returns)
        
        if tracking_error == 0:
            return 0.0
        
        return np.sqrt(self.trading_days) * np.mean(active_returns) / tracking_error
    
    # ==================== 风险指标 ====================
    
    def max_drawdown(self, equity_curve: List[float]) -> float:
        """
        最大回撤
        
        Args:
            equity_curve: 资金曲线列表
            
        Returns:
            最大回撤 (负值)
        """
        if len(equity_curve) < 2:
            return 0.0
        
        equity = np.array(equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = (peak - equity) / peak
        
        return float(drawdown.max())
    
    def current_drawdown(self, equity_curve: List[float]) -> float:
        """
        当前回撤
        
        Args:
            equity_curve: 资金曲线列表
            
        Returns:
            当前回撤 (负值)
        """
        if len(equity_curve) < 2:
            return 0.0
        
        peak = np.maximum.accumulate(equity_curve)
        current_dd = (peak[-1] - equity_curve[-1]) / peak[-1]
        
        return float(current_dd)
    
    def var_historical(
        self, 
        returns: np.ndarray = None, 
        equity_curve: List[float] = None,
        confidence: float = 0.95
    ) -> float:
        """
        历史 VaR (Value at Risk)
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线
            confidence: 置信度 (默认 95%)
            
        Returns:
            VaR 值 (负值，表示损失)
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0:
            return 0.0
        
        return float(np.percentile(returns, (1 - confidence) * 100))
    
    def cvar(
        self, 
        returns: np.ndarray = None, 
        equity_curve: List[float] = None,
        confidence: float = 0.95
    ) -> float:
        """
        条件 VaR (CVaR / Expected Shortfall)
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线
            confidence: 置信度
            
        Returns:
            CVaR 值 (负值)
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0:
            return 0.0
        
        var = self.var_historical(returns, confidence=confidence)
        tail_losses = returns[returns <= var]
        
        if len(tail_losses) == 0:
            return var
        
        return float(tail_losses.mean())
    
    def volatility(self, returns: np.ndarray = None, equity_curve: List[float] = None) -> float:
        """
        年化波动率
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线
            
        Returns:
            年化波动率
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0:
            return 0.0
        
        return float(np.std(returns) * np.sqrt(self.trading_days))
    
    def downside_volatility(
        self, 
        returns: np.ndarray = None, 
        equity_curve: List[float] = None
    ) -> float:
        """
        下行波动率
        
        Args:
            returns: 收益率数组
            equity_curve: 资金曲线
            
        Returns:
            下行波动率
        """
        if returns is None:
            returns = self.returns_series(equity_curve)
        
        if len(returns) == 0:
            return 0.0
        
        downside_returns = returns[returns < 0]
        if len(downside_returns) == 0:
            return 0.0
        
        return float(np.std(downside_returns) * np.sqrt(self.trading_days))
    
    # ==================== 交易质量指标 ====================
    
    def win_rate(self, trades: List[Dict]) -> float:
        """
        胜率
        
        Args:
            trades: 交易列表 [{pnl: float}, ...]
            
        Returns:
            胜率
        """
        if len(trades) == 0:
            return 0.0
        
        winning = sum(1 for t in trades if t.get('pnl', 0) > 0)
        return winning / len(trades)
    
    def profit_factor(self, trades: List[Dict]) -> float:
        """
        盈利因子 (总盈利/总亏损)
        
        Args:
            trades: 交易列表
            
        Returns:
            盈利因子
        """
        if len(trades) == 0:
            return 0.0
        
        total_profit = sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) > 0)
        total_loss = abs(sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) < 0))
        
        if total_loss == 0:
            return float('inf') if total_profit > 0 else 0.0
        
        return total_profit / total_loss
    
    def profit_loss_ratio(self, trades: List[Dict]) -> float:
        """
        盈亏比 (平均盈利/平均亏损)
        
        Args:
            trades: 交易列表
            
        Returns:
            盈亏比
        """
        winning_trades = [t for t in trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in trades if t.get('pnl', 0) < 0]
        
        if len(losing_trades) == 0:
            return float('inf') if len(winning_trades) > 0 else 0.0
        
        avg_win = np.mean([t.get('pnl', 0) for t in winning_trades]) if winning_trades else 0
        avg_loss = abs(np.mean([t.get('pnl', 0) for t in losing_trades])) if losing_trades else 0
        
        if avg_loss == 0:
            return 0.0
        
        return avg_win / avg_loss
    
    def expectancy(self, win_rate: float, profit_loss_ratio: float) -> float:
        """
        期望值 (每单位风险的期望收益)
        
        Args:
            win_rate: 胜率
            profit_loss_ratio: 盈亏比
            
        Returns:
            期望值
        """
        return win_rate * profit_loss_ratio - (1 - win_rate)
    
    def max_consecutive_wins(self, trades: List[Dict]) -> int:
        """
        最大连续盈利次数
        
        Args:
            trades: 交易列表
            
        Returns:
            最大连胜次数
        """
        if len(trades) == 0:
            return 0
        
        max_consecutive = 0
        current_consecutive = 0
        
        for trade in trades:
            if trade.get('pnl', 0) > 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        return max_consecutive
    
    def max_consecutive_losses(self, trades: List[Dict]) -> int:
        """
        最大连续亏损次数
        
        Args:
            trades: 交易列表
            
        Returns:
            最大连败次数
        """
        if len(trades) == 0:
            return 0
        
        max_consecutive = 0
        current_consecutive = 0
        
        for trade in trades:
            if trade.get('pnl', 0) < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        return max_consecutive
    
    # ==================== 组合风险指标 ====================
    
    def beta(self, returns: np.ndarray, benchmark_returns: np.ndarray) -> float:
        """
        Beta 系数 (相对市场的敏感度)
        
        Args:
            returns: 策略收益率
            benchmark_returns: 基准收益率
            
        Returns:
            Beta 系数
        """
        if len(returns) != len(benchmark_returns) or len(returns) == 0:
            return 0.0
        
        covariance = np.cov(returns, benchmark_returns)[0, 1]
        benchmark_variance = np.var(benchmark_returns)
        
        if benchmark_variance == 0:
            return 0.0
        
        return float(covariance / benchmark_variance)
    
    def correlation_matrix(self, returns_dict: Dict[str, List[float]]) -> Dict:
        """
        相关性矩阵
        
        Args:
            returns_dict: {code: returns_list, ...}
            
        Returns:
            相关性矩阵 (字典)
        """
        if len(returns_dict) == 0:
            return {}
        
        # 找到最小长度 (对齐)
        min_len = min(len(v) for v in returns_dict.values())
        if min_len == 0:
            return {}
        
        # 构建矩阵
        codes = list(returns_dict.keys())
        data = np.array([returns_dict[code][:min_len] for code in codes])
        
        if data.shape[0] < 2:
            return {codes[0]: {codes[0]: 1.0}}
        
        corr_matrix = np.corrcoef(data)
        
        result = {}
        for i, code1 in enumerate(codes):
            result[code1] = {}
            for j, code2 in enumerate(codes):
                result[code1][code2] = round(float(corr_matrix[i, j]), 3)
        
        return result
    
    def concentration(self, positions: Dict[str, float]) -> Tuple[float, float]:
        """
        集中度 (前 3 大和前 5 大持仓占比)
        
        Args:
            positions: {code: market_value, ...}
            
        Returns:
            (top3_concentration, top5_concentration)
        """
        if len(positions) == 0:
            return 0.0, 0.0
        
        total_value = sum(positions.values())
        if total_value == 0:
            return 0.0, 0.0
        
        sorted_values = sorted(positions.values(), reverse=True)
        
        top3 = sum(sorted_values[:3]) / total_value
        top5 = sum(sorted_values[:5]) / total_value
        
        return top3, top5
    
    # ==================== 综合计算 ====================
    
    def calculate_all(
        self,
        equity_curve: List[float],
        trades: List[Dict] = None,
        benchmark_returns: np.ndarray = None
    ) -> PerformanceMetrics:
        """
        计算所有绩效指标
        
        Args:
            equity_curve: 资金曲线
            trades: 交易列表
            benchmark_returns: 基准收益率 (可选)
            
        Returns:
            PerformanceMetrics 对象
        """
        returns = self.returns_series(equity_curve)
        
        metrics = PerformanceMetrics()
        
        # 收益指标
        metrics.total_return = self.total_return(equity_curve)
        metrics.annualized_return = self.annualized_return(equity_curve)
        metrics.final_capital = equity_curve[-1] if equity_curve else 0
        
        # 风险调整收益
        metrics.sharpe_ratio = self.sharpe_ratio(returns)
        metrics.sortino_ratio = self.sortino_ratio(returns)
        metrics.calmar_ratio = self.calmar_ratio(equity_curve)
        
        if benchmark_returns is not None and len(benchmark_returns) == len(returns):
            metrics.information_ratio = self.information_ratio(returns, benchmark_returns)
        
        # 风险指标
        metrics.max_drawdown = self.max_drawdown(equity_curve)
        metrics.current_drawdown = self.current_drawdown(equity_curve)
        metrics.var_95 = abs(self.var_historical(returns))
        metrics.cvar_95 = abs(self.cvar(returns))
        metrics.volatility = self.volatility(returns)
        metrics.downside_volatility = self.downside_volatility(returns)
        
        # 交易质量指标
        if trades:
            metrics.total_trades = len(trades)
            metrics.winning_trades = sum(1 for t in trades if t.get('pnl', 0) > 0)
            metrics.losing_trades = sum(1 for t in trades if t.get('pnl', 0) < 0)
            metrics.win_rate = self.win_rate(trades)
            metrics.profit_factor = self.profit_factor(trades)
            metrics.avg_win = np.mean([t['pnl'] for t in trades if t.get('pnl', 0) > 0]) if metrics.winning_trades > 0 else 0
            metrics.avg_loss = abs(np.mean([t['pnl'] for t in trades if t.get('pnl', 0) < 0])) if metrics.losing_trades > 0 else 0
            metrics.profit_loss_ratio = self.profit_loss_ratio(trades)
            metrics.expectancy = self.expectancy(metrics.win_rate, metrics.profit_loss_ratio)
            metrics.max_consecutive_wins = self.max_consecutive_wins(trades)
            metrics.max_consecutive_losses = self.max_consecutive_losses(trades)
        
        return metrics


# 全局实例
metrics_calculator = MetricsCalculator()
