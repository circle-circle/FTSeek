"""
策略表现分析模块 (美股版)
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder

logger = logging.getLogger("ftseek_us.review.strategy")
logger.propagate = False


class StrategyAnalyzer:
    """策略表现分析器 (美股版)"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
    
    def analyze(self, strategy_name: str = None, period: str = 'last_30_days') -> Dict:
        trades = self.trade_recorder.get_records_with_pnl()
        trades = self._filter_by_period(trades, period)
        
        if strategy_name:
            trades = [t for t in trades if t.get('strategy') == strategy_name]
            return self._analyze_single(strategy_name, trades)
        else:
            return self._analyze_all(trades)
    
    def _filter_by_period(self, trades: List, period: str) -> List:
        if period == 'all':
            return trades
        cutoff = datetime.now() - timedelta(days=30 if period == 'last_30_days' else 7)
        return [t for t in trades if datetime.fromisoformat(t.get('entry_time', '')[:19]) >= cutoff]
    
    def _analyze_single(self, name: str, trades: List) -> Dict:
        if not trades:
            return {'strategy': name, 'message': '无交易记录'}
        
        total_pnl = sum(t.get('realized_pnl', 0) for t in trades)
        winning = [t for t in trades if t.get('realized_pnl', 0) > 0]
        win_rate = len(winning) / len(trades)
        
        return {
            'strategy': name,
            'total_trades': len(trades),
            'win_rate': round(win_rate, 4),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl': round(total_pnl / len(trades), 2)
        }
    
    def _analyze_all(self, trades: List) -> Dict:
        stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'pnl': 0})
        
        for t in trades:
            strategy = t.get('strategy', 'unknown') or 'unknown'
            stats[strategy]['trades'] += 1
            if t.get('realized_pnl', 0) > 0:
                stats[strategy]['wins'] += 1
            stats[strategy]['pnl'] += t.get('realized_pnl', 0)
        
        return {
            'total_strategies': len(stats),
            'strategy_breakdown': dict(stats)
        }


def analyze_strategy(strategy_name: str = None, period: str = 'last_30_days') -> Dict:
    analyzer = StrategyAnalyzer()
    return analyzer.analyze(strategy_name, period)
