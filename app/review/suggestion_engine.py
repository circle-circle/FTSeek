"""
智能建议引擎

基于规则的交易改进建议生成系统
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.suggestion")
logger.propagate = False


class SuggestionEngine:
    """智能建议引擎"""
    
    # 基于规则的建议库
    RULES = [
        {
            'id': 'consecutive_losses',
            'name': '连续亏损警告',
            'condition': lambda stats: stats.get('max_consecutive_losses', 0) >= 3,
            'suggestion': '连续亏损 >= 3 笔，考虑暂停交易，调整心态',
            'priority': 'high',
            'category': '心理'
        },
        {
            'id': 'impulse_ratio',
            'name': '冲动交易警告',
            'condition': lambda stats: stats.get('impulse_trade_ratio', 0) > 0.3,
            'suggestion': '冲动交易比例 > 30%，建议建立交易前检查清单',
            'priority': 'high',
            'category': '决策'
        },
        {
            'id': 'stop_loss_execution',
            'name': '止损执行警告',
            'condition': lambda stats: stats.get('stop_loss_execution_rate', 1) < 0.8,
            'suggestion': '止损执行率 < 80%，需严格执行止损纪律',
            'priority': 'critical',
            'category': '风控'
        },
        {
            'id': 'low_win_rate',
            'name': '低胜率警告',
            'condition': lambda stats: stats.get('win_rate', 0) < 0.4,
            'suggestion': '胜率 < 40%，建议重新评估策略有效性',
            'priority': 'high',
            'category': '策略'
        },
        {
            'id': 'sim_real_gap',
            'name': '模拟实盘差距',
            'condition': lambda stats: stats.get('sim_real_win_rate_gap', 0) > 0.15,
            'suggestion': '模拟胜率 > 实盘胜率 + 15%，检查心理因素影响',
            'priority': 'medium',
            'category': '心理'
        },
        {
            'id': 'overtrading',
            'name': '过度交易警告',
            'condition': lambda stats: (
                stats.get('avg_holding_period', 1) < 1 and
                stats.get('win_rate', 0) < 0.5
            ),
            'suggestion': '平均持仓时间 < 1 天且胜率 < 50%，避免过度交易',
            'priority': 'medium',
            'category': '决策'
        },
        {
            'id': 'large_loss',
            'name': '大额亏损警告',
            'condition': lambda stats: stats.get('max_single_loss_ratio', 0) > 0.05,
            'suggestion': '单笔亏损 > 总资金 5%，建议降低仓位',
            'priority': 'critical',
            'category': '风控'
        },
        {
            'id': 'poor_timing',
            'name': '时机不佳警告',
            'condition': lambda stats: stats.get('avg_entry_timing_score', 5) < 5,
            'suggestion': '平均入场时机评分 < 5，建议优化入场策略',
            'priority': 'medium',
            'category': '时机'
        },
        {
            'id': 'early_exit',
            'name': '过早出场警告',
            'condition': lambda stats: stats.get('avg_exit_timing_score', 5) < 5,
            'suggestion': '平均出场时机评分 < 5，可能存在过早出场问题',
            'priority': 'medium',
            'category': '时机'
        },
        {
            'id': 'emotion_intensity',
            'name': '情绪过激警告',
            'condition': lambda stats: stats.get('avg_emotion_intensity', 3) > 7,
            'suggestion': '平均情绪强度 > 7，建议加强情绪管理',
            'priority': 'high',
            'category': '心理'
        },
        {
            'id': 'position_concentration',
            'name': '仓位集中警告',
            'condition': lambda stats: stats.get('avg_position_size', 0.1) > 0.3,
            'suggestion': '平均仓位 > 30%，风险过于集中',
            'priority': 'high',
            'category': '风控'
        },
        {
            'id': 'profit_factor',
            'name': '盈亏比不佳',
            'condition': lambda stats: stats.get('profit_factor', 1) < 1,
            'suggestion': '盈亏比 < 1，亏损大于盈利，需优化策略',
            'priority': 'high',
            'category': '策略'
        },
        {
            'id': 'confidence_mismatch',
            'name': '信心不匹配',
            'condition': lambda stats: abs(stats.get('avg_confidence', 5) - stats.get('actual_win_rate', 0) * 10) > 3,
            'suggestion': '信心水平与实际表现不匹配，建议调整预期',
            'priority': 'low',
            'category': '心理'
        }
    ]
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
    
    def generate_suggestions(self,
                             trader_id: str = 'user_001',
                             period: str = 'last_30_days',
                             account_type: str = None) -> List[Dict]:
        """
        生成智能建议
        
        Args:
            trader_id: 交易者 ID
            period: 时间周期
            account_type: 账户类型
        
        Returns:
            建议列表
        """
        logger.info(f"生成智能建议：{trader_id} ({period})")
        
        # 计算统计数据
        stats = self._calculate_statistics(period, account_type)
        
        if stats.get('total_trades', 0) == 0:
            return [{'message': '无交易记录，无法生成建议'}]
        
        # 应用规则
        suggestions = []
        for rule in self.RULES:
            try:
                if rule['condition'](stats):
                    suggestions.append({
                        'rule_id': rule['id'],
                        'rule_name': rule['name'],
                        'suggestion': rule['suggestion'],
                        'priority': rule['priority'],
                        'category': rule['category'],
                        'triggered_by': self._get_trigger_details(rule['id'], stats)
                    })
            except Exception as e:
                logger.warning(f"规则评估失败 {rule['id']}: {e}")
        
        # 按优先级排序
        priority_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        suggestions.sort(key=lambda s: priority_order.get(s['priority'], 4))
        
        # 添加正面反馈
        suggestions.extend(self._generate_positive_feedback(stats))
        
        logger.info(f"生成 {len(suggestions)} 条建议")
        return suggestions
    
    def _calculate_statistics(self, period: str, account_type: str = None) -> Dict:
        """计算统计数据"""
        trades = self.trade_recorder.get_records_with_pnl()
        
        # 时间过滤
        trades = self._filter_by_period(trades, period)
        
        # 账户类型过滤
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        if not trades:
            return {'total_trades': 0}
        
        total_trades = len(trades)
        
        # 基础统计
        winning = [t for t in trades if t.pnl and t.pnl > 0]
        losing = [t for t in trades if t.pnl and t.pnl < 0]
        
        total_pnl = sum(t.pnl for t in trades if t.pnl)
        total_won = sum(t.pnl for t in winning)
        total_lost = abs(sum(t.pnl for t in losing))
        
        win_rate = len(winning) / total_trades if total_trades > 0 else 0
        profit_factor = total_won / total_lost if total_lost > 0 else float('inf')
        
        # 连续亏损
        max_consecutive_losses = self._calculate_max_consecutive_losses(trades)
        
        # 冲动交易
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        impulse_ratio = len(impulse_trades) / total_trades if total_trades > 0 else 0
        
        # 止损执行
        stop_loss_trades = [t for t in trades if t.stop_loss_planned and t.stop_loss_planned > 0]
        stop_loss_executed = len([t for t in stop_loss_trades if t.stop_loss_executed])
        stop_loss_rate = stop_loss_executed / len(stop_loss_trades) if stop_loss_trades else 1.0
        
        # 持仓时间
        avg_holding = sum(t.holding_period or 0 for t in trades) / total_trades if total_trades > 0 else 0
        
        # 时机评分
        avg_entry_timing = sum(t.entry_timing_score or 5 for t in trades) / total_trades
        avg_exit_timing = sum(t.exit_timing_score or 5 for t in trades) / total_trades
        
        # 情绪
        avg_emotion_intensity = sum(t.emotion_intensity or 3 for t in trades) / total_trades
        
        # 仓位
        avg_position_size = sum(t.position_size_actual or 0 for t in trades) / total_trades
        
        # 最大单笔亏损
        max_single_loss = max([abs(t.pnl) for t in losing], default=0)
        max_single_loss_ratio = max_single_loss / abs(total_pnl) if total_pnl != 0 else 0
        
        # 信心
        avg_confidence = sum(t.confidence_entry or 5 for t in trades) / total_trades
        
        return {
            'total_trades': total_trades,
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'total_pnl': total_pnl,
            'win_rate': win_rate,
            'actual_win_rate': win_rate,  # 用于信心匹配计算
            'profit_factor': profit_factor if profit_factor != float('inf') else 999,
            'max_consecutive_losses': max_consecutive_losses,
            'impulse_trade_ratio': impulse_ratio,
            'stop_loss_execution_rate': stop_loss_rate,
            'avg_holding_period': avg_holding,
            'avg_entry_timing_score': avg_entry_timing,
            'avg_exit_timing_score': avg_exit_timing,
            'avg_emotion_intensity': avg_emotion_intensity,
            'avg_position_size': avg_position_size,
            'max_single_loss': max_single_loss,
            'max_single_loss_ratio': max_single_loss_ratio,
            'avg_confidence': avg_confidence,
            # 模拟实盘对比 (需要额外数据)
            'sim_real_win_rate_gap': 0  # 预留
        }
    
    def _filter_by_period(self, trades: List, period: str) -> List:
        """按时间周期过滤"""
        if period == 'all':
            return trades
        
        now = datetime.now()
        
        if period == 'last_7_days':
            cutoff = now - timedelta(days=7)
        elif period == 'last_30_days':
            cutoff = now - timedelta(days=30)
        elif period == 'last_90_days':
            cutoff = now - timedelta(days=90)
        else:
            cutoff = now - timedelta(days=30)
        
        return [
            t for t in trades
            if datetime.fromisoformat(t.timestamp[:19]) >= cutoff
        ]
    
    def _calculate_max_consecutive_losses(self, trades: List) -> int:
        """计算最大连续亏损次数"""
        sorted_trades = sorted(trades, key=lambda t: t.timestamp)
        
        max_consecutive = 0
        current_consecutive = 0
        
        for t in sorted_trades:
            if t.pnl and t.pnl < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        return max_consecutive
    
    def _get_trigger_details(self, rule_id: str, stats: Dict) -> str:
        """获取触发详情"""
        details = {
            'consecutive_losses': f"连续亏损 {stats.get('max_consecutive_losses', 0)} 笔",
            'impulse_ratio': f"冲动交易比例 {stats.get('impulse_trade_ratio', 0):.1%}",
            'stop_loss_execution': f"止损执行率 {stats.get('stop_loss_execution_rate', 0):.1%}",
            'low_win_rate': f"胜率 {stats.get('win_rate', 0):.1%}",
            'overtrading': f"平均持仓 {stats.get('avg_holding_period', 0):.1f} 天，胜率 {stats.get('win_rate', 0):.1%}",
            'large_loss': f"最大单笔亏损占比 {stats.get('max_single_loss_ratio', 0):.1%}",
            'poor_timing': f"平均入场评分 {stats.get('avg_entry_timing_score', 0):.1f}/10",
            'early_exit': f"平均出场评分 {stats.get('avg_exit_timing_score', 0):.1f}/10",
            'emotion_intensity': f"平均情绪强度 {stats.get('avg_emotion_intensity', 0):.1f}/10",
            'position_concentration': f"平均仓位 {stats.get('avg_position_size', 0):.1%}",
            'profit_factor': f"盈亏比 {stats.get('profit_factor', 0):.2f}"
        }
        
        return details.get(rule_id, '触发条件已满足')
    
    def _generate_positive_feedback(self, stats: Dict) -> List[Dict]:
        """生成正面反馈"""
        feedback = []
        
        # 高胜率
        if stats.get('win_rate', 0) > 0.6:
            feedback.append({
                'type': 'positive',
                'message': f"优秀！胜率达到 {stats['win_rate']:.1%}，保持当前策略",
                'priority': 'low',
                'category': '策略'
            })
        
        # 高止损执行率
        if stats.get('stop_loss_execution_rate', 0) > 0.95:
            feedback.append({
                'type': 'positive',
                'message': f"出色的纪律性！止损执行率 {stats['stop_loss_execution_rate']:.1%}",
                'priority': 'low',
                'category': '风控'
            })
        
        # 良好情绪控制
        if stats.get('avg_emotion_intensity', 3) < 4:
            feedback.append({
                'type': 'positive',
                'message': f"情绪控制良好，平均强度 {stats['avg_emotion_intensity']:.1f}/10",
                'priority': 'low',
                'category': '心理'
            })
        
        # 优秀盈亏比
        if stats.get('profit_factor', 0) > 2:
            feedback.append({
                'type': 'positive',
                'message': f"优秀的盈亏比 {stats['profit_factor']:.2f}，盈利是亏损的{stats['profit_factor']:.1f}倍",
                'priority': 'low',
                'category': '策略'
            })
        
        return feedback
    
    def get_suggestion_by_category(self, category: str, period: str = 'last_30_days') -> List[Dict]:
        """按类别获取建议"""
        all_suggestions = self.generate_suggestions(period=period)
        return [s for s in all_suggestions if s.get('category') == category]
    
    def get_critical_suggestions(self, period: str = 'last_30_days') -> List[Dict]:
        """获取关键建议"""
        all_suggestions = self.generate_suggestions(period=period)
        return [s for s in all_suggestions if s.get('priority') in ['critical', 'high']]


# 便捷函数
def generate_suggestions(trader_id: str = 'user_001', period: str = 'last_30_days') -> List[Dict]:
    """生成智能建议"""
    engine = SuggestionEngine()
    return engine.generate_suggestions(trader_id, period)


def get_critical_suggestions(period: str = 'last_30_days') -> List[Dict]:
    """获取关键建议"""
    engine = SuggestionEngine()
    return engine.get_critical_suggestions(period)
