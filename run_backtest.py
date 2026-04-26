#!/usr/bin/env python3
"""
DEPRECATED: 此脚本已废弃, 请使用以下方式运行回测:
  python -m app.backtest.backtest_engine  (编程方式)
  python app/autoresearch/backtest_adapter.py  (自动研究方式)
  python scripts/run_realdata_backtest.py  (真实数据回测)
"""
"""
FTSeek 策略回测试验
使用历史信号数据进行回测分析
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import json
from typing import Dict, List, Tuple
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ftseek.backtest")

class HistoricalBacktester:
    """历史数据回测器"""
    
    def __init__(self, db_path: str, initial_capital: float = 1000000.0):
        self.db_path = db_path
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.positions = {}  # {stock_code: {'quantity': int, 'avg_price': float}}
        self.trade_history = []
        self.daily_pnl = []
        
    def load_signals(self, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """从数据库加载信号数据"""
        conn = sqlite3.connect(self.db_path)
        
        query = "SELECT * FROM signals WHERE status IN ('executed', 'confirmed')"
        if start_date:
            query += f" AND created_at >= '{start_date}'"
        if end_date:
            query += f" AND created_at <= '{end_date}'"
        query += " ORDER BY created_at"
        
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        if not df.empty:
            df['created_at'] = pd.to_datetime(df['created_at'])
            logger.info(f"加载了 {len(df)} 个历史信号")
            logger.info(f"时间范围: {df['created_at'].min()} 到 {df['created_at'].max()}")
            logger.info(f"策略分布: {df['strategy'].value_counts().to_dict()}")
        
        return df
    
    def simulate_trade(self, signal: pd.Series) -> Dict:
        """模拟交易执行"""
        stock_code = signal['code']
        action = signal['action'].lower()
        price = signal['price']
        quantity = signal.get('quantity', 100)  # 默认100股
        
        # 计算交易成本
        trade_value = price * quantity
        commission = trade_value * 0.0003  # 0.03%佣金
        stamp_duty = trade_value * 0.001 if action == 'sell' else 0  # 卖出时印花税0.1%
        slippage = trade_value * 0.001  # 0.1%滑点
        
        total_cost = trade_value + commission + stamp_duty + slippage
        
        trade_record = {
            'timestamp': signal['created_at'],
            'stock_code': stock_code,
            'stock_name': signal.get('name', ''),
            'action': action,
            'price': price,
            'quantity': quantity,
            'trade_value': trade_value,
            'commission': commission,
            'stamp_duty': stamp_duty,
            'slippage': slippage,
            'total_cost': total_cost,
            'strategy': signal['strategy'],
            'confidence': signal.get('confidence', 0.5),
            'reason': signal.get('reason', '')
        }
        
        return trade_record
    
    def run_backtest(self, start_date: str = None, end_date: str = None) -> Dict:
        """运行回测"""
        logger.info("开始回测...")
        logger.info(f"初始资金: {self.initial_capital:,.2f}")
        
        # 加载信号
        signals_df = self.load_signals(start_date, end_date)
        
        if signals_df.empty:
            logger.warning("没有找到历史信号数据")
            return {
                'total_trades': 0,
                'final_capital': self.capital,
                'total_return': 0.0,
                'trade_history': []
            }
        
        # 按时间顺序处理信号
        signals_df = signals_df.sort_values('created_at')
        
        for idx, signal in signals_df.iterrows():
            trade = self.simulate_trade(signal)
            self.trade_history.append(trade)
            
            # 更新资金和持仓
            if trade['action'] == 'buy':
                if trade['total_cost'] <= self.capital:
                    self.capital -= trade['total_cost']
                    # 更新持仓
                    if trade['stock_code'] not in self.positions:
                        self.positions[trade['stock_code']] = {
                            'quantity': trade['quantity'],
                            'avg_price': trade['price'],
                            'total_cost': trade['total_cost']
                        }
                    else:
                        pos = self.positions[trade['stock_code']]
                        total_quantity = pos['quantity'] + trade['quantity']
                        total_cost = pos['total_cost'] + trade['total_cost']
                        pos['quantity'] = total_quantity
                        pos['avg_price'] = total_cost / total_quantity
                        pos['total_cost'] = total_cost
                    
                    logger.info(f"买入 {trade['stock_code']}: {trade['quantity']}股 @ {trade['price']:.2f}, 成本: {trade['total_cost']:,.2f}")
                else:
                    logger.warning(f"资金不足，无法执行买入: 需要 {trade['total_cost']:,.2f}, 可用 {self.capital:,.2f}")
            
            elif trade['action'] == 'sell':
                if trade['stock_code'] in self.positions:
                    pos = self.positions[trade['stock_code']]
                    
                    # 计算盈亏
                    buy_cost = pos['avg_price'] * trade['quantity']
                    sell_value = trade['trade_value'] - trade['commission'] - trade['stamp_duty'] - trade['slippage']
                    pnl = sell_value - buy_cost
                    
                    # 更新资金
                    self.capital += sell_value
                    
                    # 更新持仓
                    pos['quantity'] -= trade['quantity']
                    if pos['quantity'] <= 0:
                        del self.positions[trade['stock_code']]
                    else:
                        # 重新计算平均成本
                        remaining_cost = pos['total_cost'] * (pos['quantity'] / (pos['quantity'] + trade['quantity']))
                        pos['avg_price'] = remaining_cost / pos['quantity'] if pos['quantity'] > 0 else 0
                        pos['total_cost'] = remaining_cost
                    
                    trade['pnl'] = pnl
                    logger.info(f"卖出 {trade['stock_code']}: {trade['quantity']}股 @ {trade['price']:.2f}, 盈亏: {pnl:+,.2f}")
                else:
                    logger.warning(f"没有 {trade['stock_code']} 的持仓，无法执行卖出")
        
        # 计算最终结果
        total_trades = len(self.trade_history)
        final_capital = self.capital
        total_return = (final_capital - self.initial_capital) / self.initial_capital * 100
        
        # 计算胜率
        winning_trades = sum(1 for t in self.trade_history if t.get('pnl', 0) > 0)
        losing_trades = sum(1 for t in self.trade_history if t.get('pnl', 0) < 0)
        win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0
        
        result = {
            'initial_capital': self.initial_capital,
            'final_capital': final_capital,
            'total_return_pct': total_return,
            'total_return_amount': final_capital - self.initial_capital,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate_pct': win_rate,
            'current_positions': self.positions,
            'trade_count': len(self.trade_history),
            'trade_history': self.trade_history[:10]  # 只返回前10笔交易详情
        }
        
        return result
    
    def generate_report(self, result: Dict) -> str:
        """生成回测报告"""
        report = []
        report.append("=" * 60)
        report.append("FTSeek 策略回测报告")
        report.append("=" * 60)
        report.append(f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"初始资金: {result['initial_capital']:,.2f}")
        report.append(f"最终资金: {result['final_capital']:,.2f}")
        report.append(f"总收益: {result['total_return_amount']:+,.2f} ({result['total_return_pct']:+.2f}%)")
        report.append(f"总交易次数: {result['total_trades']}")
        report.append(f"盈利交易: {result['winning_trades']}")
        report.append(f"亏损交易: {result['losing_trades']}")
        report.append(f"胜率: {result['win_rate_pct']:.1f}%")
        report.append("")
        
        if result['current_positions']:
            report.append("当前持仓:")
            for stock, pos in result['current_positions'].items():
                report.append(f"  {stock}: {pos['quantity']}股 @ 均价{pos['avg_price']:.2f}")
        else:
            report.append("当前持仓: 无")
        
        report.append("")
        report.append("最近交易记录:")
        for i, trade in enumerate(result['trade_history'][:5], 1):
            pnl_str = f", 盈亏: {trade.get('pnl', 0):+,.2f}" if 'pnl' in trade else ""
            report.append(f"  {i}. {trade['timestamp']} - {trade['stock_code']}({trade.get('stock_name', '')}) "
                         f"{trade['action'].upper()} {trade['quantity']}股 @ {trade['price']:.2f}{pnl_str}")
        
        return "\n".join(report)

def main():
    """主函数"""
    db_path = "data/ftseek.db"
    
    if not os.path.exists(db_path):
        logger.error(f"数据库文件不存在: {db_path}")
        return
    
    # 创建回测器
    backtester = HistoricalBacktester(db_path, initial_capital=1000000.0)
    
    # 设置回测时间范围（最近30天）
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    
    logger.info(f"回测时间范围: {start_date} 到 {end_date}")
    
    # 运行回测
    result = backtester.run_backtest(start_date, end_date)
    
    # 生成报告
    report = backtester.generate_report(result)
    print(report)
    
    # 保存报告到文件
    report_file = f"data/backtest_output/historical_backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    os.makedirs(os.path.dirname(report_file), exist_ok=True)
    
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info(f"回测报告已保存到: {report_file}")
    
    # 保存详细结果到JSON
    json_file = report_file.replace('.txt', '.json')
    with open(json_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, default=str)
    
    logger.info(f"详细结果已保存到: {json_file}")

if __name__ == "__main__":
    main()