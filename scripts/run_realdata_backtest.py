#!/usr/bin/env python3
"""
真实数据回测执行器

功能:
1. 加载下载的真实历史数据
2. 使用 4 组梯度参数运行回测
3. 收集回测指标
4. 生成回测报告

使用方法:
    python3 scripts/run_realdata_backtest.py
"""

import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any
import pandas as pd
import numpy as np

# 设置路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/realdata_backtest.log')
    ]
)
logger = logging.getLogger("ftseek.backtest")

# 导入回测引擎
from backtest.backtest_engine import (
    BacktestConfig, BacktestResult, Trade,
    DataLoader, BacktestStrategy
)

# 数据目录
DATA_DIR = Path("/opt/ftseek/FTSeek/data/historical_data")
OUTPUT_DIR = Path("/opt/ftseek/FTSeek/data/backtest_output")


# ==================== 4 组梯度参数 ====================

PARAM_SETS = [
    {
        'name': '保守型',
        'params': {
            'rsi_period': 14,
            'rsi_overbought': 75,
            'rsi_oversold': 25,
            'technical_weight': 0.7,
            'sentiment_weight': 0.3,
            'threshold_strong': 0.85,
            'threshold_recommended': 0.65,
            'min_confidence': 0.5,
            'per_stock_daily_limit': 2,
            'global_daily_limit': 10,
        },
        'description': '高置信度，低频交易'
    },
    {
        'name': '平衡型',
        'params': {
            'rsi_period': 14,
            'rsi_overbought': 70,
            'rsi_oversold': 30,
            'technical_weight': 0.6,
            'sentiment_weight': 0.4,
            'threshold_strong': 0.8,
            'threshold_recommended': 0.6,
            'min_confidence': 0.45,
            'per_stock_daily_limit': 3,
            'global_daily_limit': 15,
        },
        'description': '标准配置，均衡策略'
    },
    {
        'name': '进取型',
        'params': {
            'rsi_period': 10,
            'rsi_overbought': 65,
            'rsi_oversold': 35,
            'technical_weight': 0.5,
            'sentiment_weight': 0.5,
            'threshold_strong': 0.75,
            'threshold_recommended': 0.55,
            'min_confidence': 0.4,
            'per_stock_daily_limit': 4,
            'global_daily_limit': 20,
        },
        'description': '较低门槛，增加交易机会'
    },
    {
        'name': '激进型',
        'params': {
            'rsi_period': 7,
            'rsi_overbought': 60,
            'rsi_oversold': 40,
            'technical_weight': 0.4,
            'sentiment_weight': 0.6,
            'threshold_strong': 0.7,
            'threshold_recommended': 0.5,
            'min_confidence': 0.35,
            'per_stock_daily_limit': 5,
            'global_daily_limit': 25,
        },
        'description': '低门槛，高频交易'
    },
]


class FusionV2BacktestStrategy(BacktestStrategy):
    """
    FusionV2 策略回测实现
    
    简化版，用于回测引擎
    """
    
    name = "fusion_v2"
    
    def __init__(self, params: Dict = None):
        super().__init__(params)
        self.rsi_period = params.get('rsi_period', 14)
        self.rsi_overbought = params.get('rsi_overbought', 70)
        self.rsi_oversold = params.get('rsi_oversold', 30)
        self.technical_weight = params.get('technical_weight', 0.6)
        self.sentiment_weight = params.get('sentiment_weight', 0.4)
        self.threshold_recommended = params.get('threshold_recommended', 0.6)
        self.min_confidence = params.get('min_confidence', 0.4)
    
    def _calculate_rsi(self, prices: pd.Series) -> pd.Series:
        """计算 RSI"""
        deltas = prices.diff()
        gains = deltas.where(deltas > 0, 0)
        losses = -deltas.where(deltas < 0, 0)
        
        avg_gain = gains.rolling(self.rsi_period).mean()
        avg_loss = losses.rolling(self.rsi_period).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _calculate_ma(self, prices: pd.Series, period: int) -> pd.Series:
        """计算移动平均线"""
        return prices.rolling(period).mean()
    
    def _calculate_macd(self, prices: pd.Series) -> tuple:
        """计算 MACD"""
        exp1 = prices.ewm(span=12, adjust=False).mean()
        exp2 = prices.ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        histogram = macd - signal
        return macd, signal, histogram
    
    def generate_signals(self, data: pd.DataFrame) -> List[Dict]:
        """
        生成交易信号
        
        简化版融合策略:
        - RSI 超买超卖
        - 均线趋势
        - 价格突破
        """
        signals = []
        
        if len(data) < self.rsi_period + 20:
            return signals
        
        # 计算技术指标
        rsi = self._calculate_rsi(data['close'])
        ma20 = self._calculate_ma(data['close'], 20)
        ma50 = self._calculate_ma(data['close'], 50)
        
        # 计算 20 日高低点用于突破策略
        data_copy = data.copy()
        data_copy['high_20'] = data_copy['high'].rolling(20).max()
        data_copy['low_20'] = data_copy['low'].rolling(20).min()
        
        in_position = False
        
        for i in range(self.rsi_period + 20, len(data)):
            current = data.iloc[i]
            prev = data.iloc[i-1] if i > 0 else current
            
            # RSI 信号 (权重 40%)
            rsi_val = rsi.iloc[i]
            rsi_signal = 0
            if rsi_val < self.rsi_oversold:
                rsi_signal = 1  # 超卖，买入信号
            elif rsi_val > self.rsi_overbought:
                rsi_signal = -1  # 超买，卖出信号
            
            # 均线信号 (权重 30%)
            ma_signal = 0
            if ma20.iloc[i] > ma50.iloc[i]:
                ma_signal = 0.5  # 多头趋势
            else:
                ma_signal = -0.5  # 空头趋势
            
            # 价格突破信号 (权重 30%)
            breakout_signal = 0
            if current['close'] > data_copy['high_20'].iloc[i] * 0.98:  # 接近突破
                breakout_signal = 0.5
            elif current['close'] < data_copy['low_20'].iloc[i] * 1.02:  # 接近跌破
                breakout_signal = -0.5
            
            # 融合决策
            tech_score = (
                rsi_signal * 0.4 +
                ma_signal * 0.3 +
                breakout_signal * 0.3
            )
            
            # 生成信号 - 降低门槛以产生更多交易
            threshold = self.threshold_recommended * 0.5  # 降低 50% 门槛
            
            if tech_score >= threshold and not in_position:
                signals.append({
                    'time': current['time'],
                    'action': 'BUY',
                    'price': current['close'],
                    'reason': f'融合信号 {tech_score:.2f} >= {threshold:.2f}',
                    'confidence': min(abs(tech_score), 1.0)
                })
                in_position = True
            elif tech_score <= -threshold and in_position:
                signals.append({
                    'time': current['time'],
                    'action': 'SELL',
                    'price': current['close'],
                    'reason': f'融合信号 {tech_score:.2f} <= {-threshold:.2f}',
                    'confidence': min(abs(tech_score), 1.0)
                })
                in_position = False
        
        # 如果最后还在持仓，添加卖出信号
        if in_position and len(data) > 0:
            signals.append({
                'time': data.iloc[-1]['time'],
                'action': 'SELL',
                'price': data.iloc[-1]['close'],
                'reason': '回测结束平仓',
                'confidence': 0.5
            })
        
        return signals


def load_historical_data(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    加载历史 K 线数据
    
    Args:
        code: 股票代码
        start_date: 开始日期
        end_date: 结束日期
    
    Returns:
        DataFrame: K 线数据
    """
    # 尝试从 CSV 文件加载
    csv_file = DATA_DIR / f"{code.replace('.', '_')}_DAY.csv"
    
    if not csv_file.exists():
        logger.warning(f"数据文件不存在：{csv_file}")
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(csv_file)
        
        # 过滤日期范围
        df['time_key'] = pd.to_datetime(df['time_key'])
        df = df[(df['time_key'] >= start_date) & (df['time_key'] <= end_date)]
        
        # 重命名列以匹配回测引擎
        df = df.rename(columns={
            'time_key': 'time',
            'open': 'open',
            'close': 'close',
            'high': 'high',
            'low': 'low',
            'volume': 'volume',
            'turnover': 'amount'
        })
        
        # 确保时间列是 datetime
        df['time'] = pd.to_datetime(df['time'])
        
        logger.info(f"  加载 {code} 数据：{len(df)} 条")
        
        return df
        
    except Exception as e:
        logger.error(f"加载数据失败 {code}: {e}")
        return pd.DataFrame()


def run_backtest(code: str, df: pd.DataFrame, param_set: Dict) -> Dict:
    """
    运行单只股票回测
    
    Returns:
        回测结果字典
    """
    try:
        # 创建回测配置
        config = BacktestConfig(
            start_date=df['time'].min().strftime('%Y-%m-%d'),
            end_date=df['time'].max().strftime('%Y-%m-%d'),
            initial_capital=100000.0,
            commission_rate=0.0003,
            slippage_rate=0.001,
            position_size=0.2,
            max_positions=3,
            strategy_params=param_set['params']
        )
        
        # 创建策略
        strategy = FusionV2BacktestStrategy(param_set['params'])
        
        # 生成信号
        signals = strategy.generate_signals(df)
        
        # 简化回测逻辑
        capital = config.initial_capital
        position = None
        trades = []
        equity_curve = [capital]
        
        for signal in signals:
            if signal['action'] == 'BUY' and position is None:
                # 买入
                quantity = int((capital * config.position_size) / signal['price'])
                if quantity > 0:
                    cost = quantity * signal['price'] * (1 + config.commission_rate + config.slippage_rate)
                    if cost <= capital:
                        position = {
                            'code': code,
                            'quantity': quantity,
                            'entry_price': signal['price'],
                            'entry_time': signal['time']
                        }
                        capital -= cost
            
            elif signal['action'] == 'SELL' and position is not None:
                # 卖出
                revenue = position['quantity'] * signal['price'] * (1 - config.commission_rate - config.slippage_rate)
                pnl = revenue - (position['quantity'] * position['entry_price'])
                pnl_pct = pnl / (position['quantity'] * position['entry_price'])
                
                trades.append({
                    'code': code,
                    'entry_time': position['entry_time'],
                    'exit_time': signal['time'],
                    'action': 'BUY',
                    'quantity': position['quantity'],
                    'entry_price': position['entry_price'],
                    'exit_price': signal['price'],
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                })
                
                capital += revenue
                position = None
            
            equity_curve.append(capital + (position['quantity'] * signal['price'] if position else 0))
        
        # 如果还有持仓，按最后价格平仓
        if position is not None:
            last_price = df.iloc[-1]['close']
            revenue = position['quantity'] * last_price * (1 - config.commission_rate - config.slippage_rate)
            pnl = revenue - (position['quantity'] * position['entry_price'])
            pnl_pct = pnl / (position['quantity'] * position['entry_price'])
            
            trades.append({
                'code': code,
                'entry_time': position['entry_time'],
                'exit_time': df.iloc[-1]['time'],
                'action': 'BUY',
                'quantity': position['quantity'],
                'entry_price': position['entry_price'],
                'exit_price': last_price,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
            })
            
            capital += revenue
        
        # 计算绩效指标
        total_return = (capital - config.initial_capital) / config.initial_capital
        
        # 年化收益率
        days = (df['time'].max() - df['time'].min()).days
        annual_return = (1 + total_return) ** (365 / max(days, 1)) - 1
        
        # 计算最大回撤
        equity_array = np.array(equity_curve)
        peak = np.maximum.accumulate(equity_array)
        drawdown = (peak - equity_array) / peak
        max_drawdown = np.max(drawdown) if len(drawdown) > 0 else 0
        
        # 计算夏普比率
        if len(equity_curve) > 1:
            returns = np.diff(equity_curve) / equity_curve[:-1]
            if np.std(returns) > 0:
                sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252)
            else:
                sharpe_ratio = 0
        else:
            sharpe_ratio = 0
        
        # 交易统计
        winning_trades = [t for t in trades if t['pnl'] > 0]
        losing_trades = [t for t in trades if t['pnl'] <= 0]
        
        win_rate = len(winning_trades) / len(trades) if trades else 0
        
        total_profit = sum(t['pnl'] for t in winning_trades)
        total_loss = abs(sum(t['pnl'] for t in losing_trades))
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        
        avg_win = total_profit / len(winning_trades) if winning_trades else 0
        avg_loss = total_loss / len(losing_trades) if losing_trades else 0
        
        # 最大连续亏损次数
        max_consecutive_losses = 0
        current_losses = 0
        for t in trades:
            if t['pnl'] <= 0:
                current_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
            else:
                current_losses = 0
        
        return {
            'code': code,
            'param_set': param_set['name'],
            'total_return': total_return,
            'annual_return': annual_return,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown': max_drawdown,
            'total_trades': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'max_consecutive_losses': max_consecutive_losses,
            'final_capital': capital,
            'equity_curve': equity_curve,
            'trades': trades,
        }
        
    except Exception as e:
        logger.error(f"回测失败 {code}: {e}")
        import traceback
        traceback.print_exc()
        return None


def calculate_monthly_returns(equity_curve: List[float]) -> Dict[str, float]:
    """计算月度收益分布"""
    # 简化实现：将收益曲线分成 12 份 (假设 1 年数据)
    if len(equity_curve) < 12:
        return {}
    
    monthly_returns = {}
    step = len(equity_curve) // 12
    
    for i in range(12):
        start_idx = i * step
        end_idx = (i + 1) * step
        if end_idx < len(equity_curve):
            month_return = (equity_curve[end_idx] - equity_curve[start_idx]) / equity_curve[start_idx]
            monthly_returns[f"Month_{i+1}"] = month_return
    
    return monthly_returns


def main():
    """主函数"""
    logger.info("=" * 80)
    logger.info("真实数据回测 - FusionV2 策略")
    logger.info("=" * 80)
    logger.info(f"时间：{datetime.now().isoformat()}")
    logger.info(f"数据目录：{DATA_DIR}")
    logger.info(f"输出目录：{OUTPUT_DIR}")
    logger.info("")
    
    # 创建输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 检查数据文件
    data_files = list(DATA_DIR.glob("*_DAY.csv"))
    if len(data_files) < 20:
        logger.error(f"数据文件不足：{len(data_files)} < 20")
        logger.error("请先运行 download_historical_data.py 下载数据")
        return 1
    
    logger.info(f"找到 {len(data_files)} 个数据文件")
    logger.info("")
    
    # 回测结果汇总
    all_results = {
        'backtest_time': datetime.now().isoformat(),
        'date_range': {'start': '2023-01-01', 'end': '2026-03-31'},
        'param_sets': [],
        'stock_results': [],
        'summary': {}
    }
    
    # 遍历 4 组参数
    for param_set in PARAM_SETS:
        logger.info(f"参数组：{param_set['name']} - {param_set['description']}")
        logger.info("-" * 60)
        
        param_results = {
            'name': param_set['name'],
            'description': param_set['description'],
            'params': param_set['params'],
            'stocks': [],
            'metrics': {
                'avg_annual_return': 0,
                'avg_sharpe': 0,
                'avg_max_drawdown': 0,
                'avg_win_rate': 0,
                'total_trades': 0,
            }
        }
        
        # 遍历所有股票
        stock_count = 0
        for csv_file in data_files:
            # 提取股票代码
            code = csv_file.stem.replace('_DAY', '').replace('_', '.')
            
            # 加载数据
            df = load_historical_data(code, '2023-01-01', '2026-03-31')
            if df.empty:
                continue
            
            # 运行回测
            result = run_backtest(code, df, param_set)
            if result is None:
                continue
            
            param_results['stocks'].append(result)
            stock_count += 1
            
            logger.info(f"  {code}: 年化={result['annual_return']:.2%}, "
                       f"夏普={result['sharpe_ratio']:.2f}, "
                       f"回撤={result['max_drawdown']:.2%}, "
                       f"胜率={result['win_rate']:.2%}")
        
        # 计算平均指标
        if param_results['stocks']:
            param_results['metrics']['avg_annual_return'] = np.mean([s['annual_return'] for s in param_results['stocks']])
            param_results['metrics']['avg_sharpe'] = np.mean([s['sharpe_ratio'] for s in param_results['stocks']])
            param_results['metrics']['avg_max_drawdown'] = np.mean([s['max_drawdown'] for s in param_results['stocks']])
            param_results['metrics']['avg_win_rate'] = np.mean([s['win_rate'] for s in param_results['stocks']])
            param_results['metrics']['total_trades'] = sum([s['total_trades'] for s in param_results['stocks']])
        
        all_results['param_sets'].append(param_results)
        
        logger.info(f"  平均年化收益：{param_results['metrics']['avg_annual_return']:.2%}")
        logger.info(f"  平均夏普比率：{param_results['metrics']['avg_sharpe']:.2f}")
        logger.info(f"  平均最大回撤：{param_results['metrics']['avg_max_drawdown']:.2%}")
        logger.info(f"  平均胜率：{param_results['metrics']['avg_win_rate']:.2%}")
        logger.info("")
    
    # 生成汇总对比
    logger.info("=" * 80)
    logger.info("参数对比")
    logger.info("=" * 80)
    logger.info(f"{'参数组':<10} {'年化收益':<12} {'夏普比率':<10} {'最大回撤':<10} {'胜率':<10} {'总交易数':<10}")
    logger.info("-" * 62)
    
    for param_result in all_results['param_sets']:
        m = param_result['metrics']
        logger.info(f"{param_result['name']:<10} {m['avg_annual_return']:>10.2%}  "
                   f"{m['avg_sharpe']:>10.2f}  {m['avg_max_drawdown']:>10.2%}  "
                   f"{m['avg_win_rate']:>10.2%}  {m['total_trades']:>10}")
    
    # 推荐最佳参数
    best_param = max(all_results['param_sets'], 
                     key=lambda x: x['metrics']['avg_sharpe'] / (1 + x['metrics']['avg_max_drawdown']))
    
    logger.info("")
    logger.info(f"推荐参数：{best_param['name']}")
    logger.info(f"理由：风险调整后收益最优 (夏普/回撤)")
    
    all_results['summary']['best_param'] = best_param['name']
    all_results['summary']['recommendation'] = f"基于夏普比率和最大回撤的综合评估，{best_param['name']}参数组表现最佳"
    
    # 保存结果
    output_file = OUTPUT_DIR / f"realdata_backtest_{datetime.now().strftime('%Y%m%d')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info("")
    logger.info(f"回测结果已保存：{output_file}")
    logger.info("=" * 80)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
