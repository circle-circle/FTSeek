"""
PostTradeRisk - 交易后风控模块

功能:
- 日终盈亏统计
- 连续亏损熔断
- 策略表现评估
- 周报/月报生成
"""
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict
import math

logger = logging.getLogger("ftseek.v4.post_trade_risk")
logger.propagate = False  # 防止日志重复


@dataclass
class TradeRecord:
    """交易记录"""
    code: str
    direction: str  # BUY or SELL
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_percent: float
    strategy: str
    entry_time: float = field(default_factory=time.time)
    exit_time: float = field(default_factory=time.time)
    
    @property
    def is_win(self) -> bool:
        return self.pnl > 0
    
    @property
    def holding_period_minutes(self) -> float:
        """持仓时间 (分钟)"""
        return (self.exit_time - self.entry_time) / 60
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "strategy": self.strategy,
            "is_win": self.is_win,
            "holding_period_minutes": self.holding_period_minutes,
            "entry_time": datetime.fromtimestamp(self.entry_time).strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": datetime.fromtimestamp(self.exit_time).strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class DailyStats:
    """日终统计"""
    date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    
    @classmethod
    def calculate(cls, trades: List[TradeRecord]) -> "DailyStats":
        """从交易列表计算日终统计"""
        if not trades:
            return cls(date=datetime.now().strftime("%Y-%m-%d"))
        
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.is_win)
        losing_trades = total_trades - winning_trades
        
        net_pnl = sum(t.pnl for t in trades)
        gross_profit = sum(t.pnl for t in trades if t.is_win)
        gross_loss = abs(sum(t.pnl for t in trades if not t.is_win))
        
        avg_win = gross_profit / winning_trades if winning_trades > 0 else 0.0
        avg_loss = gross_loss / losing_trades if losing_trades > 0 else 0.0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        largest_win = max((t.pnl for t in trades), default=0.0)
        largest_loss = min((t.pnl for t in trades), default=0.0)
        
        return cls(
            date=datetime.now().strftime("%Y-%m-%d"),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=winning_trades / total_trades if total_trades > 0 else 0.0,
            net_pnl=net_pnl,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            largest_win=largest_win,
            largest_loss=largest_loss,
        )
    
    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": f"{self.win_rate:.1%}",
            "net_pnl": f"{self.net_pnl:.2f}",
            "gross_profit": f"{self.gross_profit:.2f}",
            "gross_loss": f"{self.gross_loss:.2f}",
            "avg_win": f"{self.avg_win:.2f}",
            "avg_loss": f"{self.avg_loss:.2f}",
            "profit_factor": f"{self.profit_factor:.2f}" if self.profit_factor != float('inf') else "∞",
            "largest_win": f"{self.largest_win:.2f}",
            "largest_loss": f"{self.largest_loss:.2f}",
        }


class PerformanceTracker:
    """性能追踪器"""
    
    def __init__(self, initial_capital: float = 100000.0):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        self.trades: List[TradeRecord] = []
        self.returns: List[float] = []

        self.logger = logging.getLogger("ftseek.v4.performance_tracker")
        self.logger.propagate = False  # 防止日志重复

    def add_trade(self, trade: TradeRecord):
        """添加交易记录"""
        self.trades.append(trade)
        self.current_capital += trade.pnl
        
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital
        
        # 记录收益率
        if len(self.trades) > 1:
            prev_capital = self.current_capital - trade.pnl
            if prev_capital > 0:
                self.returns.append(trade.pnl / prev_capital)
        
        self.logger.info(f"添加交易：{trade.code} PnL={trade.pnl:.2f}, 总资本={self.current_capital:.2f}")
    
    def update_capital(self, capital: float):
        """更新资本 (用于外部调整)"""
        self.current_capital = capital
        if capital > self.peak_capital:
            self.peak_capital = capital
    
    @property
    def total_trades(self) -> int:
        return len(self.trades)
    
    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.is_win)
    
    @property
    def losing_trades(self) -> int:
        return self.total_trades - self.winning_trades
    
    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0
    
    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)
    
    @property
    def gross_profit(self) -> float:
        return sum(t.pnl for t in self.trades if t.is_win)
    
    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl for t in self.trades if not t.is_win))
    
    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float('inf')
    
    @property
    def current_drawdown(self) -> float:
        """当前回撤"""
        if self.peak_capital == 0:
            return 0.0
        return (self.peak_capital - self.current_capital) / self.peak_capital
    
    @property
    def max_drawdown(self) -> float:
        """最大回撤 (简化版)"""
        if self.peak_capital == self.initial_capital:
            return 0.0
        return (self.peak_capital - self.initial_capital) / self.peak_capital
    
    def calculate_sharpe_ratio(self, risk_free_rate: float = 0.02) -> Optional[float]:
        """计算夏普比率"""
        if len(self.returns) < 2:
            return None
        
        avg_return = sum(self.returns) / len(self.returns)
        
        # 计算标准差
        variance = sum((r - avg_return) ** 2 for r in self.returns) / len(self.returns)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return None
        
        # 年化夏普比率 (假设 252 个交易日)
        sharpe = (avg_return - risk_free_rate / 252) / std_dev * math.sqrt(252)
        return sharpe
    
    def get_strategy_breakdown(self) -> Dict[str, Dict[str, Any]]:
        """获取策略细分统计"""
        breakdown = defaultdict(lambda: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
        })
        
        for trade in self.trades:
            strategy = trade.strategy
            breakdown[strategy]["trades"] += 1
            breakdown[strategy]["total_pnl"] += trade.pnl
            
            if trade.is_win:
                breakdown[strategy]["wins"] += 1
            else:
                breakdown[strategy]["losses"] += 1
        
        # 计算胜率
        for strategy in breakdown:
            trades = breakdown[strategy]["trades"]
            if trades > 0:
                breakdown[strategy]["win_rate"] = breakdown[strategy]["wins"] / trades
        
        return dict(breakdown)
    
    def get_monthly_performance(self) -> Dict[str, Any]:
        """获取月度表现"""
        if not self.trades:
            return {"total_trades": 0, "net_pnl": 0.0, "win_rate": 0.0}
        
        return {
            "total_trades": self.total_trades,
            "net_pnl": self.total_pnl,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "total_return": (self.current_capital - self.initial_capital) / self.initial_capital,
            "sharpe_ratio": self.calculate_sharpe_ratio(),
        }
    
    def reset(self):
        """重置追踪器"""
        self.current_capital = self.initial_capital
        self.peak_capital = self.initial_capital
        self.trades.clear()
        self.returns.clear()


class PostTradeRisk:
    """交易后风控核心类"""
    
    def __init__(
        self,
        consecutive_loss_limit: int = 5,
        daily_loss_limit: float = 2000.0,
        cooling_off_minutes: int = 60,
        initial_capital: float = 100000.0,
    ):
        """
        初始化交易后风控
        
        Args:
            consecutive_loss_limit: 连续亏损次数限制 (默认 5 次)
            daily_loss_limit: 日亏损限额 (默认 2000)
            cooling_off_minutes: 冷静期时长 (分钟，默认 60)
            initial_capital: 初始资本
        """
        self.consecutive_loss_limit = consecutive_loss_limit
        self.daily_loss_limit = daily_loss_limit
        self.cooling_off_minutes = cooling_off_minutes
        
        # 交易记录
        self.trades: List[TradeRecord] = []
        self.daily_trades: List[TradeRecord] = []
        
        # 连续亏损追踪
        self.consecutive_losses = 0
        self.last_loss_time: Optional[float] = None
        
        # 熔断状态
        self.circuit_break_triggered = False
        self.circuit_break_time: Optional[float] = None
        
        # 日亏损追踪
        self.daily_pnl = 0.0
        
        # 性能追踪器
        self.performance_tracker = PerformanceTracker(initial_capital)
        
        self.logger = logging.getLogger("ftseek.v4.post_trade_risk")
        self.logger.propagate = False  # 防止日志重复

    def record_trade(self, trade: TradeRecord):
        """记录交易"""
        self.trades.append(trade)
        self.daily_trades.append(trade)
        self.daily_pnl += trade.pnl
        
        # 更新性能追踪
        self.performance_tracker.add_trade(trade)
        
        # 更新连续亏损
        if trade.is_win:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.last_loss_time = time.time()
        
        self.logger.info(f"记录交易：{trade.code} PnL={trade.pnl:.2f}, 连续亏损={self.consecutive_losses}")
    
    def record_win(self, pnl: float):
        """记录盈利 (简化接口)"""
        trade = TradeRecord(
            code="UNKNOWN",
            direction="BUY",
            entry_price=0,
            exit_price=pnl,
            quantity=0,
            pnl=pnl,
            pnl_percent=0,
            strategy="unknown"
        )
        self.record_trade(trade)
    
    def record_loss(self, loss: float):
        """记录亏损 (简化接口)"""
        trade = TradeRecord(
            code="UNKNOWN",
            direction="BUY",
            entry_price=0,
            exit_price=-abs(loss),
            quantity=0,
            pnl=-abs(loss),
            pnl_percent=0,
            strategy="unknown"
        )
        self.record_trade(trade)
    
    def should_circuit_break(self) -> bool:
        """检查是否应该触发熔断"""
        # 连续亏损熔断
        if self.consecutive_losses >= self.consecutive_loss_limit:
            self.circuit_break_triggered = True
            self.circuit_break_time = time.time()
            self.logger.warning(f"触发连续亏损熔断：{self.consecutive_losses} 次")
            return True
        
        return False
    
    def should_stop_trading(self) -> bool:
        """检查是否应该停止交易"""
        # 检查熔断
        if self.circuit_break_triggered:
            if self.circuit_break_time:
                elapsed = (time.time() - self.circuit_break_time) / 60
                if elapsed >= self.cooling_off_minutes:
                    # 冷静期结束，重置
                    self.circuit_break_triggered = False
                    self.circuit_break_time = None
                    self.consecutive_losses = 0
                    self.logger.info("冷静期结束，恢复交易")
                    return False
                else:
                    return True
        
        # 日亏损限额
        if self.daily_pnl <= -self.daily_loss_limit:
            self.logger.warning(f"触发日亏损限额：{self.daily_pnl:.2f}")
            return True
        
        return False
    
    @property
    def cooling_off_remaining_minutes(self) -> float:
        """剩余冷静期时间 (分钟)"""
        if not self.circuit_break_time:
            return 0.0
        
        elapsed = (time.time() - self.circuit_break_time) / 60
        remaining = self.cooling_off_minutes - elapsed
        return max(0.0, remaining)
    
    def generate_daily_stats(self) -> DailyStats:
        """生成日终统计"""
        stats = DailyStats.calculate(self.daily_trades)
        self.logger.info(
            f"日终统计：{stats.total_trades}笔交易，胜率{stats.win_rate:.1%}, "
            f"净盈亏={stats.net_pnl:.2f}"
        )
        return stats
    
    def reset_daily_stats(self):
        """重置日终统计"""
        self.daily_trades.clear()
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.circuit_break_triggered = False
        self.circuit_break_time = None
        self.logger.info("日终统计已重置")
    
    def get_strategy_performance(self) -> Dict[str, Dict[str, Any]]:
        """获取策略表现"""
        return self.performance_tracker.get_strategy_breakdown()
    
    def generate_weekly_report(self) -> Dict[str, Any]:
        """生成周报"""
        if not self.trades:
            return {"error": "No trades"}
        
        stats = DailyStats.calculate(self.trades[-50:])  # 最近 50 笔交易
        
        return {
            "period": "weekly",
            "total_trades": stats.total_trades,
            "winning_trades": stats.winning_trades,
            "losing_trades": stats.losing_trades,
            "win_rate": f"{stats.win_rate:.1%}",
            "net_pnl": f"{stats.net_pnl:.2f}",
            "profit_factor": f"{stats.profit_factor:.2f}" if stats.profit_factor != float('inf') else "∞",
            "avg_win": f"{stats.avg_win:.2f}",
            "avg_loss": f"{stats.avg_loss:.2f}",
            "largest_win": f"{stats.largest_win:.2f}",
            "largest_loss": f"{stats.largest_loss:.2f}",
            "current_capital": f"{self.performance_tracker.current_capital:.2f}",
            "total_return": f"{(self.performance_tracker.current_capital - self.performance_tracker.initial_capital) / self.performance_tracker.initial_capital:.1%}",
        }
    
    def generate_monthly_report(self) -> Dict[str, Any]:
        """生成月报"""
        monthly = self.performance_tracker.get_monthly_performance()
        
        return {
            "period": "monthly",
            **monthly,
            "net_pnl": f"{monthly['net_pnl']:.2f}",
            "total_return": f"{monthly['total_return']:.1%}",
            "sharpe_ratio": f"{monthly['sharpe_ratio']:.2f}" if monthly['sharpe_ratio'] else "N/A",
        }
    
    def get_risk_metrics(self) -> Dict[str, Any]:
        """获取风险指标"""
        if not self.trades:
            return {"error": "No trades"}
        
        wins = [t for t in self.trades if t.is_win]
        losses = [t for t in self.trades if not t.is_win]
        
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(t.pnl for t in losses)) / len(losses) if losses else 0.0
        
        holding_periods = [t.holding_period_minutes for t in self.trades]
        avg_holding = sum(holding_periods) / len(holding_periods) if holding_periods else 0.0
        
        return {
            "total_trades": len(self.trades),
            "win_rate": f"{self.performance_tracker.win_rate:.1%}",
            "profit_factor": f"{self.performance_tracker.profit_factor:.2f}" if self.performance_tracker.profit_factor != float('inf') else "∞",
            "avg_win": f"{avg_win:.2f}",
            "avg_loss": f"{avg_loss:.2f}",
            "win_loss_ratio": f"{avg_win / avg_loss:.2f}" if avg_loss > 0 else "∞",
            "largest_win": f"{max((t.pnl for t in wins), default=0.0):.2f}",
            "largest_loss": f"{min((t.pnl for t in losses), default=0.0):.2f}",
            "avg_holding_period_minutes": f"{avg_holding:.1f}",
            "current_drawdown": f"{self.performance_tracker.current_drawdown:.1%}",
            "sharpe_ratio": self.performance_tracker.calculate_sharpe_ratio(),
        }
    
    def get_status(self) -> Dict[str, Any]:
        """获取风控状态"""
        return {
            "consecutive_losses": self.consecutive_losses,
            "consecutive_loss_limit": self.consecutive_loss_limit,
            "daily_pnl": f"{self.daily_pnl:.2f}",
            "daily_loss_limit": f"{self.daily_loss_limit:.2f}",
            "circuit_break_triggered": self.circuit_break_triggered,
            "cooling_off_remaining_minutes": f"{self.cooling_off_remaining_minutes:.1f}",
            "total_trades": len(self.trades),
            "current_capital": f"{self.performance_tracker.current_capital:.2f}",
            "should_stop_trading": self.should_stop_trading(),
        }
    
    def reset(self):
        """重置所有状态"""
        self.trades.clear()
        self.daily_trades.clear()
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.circuit_break_triggered = False
        self.circuit_break_time = None
        self.performance_tracker.reset()
        self.logger.info("PostTradeRisk 已重置")


# 全局实例 (可选)
post_trade_risk: Optional[PostTradeRisk] = None


def get_post_trade_risk() -> PostTradeRisk:
    """获取 PostTradeRisk 单例"""
    global post_trade_risk
    if post_trade_risk is None:
        post_trade_risk = PostTradeRisk()
    return post_trade_risk
