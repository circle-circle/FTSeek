"""
简易策略回测框架

功能:
- 历史数据回测
- 策略性能评估
- 回测报告生成
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable
from datetime import datetime, timedelta
import json

logger = logging.getLogger("ftseek.backtest.simple_backtester")
logger.propagate = False  # 防止日志重复


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 1000000.0  # 初始资金 100 万
    commission_rate: float = 0.0003     # 佣金 0.03%
    stamp_duty_rate: float = 0.001      # 印花税 0.1%
    slippage_rate: float = 0.001        # 滑点 0.1%
    
    # 仓位限制
    max_position_ratio: float = 0.2     # 单票最大仓位 20%
    max_total_position: float = 0.8     # 总仓位上限 80%


@dataclass
class Trade:
    """回测交易记录"""
    date: str
    stock_code: str
    action: str  # "BUY" or "SELL"
    quantity: int
    price: float
    value: float
    commission: float
    stamp_duty: float
    slippage: float
    pnl: float = 0.0  # 仅 SELL 时有值


@dataclass
class BacktestResult:
    """回测结果"""
    total_return: float           # 总收益率
    annualized_return: float      # 年化收益
    max_drawdown: float           # 最大回撤
    sharpe_ratio: float           # 夏普比率
    win_rate: float               # 胜率
    profit_factor: float          # 盈亏比
    total_trades: int             # 总交易数
    winning_trades: int           # 盈利交易数
    losing_trades: int            # 亏损交易数
    avg_win: float                # 平均盈利
    avg_loss: float               # 平均亏损
    final_capital: float          # 最终资金
    trades: List[Trade] = field(default_factory=list)


class SimpleBacktester:
    """简易回测器"""
    
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.trades: List[Trade] = []
        self.positions: Dict[str, int] = {}  # stock -> quantity
        self.avg_cost: Dict[str, float] = {}  # stock -> avg cost
        self.cash = self.config.initial_capital
        self.peak_capital = self.config.initial_capital
        self.max_drawdown = 0.0
        
        logger.info(f"回测器已初始化：初始资金{self.config.initial_capital:,.0f}")
    
    def run(self,
            data: List[Dict],
            strategy_func: Callable,
            start_date: str = None,
            end_date: str = None) -> BacktestResult:
        """运行回测
        
        Args:
            data: 历史数据 [{date, stock_code, open, high, low, close, volume}]
            strategy_func: 策略函数 (接收数据，返回信号)
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            BacktestResult 回测结果
        """
        logger.info(f"开始回测：{len(data)} 条数据")
        
        # 重置状态
        self.trades = []
        self.positions = {}
        self.avg_cost = {}
        self.cash = self.config.initial_capital
        self.peak_capital = self.config.initial_capital
        self.max_drawdown = 0.0
        
        # 按日期分组
        daily_data = self._group_by_date(data)
        dates = sorted(daily_data.keys())
        
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]
        
        # 逐日回测
        for date in dates:
            day_data = daily_data[date]
            
            # 生成信号
            signals = strategy_func(day_data, self.positions, self.cash)
            
            # 执行交易
            self._execute_signals(date, day_data, signals)
            
            # 更新市值
            self._update_portfolio_value(day_data)
        
        # 平仓所有持仓
        self._close_all_positions(dates[-1] if dates else datetime.now().strftime("%Y-%m-%d"), data)
        
        # 计算结果
        result = self._calculate_result()
        
        logger.info(f"回测完成：总收益{result.total_return*100:.2f}%, 最大回撤{result.max_drawdown*100:.2f}%")
        
        return result
    
    def _group_by_date(self, data: List[Dict]) -> Dict[str, List[Dict]]:
        """按日期分组数据"""
        daily = {}
        for row in data:
            date = row.get('date', '')
            if date not in daily:
                daily[date] = []
            daily[date].append(row)
        return daily
    
    def _execute_signals(self, date: str, data: List[Dict], signals: List[Dict]):
        """执行交易信号"""
        price_map = {row['stock_code']: row['close'] for row in data}
        
        for signal in signals:
            stock_code = signal.get('stock_code')
            action = signal.get('action')
            quantity = signal.get('quantity', 0)
            
            if stock_code not in price_map:
                continue
            
            price = price_map[stock_code]
            
            # 应用滑点
            if action == "BUY":
                exec_price = price * (1 + self.config.slippage_rate)
            else:
                exec_price = price * (1 - self.config.slippage_rate)
            
            # 执行交易
            if action == "BUY":
                self._execute_buy(date, stock_code, quantity, exec_price)
            elif action == "SELL":
                self._execute_sell(date, stock_code, quantity, exec_price)
    
    def _execute_buy(self, date: str, stock_code: str, quantity: int, price: float):
        """执行买入"""
        value = quantity * price
        commission = max(value * self.config.commission_rate, 3.0)  # 最低 3 港元
        slippage = value * self.config.slippage_rate
        total_cost = value + commission + slippage
        
        # 检查资金
        if total_cost > self.cash:
            logger.debug(f"资金不足：{stock_code} 需要{total_cost:,.0f}, 可用{self.cash:,.0f}")
            return
        
        # 检查仓位限制
        current_value = self.positions.get(stock_code, 0) * price
        if (current_value + value) / self.cash > self.config.max_position_ratio:
            logger.debug(f"仓位超限：{stock_code}")
            return
        
        # 执行买入
        self.cash -= total_cost
        
        # 更新持仓
        if stock_code in self.positions:
            old_qty = self.positions[stock_code]
            old_cost = self.avg_cost[stock_code]
            self.avg_cost[stock_code] = (old_cost * old_qty + value) / (old_qty + quantity)
            self.positions[stock_code] += quantity
        else:
            self.positions[stock_code] = quantity
            self.avg_cost[stock_code] = price
        
        trade = Trade(
            date=date,
            stock_code=stock_code,
            action="BUY",
            quantity=quantity,
            price=price,
            value=value,
            commission=commission,
            stamp_duty=0.0,
            slippage=slippage,
        )
        self.trades.append(trade)
        
        logger.debug(f"买入：{stock_code} x{quantity} @${price:.2f}")
    
    def _execute_sell(self, date: str, stock_code: str, quantity: int, price: float):
        """执行卖出"""
        if stock_code not in self.positions or self.positions[stock_code] <= 0:
            logger.debug(f"无持仓：{stock_code}")
            return
        
        # 限制卖出数量
        quantity = min(quantity, self.positions[stock_code])
        
        value = quantity * price
        commission = max(value * self.config.commission_rate, 3.0)
        stamp_duty = max(1.0, round(value * self.config.stamp_duty_rate))
        slippage = value * self.config.slippage_rate
        
        # 计算盈亏
        avg_cost = self.avg_cost.get(stock_code, price)
        pnl = (price - avg_cost) * quantity - commission - stamp_duty - slippage
        
        # 执行卖出
        self.cash += value - commission - stamp_duty - slippage
        
        # 更新持仓
        self.positions[stock_code] -= quantity
        if self.positions[stock_code] <= 0:
            del self.positions[stock_code]
            del self.avg_cost[stock_code]
        
        trade = Trade(
            date=date,
            stock_code=stock_code,
            action="SELL",
            quantity=quantity,
            price=price,
            value=value,
            commission=commission,
            stamp_duty=stamp_duty,
            slippage=slippage,
            pnl=pnl,
        )
        self.trades.append(trade)
        
        logger.debug(f"卖出：{stock_code} x{quantity} @${price:.2f} 盈亏{pnl:,.0f}")
    
    def _update_portfolio_value(self, data: List[Dict]):
        """更新组合市值"""
        price_map = {row['stock_code']: row['close'] for row in data}
        
        position_value = sum(
            qty * price_map.get(stock, 0)
            for stock, qty in self.positions.items()
        )
        
        total_value = self.cash + position_value
        
        # 更新峰值
        if total_value > self.peak_capital:
            self.peak_capital = total_value
        
        # 计算回撤
        if self.peak_capital > 0:
            drawdown = (self.peak_capital - total_value) / self.peak_capital
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
    
    def _close_all_positions(self, date: str, data: List[Dict]):
        """平仓所有持仓"""
        price_map = {row['stock_code']: row['close'] for row in data}
        
        for stock_code, quantity in list(self.positions.items()):
            if quantity > 0 and stock_code in price_map:
                self._execute_sell(date, stock_code, quantity, price_map[stock_code])
    
    def _calculate_result(self) -> BacktestResult:
        """计算回测结果"""
        final_capital = self.cash
        total_return = (final_capital - self.config.initial_capital) / self.config.initial_capital
        
        # 交易统计
        sell_trades = [t for t in self.trades if t.action == "SELL"]
        winning_trades = [t for t in sell_trades if t.pnl > 0]
        losing_trades = [t for t in sell_trades if t.pnl < 0]
        
        total_trades = len(sell_trades)
        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        
        win_rate = win_count / total_trades if total_trades > 0 else 0
        
        total_profit = sum(t.pnl for t in winning_trades)
        total_loss = abs(sum(t.pnl for t in losing_trades))
        
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        
        avg_win = total_profit / win_count if win_count > 0 else 0
        avg_loss = total_loss / loss_count if loss_count > 0 else 0
        
        # 夏普比率 (简化计算)
        if len(self.trades) > 1:
            import statistics
            returns = [t.pnl for t in sell_trades]
            if len(returns) > 1:
                sharpe = statistics.mean(returns) / (statistics.stdev(returns) + 0.001) * 2.5  # 年化
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0
        
        # 年化收益 (假设 250 个交易日)
        if self.trades:
            first_date = datetime.fromisoformat(self.trades[0].date)
            last_date = datetime.fromisoformat(self.trades[-1].date)
            days = (last_date - first_date).days
            if days > 0:
                annualized = ((1 + total_return) ** (365 / days)) - 1
            else:
                annualized = total_return
        else:
            annualized = 0.0
        
        return BacktestResult(
            total_return=total_return,
            annualized_return=annualized,
            max_drawdown=self.max_drawdown,
            sharpe_ratio=sharpe,
            win_rate=win_rate,
            profit_factor=profit_factor,
            total_trades=total_trades,
            winning_trades=win_count,
            losing_trades=loss_count,
            avg_win=avg_win,
            avg_loss=avg_loss,
            final_capital=final_capital,
            trades=self.trades,
        )
    
    def generate_report(self, result: BacktestResult) -> str:
        """生成回测报告"""
        report = f"""
╔══════════════════════════════════════════════════════════╗
║                    策略回测报告                           ║
╠══════════════════════════════════════════════════════════╣

【收益指标】
  初始资金：     {self.config.initial_capital:,.0f}
  最终资金：     {result.final_capital:,.0f}
  总收益率：     {result.total_return*100:+.2f}%
  年化收益：     {result.annualized_return*100:+.2f}%

【风险指标】
  最大回撤：     {result.max_drawdown*100:.2f}%
  夏普比率：     {result.sharpe_ratio:.2f}

【交易统计】
  总交易数：     {result.total_trades}
  盈利交易：     {result.winning_trades}
  亏损交易：     {result.losing_trades}
  胜率：         {result.win_rate*100:.1f}%
  盈亏比：       {result.profit_factor:.2f}

【盈亏分析】
  平均盈利：     {result.avg_win:,.0f}
  平均亏损：     {result.avg_loss:,.0f}
  盈利/亏损比：  {result.avg_win/abs(result.avg_loss) if result.avg_loss and result.avg_loss != 0 else float('inf'):.2f}

╚══════════════════════════════════════════════════════════╝
"""
        return report


# 全局单例
_backtester = None

def get_backtester(config: Dict = None) -> SimpleBacktester:
    """获取回测器单例"""
    global _backtester
    if _backtester is None:
        if config:
            _backtester = SimpleBacktester(BacktestConfig(**config))
        else:
            _backtester = SimpleBacktester()
    return _backtester
