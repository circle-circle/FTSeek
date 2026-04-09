"""
SimpleBacktest - 简化版回测模块

功能:
- 加载历史信号
- 模拟执行结果
- 计算胜率/盈亏比/夏普比率
- 生成回测报告
"""
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import math

logger = logging.getLogger("ftseek.v4.simple_backtest")
logger.propagate = False  # 防止日志重复


@dataclass
class Trade:
    """交易数据"""
    code: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    entry_time: Optional[float] = None
    exit_time: Optional[float] = None
    
    @property
    def return_pct(self) -> float:
        """收益率"""
        if self.entry_price == 0:
            return 0.0
        return self.pnl / (self.entry_price * self.quantity)
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
        }


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 100000.0
    commission_rate: float = 0.0003  # 万分之三
    slippage_rate: float = 0.001     # 千分之一
    max_position_pct: float = 0.20   # 单仓最大 20%
    
    def to_dict(self) -> dict:
        return {
            "initial_capital": self.initial_capital,
            "commission_rate": self.commission_rate,
            "slippage_rate": self.slippage_rate,
            "max_position_pct": self.max_position_pct,
        }


@dataclass
class BacktestResult:
    """回测结果"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    net_pnl: float
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    total_return: float
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_holding_period: float = 0.0
    
    def get_rating(self) -> str:
        """获取回测评级"""
        score = 0
        
        # 胜率评分 (0-30 分)
        if self.win_rate >= 0.60:
            score += 30
        elif self.win_rate >= 0.50:
            score += 20
        elif self.win_rate >= 0.40:
            score += 10
        
        # 盈利因子评分 (0-30 分)
        if self.profit_factor >= 2.0:
            score += 30
        elif self.profit_factor >= 1.5:
            score += 20
        elif self.profit_factor >= 1.0:
            score += 10
        
        # 夏普比率评分 (0-20 分)
        if self.sharpe_ratio >= 1.5:
            score += 20
        elif self.sharpe_ratio >= 1.0:
            score += 15
        elif self.sharpe_ratio >= 0.5:
            score += 10
        
        # 回撤评分 (0-20 分)
        if self.max_drawdown <= 0.10:
            score += 20
        elif self.max_drawdown <= 0.20:
            score += 15
        elif self.max_drawdown <= 0.30:
            score += 10
        
        # 评级
        if score >= 80:
            return "EXCELLENT"
        elif score >= 60:
            return "GOOD"
        elif score >= 40:
            return "FAIR"
        elif score >= 20:
            return "POOR"
        else:
            return "VERY_POOR"
    
    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "net_pnl": f"{self.net_pnl:.2f}",
            "win_rate": f"{self.win_rate:.1%}",
            "profit_factor": f"{self.profit_factor:.2f}",
            "sharpe_ratio": f"{self.sharpe_ratio:.2f}",
            "max_drawdown": f"{self.max_drawdown:.1%}",
            "total_return": f"{self.total_return:.1%}",
            "rating": self.get_rating(),
        }


class SimpleBacktest:
    """简化版回测引擎"""
    
    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.0003,
        slippage_rate: float = 0.001
    ):
        """
        初始化回测引擎
        
        Args:
            initial_capital: 初始资金
            commission_rate: 佣金率
            slippage_rate: 滑点率
        """
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        
        self.config = BacktestConfig(
            initial_capital=initial_capital,
            commission_rate=commission_rate,
            slippage_rate=slippage_rate
        )
        
        self.signals: List[Dict[str, Any]] = []
        self.trades: List[Trade] = []
        self.capital_curve: List[float] = [initial_capital]
        
        self.logger = logging.getLogger("ftseek.v4.simple_backtest")
logger.propagate = False  # 防止日志重复
    
    def add_signal(self, signal: Dict[str, Any]):
        """
        添加信号
        
        Args:
            signal: 信号字典 {code, action, entry_price, exit_price, quantity}
        """
        self.signals.append(signal)
        self.logger.debug(f"添加信号：{signal.get('code')} @ {signal.get('entry_price')}")
    
    def load_signals(self, signals: List[Dict[str, Any]]):
        """
        批量加载信号
        
        Args:
            signals: 信号列表
        """
        self.signals = signals
        self.logger.info(f"加载{len(signals)}个信号")
    
    def reset(self):
        """重置回测"""
        self.signals.clear()
        self.trades.clear()
        self.capital_curve = [self.initial_capital]
        self.logger.info("回测已重置")
    
    def run(self) -> BacktestResult:
        """
        运行回测
        
        Returns:
            回测结果
        """
        if not self.signals:
            self.logger.warning("无信号，返回空结果")
            return self._empty_result()
        
        self.trades.clear()
        self.capital_curve = [self.initial_capital]
        current_capital = self.initial_capital
        
        # 执行每个信号
        for signal in self.signals:
            trade = self._execute_signal(signal, current_capital)
            if trade:
                self.trades.append(trade)
                current_capital += trade.pnl
                self.capital_curve.append(current_capital)
        
        self.logger.info(f"回测完成：{len(self.trades)}笔交易")
        return self._calculate_metrics()
    
    def _execute_signal(
        self,
        signal: Dict[str, Any],
        current_capital: float
    ) -> Optional[Trade]:
        """
        执行信号
        
        Args:
            signal: 信号
            current_capital: 当前资金
            
        Returns:
            交易记录
        """
        code = signal.get("code", "")
        entry_price = signal.get("entry_price", 0)
        exit_price = signal.get("exit_price", 0)
        quantity = signal.get("quantity", 0)
        
        if entry_price <= 0 or quantity <= 0:
            return None
        
        # 计算佣金和滑点
        notional = entry_price * quantity
        commission = notional * self.commission_rate * 2  # 买卖双向
        slippage = notional * self.slippage_rate * 2
        
        # 计算盈亏
        gross_pnl = (exit_price - entry_price) * quantity
        net_pnl = gross_pnl - commission - slippage
        
        trade = Trade(
            code=code,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=net_pnl
        )
        
        self.logger.debug(f"执行交易：{code} PnL={net_pnl:.2f}")
        return trade
    
    def _calculate_metrics(self) -> BacktestResult:
        """计算回测指标"""
        if not self.trades:
            return self._empty_result()
        
        total_trades = len(self.trades)
        winning_trades = sum(1 for t in self.trades if t.pnl > 0)
        losing_trades = total_trades - winning_trades
        
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        # 盈亏统计
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        
        largest_win = max(wins) if wins else 0.0
        largest_loss = min(losses) if losses else 0.0
        
        net_pnl = sum(t.pnl for t in self.trades)
        total_return = net_pnl / self.initial_capital
        
        # 夏普比率
        sharpe_ratio = self._calculate_sharpe_ratio()
        
        # 最大回撤
        max_drawdown = self._calculate_max_drawdown()
        
        # 平均持仓周期 (简化为 1)
        avg_holding_period = 1.0
        
        result = BacktestResult(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            net_pnl=net_pnl,
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_drawdown,
            total_return=total_return,
            avg_win=avg_win,
            avg_loss=avg_loss,
            largest_win=largest_win,
            largest_loss=largest_loss,
            avg_holding_period=avg_holding_period
        )
        
        self.logger.info(
            f"回测指标：胜率={win_rate:.1%}, 盈亏比={profit_factor:.2f}, "
            f"夏普={sharpe_ratio:.2f}, 回撤={max_drawdown:.1%}"
        )
        
        return result
    
    def _calculate_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """
        计算夏普比率
        
        Args:
            risk_free_rate: 无风险利率 (年化)
            
        Returns:
            夏普比率
        """
        if len(self.trades) < 2:
            return 0.0
        
        # 计算收益率序列
        returns = []
        capital = self.initial_capital
        for trade in self.trades:
            ret = trade.pnl / capital
            returns.append(ret)
            capital += trade.pnl
        
        if not returns:
            return 0.0
        
        # 平均收益
        avg_return = sum(returns) / len(returns)
        
        # 标准差
        if len(returns) < 2:
            return 0.0
        
        variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return 0.0
        
        # 年化夏普比率 (假设 252 个交易日)
        sharpe = (avg_return - risk_free_rate / 252) / std_dev * math.sqrt(252)
        
        return sharpe
    
    def _calculate_max_drawdown(self) -> float:
        """
        计算最大回撤
        
        Returns:
            最大回撤
        """
        if len(self.capital_curve) < 2:
            return 0.0
        
        peak = self.capital_curve[0]
        max_dd = 0.0
        
        for capital in self.capital_curve:
            if capital > peak:
                peak = capital
            
            drawdown = (peak - capital) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, drawdown)
        
        return max_dd
    
    def _empty_result(self) -> BacktestResult:
        """返回空结果"""
        return BacktestResult(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            net_pnl=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            sharpe_ratio=0.0,
            max_drawdown=0.0,
            total_return=0.0
        )
    
    def generate_report(self, result: BacktestResult) -> Dict[str, Any]:
        """
        生成回测报告
        
        Args:
            result: 回测结果
            
        Returns:
            报告字典
        """
        report = {
            "summary": result.to_dict(),
            "config": self.config.to_dict(),
            "trades": [t.to_dict() for t in self.trades[:20]],  # 前 20 笔
            "capital_curve": self.capital_curve,
        }
        
        self.logger.info(f"生成回测报告：{result.total_trades}笔交易")
        return report
    
    def get_status(self) -> dict:
        """获取回测状态"""
        return {
            "signals_loaded": len(self.signals),
            "trades_executed": len(self.trades),
            "current_capital": self.capital_curve[-1] if self.capital_curve else self.initial_capital,
        }


# 全局实例 (可选)
simple_backtest: Optional[SimpleBacktest] = None


def get_simple_backtest() -> SimpleBacktest:
    """获取 SimpleBacktest 单例"""
    global simple_backtest
    if simple_backtest is None:
        simple_backtest = SimpleBacktest()
    return simple_backtest
