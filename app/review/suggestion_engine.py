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
        {
            'id': 'stop_loss_execution',
            'condition': lambda s: s.get('stop_loss_rate', 1) < 0.8,
            'suggestion': '止损执行率 < 80%，需严格执行止损纪律',
            'priority': 'critical',
            'category': '风控'
        },
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
        # P0 修复 (F21 2026-05-12): 确保 trade_records 有 stop_loss 列
        self._ensure_stop_loss_columns()
    
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
        
        # 止损执行
        sl_trades = [t for t in trades if t.get('stop_loss_planned') and t['stop_loss_planned'] > 0]
        sl_executed = len([t for t in sl_trades if t.get('stop_loss_executed', True)])
        
        return {
            'total_trades': total,
            'win_rate': len(winning) / total if total > 0 else 0,
            'total_pnl': total_pnl,
            'max_consecutive_losses': max_consecutive,
            'impulse_ratio': impulse / total if total > 0 else 0,
            'stop_loss_rate': sl_executed / len(sl_trades) if sl_trades else 1.0,
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


    def _ensure_stop_loss_columns(self):
        """确保 trade_records 有 stop_loss_planned 和 stop_loss_executed 列 (F21 2026-05-12)"""
        import sqlite3
        try:
            db_path = self.trade_recorder.db_path if hasattr(self.trade_recorder, 'db_path') else None
            if not db_path:
                return
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT stop_loss_planned FROM trade_records LIMIT 0")
            except Exception:
                cursor.execute("ALTER TABLE trade_records ADD COLUMN stop_loss_planned REAL DEFAULT 0")
                conn.commit()
            try:
                cursor.execute("SELECT stop_loss_executed FROM trade_records LIMIT 0")
            except Exception:
                cursor.execute("ALTER TABLE trade_records ADD COLUMN stop_loss_executed REAL DEFAULT 0")
                conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"[SuggestionEngine] 列对齐检查失败: {e}")


def generate_suggestions(period: str = 'last_30_days') -> List[Dict]:
    engine = SuggestionEngine()
    return engine.generate_suggestions(period)


def get_critical_suggestions(period: str = 'last_30_days') -> List[Dict]:
    engine = SuggestionEngine()
    return engine.get_critical_suggestions(period)
