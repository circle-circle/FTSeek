#!/usr/bin/env python3
"""
FTSeek 数据积累脚本

功能:
1. 收集每日交易数据
2. 汇总信号记录
3. 更新历史数据库
4. 生成训练数据集

运行: 每日收盘后执行
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List
import shutil

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.database import get_database
from app.storage.signal_tracker import signal_tracker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.data_accumulator")


class DataAccumulator:
    """数据积累器"""
    
    def __init__(self):
        self.db = get_database()
        self.today = date.today().isoformat()
        self.data_dir = Path('/opt/ftseek/FTSeek/data/accumulated')
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def collect_daily_trades(self) -> List[Dict]:
        """收集今日交易数据"""
        trades = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           exit_price, exit_time, realized_pnl, stop_loss, 
                           take_profit, status, signal_id, strategy
                    FROM positions 
                    WHERE date(entry_time) = ?
                """, (self.today,))
                
                for row in cursor.fetchall():
                    trades.append({
                        'id': row[0],
                        'code': row[1],
                        'action': row[2],
                        'quantity': row[3],
                        'entry_price': row[4],
                        'entry_time': row[5],
                        'exit_price': row[6],
                        'exit_time': row[7],
                        'realized_pnl': row[8],
                        'stop_loss': row[9],
                        'take_profit': row[10],
                        'status': row[11],
                        'signal_id': row[12],
                        'strategy': row[13],
                        'date': self.today
                    })
        except Exception as e:
            logger.error(f"收集交易数据失败: {e}")
        
        logger.info(f"收集到 {len(trades)} 条交易记录")
        return trades
    
    def collect_daily_signals(self) -> List[Dict]:
        """收集今日信号数据"""
        signals = signal_tracker.get_today_signals()
        
        signal_data = []
        for s in signals:
            signal_data.append({
                'signal_id': s.signal_id,
                'code': s.code,
                'timestamp': s.timestamp,
                'strategy': s.strategy,
                'action': s.action,
                'price': s.signal_price,
                'quantity': s.quantity,
                'confidence': s.confidence,
                'consensus_score': s.consensus_score,
                'strategies': s.strategies,
                'executed': s.executed,
                'execution_price': s.execution_price,
                'reject_reason': s.reject_reason,
                'pnl': s.pnl,
                'market_regime': s.market_regime,
                'adx': s.adx,
                'date': self.today
            })
        
        logger.info(f"收集到 {len(signal_data)} 条信号记录")
        return signal_data
    
    def collect_market_data(self) -> Dict:
        """收集市场数据"""
        # 从富途获取当日收盘数据
        market_data = {
            'date': self.today,
            'stocks': {}
        }
        
        try:
            from futu import OpenQuoteContext, KLType
            ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            
            codes = ['HK.09992', 'HK.00981', 'HK.03690']
            for code in codes:
                ret, data = ctx.get_history_kline(code, start=self.today, end=self.today, ktype=KLType.K_DAY)
                if ret == 0 and len(data) > 0:
                    row = data.iloc[-1]
                    market_data['stocks'][code] = {
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'close': row['close'],
                        'volume': row['volume'],
                        'turnover': row['turnover']
                    }
            
            ctx.close()
        except Exception as e:
            logger.warning(f"获取市场数据失败: {e}")
        
        return market_data
    
    def update_historical_db(self, trades: List[Dict], signals: List[Dict]):
        """更新历史数据库"""
        history_db_path = self.data_dir / 'historical_data.db'
        
        conn = sqlite3.connect(history_db_path)
        cursor = conn.cursor()
        
        # 创建表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historical_trades (
                id INTEGER,
                code TEXT,
                action TEXT,
                quantity INTEGER,
                entry_price REAL,
                entry_time TEXT,
                exit_price REAL,
                exit_time TEXT,
                realized_pnl REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT,
                signal_id TEXT,
                strategy TEXT,
                date TEXT,
                PRIMARY KEY (id, date)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS historical_signals (
                signal_id TEXT PRIMARY KEY,
                code TEXT,
                timestamp TEXT,
                strategy TEXT,
                action TEXT,
                price REAL,
                quantity INTEGER,
                confidence REAL,
                consensus_score REAL,
                strategies TEXT,
                executed INTEGER,
                execution_price REAL,
                reject_reason TEXT,
                pnl REAL,
                market_regime TEXT,
                adx REAL,
                date TEXT
            )
        ''')
        
        # 插入交易数据
        for trade in trades:
            cursor.execute('''
                INSERT OR REPLACE INTO historical_trades 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade['id'], trade['code'], trade['action'], trade['quantity'],
                trade['entry_price'], trade['entry_time'], trade['exit_price'],
                trade['exit_time'], trade['realized_pnl'], trade['stop_loss'],
                trade['take_profit'], trade['status'], trade['signal_id'],
                trade['strategy'], trade['date']
            ))
        
        # 插入信号数据
        for signal in signals:
            cursor.execute('''
                INSERT OR REPLACE INTO historical_signals 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                signal['signal_id'], signal['code'], signal['timestamp'],
                signal['strategy'], signal['action'], signal['price'],
                signal['quantity'], signal['confidence'], signal['consensus_score'],
                json.dumps(signal['strategies']), 1 if signal['executed'] else 0,
                signal['execution_price'], signal['reject_reason'], signal['pnl'],
                signal['market_regime'], signal['adx'], signal['date']
            ))
        
        conn.commit()
        conn.close()
        
        logger.info(f"历史数据库已更新: {len(trades)} 交易, {len(signals)} 信号")
    
    def export_training_data(self, signals: List[Dict]) -> str:
        """导出训练数据集"""
        if not signals:
            return ""
        
        training_data = []
        for s in signals:
            # 特征
            features = {
                'confidence': s['confidence'],
                'consensus_score': s['consensus_score'],
                'strategies_count': len(s['strategies']) if s['strategies'] else 0,
                'market_regime': s['market_regime'],
                'adx': s['adx'] or 0,
            }
            
            # 标签
            label = {
                'executed': s['executed'],
                'profitable': s['pnl'] > 0 if s['pnl'] else None
            }
            
            training_data.append({
                'features': features,
                'label': label,
                'metadata': {
                    'signal_id': s['signal_id'],
                    'code': s['code'],
                    'date': s['date'],
                    'action': s['action']
                }
            })
        
        # 保存
        output_path = self.data_dir / f"training_data_{self.today}.json"
        with open(output_path, 'w') as f:
            json.dump(training_data, f, indent=2, ensure_ascii=False)
        
        logger.info(f"训练数据已导出: {output_path}")
        return str(output_path)
    
    def generate_daily_summary(self, trades: List[Dict], signals: List[Dict]) -> Dict:
        """生成每日汇总"""
        # 交易统计
        total_trades = len(trades)
        closed_trades = [t for t in trades if t['status'] == 'CLOSED']
        total_pnl = sum(t['realized_pnl'] or 0 for t in closed_trades)
        
        win_trades = [t for t in closed_trades if (t['realized_pnl'] or 0) > 0]
        loss_trades = [t for t in closed_trades if (t['realized_pnl'] or 0) < 0]
        win_rate = len(win_trades) / len(closed_trades) * 100 if closed_trades else 0
        
        # 信号统计
        total_signals = len(signals)
        executed_signals = [s for s in signals if s['executed']]
        rejected_signals = [s for s in signals if not s['executed'] and s['reject_reason']]
        execution_rate = len(executed_signals) / total_signals * 100 if total_signals > 0 else 0
        
        # 置信度统计
        confidences = [s['confidence'] for s in signals if s['confidence']]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
        
        summary = {
            'date': self.today,
            'trades': {
                'total': total_trades,
                'closed': len(closed_trades),
                'winning': len(win_trades),
                'losing': len(loss_trades),
                'win_rate': win_rate,
                'total_pnl': total_pnl
            },
            'signals': {
                'total': total_signals,
                'executed': len(executed_signals),
                'rejected': len(rejected_signals),
                'execution_rate': execution_rate,
                'avg_confidence': avg_confidence
            }
        }
        
        return summary
    
    def run(self):
        """运行数据积累"""
        logger.info(f"开始数据积累: {self.today}")
        
        # 1. 收集数据
        trades = self.collect_daily_trades()
        signals = self.collect_daily_signals()
        market_data = self.collect_market_data()
        
        # 2. 更新历史数据库
        self.update_historical_db(trades, signals)
        
        # 3. 导出训练数据
        training_file = self.export_training_data(signals)
        
        # 4. 生成汇总
        summary = self.generate_daily_summary(trades, signals)
        
        # 保存汇总
        summary_file = self.data_dir / f"summary_{self.today}.json"
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        # 保存市场数据
        market_file = self.data_dir / f"market_{self.today}.json"
        with open(market_file, 'w') as f:
            json.dump(market_data, f, indent=2, ensure_ascii=False)
        
        logger.info("数据积累完成")
        
        # 打印摘要
        print(f"\n{'='*60}")
        print(f"📊 FTSeek 数据积累 - {self.today}")
        print(f"{'='*60}")
        print(f"交易: {summary['trades']['total']} 笔, 盈亏: HK${summary['trades']['total_pnl']:,.0f}")
        print(f"信号: {summary['signals']['total']} 个, 执行率: {summary['signals']['execution_rate']:.1f}%")
        print(f"{'='*60}\n")
        
        return summary


def main():
    accumulator = DataAccumulator()
    accumulator.run()


if __name__ == '__main__':
    main()