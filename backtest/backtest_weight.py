#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测验证脚本 - 动态权重分配系统回测

功能:
1. 使用 3 个月历史数据回测
2. 验证胜率≥45%
3. 验证年化收益≥20%
4. 验证最大回撤<5%
5. 生成回测报告

验收标准:
- 胜率≥45%
- 年化收益≥20%
- 最大回撤<5%
- 夏普比率>1.5
"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# 添加路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from strategies.weight_engine import WeightEngine, WeightResult
from modules.decision_log import DecisionLog

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backtest_weight')


@dataclass
class Trade:
    """交易记录"""
    decision_id: str
    timestamp: datetime
    stock: str
    action: str
    entry_price: float
    exit_price: Optional[float]
    position_size: float
    pnl: float
    pnl_percent: float
    news_weight: float
    hk_us_weight: float
    technical_weight: float
    holding_days: int
    exit_reason: str  # stop_loss/take_profit/time


@dataclass
class BacktestResult:
    """回测结果"""
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_capital: float
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float
    profit_factor: float
    trades: List[Trade]
    daily_returns: List[float]
    equity_curve: List[float]
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat(),
            'initial_capital': self.initial_capital,
            'final_capital': self.final_capital,
            'total_return': self.total_return,
            'annualized_return': self.annualized_return,
            'max_drawdown': self.max_drawdown,
            'sharpe_ratio': self.sharpe_ratio,
            'win_rate': self.win_rate,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'avg_win': self.avg_win,
            'avg_loss': self.avg_loss,
            'profit_factor': self.profit_factor,
            'daily_returns': self.daily_returns,
            'equity_curve': self.equity_curve,
            'trades_summary': [
                {
                    'decision_id': t.decision_id,
                    'timestamp': t.timestamp.isoformat(),
                    'stock': t.stock,
                    'action': t.action,
                    'pnl_percent': t.pnl_percent,
                    'holding_days': t.holding_days
                }
                for t in self.trades
            ]
        }
    
    def validate(self) -> Tuple[bool, List[str]]:
        """验证是否达到验收标准"""
        errors = []
        
        # 胜率≥45%
        if self.win_rate < 0.45:
            errors.append(f"胜率={self.win_rate:.2%} < 45%")
        
        # 年化收益≥20%
        if self.annualized_return < 0.20:
            errors.append(f"年化收益={self.annualized_return:.2%} < 20%")
        
        # 最大回撤<5%
        if self.max_drawdown > 0.05:
            errors.append(f"最大回撤={self.max_drawdown:.2%} > 5%")
        
        # 夏普比率>1.5
        if self.sharpe_ratio < 1.5:
            errors.append(f"夏普比率={self.sharpe_ratio:.2f} < 1.5")
        
        return len(errors) == 0, errors


class BacktestEngine:
    """
    回测引擎
    
    功能:
    1. 加载历史数据
    2. 模拟交易
    3. 计算绩效指标
    4. 生成回测报告
    """
    
    def __init__(
        self,
        initial_capital: float = 1000000,
        commission_rate: float = 0.0003,
        slippage: float = 0.001
    ):
        """
        初始化回测引擎
        
        Args:
            initial_capital: 初始资金
            commission_rate: 佣金率
            slippage: 滑点
        """
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.slippage = slippage
        
        self.weight_engine = WeightEngine()
        self.decision_log = DecisionLog()
        
        # 回测状态
        self.capital = initial_capital
        self.positions: Dict[str, dict] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.daily_returns: List[float] = []
    
    def load_historical_data(
        self,
        stocks: List[str],
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, List[dict]]:
        """
        加载历史数据
        
        Args:
            stocks: 股票列表
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            Dict[str, List[dict]]: 股票 -> 历史数据
        """
        # 实际实现需要从数据库或 API 加载
        # 这里使用模拟数据
        logger.info(f"Loading historical data for {len(stocks)} stocks")
        
        data = {}
        for stock in stocks:
            # 生成模拟数据
            days = (end_date - start_date).days
            prices = self._generate_mock_prices(days)
            
            data[stock] = [
                {
                    'date': start_date + timedelta(days=i),
                    'open': prices[i] * (1 + np.random.uniform(-0.01, 0.01)),
                    'high': prices[i] * (1 + np.random.uniform(0, 0.03)),
                    'low': prices[i] * (1 - np.random.uniform(0, 0.03)),
                    'close': prices[i],
                    'volume': np.random.randint(1000000, 10000000)
                }
                for i in range(days)
            ]
        
        return data
    
    def _generate_mock_prices(self, days: int, start_price: float = 100) -> List[float]:
        """生成模拟价格序列"""
        prices = [start_price]
        
        for i in range(1, days):
            # 随机游走 + 趋势
            drift = 0.0005  # 日均收益
            volatility = 0.02  # 日波动率
            
            change = np.random.normal(drift, volatility)
            prices.append(prices[-1] * (1 + change))
        
        return prices
    
    def generate_signals(
        self,
        stock: str,
        data: List[dict],
        index: int
    ) -> Tuple[Optional[str], Dict[str, float]]:
        """
        生成交易信号
        
        Args:
            stock: 股票代码
            data: 历史数据
            index: 当前索引
            
        Returns:
            Tuple[Optional[str], Dict[str, float]]: (信号，技术指标)
        """
        if index < 60:  # 需要足够的数据计算指标
            return None, {}
        
        # 计算技术指标
        closes = [d['close'] for d in data[index-60:index+1]]
        
        # RSI
        gains = []
        losses = []
        for i in range(1, 14):
            change = closes[-i] - closes[-i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        rs = avg_gain / avg_loss if avg_loss > 0 else 0
        rsi = 100 - (100 / (1 + rs))
        
        # MACD (简化)
        ema12 = np.mean(closes[-12:])
        ema26 = np.mean(closes[-26:])
        macd = ema12 - ema26
        
        # 布林带
        ma20 = np.mean(closes[-20:])
        std20 = np.std(closes[-20:])
        bb = (closes[-1] - ma20) / (2 * std20) if std20 > 0 else 0
        
        indicators = {
            'rsi': rsi / 100,  # 归一化
            'macd': macd / closes[-1],
            'bb': (bb + 1) / 2  # 归一化到 0-1
        }
        
        # 生成信号
        # 买入条件：RSI<40, MACD>0, BB<0.5
        if rsi < 40 and macd > 0 and bb < 0:
            return 'BUY', indicators
        
        # 卖出条件：RSI>60, MACD<0, BB>0.5
        if rsi > 60 and macd < 0 and bb > 0:
            return 'SELL', indicators
        
        return None, indicators
    
    async def run_backtest(
        self,
        stocks: List[str],
        start_date: datetime,
        end_date: datetime
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            stocks: 股票列表
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            BacktestResult: 回测结果
        """
        logger.info(f"Starting backtest: {start_date} to {end_date}")
        
        # 加载历史数据
        data = self.load_historical_data(stocks, start_date, end_date)
        
        # 获取交易日期
        dates = sorted(set(
            d['date'] 
            for stock_data in data.values() 
            for d in stock_data
        ))
        
        # 回测主循环
        daily_equity = [self.capital]
        
        for date_idx, current_date in enumerate(dates):
            logger.debug(f"Processing date: {current_date}")
            
            # 更新持仓
            for stock, position in list(self.positions.items()):
                if stock not in data:
                    continue
                
                # 获取当日价格
                stock_data = data[stock]
                day_data = next(
                    (d for d in stock_data if d['date'] == current_date),
                    None
                )
                
                if not day_data:
                    continue
                
                # 检查止损/止盈
                current_price = day_data['close']
                entry_price = position['entry_price']
                
                pnl_percent = (current_price - entry_price) / entry_price
                
                # 止损检查 (2%)
                if pnl_percent <= -0.02:
                    self._close_position(
                        stock, current_price, 
                        position, 'stop_loss',
                        current_date
                    )
                
                # 止盈检查 (5%)
                elif pnl_percent >= 0.05:
                    self._close_position(
                        stock, current_price,
                        position, 'take_profit',
                        current_date
                    )
            
            # 生成新信号
            if len(self.positions) < 5:  # 最多 5 个持仓
                for stock in stocks:
                    if stock in self.positions:
                        continue
                    
                    if stock not in data:
                        continue
                    
                    stock_data = data[stock]
                    day_data = next(
                        (d for d in stock_data if d['date'] == current_date),
                        None
                    )
                    
                    if not day_data:
                        continue
                    
                    # 生成信号
                    signal, indicators = self.generate_signals(
                        stock, stock_data,
                        stock_data.index(day_data)
                    )
                    
                    if signal == 'BUY':
                        # 计算权重
                        weight_result = await self.weight_engine.calculate_weights(
                            stock=stock,
                            action='BUY',
                            technical_indicators=indicators
                        )
                        
                        # 检查约束
                        if not weight_result.constraints_valid:
                            logger.warning(
                                f"Constraint failed for {stock}: "
                                f"{weight_result.constraint_errors}"
                            )
                            continue
                        
                        # 开仓
                        self._open_position(
                            stock, day_data['close'],
                            weight_result, current_date
                        )
            
            # 计算当日权益
            total_equity = self.capital
            for stock, position in self.positions.items():
                if stock in data:
                    stock_data = data[stock]
                    day_data = next(
                        (d for d in stock_data if d['date'] == current_date),
                        None
                    )
                    if day_data:
                        total_equity += position['size'] * day_data['close']
            
            daily_equity.append(total_equity)
            
            # 计算日收益率
            if len(daily_equity) > 1:
                daily_return = (daily_equity[-1] - daily_equity[-2]) / daily_equity[-2]
                self.daily_returns.append(daily_return)
        
        # 关闭所有剩余持仓
        for stock, position in list(self.positions.items()):
            if stock in data:
                last_price = data[stock][-1]['close']
                self._close_position(
                    stock, last_price,
                    position, 'time',
                    data[stock][-1]['date']
                )
        
        # 计算回测结果
        return self._calculate_result(
            start_date, end_date,
            daily_equity
        )
    
    def _open_position(
        self,
        stock: str,
        price: float,
        weight_result: WeightResult,
        date: datetime
    ):
        """开仓"""
        # 计算仓位大小 (5% 仓位起)
        position_value = self.capital * 0.05
        size = position_value / price
        
        # 扣除佣金和滑点
        cost = size * price * (1 + self.commission_rate + self.slippage)
        self.capital -= cost
        
        self.positions[stock] = {
            'size': size,
            'entry_price': price,
            'entry_date': date,
            'decision_id': weight_result.decision_id,
            'news_weight': weight_result.news_weight,
            'hk_us_weight': weight_result.hk_us_weight,
            'technical_weight': weight_result.technical_weight
        }
        
        logger.info(f"Open position: {stock} @ {price:.2f}")
    
    def _close_position(
        self,
        stock: str,
        price: float,
        position: dict,
        reason: str,
        date: datetime
    ):
        """平仓"""
        size = position['size']
        entry_price = position['entry_price']
        
        # 计算盈亏
        pnl = size * (price - entry_price)
        pnl_percent = (price - entry_price) / entry_price
        
        # 扣除佣金和滑点
        proceeds = size * price * (1 - self.commission_rate - self.slippage)
        self.capital += proceeds
        
        # 计算持有天数
        holding_days = (date - position['entry_date']).days
        
        # 记录交易
        trade = Trade(
            decision_id=position['decision_id'],
            timestamp=position['entry_date'],
            stock=stock,
            action='BUY',
            entry_price=entry_price,
            exit_price=price,
            position_size=size,
            pnl=pnl,
            pnl_percent=pnl_percent,
            news_weight=position['news_weight'],
            hk_us_weight=position['hk_us_weight'],
            technical_weight=position['technical_weight'],
            holding_days=holding_days,
            exit_reason=reason
        )
        self.trades.append(trade)
        
        # 移除持仓
        del self.positions[stock]
        
        logger.info(
            f"Close position: {stock} @ {price:.2f}, "
            f"PnL: {pnl_percent:.2%}, Reason: {reason}"
        )
    
    def _calculate_result(
        self,
        start_date: datetime,
        end_date: datetime,
        equity_curve: List[float]
    ) -> BacktestResult:
        """计算回测结果"""
        final_capital = equity_curve[-1]
        total_return = (final_capital - self.initial_capital) / self.initial_capital
        
        # 年化收益
        days = (end_date - start_date).days
        annualized_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0
        
        # 最大回撤
        peak = equity_curve[0]
        max_drawdown = 0
        for equity in equity_curve:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        # 夏普比率
        if self.daily_returns:
            avg_return = np.mean(self.daily_returns)
            std_return = np.std(self.daily_returns)
            sharpe_ratio = (avg_return / std_return) * np.sqrt(252) if std_return > 0 else 0
        else:
            sharpe_ratio = 0
        
        # 胜率
        winning_trades = [t for t in self.trades if t.pnl_percent > 0]
        losing_trades = [t for t in self.trades if t.pnl_percent <= 0]
        win_rate = len(winning_trades) / len(self.trades) if self.trades else 0
        
        # 平均盈亏
        avg_win = np.mean([t.pnl_percent for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t.pnl_percent for t in losing_trades]) if losing_trades else 0
        
        # 盈亏比
        gross_profit = sum(t.pnl for t in winning_trades)
        gross_loss = abs(sum(t.pnl for t in losing_trades))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        return BacktestResult(
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            win_rate=win_rate,
            total_trades=len(self.trades),
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            trades=self.trades,
            daily_returns=self.daily_returns,
            equity_curve=equity_curve
        )
    
    async def close(self):
        """关闭资源"""
        await self.weight_engine.close()


async def main():
    """主函数"""
    logger.info("Starting backtest...")
    
    # 配置
    stocks = [
        "00700.HK", "09988.HK", "03690.HK",
        "02318.HK", "00941.HK", "01810.HK",
        "09866.HK", "02020.HK", "00883.HK",
        "01211.HK"
    ]
    
    # 3 个月回测
    end_date = datetime.now()
    start_date = end_date - timedelta(days=90)
    
    # 运行回测
    engine = BacktestEngine(initial_capital=1000000)
    
    try:
        result = await engine.run_backtest(stocks, start_date, end_date)
        
        # 打印结果
        print("\n" + "="*60)
        print("回测结果")
        print("="*60)
        print(f"回测期间：{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
        print(f"初始资金：¥{result.initial_capital:,.0f}")
        print(f"最终资金：¥{result.final_capital:,.0f}")
        print(f"\n收益指标:")
        print(f"  总收益：{result.total_return:.2%}")
        print(f"  年化收益：{result.annualized_return:.2%}")
        print(f"  最大回撤：{result.max_drawdown:.2%}")
        print(f"  夏普比率：{result.sharpe_ratio:.2f}")
        print(f"\n交易统计:")
        print(f"  总交易：{result.total_trades}")
        print(f"  盈利交易：{result.winning_trades}")
        print(f"  亏损交易：{result.losing_trades}")
        print(f"  胜率：{result.win_rate:.2%}")
        print(f"  平均盈利：{result.avg_win:.2%}")
        print(f"  平均亏损：{result.avg_loss:.2%}")
        print(f"  盈亏比：{result.profit_factor:.2f}")
        
        # 验收标准验证
        print(f"\n验收标准验证:")
        valid, errors = result.validate()
        
        if valid:
            print("  ✅ 所有验收标准通过!")
        else:
            print("  ❌ 以下验收标准未通过:")
            for error in errors:
                print(f"    - {error}")
        
        # 保存结果
        output_path = f"/opt/ftseek/FTSeek/backtest/backtest_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        
        print(f"\n回测报告已保存：{output_path}")
        
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
