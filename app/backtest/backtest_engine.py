"""
BacktestEngine V2 - 增强版回测引擎

功能:
- 多指标计算（15+ 指标）
- 绩效分析
- 风险分析
- 策略对比
- 蒙特卡洛模拟
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging

logger = logging.getLogger("ftseek.backtest.engine")
logger.propagate = False  # 防止日志重复


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 100000.0
    commission_rate: float = 0.0003  # 万分之三
    slippage: float = 0.001  # 千分之一滑点
    risk_free_rate: float = 0.02  # 无风险利率 2%
    benchmark: str = 'SPY'  # 基准


@dataclass
class Trade:
    """交易记录"""
    entry_time: datetime
    exit_time: Optional[datetime]
    symbol: str
    direction: str  # LONG or SHORT
    entry_price: float
    exit_price: Optional[float]
    quantity: int
    pnl: float = 0.0
    pnl_percent: float = 0.0
    commission: float = 0.0
    mae: float = 0.0  # Maximum Adverse Excursion
    mfe: float = 0.0  # Maximum Favorable Excursion


@dataclass
class BacktestResult:
    """回测结果"""
    # 基本信息
    config: BacktestConfig
    start_date: datetime
    end_date: datetime
    total_days: int
    
    # 收益指标
    total_return: float = 0.0
    annual_return: float = 0.0
    excess_return: float = 0.0
    benchmark_return: float = 0.0
    
    # 风险指标
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0  # 天
    volatility: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    
    # 风险调整收益
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    
    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_trade: float = 0.0
    avg_holding_period: float = 0.0  # 天
    
    # 连续统计
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    
    # 月度收益
    monthly_returns: Dict[str, float] = field(default_factory=dict)
    
    # 权益曲线
    equity_curve: List[float] = field(default_factory=list)
    drawdown_curve: List[float] = field(default_factory=list)
    
    # 交易明细
    trades: List[Trade] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'total_return': self.total_return,
            'annual_return': self.annual_return,
            'sharpe_ratio': self.sharpe_ratio,
            'sortino_ratio': self.sortino_ratio,
            'calmar_ratio': self.calmar_ratio,
            'max_drawdown': self.max_drawdown,
            'volatility': self.volatility,
            'win_rate': self.win_rate,
            'profit_factor': self.profit_factor,
            'total_trades': self.total_trades,
            'var_95': self.var_95,
            'cvar_95': self.cvar_95,
        }
    
    def to_json(self, filepath: str) -> None:
        """导出为 JSON"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, default=str)


class BacktestEngine:
    """增强版回测引擎"""
    
    def __init__(self, config: Optional[BacktestConfig] = None):
        """
        初始化回测引擎
        
        Args:
            config: 回测配置
        """
        self.config = config or BacktestConfig()
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []
        self.drawdown_curve: List[float] = []
        self.daily_returns: List[float] = []
    
    def add_trade(self, trade: Trade) -> None:
        """添加交易记录"""
        self.trades.append(trade)
    
    def calculate_metrics(self) -> BacktestResult:
        """计算所有回测指标"""
        if not self.trades:
            return BacktestResult(
                config=self.config,
                start_date=datetime.now(),
                end_date=datetime.now(),
                total_days=0
            )
        
        # 计算权益曲线
        self._calculate_equity_curve()
        
        # 计算日收益率
        self._calculate_daily_returns()
        
        # 计算收益指标
        total_return = self._calculate_total_return()
        annual_return = self._calculate_annual_return(total_return)
        
        # 计算风险指标
        max_dd = self._calculate_max_drawdown()
        volatility = self._calculate_volatility()
        var_95 = self._calculate_var(0.95)
        cvar_95 = self._calculate_cvar(0.95)
        
        # 计算风险调整收益
        sharpe = self._calculate_sharpe_ratio()
        sortino = self._calculate_sortino_ratio()
        calmar = self._calculate_calmar_ratio(annual_return, max_dd)
        
        # 计算交易统计
        trade_stats = self._calculate_trade_statistics()
        
        # 计算月度收益
        monthly_returns = self._calculate_monthly_returns()
        
        # 计算连续统计
        max_consec_wins, max_consec_losses = self._calculate_consecutive_stats()
        
        # 计算最大回撤持续期
        max_dd_duration = self._calculate_max_drawdown_duration()
        
        result = BacktestResult(
            config=self.config,
            start_date=min(t.entry_time for t in self.trades),
            end_date=max(t.exit_time for t in self.trades if t.exit_time),
            total_days=(max(t.exit_time for t in self.trades if t.exit_time) - 
                       min(t.entry_time for t in self.trades)).days,
            total_return=total_return,
            annual_return=annual_return,
            max_drawdown=max_dd,
            max_drawdown_duration=max_dd_duration,
            volatility=volatility,
            var_95=var_95,
            cvar_95=cvar_95,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            monthly_returns=monthly_returns,
            equity_curve=self.equity_curve,
            drawdown_curve=self.drawdown_curve,
            trades=self.trades.copy(),
            **trade_stats
        )
        
        return result
    
    def _calculate_equity_curve(self) -> None:
        """计算权益曲线"""
        equity = self.config.initial_capital
        self.equity_curve = [equity]
        self.drawdown_curve = [0.0]
        
        peak = equity
        for trade in sorted(self.trades, key=lambda t: t.exit_time if t.exit_time else datetime.now()):
            if trade.exit_price:
                equity += trade.pnl
                self.equity_curve.append(equity)
                
                # 计算回撤
                if equity > peak:
                    peak = equity
                drawdown = (peak - equity) / peak if peak > 0 else 0
                self.drawdown_curve.append(drawdown)
    
    def _calculate_daily_returns(self) -> None:
        """计算日收益率"""
        if len(self.equity_curve) < 2:
            return
        
        for i in range(1, len(self.equity_curve)):
            daily_ret = (self.equity_curve[i] - self.equity_curve[i-1]) / self.equity_curve[i-1]
            self.daily_returns.append(daily_ret)
    
    def _calculate_total_return(self) -> float:
        """计算总收益率"""
        if not self.equity_curve:
            return 0.0
        return (self.equity_curve[-1] - self.config.initial_capital) / self.config.initial_capital
    
    def _calculate_annual_return(self, total_return: float) -> float:
        """计算年化收益率"""
        if not self.trades:
            return 0.0
        
        days = (max(t.exit_time for t in self.trades if t.exit_time) - 
                min(t.entry_time for t in self.trades)).days
        if days <= 0:
            return 0.0
        
        years = days / 365.25
        return (1 + total_return) ** (1 / years) - 1
    
    def _calculate_max_drawdown(self) -> float:
        """计算最大回撤"""
        if not self.drawdown_curve:
            return 0.0
        return max(self.drawdown_curve)
    
    def _calculate_max_drawdown_duration(self) -> int:
        """计算最大回撤持续期"""
        if not self.drawdown_curve:
            return 0
        
        max_duration = 0
        current_duration = 0
        in_drawdown = False
        
        for dd in self.drawdown_curve:
            if dd > 0:
                in_drawdown = True
                current_duration += 1
            else:
                if in_drawdown:
                    max_duration = max(max_duration, current_duration)
                    in_drawdown = False
                    current_duration = 0
        
        return max_duration
    
    def _calculate_volatility(self) -> float:
        """计算年化波动率"""
        if len(self.daily_returns) < 2:
            return 0.0
        return np.std(self.daily_returns) * np.sqrt(252)
    
    def _calculate_var(self, confidence: float = 0.95) -> float:
        """计算 VaR (Value at Risk)"""
        if not self.daily_returns:
            return 0.0
        
        percentile = (1 - confidence) * 100
        return np.percentile(self.daily_returns, percentile)
    
    def _calculate_cvar(self, confidence: float = 0.95) -> float:
        """计算 CVaR (Conditional VaR)"""
        if not self.daily_returns:
            return 0.0
        
        var = self._calculate_var(confidence)
        tail_returns = [r for r in self.daily_returns if r <= var]
        if not tail_returns:
            return var
        return np.mean(tail_returns)
    
    def _calculate_sharpe_ratio(self) -> float:
        """计算 Sharpe 比率"""
        if len(self.daily_returns) < 2 or self._calculate_volatility() == 0:
            return 0.0
        
        daily_rf = self.config.risk_free_rate / 252
        excess_returns = [r - daily_rf for r in self.daily_returns]
        return np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(252)
    
    def _calculate_sortino_ratio(self) -> float:
        """计算 Sortino 比率"""
        if len(self.daily_returns) < 2:
            return 0.0
        
        daily_rf = self.config.risk_free_rate / 252
        excess_returns = [r - daily_rf for r in self.daily_returns]
        
        # 只考虑下行波动
        downside_returns = [r for r in excess_returns if r < 0]
        if not downside_returns:
            return float('inf')
        
        downside_std = np.std(downside_returns) * np.sqrt(252)
        if downside_std == 0:
            return float('inf')
        
        annual_excess = np.mean(excess_returns) * 252
        return annual_excess / downside_std
    
    def _calculate_calmar_ratio(self, annual_return: float, max_drawdown: float) -> float:
        """计算 Calmar 比率"""
        if max_drawdown == 0:
            return 0.0
        return annual_return / max_drawdown
    
    def _calculate_trade_statistics(self) -> Dict[str, Any]:
        """计算交易统计"""
        completed_trades = [t for t in self.trades if t.exit_price]
        
        if not completed_trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'profit_factor': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'avg_trade': 0.0,
                'avg_holding_period': 0.0,
            }
        
        winning = [t for t in completed_trades if t.pnl > 0]
        losing = [t for t in completed_trades if t.pnl < 0]
        
        total_pnl = sum(t.pnl for t in completed_trades)
        gross_profit = sum(t.pnl for t in winning)
        gross_loss = abs(sum(t.pnl for t in losing))
        
        holding_periods = []
        for t in completed_trades:
            if t.exit_time:
                period = (t.exit_time - t.entry_time).days
                holding_periods.append(period)
        
        return {
            'total_trades': len(completed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': len(winning) / len(completed_trades) if completed_trades else 0.0,
            'profit_factor': gross_profit / gross_loss if gross_loss > 0 else float('inf'),
            'avg_win': np.mean([t.pnl for t in winning]) if winning else 0.0,
            'avg_loss': np.mean([t.pnl for t in losing]) if losing else 0.0,
            'avg_trade': total_pnl / len(completed_trades),
            'avg_holding_period': np.mean(holding_periods) if holding_periods else 0.0,
        }
    
    def _calculate_monthly_returns(self) -> Dict[str, float]:
        """计算月度收益"""
        if not self.trades:
            return {}
        
        monthly_pnl = {}
        for trade in self.trades:
            if trade.exit_time and trade.exit_price:
                month_key = trade.exit_time.strftime('%Y-%m')
                monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + trade.pnl
        
        # 转换为收益率
        monthly_returns = {}
        for month, pnl in sorted(monthly_pnl.items()):
            monthly_returns[month] = pnl / self.config.initial_capital
        
        return monthly_returns
    
    def _calculate_consecutive_stats(self) -> Tuple[int, int]:
        """计算连续盈利/亏损统计"""
        completed_trades = sorted(
            [t for t in self.trades if t.exit_price],
            key=lambda t: t.exit_time
        )
        
        max_consec_wins = 0
        max_consec_losses = 0
        current_wins = 0
        current_losses = 0
        
        for trade in completed_trades:
            if trade.pnl > 0:
                current_wins += 1
                current_losses = 0
                max_consec_wins = max(max_consec_wins, current_wins)
            elif trade.pnl < 0:
                current_losses += 1
                current_wins = 0
                max_consec_losses = max(max_consec_losses, current_losses)
        
        return max_consec_wins, max_consec_losses


class MonteCarloSimulator:
    """蒙特卡洛模拟器"""
    
    def __init__(self, engine: BacktestEngine):
        """
        初始化蒙特卡洛模拟器
        
        Args:
            engine: 回测引擎实例
        """
        self.engine = engine
        self.simulation_results: List[BacktestResult] = []
    
    def run_simulation(self, n_simulations: int = 1000) -> Dict[str, Any]:
        """
        运行蒙特卡洛模拟
        
        Args:
            n_simulations: 模拟次数
        
        Returns:
            模拟结果统计
        """
        if not self.engine.trades:
            return {}
        
        completed_trades = [t for t in self.engine.trades if t.exit_price]
        if not completed_trades:
            return {}
        
        # 提取历史收益率
        historical_returns = [t.pnl_percent for t in completed_trades]
        
        simulation_stats = {
            'total_returns': [],
            'max_drawdowns': [],
            'sharpe_ratios': [],
        }
        
        for i in range(n_simulations):
            # 随机采样生成新的交易序列
            n_trades = len(completed_trades)
            sampled_returns = np.random.choice(historical_returns, size=n_trades, replace=True)
            
            # 计算模拟结果
            total_return = np.prod([1 + r for r in sampled_returns]) - 1
            max_dd = self._estimate_max_drawdown(sampled_returns)
            sharpe = self._estimate_sharpe(sampled_returns)
            
            simulation_stats['total_returns'].append(total_return)
            simulation_stats['max_drawdowns'].append(max_dd)
            simulation_stats['sharpe_ratios'].append(sharpe)
        
        # 计算统计量
        results = {
            'n_simulations': n_simulations,
            'total_return': {
                'mean': np.mean(simulation_stats['total_returns']),
                'std': np.std(simulation_stats['total_returns']),
                'median': np.median(simulation_stats['total_returns']),
                'percentile_5': np.percentile(simulation_stats['total_returns'], 5),
                'percentile_95': np.percentile(simulation_stats['total_returns'], 95),
            },
            'max_drawdown': {
                'mean': np.mean(simulation_stats['max_drawdowns']),
                'std': np.std(simulation_stats['max_drawdowns']),
                'percentile_95': np.percentile(simulation_stats['max_drawdowns'], 95),
            },
            'sharpe_ratio': {
                'mean': np.mean(simulation_stats['sharpe_ratios']),
                'std': np.std(simulation_stats['sharpe_ratios']),
                'percentile_5': np.percentile(simulation_stats['sharpe_ratios'], 5),
            },
        }
        
        return results
    
    def _estimate_max_drawdown(self, returns: np.ndarray) -> float:
        """估算最大回撤"""
        equity = [1.0]
        for r in returns:
            equity.append(equity[-1] * (1 + r))
        
        peak = equity[0]
        max_dd = 0.0
        for e in equity:
            if e > peak:
                peak = e
            dd = (peak - e) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)
        
        return max_dd
    
    def _estimate_sharpe(self, returns: np.ndarray) -> float:
        """估算 Sharpe 比率"""
        if len(returns) < 2:
            return 0.0
        
        mean_return = np.mean(returns)
        std_return = np.std(returns)
        
        if std_return == 0:
            return 0.0
        
        daily_rf = 0.02 / 252
        excess_return = mean_return - daily_rf
        
        return excess_return / std_return * np.sqrt(252)
    
    def run_stress_test(self, scenario: Dict[str, Any]) -> BacktestResult:
        """
        运行压力测试
        
        Args:
            scenario: 压力场景配置
                - name: 场景名称
                - market_drop: 市场下跌幅度
                - volatility_increase: 波动率增加倍数
        
        Returns:
            压力测试结果
        """
        # 创建压力场景下的交易
        stressed_trades = []
        for trade in self.engine.trades:
            if trade.exit_price:
                # 应用市场下跌
                stressed_exit = trade.exit_price * (1 + scenario.get('market_drop', -0.30))
                
                # 应用波动率增加
                volatility_mult = scenario.get('volatility_increase', 1.5)
                stressed_exit *= (1 + np.random.normal(0, 0.02 * volatility_mult))
                
                stressed_trade = Trade(
                    entry_time=trade.entry_time,
                    exit_time=trade.exit_time,
                    symbol=trade.symbol,
                    direction=trade.direction,
                    entry_price=trade.entry_price,
                    exit_price=stressed_exit,
                    quantity=trade.quantity,
                )
                stressed_trade.pnl = (stressed_exit - trade.entry_price) * trade.quantity
                if trade.direction == 'SHORT':
                    stressed_trade.pnl = -stressed_trade.pnl
                
                stressed_trades.append(stressed_trade)
        
        # 创建新的回测引擎并计算
        stress_engine = BacktestEngine(self.engine.config)
        stress_engine.trades = stressed_trades
        return stress_engine.calculate_metrics()
