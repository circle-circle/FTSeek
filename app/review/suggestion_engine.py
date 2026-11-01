"""
智能建议引擎 (美股版)

基于规则的交易改进建议生成系统
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List

from app.storage.trade_recorder import get_trade_recorder

logger = logging.getLogger("ftseek_us.review.suggestion")
logger.propagate = False


class SuggestionEngine:
    """智能建议引擎 (美股版)"""
    
    RULES = [
        {
            'id': 'consecutive_losses',
            'condition': lambda s: s.get('max_consecutive_losses', 0) >= 3,
            'suggestion': '连续亏损 >= 3 笔，考虑暂停交易，调整心态',
            'priority': 'high',
            'category': '心理'
        },
        {
            'id': 'impulse_ratio',
            'condition': lambda s: s.get('impulse_ratio', 0) > 0.3,
            'suggestion': '冲动交易比例 > 30%，建议建立交易前检查清单',
            'priority': 'high',
            'category': '决策'
        },
        # 2026-11-01 拆除 'stop_loss_execution' 规则: 判据读 stop_loss_planned/stop_loss_executed,
        #   而活跃库 ftseek_us.db 无此两列 (PRAGMA 实测 52 列) ⇒ stop_loss_rate 恒默认 1.0 ⇒ 永不触发。
        #   该规则只在本模块自建的临时库 (TradeRecorder 旧 schema) 上"能跑"; 且本模块当前无任何调用方,
        #   数据源还是 0 行遗留库 data/trade_records.db — 是整块死子系统, 指标层不再保留冒牌读数。
        {
            'id': 'low_win_rate',
            'condition': lambda s: s.get('win_rate', 0) < 0.4,
            'suggestion': '胜率 < 40%，建议重新评估策略有效性',
            'priority': 'high',
            'category': '策略'
        },
        {
            'id': 'large_loss',
            'condition': lambda s: s.get('max_loss_ratio', 0) > 0.05,
            'suggestion': '单笔亏损 > 总资金 5%，建议降低仓位',
            'priority': 'critical',
            'category': '风控'
        }
    ]
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        # 2026-11-01 拆除 _ensure_stop_loss_columns(): 它把 stop_loss_planned/executed 的 ALTER
        #   打在**遗留库** data/trade_records.db (0 行, 4 月起停更), 活跃库 ftseek_us.db 永远没有这两列
        #   —— 正是"止损执行率恒 100%/恒 0"这类伪指标的根源 (band-aid 指向错库)。
        #   指标已按不可测拆除, 这里不再做无意义的建列。
    
    def generate_suggestions(self, period: str = 'last_30_days') -> List[Dict]:
        logger.info(f"生成智能建议 ({period})")
        
        stats = self._calculate_stats(period)
        
        if stats.get('total_trades', 0) == 0:
            return [{'message': '无交易记录'}]
        
        suggestions = []
        for rule in self.RULES:
            try:
                if rule['condition'](stats):
                    suggestions.append({
                        'rule_id': rule['id'],
                        'suggestion': rule['suggestion'],
                        'priority': rule['priority'],
                        'category': rule['category']
                    })
            except Exception as e:
                logger.warning(f"规则评估失败 {rule['id']}: {e}")
        
        # 排序
        priority_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        suggestions.sort(key=lambda s: priority_order.get(s['priority'], 4))
        
        # 正面反馈
        if stats.get('win_rate', 0) > 0.6:
            suggestions.append({
                'type': 'positive',
                'message': f"优秀！胜率达到 {stats['win_rate']:.1%}",
                'priority': 'low'
            })
        
        return suggestions
    
    def _calculate_stats(self, period: str) -> Dict:
        trades = self.trade_recorder.get_records_with_pnl()
        trades = self._filter_by_period(trades, period)
        
        if not trades:
            return {'total_trades': 0}
        
        total = len(trades)
        winning = [t for t in trades if t.get('realized_pnl', 0) > 0]
        losing = [t for t in trades if t.get('realized_pnl', 0) < 0]
        
        total_pnl = sum(t.get('realized_pnl', 0) for t in trades)
        max_loss = max([abs(t.get('realized_pnl', 0)) for t in losing], default=0)
        
        # 连续亏损
        max_consecutive = self._max_consecutive_losses(trades)
        
        # 冲动交易
        impulse = len([t for t in trades if t.get('decision_reason') == 'impulse'])
        
        return {
            'total_trades': total,
            'win_rate': len(winning) / total if total > 0 else 0,
            'total_pnl': total_pnl,
            'max_consecutive_losses': max_consecutive,
            'impulse_ratio': impulse / total if total > 0 else 0,
            'max_loss_ratio': max_loss / abs(total_pnl) if total_pnl != 0 else 0
        }
    
    def _filter_by_period(self, trades: List, period: str) -> List:
        if period == 'all':
            return trades
        cutoff = datetime.now() - timedelta(days=30)
        return [t for t in trades if datetime.fromisoformat(t.get('entry_time', '')[:19]) >= cutoff]
    
    def _max_consecutive_losses(self, trades: List) -> int:
        sorted_trades = sorted(trades, key=lambda t: t.get('entry_time', ''))
        max_consec = current = 0
        
        for t in sorted_trades:
            if t.get('realized_pnl', 0) < 0:
                current += 1
                max_consec = max(max_consec, current)
            else:
                current = 0
        
        return max_consec
    
    def get_critical_suggestions(self, period: str = 'last_30_days') -> List[Dict]:
        all_suggestions = self.generate_suggestions(period)
        return [s for s in all_suggestions if s.get('priority') in ['critical', 'high']]


def generate_suggestions(period: str = 'last_30_days') -> List[Dict]:
    engine = SuggestionEngine()
    return engine.generate_suggestions(period)


def get_critical_suggestions(period: str = 'last_30_days') -> List[Dict]:
    engine = SuggestionEngine()
    return engine.get_critical_suggestions(period)
