"""
PaperTrading - 模拟盘测试模块

功能:
- 模拟账户配置
- 信号执行记录
- 每日复盘报告
- 连续交易日验证
"""
import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import math

logger = logging.getLogger("ftseek.v4.paper_trading")
logger.propagate = False  # 防止日志重复


@dataclass
class PaperAccount:
    """模拟账户"""
    account_id: str = "PAPER001"
    initial_capital: float = 100000.0
    current_capital: float = 100000.0
    account_type: str = "SIMULATE"  # SIMULATE or REAL
    created_at: float = field(default_factory=time.time)
    
    @property
    def total_pnl(self) -> float:
        """总盈亏"""
        return self.current_capital - self.initial_capital
    
    @property
    def total_return(self) -> float:
        """总收益率"""
        if self.initial_capital == 0:
            return 0.0
        return self.total_pnl / self.initial_capital
    
    @property
    def peak_capital(self) -> float:
        """峰值资本 (简化)"""
        return max(self.current_capital, self.initial_capital)
    
    @property
    def current_drawdown(self) -> float:
        """当前回撤"""
        if self.peak_capital == 0:
            return 0.0
        return (self.peak_capital - self.current_capital) / self.peak_capital
    
    def update_capital(self, pnl: float):
        """更新资本"""
        self.current_capital += pnl
    
    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "initial_capital": self.initial_capital,
            "current_capital": self.current_capital,
            "total_pnl": self.total_pnl,
            "total_return": self.total_return,
            "current_drawdown": self.current_drawdown,
            "account_type": self.account_type,
        }


@dataclass
class TradeLog:
    """交易日志"""
    code: str
    action: str  # BUY or SELL
    entry_price: float
    quantity: int
    entry_time: float = field(default_factory=time.time)
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl: float = 0.0
    pnl_percent: float = 0.0
    commission: float = 0.0
    notes: str = ""
    
    @property
    def is_closed(self) -> bool:
        """是否已平仓"""
        return self.exit_price is not None
    
    @property
    def holding_period(self) -> float:
        """持仓时间 (分钟)"""
        if self.exit_time:
            return (self.exit_time - self.entry_time) / 60
        return (time.time() - self.entry_time) / 60
    
    def exit(self, exit_price: float, exit_time: Optional[float] = None):
        """平仓"""
        self.exit_price = exit_price
        self.exit_time = exit_time or time.time()
        
        # 计算盈亏
        if self.action == "BUY":
            gross_pnl = (exit_price - self.entry_price) * self.quantity
        else:
            gross_pnl = (self.entry_price - exit_price) * self.quantity
        
        self.pnl = gross_pnl - self.commission
        self.pnl_percent = self.pnl / (self.entry_price * self.quantity) if self.entry_price > 0 else 0.0
        
        logger.debug(f"平仓：{self.code} PnL={self.pnl:.2f}")
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "action": self.action,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
            "entry_time": datetime.fromtimestamp(self.entry_time).strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": datetime.fromtimestamp(self.exit_time).strftime("%Y-%m-%d %H:%M:%S") if self.exit_time else None,
            "is_closed": self.is_closed,
            "holding_period_minutes": self.holding_period,
        }


@dataclass
class DailyReview:
    """每日复盘"""
    date: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    net_pnl: float
    win_rate: float
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    trading_hours: float = 0.0
    notes: str = ""
    
    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "net_pnl": f"{self.net_pnl:.2f}",
            "win_rate": f"{self.win_rate:.1%}",
            "gross_profit": f"{self.gross_profit:.2f}",
            "gross_loss": f"{self.gross_loss:.2f}",
            "largest_win": f"{self.largest_win:.2f}",
            "largest_loss": f"{self.largest_loss:.2f}",
        }


class PaperTradingEngine:
    """模拟盘交易引擎"""
    
    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.0003,
        account_id: str = "PAPER001"
    ):
        """
        初始化模拟盘引擎
        
        Args:
            initial_capital: 初始资金
            commission_rate: 佣金率
            account_id: 账户 ID
        """
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        
        self.account = PaperAccount(
            account_id=account_id,
            initial_capital=initial_capital
        )
        
        self.trade_logs: List[TradeLog] = []
        self.daily_reviews: List[DailyReview] = []
        
        self.current_date: Optional[str] = None
        self.trading_days = 0
        self.is_trading = False
        
        # 验证标准
        self.min_trading_days = 5
        self.min_total_trades = 20
        self.target_win_rate = 0.55
        
        self.logger = logging.getLogger("ftseek.v4.paper_trading")
logger.propagate = False  # 防止日志重复
        self.logger.info(f"模拟盘引擎初始化：{account_id}, 初始资金={initial_capital}")
    
    def set_validation_criteria(
        self,
        min_days: int = 5,
        min_trades: int = 20,
        target_win_rate: float = 0.55
    ):
        """
        设置验证标准
        
        Args:
            min_days: 最小交易天数
            min_trades: 最小交易笔数
            target_win_rate: 目标胜率
        """
        self.min_trading_days = min_days
        self.min_total_trades = min_trades
        self.target_win_rate = target_win_rate
        self.logger.info(
            f"设置验证标准：{min_days}天，{min_trades}笔，胜率{target_win_rate:.0%}"
        )
    
    def start_trading_day(self, date: Optional[str] = None):
        """
        开始交易日
        
        Args:
            date: 交易日期 (默认今天)
        """
        self.current_date = date or datetime.now().strftime("%Y-%m-%d")
        self.is_trading = True
        self.trading_days += 1
        self.logger.info(f"开始交易日：{self.current_date}")
    
    def end_trading_day(self):
        """结束交易日"""
        if not self.is_trading:
            return
        
        review = self.generate_daily_review()
        self.daily_reviews.append(review)
        
        self.is_trading = False
        self.logger.info(
            f"结束交易日：{self.current_date}, "
            f"交易{review.total_trades}笔，盈亏={review.net_pnl:.2f}"
        )
    
    def execute_signal(self, signal: Dict[str, Any]) -> TradeLog:
        """
        执行信号
        
        Args:
            signal: 交易信号 {code, action, entry_price, quantity, ...}
            
        Returns:
            交易日志
        """
        code = signal.get("code", "")
        action = signal.get("action", "BUY")
        entry_price = signal.get("entry_price", 0.0)
        quantity = signal.get("quantity", 100)
        
        # 计算佣金
        notional = entry_price * quantity
        commission = notional * self.commission_rate
        
        # 创建交易日志
        trade_log = TradeLog(
            code=code,
            action=action,
            entry_price=entry_price,
            quantity=quantity,
            commission=commission
        )
        
        self.trade_logs.append(trade_log)
        self.logger.info(f"执行信号：{code} {action} @ {entry_price}")
        
        return trade_log
    
    def close_position(
        self,
        code: str,
        exit_price: float,
        exit_time: Optional[float] = None
    ) -> Optional[TradeLog]:
        """
        平仓
        
        Args:
            code: 股票代码
            exit_price: 平仓价格
            exit_time: 平仓时间
            
        Returns:
            交易日志
        """
        # 查找未平仓记录
        for log in reversed(self.trade_logs):
            if log.code == code and not log.is_closed:
                log.exit(exit_price, exit_time)
                
                # 更新账户资金
                self.account.update_capital(log.pnl)
                
                self.logger.info(f"平仓：{code} @ {exit_price}, PnL={log.pnl:.2f}")
                return log
        
        self.logger.warning(f"未找到未平仓记录：{code}")
        return None
    
    def generate_daily_review(self) -> DailyReview:
        """生成每日复盘"""
        # 获取当日交易
        today_logs = [
            log for log in self.trade_logs
            if log.exit_time and datetime.fromtimestamp(log.exit_time).strftime("%Y-%m-%d") == self.current_date
        ]
        
        if not today_logs:
            # 使用所有已平仓记录
            today_logs = [log for log in self.trade_logs if log.is_closed]
        
        total_trades = len(today_logs)
        winning_trades = sum(1 for log in today_logs if log.pnl > 0)
        losing_trades = total_trades - winning_trades
        
        net_pnl = sum(log.pnl for log in today_logs)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        wins = [log.pnl for log in today_logs if log.pnl > 0]
        losses = [log.pnl for log in today_logs if log.pnl < 0]
        
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        
        largest_win = max(wins) if wins else 0.0
        largest_loss = min(losses) if losses else 0.0
        
        review = DailyReview(
            date=self.current_date or datetime.now().strftime("%Y-%m-%d"),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            net_pnl=net_pnl,
            win_rate=win_rate,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            largest_win=largest_win,
            largest_loss=largest_loss
        )
        
        self.logger.info(
            f"生成每日复盘：{review.total_trades}笔，胜率{review.win_rate:.0%}"
        )
        
        return review
    
    def check_validation(self) -> Tuple[bool, Dict[str, Any]]:
        """
        检查验证标准
        
        Returns:
            (是否通过，验证报告)
        """
        report = {
            "trading_days": self.trading_days,
            "total_trades": len([l for l in self.trade_logs if l.is_closed]),
            "win_rate": self._calculate_win_rate(),
            "net_pnl": self.account.total_pnl,
            "sharpe_ratio": self._calculate_sharpe_ratio(),
            "max_drawdown": self.account.current_drawdown,
        }
        
        # 检查标准
        is_valid = (
            report["trading_days"] >= self.min_trading_days and
            report["total_trades"] >= self.min_total_trades and
            report["win_rate"] >= self.target_win_rate
        )
        
        report["is_valid"] = is_valid
        report["criteria"] = {
            "min_days": self.min_trading_days,
            "min_trades": self.min_total_trades,
            "target_win_rate": self.target_win_rate,
        }
        
        self.logger.info(
            f"验证结果：{'通过' if is_valid else '未通过'}, "
            f"{self.trading_days}天，{report['total_trades']}笔，胜率{report['win_rate']:.0%}"
        )
        
        return is_valid, report
    
    def _calculate_win_rate(self) -> float:
        """计算胜率"""
        closed_logs = [log for log in self.trade_logs if log.is_closed]
        if not closed_logs:
            return 0.0
        
        wins = sum(1 for log in closed_logs if log.pnl > 0)
        return wins / len(closed_logs)
    
    def _calculate_sharpe_ratio(self) -> float:
        """计算夏普比率"""
        closed_logs = [log for log in self.trade_logs if log.is_closed]
        if len(closed_logs) < 2:
            return 0.0
        
        returns = [log.pnl_percent for log in closed_logs]
        avg_return = sum(returns) / len(returns)
        
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_dev = math.sqrt(variance) if variance > 0 else 1e-6
        
        sharpe = avg_return / std_dev * math.sqrt(252)
        return sharpe
    
    def get_account_status(self) -> Dict[str, Any]:
        """获取账户状态"""
        return self.account.to_dict()
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """获取绩效摘要"""
        closed_logs = [log for log in self.trade_logs if log.is_closed]
        
        if not closed_logs:
            return {"error": "No closed trades"}
        
        total_trades = len(closed_logs)
        winning_trades = sum(1 for log in closed_logs if log.pnl > 0)
        net_pnl = sum(log.pnl for log in closed_logs)
        
        wins = [log.pnl for log in closed_logs if log.pnl > 0]
        losses = [log.pnl for log in closed_logs if log.pnl < 0]
        
        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        return {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "win_rate": f"{winning_trades / total_trades:.1%}",
            "net_pnl": f"{net_pnl:.2f}",
            "gross_profit": f"{gross_profit:.2f}",
            "gross_loss": f"{gross_loss:.2f}",
            "profit_factor": f"{profit_factor:.2f}",
            "largest_win": f"{max(wins):.2f}" if wins else "0.00",
            "largest_loss": f"{min(losses):.2f}" if losses else "0.00",
            "avg_holding_period": f"{sum(log.holding_period for log in closed_logs) / total_trades:.1f}分钟",
        }
    
    def get_risk_metrics(self) -> Dict[str, Any]:
        """获取风险指标"""
        return {
            "max_drawdown": f"{self.account.current_drawdown:.1%}",
            "sharpe_ratio": f"{self._calculate_sharpe_ratio():.2f}",
            "win_rate": f"{self._calculate_win_rate():.1%}",
            "total_return": f"{self.account.total_return:.1%}",
            "current_capital": f"{self.account.current_capital:.2f}",
        }
    
    def export_trade_log(self) -> List[Dict[str, Any]]:
        """导出交易日志"""
        return [log.to_dict() for log in self.trade_logs]
    
    def generate_final_report(self) -> Dict[str, Any]:
        """生成最终报告"""
        is_valid, validation = self.check_validation()
        
        report = {
            "validation": validation,
            "account": self.get_account_status(),
            "performance": self.get_performance_summary(),
            "risk_metrics": self.get_risk_metrics(),
            "daily_reviews": [review.to_dict() for review in self.daily_reviews],
            "total_trading_days": self.trading_days,
            "conclusion": "通过验证" if is_valid else "未通过验证",
        }
        
        self.logger.info(f"生成最终报告：{'通过' if is_valid else '未通过'}验证")
        return report
    
    def reset(self):
        """重置引擎"""
        self.account = PaperAccount(
            account_id=self.account.account_id,
            initial_capital=self.initial_capital
        )
        self.trade_logs.clear()
        self.daily_reviews.clear()
        self.trading_days = 0
        self.is_trading = False
        self.current_date = None
        
        self.logger.info("模拟盘引擎已重置")
    
    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "account_id": self.account.account_id,
            "current_capital": self.account.current_capital,
            "total_trades": len(self.trade_logs),
            "closed_trades": len([l for l in self.trade_logs if l.is_closed]),
            "trading_days": self.trading_days,
            "is_trading": self.is_trading,
        }


# 全局实例 (可选)
paper_trading_engine: Optional[PaperTradingEngine] = None


def get_paper_trading_engine() -> PaperTradingEngine:
    """获取 PaperTradingEngine 单例"""
    global paper_trading_engine
    if paper_trading_engine is None:
        paper_trading_engine = PaperTradingEngine()
    return paper_trading_engine
