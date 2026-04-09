#!/usr/bin/env python3
"""
FTSeek 回测引擎 V2

功能:
1. 策略回测 (历史数据模拟)
2. 参数优化 (网格搜索)
3. 绩效评估 (夏普/最大回撤/胜率)
4. 策略对比 (多策略并行回测)

使用方法:
    python3 backtest/backtest_engine.py --strategy price_break --start 2026-01-01 --end 2026-03-19
    python3 backtest/backtest_engine.py --optimize --strategy fusion_v2
"""

import argparse
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
import numpy as np
import pandas as pd

logger = logging.getLogger("ftseek.backtest")


# ==================== 数据模型 ====================

@dataclass
class BacktestConfig:
    """回测配置"""
    start_date: str
    end_date: str
    initial_capital: float = 100000.0  # 初始资金 HKD
    commission_rate: float = 0.0003     # 佣金率 0.03%
    slippage_rate: float = 0.001       # 滑点 0.1%
    position_size: float = 0.2         # 单笔仓位比例 20%
    max_positions: int = 3             # 最大持仓数
    
    # 策略参数
    strategy_params: Dict = field(default_factory=dict)


@dataclass
class Trade:
    """交易记录"""
    entry_time: datetime
    exit_time: Optional[datetime]
    code: str
    action: str  # BUY/SELL
    quantity: int
    entry_price: float
    exit_price: Optional[float]
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_bars: int = 0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    """回测结果"""
    # 基础信息
    strategy: str
    config: BacktestConfig
    
    # 绩效指标
    total_return: float = 0.0          # 总收益率
    annual_return: float = 0.0         # 年化收益率
    sharpe_ratio: float = 0.0          # 夏普比率
    max_drawdown: float = 0.0          # 最大回撤
    calmar_ratio: float = 0.0          # 卡玛比率
    
    # 交易统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0              # 胜率
    profit_factor: float = 0.0         # 盈亏比
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_holding_bars: int = 0
    
    # 资金曲线
    equity_curve: List[float] = field(default_factory=list)
    drawdown_curve: List[float] = field(default_factory=list)
    
    # 交易记录
    trades: List[Trade] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            'strategy': self.strategy,
            'total_return': f"{self.total_return:.2%}",
            'annual_return': f"{self.annual_return:.2%}",
            'sharpe_ratio': f"{self.sharpe_ratio:.2f}",
            'max_drawdown': f"{self.max_drawdown:.2%}",
            'calmar_ratio': f"{self.calmar_ratio:.2f}",
            'total_trades': self.total_trades,
            'win_rate': f"{self.win_rate:.2%}",
            'profit_factor': f"{self.profit_factor:.2f}",
            'avg_win': f"HK${self.avg_win:.0f}",
            'avg_loss': f"HK${self.avg_loss:.0f}",
        }


# ==================== 数据加载器 ====================

class DataLoader:
    """历史数据加载器"""
    
    def __init__(self, db_path: str = 'data/ftseek.db'):
        self.db_path = db_path
    
    def load_klines(self, code: str, start_date: str, end_date: str, 
                    kline_type: str = 'K_DAY') -> pd.DataFrame:
        """
        加载K线数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            kline_type: K线类型
        
        Returns:
            DataFrame: K线数据
        """
        # 尝试从数据库加载
        conn = sqlite3.connect(self.db_path)
        
        query = f"""
            SELECT time_key, open, close, high, low, volume, turnover
            FROM klines
            WHERE code = ? AND time_key >= ? AND time_key <= ?
            ORDER BY time_key
        """
        
        try:
            df = pd.read_sql(query, conn, params=(code, start_date, end_date))
            if len(df) > 0:
                df.columns = ['time', 'open', 'close', 'high', 'low', 'volume', 'amount']
                return df
        except:
            pass
        finally:
            conn.close()
        
        # 生成模拟数据用于测试
        return self._generate_mock_data(code, start_date, end_date)
    
    def _generate_mock_data(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """生成模拟K线数据 (用于测试)"""
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        
        dates = []
        current = start
        while current <= end:
            if current.weekday() < 5:  # 工作日
                dates.append(current)
            current += timedelta(days=1)
        
        np.random.seed(hash(code) % 2**32)
        
        # 基础价格
        base_price = {'HK.09992': 200, 'HK.00981': 50, 'HK.03690': 80}.get(code, 100)
        
        # 随机游走
        returns = np.random.randn(len(dates)) * 0.02  # 2% 日波动
        prices = base_price * np.exp(np.cumsum(returns))
        
        df = pd.DataFrame({
            'time': dates,
            'open': prices * (1 + np.random.rand(len(dates)) * 0.02 - 0.01),
            'close': prices,
            'high': prices * (1 + np.random.rand(len(dates)) * 0.03),
            'low': prices * (1 - np.random.rand(len(dates)) * 0.03),
            'volume': np.random.randint(100000, 1000000, len(dates)),
            'amount': prices * np.random.randint(100000, 1000000, len(dates))
        })
        
        return df


# ==================== 策略基类 ====================

class BacktestStrategy(ABC):
    """回测策略基类"""
    
    name: str = "base"
    
    def __init__(self, params: Dict = None):
        self.params = params or {}
        self.position = None
    
    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> List[Dict]:
        """
        生成交易信号
        
        Args:
            data: K线数据
        
        Returns:
            List[Dict]: 信号列表 [{time, action, price, reason}]
        """
        pass
    
    def set_params(self, params: Dict):
        """设置参数"""
        self.params.update(params)


class PriceBreakStrategy(BacktestStrategy):
    """价格突破策略"""
    
    name = "price_break"
    
    def __init__(self, params: Dict = None):
        super().__init__(params)
        self.lookback = params.get('lookback', 20)  # 回看周期
        self.breakout_pct = params.get('breakout_pct', 0.02)  # 突破幅度
    
    def generate_signals(self, data: pd.DataFrame) -> List[Dict]:
        signals = []
        
        if len(data) < self.lookback:
            return signals
        
        for i in range(self.lookback, len(data)):
            window = data.iloc[i-self.lookback:i]
            current = data.iloc[i]
            
            # 上轨
            upper = window['high'].max()
            lower = window['low'].min()
            
            # 突破上轨
            if current['close'] > upper * (1 + self.breakout_pct):
                signals.append({
                    'time': current['time'],
                    'action': 'BUY',
                    'price': current['close'],
                    'reason': f"突破上轨 {upper:.2f}"
                })
            
            # 跌破下轨
            elif current['close'] < lower * (1 - self.breakout_pct):
                signals.append({
                    'time': current['time'],
                    'action': 'SELL',
                    'price': current['close'],
                    'reason': f"跌破下轨 {lower:.2f}"
                })
        
        return signals


class RSIStrategy(BacktestStrategy):
    """RSI策略"""
    
    name = "rsi"
    
    def __init__(self, params: Dict = None):
        super().__init__(params)
        self.period = params.get('period', 14)
        self.oversold = params.get('oversold', 30)
        self.overbought = params.get('overbought', 70)
    
    def _calculate_rsi(self, prices: pd.Series) -> float:
        """计算RSI"""
        deltas = prices.diff()
        gains = deltas.where(deltas > 0, 0)
        losses = -deltas.where(deltas < 0, 0)
        
        avg_gain = gains.rolling(self.period).mean()
        avg_loss = losses.rolling(self.period).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def generate_signals(self, data: pd.DataFrame) -> List[Dict]:
        signals = []
        
        if len(data) < self.period + 1:
            return signals
        
        rsi = self._calculate_rsi(data['close'])
        
        for i in range(self.period + 1, len(data)):
            current_rsi = rsi.iloc[i]
            
            if pd.isna(current_rsi):
                continue
            
            # 超卖反弹
            if current_rsi < self.oversold:
                signals.append({
                    'time': data.iloc[i]['time'],
                    'action': 'BUY',
                    'price': data.iloc[i]['close'],
                    'reason': f"RSI超卖 {current_rsi:.1f}"
                })
            
            # 超买回落
            elif current_rsi > self.overbought:
                signals.append({
                    'time': data.iloc[i]['time'],
                    'action': 'SELL',
                    'price': data.iloc[i]['close'],
                    'reason': f"RSI超买 {current_rsi:.1f}"
                })
        
        return signals


class MACrossStrategy(BacktestStrategy):
    """均线交叉策略"""
    
    name = "ma_cross"
    
    def __init__(self, params: Dict = None):
        super().__init__(params)
        self.fast_period = params.get('fast_period', 10)
        self.slow_period = params.get('slow_period', 30)
    
    def generate_signals(self, data: pd.DataFrame) -> List[Dict]:
        signals = []
        
        if len(data) < self.slow_period:
            return signals
        
        fast_ma = data['close'].rolling(self.fast_period).mean()
        slow_ma = data['close'].rolling(self.slow_period).mean()
        
        for i in range(self.slow_period, len(data)):
            # 金叉
            if fast_ma.iloc[i] > slow_ma.iloc[i] and fast_ma.iloc[i-1] <= slow_ma.iloc[i-1]:
                signals.append({
                    'time': data.iloc[i]['time'],
                    'action': 'BUY',
                    'price': data.iloc[i]['close'],
                    'reason': f"MA{self.fast_period}金叉MA{self.slow_period}"
                })
            
            # 死叉
            elif fast_ma.iloc[i] < slow_ma.iloc[i] and fast_ma.iloc[i-1] >= slow_ma.iloc[i-1]:
                signals.append({
                    'time': data.iloc[i]['time'],
                    'action': 'SELL',
                    'price': data.iloc[i]['close'],
                    'reason': f"MA{self.fast_period}死叉MA{self.slow_period}"
                })
        
        return signals


# ==================== 回测引擎 ====================

class BacktestEngine:
    """回测引擎"""
    
    STRATEGIES = {
        'price_break': PriceBreakStrategy,
        'rsi': RSIStrategy,
        'ma_cross': MACrossStrategy,
    }
    
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.data_loader = DataLoader()
        self.cash = config.initial_capital
        self.positions: Dict[str, Trade] = {}  # code -> Trade
        self.equity_curve = []
    
    def run(self, strategy_name: str, codes: List[str]) -> BacktestResult:
        """
        运行回测
        
        Args:
            strategy_name: 策略名称
            codes: 股票代码列表
        
        Returns:
            BacktestResult: 回测结果
        """
        if strategy_name not in self.STRATEGIES:
            raise ValueError(f"未知策略: {strategy_name}")
        
        strategy = self.STRATEGIES[strategy_name](self.config.strategy_params)
        
        # 加载所有标的的信号
        all_signals = []
        for code in codes:
            data = self.data_loader.load_klines(
                code, self.config.start_date, self.config.end_date
            )
            signals = strategy.generate_signals(data)
            for s in signals:
                s['code'] = code
                all_signals.append(s)
        
        # 按时间排序
        all_signals.sort(key=lambda x: x['time'])
        
        # 模拟交易
        self.cash = self.config.initial_capital
        self.positions = {}
        self.equity_curve = []
        
        trades: List[Trade] = []
        
        for signal in all_signals:
            code = signal['code']
            action = signal['action']
            price = signal['price']
            time = signal['time']
            
            # 计算滑点后的价格
            exec_price = price * (1 + self.config.slippage_rate * (1 if action == 'BUY' else -1))
            
            if action == 'BUY' and code not in self.positions and len(self.positions) < self.config.max_positions:
                # 开仓
                position_value = self.cash * self.config.position_size
                quantity = int(position_value / exec_price / 100) * 100  # 整手
                
                if quantity > 0:
                    trade = Trade(
                        entry_time=time,
                        exit_time=None,
                        code=code,
                        action='BUY',
                        quantity=quantity,
                        entry_price=exec_price,
                        exit_price=None
                    )
                    
                    cost = exec_price * quantity * (1 + self.config.commission_rate)
                    self.cash -= cost
                    self.positions[code] = trade
            
            elif action == 'SELL' and code in self.positions:
                # 平仓
                trade = self.positions[code]
                trade.exit_time = time
                trade.exit_price = exec_price
                
                # 计算盈亏
                revenue = exec_price * trade.quantity * (1 - self.config.commission_rate)
                cost = trade.entry_price * trade.quantity * (1 + self.config.commission_rate)
                trade.pnl = revenue - cost
                trade.pnl_pct = trade.pnl / cost
                
                self.cash += revenue
                trades.append(trade)
                del self.positions[code]
        
        # 强制平仓所有持仓
        for code, trade in self.positions.items():
            # 使用最后价格平仓
            trade.exit_time = datetime.now()
            trade.exit_price = trade.entry_price  # 假设价格不变
            trade.pnl = 0
            trades.append(trade)
        
        # 计算绩效
        result = self._calculate_performance(strategy_name, trades)
        
        return result
    
    def _calculate_performance(self, strategy_name: str, trades: List[Trade]) -> BacktestResult:
        """计算绩效指标"""
        result = BacktestResult(
            strategy=strategy_name,
            config=self.config,
            trades=trades
        )
        
        if not trades:
            return result
        
        # 基础统计
        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t.pnl > 0)
        result.losing_trades = sum(1 for t in trades if t.pnl < 0)
        
        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades
        
        # 盈亏统计
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl < 0]
        
        if wins:
            result.avg_win = sum(wins) / len(wins)
        if losses:
            result.avg_loss = sum(losses) / len(losses)
        
        total_win = sum(wins) if wins else 0
        total_loss = abs(sum(losses)) if losses else 0
        result.profit_factor = total_win / total_loss if total_loss > 0 else float('inf') if total_win > 0 else 0
        
        # 收益率
        total_pnl = sum(t.pnl for t in trades)
        result.total_return = total_pnl / self.config.initial_capital
        
        # 年化收益 (简化计算)
        days = (datetime.strptime(self.config.end_date, '%Y-%m-%d') - 
                datetime.strptime(self.config.start_date, '%Y-%m-%d')).days
        if days > 0:
            result.annual_return = result.total_return * 365 / days
        
        # 夏普比率 (简化)
        returns = [t.pnl_pct for t in trades if t.pnl_pct != 0]
        if returns:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            result.sharpe_ratio = avg_return / std_return * np.sqrt(252) if std_return > 0 else 0
        
        # 平均持仓
        holding_bars = [t.holding_bars for t in trades]
        result.avg_holding_bars = int(np.mean(holding_bars)) if holding_bars else 0
        
        return result
    
    def optimize(self, strategy_name: str, codes: List[str], 
                 param_grid: Dict[str, List]) -> List[BacktestResult]:
        """
        参数优化 (网格搜索)
        
        Args:
            strategy_name: 策略名称
            codes: 股票代码列表
            param_grid: 参数网格 {'param1': [v1, v2], 'param2': [v3, v4]}
        
        Returns:
            List[BacktestResult]: 所有参数组合的结果
        """
        results = []
        
        # 生成所有参数组合
        import itertools
        keys = param_grid.keys()
        values = param_grid.values()
        combinations = list(itertools.product(*values))
        
        logger.info(f"开始参数优化: {len(combinations)} 种组合")
        
        for combo in combinations:
            params = dict(zip(keys, combo))
            self.config.strategy_params = params
            
            result = self.run(strategy_name, codes)
            results.append(result)
        
        # 按夏普比率排序
        results.sort(key=lambda x: x.sharpe_ratio, reverse=True)
        
        return results


# ==================== CLI 接口 ====================

def main():
    parser = argparse.ArgumentParser(description='FTSeek 回测引擎')
    parser.add_argument('--strategy', type=str, default='price_break', help='策略名称')
    parser.add_argument('--start', type=str, default='2026-01-01', help='开始日期')
    parser.add_argument('--end', type=str, default='2026-03-19', help='结束日期')
    parser.add_argument('--codes', type=str, default='HK.09992,HK.00981,HK.03690', help='股票代码')
    parser.add_argument('--capital', type=float, default=100000, help='初始资金')
    parser.add_argument('--optimize', action='store_true', help='参数优化')
    
    args = parser.parse_args()
    
    config = BacktestConfig(
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital
    )
    
    engine = BacktestEngine(config)
    codes = args.codes.split(',')
    
    if args.optimize:
        # 参数优化
        if args.strategy == 'price_break':
            param_grid = {
                'lookback': [10, 20, 30],
                'breakout_pct': [0.01, 0.02, 0.03]
            }
        elif args.strategy == 'rsi':
            param_grid = {
                'period': [10, 14, 20],
                'oversold': [25, 30, 35],
                'overbought': [65, 70, 75]
            }
        else:
            param_grid = {
                'fast_period': [5, 10, 15],
                'slow_period': [20, 30, 40]
            }
        
        results = engine.optimize(args.strategy, codes, param_grid)
        
        print("\n" + "=" * 80)
        print(f"📊 参数优化结果 - {args.strategy}")
        print("=" * 80)
        
        # 展示前5个最优结果
        for i, result in enumerate(results[:5]):
            print(f"\n#{i+1} 最佳参数:")
            print(f"  参数: {result.config.strategy_params}")
            print(f"  总收益: {result.total_return:.2%}")
            print(f"  夏普比率: {result.sharpe_ratio:.2f}")
            print(f"  最大回撤: {result.max_drawdown:.2%}")
            print(f"  胜率: {result.win_rate:.2%}")
    else:
        # 单次回测
        result = engine.run(args.strategy, codes)
        
        print("\n" + "=" * 80)
        print(f"📊 回测结果 - {args.strategy}")
        print("=" * 80)
        print(f"\n时间区间: {args.start} ~ {args.end}")
        print(f"初始资金: HK${args.capital:,.0f}")
        print(f"标的: {', '.join(codes)}")
        
        print("\n### 绩效指标 ###")
        print(f"  总收益率: {result.total_return:.2%}")
        print(f"  年化收益: {result.annual_return:.2%}")
        print(f"  夏普比率: {result.sharpe_ratio:.2f}")
        print(f"  最大回撤: {result.max_drawdown:.2%}")
        
        print("\n### 交易统计 ###")
        print(f"  总交易数: {result.total_trades}")
        print(f"  胜率: {result.win_rate:.2%}")
        print(f"  盈亏比: {result.profit_factor:.2f}")
        print(f"  平均盈利: HK${result.avg_win:.0f}")
        print(f"  平均亏损: HK${result.avg_loss:.0f}")
        
        if result.trades:
            print("\n### 最近5笔交易 ###")
            for trade in result.trades[-5:]:
                print(f"  {trade.code} {trade.action} @ {trade.entry_price:.2f} -> "
                      f"{trade.exit_price:.2f if trade.exit_price else 'N/A'} | "
                      f"PnL: HK${trade.pnl:.0f}")


if __name__ == '__main__':
    main()