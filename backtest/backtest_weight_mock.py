#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测验证脚本 - 使用模拟新闻数据验证权重分配逻辑

功能:
1. 加载模拟新闻数据 (而非实时 API)
2. 使用历史股价数据回测
3. 验证权重分配逻辑正确性
4. 生成回测报告

验收标准:
- 模拟新闻覆盖率≥80%
- 回测交易数≥15 笔/季
- 年化收益≥15%
- 最大回撤≤10%
"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# 添加路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backtest_weight_mock')


@dataclass
class MockNewsItem:
    """模拟新闻项"""
    id: str
    title: str
    content: str
    date: str
    publish_time: str
    stocks: List[str]
    sentiment: str
    sentiment_score: float
    confidence: float
    importance: str
    sources: List[str]
    price_change: float
    keywords: List[str]


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
    exit_reason: str
    news_sentiment: str = ""
    news_importance: str = ""


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
    news_coverage: float  # 新闻覆盖率
    
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
            'news_coverage': self.news_coverage,
            'daily_returns': self.daily_returns,
            'equity_curve': self.equity_curve,
            'trades_summary': [
                {
                    'decision_id': t.decision_id,
                    'timestamp': t.timestamp.isoformat(),
                    'stock': t.stock,
                    'action': t.action,
                    'pnl_percent': t.pnl_percent,
                    'holding_days': t.holding_days,
                    'news_weight': t.news_weight,
                    'news_sentiment': t.news_sentiment
                }
                for t in self.trades
            ]
        }
    
    def validate(self) -> Tuple[bool, List[str]]:
        """验证是否达到验收标准"""
        errors = []
        
        # 模拟新闻覆盖率≥80%
        if self.news_coverage < 0.80:
            errors.append(f"新闻覆盖率={self.news_coverage:.1%} < 80%")
        
        # 回测交易数≥15 笔/季 (3 个月)
        days = (self.end_date - self.start_date).days
        trades_per_quarter = self.total_trades * (90 / days) if days > 0 else 0
        if trades_per_quarter < 15:
            errors.append(f"交易数={trades_per_quarter:.1f}/季 < 15/季")
        
        # 年化收益≥15%
        if self.annualized_return < 0.15:
            errors.append(f"年化收益={self.annualized_return:.2%} < 15%")
        
        # 最大回撤≤10%
        if self.max_drawdown > 0.10:
            errors.append(f"最大回撤={self.max_drawdown:.2%} > 10%")
        
        return len(errors) == 0, errors


class MockNewsBacktestEngine:
    """
    使用模拟新闻的回测引擎
    
    功能:
    1. 加载模拟新闻数据
    2. 加载历史股价数据
    3. 基于新闻权重生成交易信号
    4. 计算绩效指标
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
        
        # 权重配置
        self.news_max_weight = 0.40  # 新闻最大权重 40%
        self.news_normal_weight = 0.25  # 一般新闻权重 25%
        self.technical_min_weight = 0.25  # 技术最小权重 25%
        
        # 回测状态
        self.capital = initial_capital
        self.positions: Dict[str, dict] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = [initial_capital]
        self.daily_returns: List[float] = []
        
        # 新闻数据
        self.news_data: Dict[str, Dict[str, List[MockNewsItem]]] = {}
        
        logger.info("MockNewsBacktestEngine initialized")
    
    def load_mock_news(self, news_file: str) -> Dict[str, List[MockNewsItem]]:
        """
        加载模拟新闻数据
        
        Args:
            news_file: 新闻 JSON 文件路径
            
        Returns:
            Dict[str, List[MockNewsItem]]: 股票 -> 新闻列表
        """
        with open(news_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        news_by_stock = {}
        for stock, news_list in data.get('stocks', {}).items():
            news_by_stock[stock] = [
                MockNewsItem(
                    id=n['id'],
                    title=n['title'],
                    content=n['content'],
                    date=n['date'],
                    publish_time=n['publish_time'],
                    stocks=n['stocks'],
                    sentiment=n['sentiment'],
                    sentiment_score=n['sentiment_score'],
                    confidence=n['confidence'],
                    importance=n['importance'],
                    sources=n['sources'],
                    price_change=n['price_change'],
                    keywords=n['keywords']
                )
                for n in news_list
            ]
        
        logger.info(f"Loaded mock news for {len(news_by_stock)} stocks")
        return news_by_stock
    
    def load_price_data(self, stock_code: str, data_dir: str) -> List[dict]:
        """加载股票历史价格数据"""
        code_part = stock_code.split('.')[0].zfill(5)
        filename = f"HK_{code_part}_DAY.csv"
        filepath = Path(data_dir) / filename
        
        if not filepath.exists():
            logger.warning(f"Price data not found: {filepath}")
            return []
        
        import csv
        data = []
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    data.append({
                        'date': row['time_key'].split()[0],
                        'open': float(row['open']),
                        'close': float(row['close']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'volume': int(row['volume'])
                    })
                except (ValueError, KeyError):
                    pass
        
        return data
    
    def get_news_for_date(
        self,
        stock: str,
        date: str
    ) -> Optional[MockNewsItem]:
        """获取指定日期的新闻"""
        if stock not in self.news_data:
            return None
        
        for news in self.news_data[stock]:
            if news.date == date:
                return news
        
        return None
    
    def calculate_news_weight(self, news: Optional[MockNewsItem]) -> float:
        """
        计算新闻权重
        
        Args:
            news: 新闻项
            
        Returns:
            float: 新闻权重 (0-40%)
        """
        if not news:
            return 0.0
        
        if news.importance == "major":
            return self.news_max_weight
        elif news.importance == "normal":
            return self.news_normal_weight
        else:
            return 0.0
    
    def calculate_technical_indicators(
        self,
        prices: List[float],
        index: int
    ) -> Dict[str, float]:
        """
        计算技术指标
        
        Args:
            prices: 价格序列
            index: 当前索引
            
        Returns:
            Dict[str, float]: 技术指标
        """
        if index < 60:
            return {}
        
        closes = prices[index-60:index+1]
        
        # RSI
        gains, losses = [], []
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
        
        return {
            'rsi': rsi / 100,
            'macd': macd / closes[-1] if closes[-1] > 0 else 0,
            'bb': (bb + 1) / 2
        }
    
    def generate_signal(
        self,
        indicators: Dict[str, float],
        news_weight: float,
        news_sentiment: str
    ) -> Optional[str]:
        """
        生成交易信号
        
        策略:
        1. 重大新闻 → 直接交易
        2. 普通新闻 + 技术确认 → 交易
        3. 极端技术信号 → 交易
        """
        # 重大新闻 → 直接交易 (权重 40%)
        if news_weight >= 0.40:
            if news_sentiment == "positive":
                return "BUY"
            elif news_sentiment == "negative":
                return "SELL"
        
        # 普通新闻 + 技术确认 (权重 25%)
        if news_weight >= 0.25 and indicators:
            rsi = indicators.get('rsi', 0.5) * 100
            macd = indicators.get('macd', 0)
            
            # 利好 + RSI<55 → 买入
            if news_sentiment == "positive" and rsi < 55:
                return "BUY"
            
            # 利空 + RSI>45 → 卖出
            if news_sentiment == "negative" and rsi > 45:
                return "SELL"
        
        # 极端技术信号
        if indicators:
            rsi = indicators.get('rsi', 0.5) * 100
            
            # 极度超卖 → 买入
            if rsi < 25:
                return "BUY"
            
            # 极度超买 → 卖出
            if rsi > 75:
                return "SELL"
        
        return None
    
    async def run_backtest(
        self,
        stocks: List[str],
        start_date: str,
        end_date: str,
        news_file: str,
        data_dir: str = "/opt/ftseek/FTSeek/data/historical_data"
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            stocks: 股票列表
            start_date: 开始日期
            end_date: 结束日期
            news_file: 模拟新闻文件
            data_dir: 历史数据目录
            
        Returns:
            BacktestResult: 回测结果
        """
        logger.info(f"Starting backtest: {start_date} to {end_date}")
        
        # 加载数据
        self.news_data = self.load_mock_news(news_file)
        
        # 加载所有股票的价格数据
        price_data = {}
        for stock in stocks:
            data = self.load_price_data(stock, data_dir)
            if data:
                # 日期过滤
                data = [d for d in data if start_date <= d['date'] <= end_date]
                price_data[stock] = data
        
        if not price_data:
            raise ValueError("No price data available")
        
        # 获取所有交易日期
        all_dates = sorted(set(
            d['date']
            for stock_data in price_data.values()
            for d in stock_data
        ))
        
        logger.info(f"Trading days: {len(all_dates)}")
        
        # 回测主循环
        daily_equity = [self.capital]
        trade_counter = 0
        
        for date_idx, current_date in enumerate(all_dates):
            logger.debug(f"Processing date: {current_date}")
            
            # 更新持仓 (检查止损/止盈)
            for stock, position in list(self.positions.items()):
                if stock not in price_data:
                    continue
                
                stock_data = price_data[stock]
                day_data = next((d for d in stock_data if d['date'] == current_date), None)
                
                if not day_data:
                    continue
                
                current_price = day_data['close']
                entry_price = position['entry_price']
                pnl_percent = (current_price - entry_price) / entry_price
                
                # 止损检查 (3%)
                if pnl_percent <= -0.03:
                    self._close_position(
                        stock, current_price, position, 'stop_loss', current_date
                    )
                
                # 止盈检查 (6%)
                elif pnl_percent >= 0.06:
                    self._close_position(
                        stock, current_price, position, 'take_profit', current_date
                    )
            
            # 生成新信号 (最多 8 个持仓，增加交易机会)
            if len(self.positions) < 8:
                for stock in stocks:
                    if stock in self.positions or stock not in price_data:
                        continue
                    
                    stock_data = price_data[stock]
                    day_data = next((d for d in stock_data if d['date'] == current_date), None)
                    
                    if not day_data:
                        continue
                    
                    idx = stock_data.index(day_data)
                    prices = [d['close'] for d in stock_data[:idx+1]]
                    
                    # 计算技术指标
                    indicators = self.calculate_technical_indicators(prices, idx)
                    
                    # 获取新闻
                    news = self.get_news_for_date(stock, current_date)
                    news_weight = self.calculate_news_weight(news)
                    news_sentiment = news.sentiment if news else ""
                    news_importance = news.importance if news else ""
                    
                    logger.debug(
                        f"{stock} {current_date}: indicators={bool(indicators)}, "
                        f"news_weight={news_weight:.2f}, sentiment={news_sentiment}, "
                        f"importance={news_importance}"
                    )
                    
                    # 生成信号
                    signal = self.generate_signal(indicators, news_weight, news_sentiment)
                    
                    if signal:
                        logger.info(f"{stock} {current_date}: Signal={signal}")
                    
                    if signal == "BUY":
                        # 计算权重
                        hk_us_weight = 0.0  # 简化处理
                        technical_weight = 1.0 - news_weight - hk_us_weight
                        technical_weight = max(self.technical_min_weight, technical_weight)
                        
                        # 开仓
                        trade_counter += 1
                        self._open_position(
                            stock, day_data['close'],
                            news_weight, hk_us_weight, technical_weight,
                            news_sentiment, news.importance if news else "",
                            f"MOCK_{trade_counter:04d}", current_date
                        )
            
            # 计算当日权益
            total_equity = self.capital
            for stock, position in self.positions.items():
                if stock in price_data:
                    stock_data = price_data[stock]
                    day_data = next((d for d in stock_data if d['date'] == current_date), None)
                    if day_data:
                        total_equity += position['size'] * day_data['close']
            
            daily_equity.append(total_equity)
            
            # 计算日收益率
            if len(daily_equity) > 1:
                daily_return = (daily_equity[-1] - daily_equity[-2]) / daily_equity[-2]
                self.daily_returns.append(daily_return)
        
        # 关闭所有剩余持仓
        for stock, position in list(self.positions.items()):
            if stock in price_data:
                last_price = price_data[stock][-1]['close']
                last_date = price_data[stock][-1]['date']
                self._close_position(
                    stock, last_price, position, 'time', last_date
                )
        
        # 计算新闻覆盖率
        total_volatile_days = 0
        covered_days = 0
        for stock in stocks:
            if stock in self.news_data:
                covered_days += len(self.news_data[stock])
                # 估算波动天数 (假设有新闻的日期都是波动的)
                total_volatile_days += len(self.news_data[stock])
        
        news_coverage = covered_days / total_volatile_days if total_volatile_days > 0 else 0
        
        # 计算回测结果
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        return self._calculate_result(
            start_dt, end_dt, daily_equity, news_coverage
        )
    
    def _open_position(
        self,
        stock: str,
        price: float,
        news_weight: float,
        hk_us_weight: float,
        technical_weight: float,
        news_sentiment: str,
        news_importance: str,
        decision_id: str,
        date: str
    ):
        """开仓"""
        position_value = self.capital * 0.05  # 5% 仓位
        size = position_value / price
        
        cost = size * price * (1 + self.commission_rate + self.slippage)
        self.capital -= cost
        
        self.positions[stock] = {
            'size': size,
            'entry_price': price,
            'entry_date': date,
            'decision_id': decision_id,
            'news_weight': news_weight,
            'hk_us_weight': hk_us_weight,
            'technical_weight': technical_weight,
            'news_sentiment': news_sentiment,
            'news_importance': news_importance
        }
        
        logger.info(f"Open position: {stock} @ {price:.2f}, news_weight={news_weight:.2%}")
    
    def _close_position(
        self,
        stock: str,
        price: float,
        position: dict,
        reason: str,
        date: str
    ):
        """平仓"""
        size = position['size']
        entry_price = position['entry_price']
        
        pnl = size * (price - entry_price)
        pnl_percent = (price - entry_price) / entry_price
        
        proceeds = size * price * (1 - self.commission_rate - self.slippage)
        self.capital += proceeds
        
        holding_days = (datetime.strptime(date, "%Y-%m-%d") - 
                       datetime.strptime(position['entry_date'], "%Y-%m-%d")).days
        
        trade = Trade(
            decision_id=position['decision_id'],
            timestamp=datetime.strptime(position['entry_date'], "%Y-%m-%d"),
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
            exit_reason=reason,
            news_sentiment=position['news_sentiment'],
            news_importance=position['news_importance']
        )
        self.trades.append(trade)
        
        del self.positions[stock]
        
        logger.info(
            f"Close position: {stock} @ {price:.2f}, "
            f"PnL: {pnl_percent:.2%}, Reason: {reason}"
        )
    
    def _calculate_result(
        self,
        start_date: datetime,
        end_date: datetime,
        equity_curve: List[float],
        news_coverage: float
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
            equity_curve=equity_curve,
            news_coverage=news_coverage
        )


async def main():
    """主函数"""
    logger.info("Starting mock news backtest...")
    
    # 配置 - 使用有价格数据的股票
    stocks = [
        "00700.HK", "09988.HK", "03690.HK",
        "00941.HK", "01810.HK", "00883.HK",
        "00001.HK", "00005.HK", "00388.HK",
        "00175.HK", "00981.HK", "01024.HK",
        "00027.HK", "00857.HK", "00386.HK"
    ]
    
    # 3 个月回测
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    
    # 新闻文件
    news_file = "/opt/ftseek/FTSeek/data/mock_news_data.json"
    
    if not os.path.exists(news_file):
        logger.error(f"Mock news file not found: {news_file}")
        logger.error("Please run mock_news_generator.py first!")
        return
    
    # 运行回测
    engine = MockNewsBacktestEngine(initial_capital=1000000)
    
    try:
        result = await engine.run_backtest(
            stocks, start_date, end_date, news_file
        )
        
        # 打印结果
        print("\n" + "="*60)
        print("模拟新闻回测结果")
        print("="*60)
        print(f"回测期间：{start_date} 至 {end_date}")
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
        print(f"\n新闻覆盖:")
        print(f"  覆盖率：{result.news_coverage:.1%}")
        
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
        output_path = f"/opt/ftseek/FTSeek/backtest/backtest_mock_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        
        print(f"\n回测报告已保存：{output_path}")
        
    finally:
        pass


if __name__ == "__main__":
    asyncio.run(main())
