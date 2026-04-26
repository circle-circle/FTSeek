#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨市场套利与事件驱动策略回测脚本

功能:
- 回测 Lead-Lag 套利策略
- 回测事件驱动策略
- 分析绩效指标

使用示例:
    python backtest_cross_market.py --start 2025-01-01 --end 2025-12-31
"""

import sys
import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from dataclasses import dataclass, field

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy.lead_lag_tracker import get_lead_lag_tracker
from app.factors.duration_predictor import get_duration_predictor
from app.risk.dynamic_sizer import get_dynamic_positionSizer
from app.consensus.event_performance_tracker import get_event_performance_tracker


@dataclass
class BacktestConfig:
    """回测配置"""
    start_date: str = "2025-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 1000000.0
    commission_rate: float = 0.0003  # 0.03%
    slippage_rate: float = 0.001     # 0.1%


@dataclass
class BacktestResult:
    """回测结果"""
    # 基础统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # 收益统计
    total_return: float = 0.0
    avg_return: float = 0.0
    best_return: float = 0.0
    worst_return: float = 0.0
    max_drawdown: float = 0.0

    # 风险调整收益
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    # 策略细分
    strategy_stats: Dict = field(default_factory=dict)
    # {'lead_lag': {...}, 'event_driven': {...}}

    # 交易明细
    trades: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': self.win_rate,
            'total_return': self.total_return,
            'avg_return': self.avg_return,
            'best_return': self.best_return,
            'worst_return': self.worst_return,
            'max_drawdown': self.max_drawdown,
            'sharpe_ratio': self.sharpe_ratio,
            'sortino_ratio': self.sortino_ratio,
            'strategy_stats': self.strategy_stats,
        }


class CrossMarketBacktester:
    """跨市场套利回测器"""

    def __init__(self, config: BacktestConfig):
        """
        初始化回测器

        Args:
            config: 回测配置
        """
        self.config = config
        self.capital = config.initial_capital
        self.positions: Dict[str, Dict] = {}  # code -> {quantity, entry_price, direction}

        # 初始化模块
        self.lead_lag = get_lead_lag_tracker()
        self.sizer = get_dynamic_positionSizer()
        self.duration_predictor = get_duration_predictor()
        self.perf_tracker = get_event_performance_tracker()

        # 绩效记录
        self._trade_history: List[Dict] = []
        self._daily_returns: List[float] = []
        self._peak_capital = config.initial_capital

        # 美港股映射 (简化示例)
        self.hk_us_pairs = {
            'HK.00700': 'US.TCEHY',
            'HK.09988': 'US.BABA',
            'HK.01211': 'US.BYDDY',
            'HK.09868': 'US.NIO',
        }

        print(f"回测器已初始化")
        print(f"  开始日期：{config.start_date}")
        print(f"  结束日期：{config.end_date}")
        print(f"  初始资金：{config.initial_capital:,.0f}")

    def run(self) -> BacktestResult:
        """
        运行回测

        Returns:
            BacktestResult: 回测结果
        """
        print("\n开始回测...")

        # 生成模拟数据 (实际应从数据库或 API 获取)
        mock_data = self._generate_mock_data()

        # 回测主循环
        for date_str, data in mock_data.items():
            self._process_day(date_str, data)

        # 计算最终结果
        result = self._calculate_result()

        return result

    def _generate_mock_data(self) -> Dict[str, Dict]:
        """
        生成模拟回测数据

        实际使用时应从数据库或 API 获取真实数据
        """
        data = {}
        current = datetime.strptime(self.config.start_date, '%Y-%m-%d')
        end = datetime.strptime(self.config.end_date, '%Y-%m-%d')

        while current <= end:
            date_str = current.strftime('%Y-%m-%d')

            # 跳过周末
            if current.weekday() >= 6:
                current += timedelta(days=1)
                continue

            # 生成模拟的隔夜美股数据
            us_data = {}
            for hk_code, us_code in self.hk_us_pairs.items():
                # 随机生成涨跌幅 (-5% 到 +5%)
                import random
                change_pct = random.uniform(-0.05, 0.05)
                us_data[us_code] = {
                    'change_pct': change_pct,
                    'volume': random.randint(1000000, 10000000)
                }

            # 生成模拟新闻
            news_items = []
            if random.random() < 0.3:  # 30% 概率有新闻
                news_items.append({
                    'id': f'news_{date_str}',
                    'title': f'模拟新闻：{random.choice(["财报超预期", "政策利好", "产品发布", "高管变动"])}',
                    'sentiment': random.uniform(-1, 1)
                })

            data[date_str] = {
                'us_overnight': us_data,
                'news': news_items
            }

            current += timedelta(days=1)

        return data

    def _process_day(self, date_str: str, data: Dict):
        """
        处理单日数据

        Args:
            date_str: 日期字符串
            data: 当日数据
        """
        import random

        # 0. 先检查是否需要平仓 (每日开盘前检查)
        codes_to_close = []
        for code, position in list(self.positions.items()):
            # 模拟当日价格变动 (随机生成用于测试止损止盈)
            daily_move = random.uniform(-0.08, 0.08)  # -8% 到 +8%

            # 计算当前价格
            entry_price = position['entry_price']
            direction = position['direction']

            if direction == 'long':
                current_price = entry_price * (1 + daily_move)
            else:
                current_price = entry_price * (1 - daily_move)

            # 检查止损
            stop_loss_price = entry_price * (1 - position['stop_loss_pct'])
            take_profit_price = entry_price * (1 + position['take_profit_pct'])

            should_close = False
            reason = ""

            if direction == 'long':
                if current_price <= stop_loss_price:
                    should_close = True
                    reason = "stop_loss"
                elif current_price >= take_profit_price:
                    should_close = True
                    reason = "take_profit"
            else:  # short
                if current_price >= stop_loss_price:
                    should_close = True
                    reason = "stop_loss"
                elif current_price <= take_profit_price:
                    should_close = True
                    reason = "take_profit"

            if should_close:
                codes_to_close.append((code, current_price, reason))

        # 执行平仓
        for code, exit_price, reason in codes_to_close:
            self._close_trade(code, exit_price, reason)

        # 1. 处理美股隔夜表现 → 港股开盘套利
        us_data = data.get('us_overnight', {})

        # 累积更多数据用于 Lead-Lag 检测
        for hk_code, us_code in self.hk_us_pairs.items():
            if us_code not in us_data:
                continue

            us_change = us_data[us_code]['change_pct']

            # 记录价格变动到 Lead-Lag 追踪器
            base_time = datetime.strptime(date_str, '%Y-%m-%d').timestamp()
            self.lead_lag.record_price_move(us_code, base_time, us_change * 100)

            # 也为港股记录数据 (用于相关性计算)
            hk_change = us_change * 0.7  # 假设港股跟随美股 70% 的变动
            self.lead_lag.record_price_move(hk_code, base_time + 180, hk_change * 100)

        # 每 10 天执行一次套利 (等待 Lead-Lag 数据累积)
        day_num = datetime.strptime(date_str, '%Y-%m-%d').timetuple().tm_yday
        if day_num % 10 == 0:
            for hk_code, us_code in self.hk_us_pairs.items():
                if us_code not in us_data:
                    continue

                us_change = us_data[us_code]['change_pct']

                # 检测滞后关系
                metrics = self.lead_lag.detect_lag(us_code, hk_code, force_refresh=True)

                # 降低阈值以生成更多信号
                if metrics.confidence > 0.3 and abs(us_change) > 0.02:
                    # 计算动态仓位
                    arb_score = max(0.5, metrics.strength + metrics.confidence)
                    params = self.sizer.calculate(
                        arb_score=min(1.0, arb_score),
                        news_sentiment=0.0,
                        volatility=0.25
                    )

                    # 生成交易
                    direction = 'long' if us_change > 0 else 'short'
                    self._execute_trade(
                        code=hk_code,
                        direction=direction,
                        entry_price=self._get_mock_price(hk_code),
                        position_pct=params.position_pct,
                        stop_loss_pct=params.stop_loss_pct,
                        take_profit_pct=params.take_profit_pct,
                        strategy='lead_lag',
                        event_id=f'll_{date_str}_{hk_code}',
                        metadata={'us_code': us_code, 'us_change': us_change}
                    )

        # 2. 处理新闻事件 (提高新闻概率到 50%)
        if random.random() < 0.5:
            news = {
                'id': f'news_{date_str}',
                'title': f'模拟新闻：{random.choice(["财报超预期", "政策利好", "产品发布", "高管变动"])}',
                'sentiment': random.uniform(-1, 1)
            }
            # 预测持续时间
            duration = self.duration_predictor.predict(
                event_type='other',
                sentiment_score=news.get('sentiment', 0),
                news_count=1
            )

            # 生成交易
            params = self.sizer.calculate(
                arb_score=duration.confidence,
                news_sentiment=news.get('sentiment', 0),
                volatility=0.2
            )

            # 随机选择一个股票 (实际应使用新闻映射器)
            target_code = list(self.hk_us_pairs.keys())[0]
            direction = 'long' if news['sentiment'] > 0 else 'short'

            self._execute_trade(
                code=target_code,
                direction=direction,
                entry_price=self._get_mock_price(target_code),
                position_pct=params.position_pct,
                stop_loss_pct=params.stop_loss_pct,
                take_profit_pct=params.take_profit_pct,
                strategy='event_driven',
                event_id=news['id'],
                metadata={'news_title': news['title'], 'sentiment': news['sentiment']}
            )

    def _execute_trade(self,
                      code: str,
                      direction: str,
                      entry_price: float,
                      position_pct: float,
                      stop_loss_pct: float,
                      take_profit_pct: float,
                      strategy: str,
                      event_id: str,
                      metadata: Dict = None):
        """
        执行交易

        Args:
            code: 股票代码
            direction: 方向 (long/short)
            entry_price: 入场价
            position_pct: 仓位百分比
            stop_loss_pct: 止损百分比
            take_profit_pct: 止盈百分比
            strategy: 策略名称
            event_id: 事件 ID
            metadata: 元数据
        """
        # 计算仓位价值
        position_value = self.capital * position_pct

        # 扣除手续费和滑点
        commission = position_value * self.config.commission_rate
        slippage = position_value * self.config.slippage_rate
        total_cost = commission + slippage

        # 记录事件
        self.perf_tracker.record_event(
            event_id=event_id,
            event_type=strategy,
            predicted_direction=direction,
            target_stocks=[{'code': code, 'direction': direction}],
            arb_score=position_pct / 0.10,  # 标准化
            news_sentiment=metadata.get('sentiment', 0) if metadata else 0
        )

        # 持仓超过 1 个则跳过 (简化处理)
        if len(self.positions) >= 1:
            return

        # 记录持仓
        self.positions[code] = {
            'direction': direction,
            'entry_price': entry_price,
            'position_value': position_value,
            'stop_loss_pct': stop_loss_pct,
            'take_profit_pct': take_profit_pct,
            'event_id': event_id,
            'strategy': strategy,
            'metadata': metadata or {}
        }

        self.capital -= total_cost

    def _get_mock_price(self, code: str) -> float:
        """获取模拟价格"""
        # 简化处理：使用固定价格
        base_prices = {
            'HK.00700': 500.0,
            'HK.09988': 100.0,
            'HK.01211': 250.0,
            'HK.09868': 80.0,
        }
        return base_prices.get(code, 100.0)

    def _close_trade(self, code: str, exit_price: float, reason: str) -> Dict:
        """
        平仓交易

        Args:
            code: 股票代码
            exit_price: 出场价
            reason: 平仓原因

        Returns:
            Dict: 交易结果
        """
        if code not in self.positions:
            return {}

        position = self.positions[code]
        entry_price = position['entry_price']
        direction = position['direction']

        # 计算收益率
        if direction == 'long':
            return_pct = (exit_price - entry_price) / entry_price
        else:
            return_pct = (entry_price - exit_price) / entry_price

        # 扣除手续费和滑点
        return_pct -= (self.config.commission_rate + self.config.slippage_rate)

        # 更新资金
        position_value = position['position_value']
        profit_loss = position_value * return_pct
        self.capital += position_value + profit_loss

        # 记录绩效
        # 检查是否已记录过 (避免重复)
        existing = any(t.get('event_id') == position['event_id'] for t in self._trade_history)
        if not existing:
            self.perf_tracker.record_outcome(
                event_id=position['event_id'],
                trade_id=f"trade_{position['event_id']}_{code}",
                code=code,
                direction=direction,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=int(position_value / entry_price),
                actual_return=return_pct,
                max_drawdown=min(0, return_pct),
                max_profit=max(0, return_pct),
                holding_minutes=30,  # 简化
                exit_reason=reason,
                predicted_direction=direction
            )

        # 记录交易历史
        trade_record = {
            'code': code,
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'return_pct': return_pct,
            'strategy': position['strategy'],
            'event_id': position['event_id'],
            'date': datetime.now().strftime('%Y-%m-%d')
        }
        self._trade_history.append(trade_record)

        # 更新峰值资金和最大回撤
        if self.capital > self._peak_capital:
            self._peak_capital = self.capital
        current_drawdown = (self._peak_capital - self.capital) / self._peak_capital

        return {
            'return_pct': return_pct,
            'drawdown': current_drawdown
        }

    def _calculate_result(self) -> BacktestResult:
        """计算回测结果"""
        if not self._trade_history:
            return BacktestResult()

        returns = [t['return_pct'] for t in self._trade_history]
        winning = [r for r in returns if r > 0]
        losing = [r for r in returns if r <= 0]

        # 计算夏普比率 (简化)
        import numpy as np
        if len(returns) > 1:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            sharpe = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0

            # 索提诺比率
            downside_returns = [r for r in returns if r < 0]
            downside_std = np.std(downside_returns) if downside_returns else 0
            sortino = (avg_return / downside_std * np.sqrt(252)) if downside_std > 0 else 0
        else:
            sharpe = 0
            sortino = 0

        # 按策略分组统计
        strategy_stats = {}
        for strategy in ['lead_lag', 'event_driven']:
            strategy_returns = [t['return_pct'] for t in self._trade_history if t['strategy'] == strategy]
            if strategy_returns:
                strategy_stats[strategy] = {
                    'trades': len(strategy_returns),
                    'avg_return': sum(strategy_returns) / len(strategy_returns),
                    'win_rate': sum(1 for r in strategy_returns if r > 0) / len(strategy_returns)
                }

        result = BacktestResult(
            total_trades=len(self._trade_history),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=len(winning) / len(self._trade_history) if self._trade_history else 0,
            total_return=sum(returns),
            avg_return=sum(returns) / len(returns),
            best_return=max(returns),
            worst_return=min(returns),
            max_drawdown=min(0, min(returns)),  # 简化
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            strategy_stats=strategy_stats,
            trades=self._trade_history
        )

        return result


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='跨市场套利回测')
    parser.add_argument('--start', type=str, default='2025-01-01', help='开始日期')
    parser.add_argument('--end', type=str, default='2025-12-31', help='结束日期')
    parser.add_argument('--capital', type=float, default=1000000, help='初始资金')
    parser.add_argument('--output', type=str, help='输出文件路径')

    args = parser.parse_args()

    config = BacktestConfig(
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital
    )

    backtester = CrossMarketBacktester(config)
    result = backtester.run()

    # 输出结果
    print("\n" + "="*60)
    print("回测结果")
    print("="*60)

    print(f"总交易数：{result.total_trades}")
    print(f"胜率：{result.win_rate:.1%}")
    print(f"总收益：{result.total_return:.2%}")
    print(f"平均收益：{result.avg_return:.2%}")
    print(f"夏普比率：{result.sharpe_ratio:.2f}")
    print(f"索提诺比率：{result.sortino_ratio:.2f}")
    print(f"最大回撤：{result.max_drawdown:.2%}")

    print("\n策略细分:")
    for strategy, stats in result.strategy_stats.items():
        print(f"  {strategy}:")
        print(f"    交易数：{stats['trades']}")
        print(f"    平均收益：{stats['avg_return']:.2%}")
        print(f"    胜率：{stats['win_rate']:.1%}")

    # 保存到文件
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"\n结果已保存到：{args.output}")


if __name__ == "__main__":
    main()
