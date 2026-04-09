"""
心理状态分析模块

分析交易者的心理状态对交易表现的影响
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.psychology")
logger.propagate = False


class PsychologyAnalyzer:
    """心理状态分析器"""
    
    # 情绪类型定义
    EMOTION_TYPES = {
        'calm': {'name': '平静', 'impact': 'neutral', 'description': '心态平和，理性决策'},
        'anxious': {'name': '焦虑', 'impact': 'negative', 'description': '过度担忧，可能过早出场'},
        'excited': {'name': '兴奋', 'impact': 'negative', 'description': '过度乐观，可能忽视风险'},
        'fearful': {'name': '恐惧', 'impact': 'negative', 'description': '害怕亏损，不敢行动或过早出场'},
        'greedy': {'name': '贪婪', 'impact': 'negative', 'description': '追求超额收益，可能过度交易'},
        'frustrated': {'name': '沮丧', 'impact': 'negative', 'description': '亏损后情绪低落，可能报复性交易'}
    }
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
    
    def analyze(self,
                period: str = 'last_30_days',
                account_type: str = None) -> Dict[str, Any]:
        """
        分析心理状态
        
        Args:
            period: 时间周期
            account_type: 账户类型
        
        Returns:
            心理分析报告
        """
        logger.info(f"分析心理状态：{period}")
        
        # 获取交易记录
        trades = self.trade_recorder.get_records_with_pnl()
        
        # 时间过滤
        trades = self._filter_by_period(trades, period)
        
        # 账户类型过滤
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        if not trades:
            return {'message': '无符合条件的交易记录'}
        
        return {
            'period': period,
            'total_trades': len(trades),
            'emotion_distribution': self._analyze_emotion_distribution(trades),
            'emotion_performance': self._analyze_emotion_performance(trades),
            'intensity_analysis': self._analyze_intensity(trades),
            'confidence_analysis': self._analyze_confidence(trades),
            'psychology_impact': self._calculate_psychology_impact(trades),
            'best_state': self._identify_best_state(trades),
            'worst_state': self._identify_worst_state(trades),
            'recommendations': self._generate_recommendations(trades)
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
    
    def _analyze_emotion_distribution(self, trades: List) -> Dict:
        """分析情绪分布"""
        emotion_counts = defaultdict(int)
        
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_counts[emotion] += 1
        
        total = len(trades)
        
        return {
            'counts': dict(emotion_counts),
            'percentages': {
                emotion: round(count / total, 4)
                for emotion, count in emotion_counts.items()
            },
            'dominant_emotion': max(emotion_counts.items(), key=lambda x: x[1])[0] if emotion_counts else 'calm'
        }
    
    def _analyze_emotion_performance(self, trades: List) -> Dict:
        """分析各情绪状态下的交易表现"""
        emotion_stats = defaultdict(lambda: {
            'trades': 0,
            'wins': 0,
            'total_pnl': 0,
            'intensity_sum': 0
        })
        
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_stats[emotion]['trades'] += 1
            
            if t.pnl and t.pnl > 0:
                emotion_stats[emotion]['wins'] += 1
            
            emotion_stats[emotion]['total_pnl'] += t.pnl or 0
            emotion_stats[emotion]['intensity_sum'] += t.emotion_intensity or 3
        
        # 计算指标
        result = {}
        for emotion, stats in emotion_stats.items():
            result[emotion] = {
                'trades': stats['trades'],
                'win_rate': round(stats['wins'] / stats['trades'], 4) if stats['trades'] > 0 else 0,
                'total_pnl': round(stats['total_pnl'], 2),
                'avg_pnl': round(stats['total_pnl'] / stats['trades'], 2) if stats['trades'] > 0 else 0,
                'avg_intensity': round(stats['intensity_sum'] / stats['trades'], 2) if stats['trades'] > 0 else 0
            }
        
        return result
    
    def _analyze_intensity(self, trades: List) -> Dict:
        """分析情绪强度"""
        intensities = [t.emotion_intensity or 3 for t in trades]
        
        avg_intensity = sum(intensities) / len(intensities)
        
        # 按强度分组
        low_intensity = [t for t in trades if (t.emotion_intensity or 3) <= 3]
        medium_intensity = [t for t in trades if 3 < (t.emotion_intensity or 3) <= 7]
        high_intensity = [t for t in trades if (t.emotion_intensity or 3) > 7]
        
        return {
            'avg_intensity': round(avg_intensity, 2),
            'min_intensity': min(intensities),
            'max_intensity': max(intensities),
            'low_intensity_trades': len(low_intensity),
            'medium_intensity_trades': len(medium_intensity),
            'high_intensity_trades': len(high_intensity),
            'low_intensity_win_rate': round(
                len([t for t in low_intensity if t.pnl and t.pnl > 0]) / len(low_intensity), 4
            ) if low_intensity else 0,
            'high_intensity_win_rate': round(
                len([t for t in high_intensity if t.pnl and t.pnl > 0]) / len(high_intensity), 4
            ) if high_intensity else 0
        }
    
    def _analyze_confidence(self, trades: List) -> Dict:
        """分析信心水平"""
        entry_confidence = [t.confidence_entry or 5 for t in trades]
        exit_confidence = [t.confidence_exit or 5 for t in trades]
        
        avg_entry = sum(entry_confidence) / len(entry_confidence)
        avg_exit = sum(exit_confidence) / len(exit_confidence)
        
        # 信心变化
        confidence_changes = [
            (t.confidence_exit or 5) - (t.confidence_entry or 5)
            for t in trades
        ]
        
        # 信心过高/过低的交易
        overconfident = [t for t in trades if (t.confidence_entry or 5) >= 9]
        underconfident = [t for t in trades if (t.confidence_entry or 5) <= 3]
        
        return {
            'avg_entry_confidence': round(avg_entry, 2),
            'avg_exit_confidence': round(avg_exit, 2),
            'avg_confidence_change': round(sum(confidence_changes) / len(confidence_changes), 2),
            'confidence_increase_count': len([c for c in confidence_changes if c > 0]),
            'confidence_decrease_count': len([c for c in confidence_changes if c < 0]),
            'overconfident_trades': len(overconfident),
            'underconfident_trades': len(underconfident),
            'overconfident_win_rate': round(
                len([t for t in overconfident if t.pnl and t.pnl > 0]) / len(overconfident), 4
            ) if overconfident else 0,
            'underconfident_win_rate': round(
                len([t for t in underconfident if t.pnl and t.pnl > 0]) / len(underconfident), 4
            ) if underconfident else 0
        }
    
    def _calculate_psychology_impact(self, trades: List) -> Dict:
        """计算心理因素对交易的影响"""
        # 冲动交易
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        impulse_ratio = len(impulse_trades) / len(trades) if trades else 0
        
        # 止损违规
        stop_loss_violations = [
            t for t in trades
            if t.stop_loss_planned and t.stop_loss_planned > 0 and not t.stop_loss_executed
        ]
        violation_ratio = len(stop_loss_violations) / len(trades) if trades else 0
        
        # 情绪化交易 (高情绪强度 + 低决策质量)
        emotional_trades = [
            t for t in trades
            if (t.emotion_intensity or 3) > 7 and (t.decision_quality_score or 5) < 5
        ]
        emotional_ratio = len(emotional_trades) / len(trades) if trades else 0
        
        # 心理影响评分 (1-10, 越高表示心理负面影响越大)
        impact_score = (
            impulse_ratio * 3 +
            violation_ratio * 4 +
            emotional_ratio * 3
        ) * 10
        
        return {
            'impulse_trade_ratio': round(impulse_ratio, 4),
            'stop_loss_violation_ratio': round(violation_ratio, 4),
            'emotional_trade_ratio': round(emotional_ratio, 4),
            'psychology_impact_score': round(min(impact_score, 10), 2),
            'impact_level': 'low' if impact_score < 3 else 'medium' if impact_score < 6 else 'high'
        }
    
    def _identify_best_state(self, trades: List) -> Dict:
        """识别最佳心理状态"""
        emotion_pnl = defaultdict(list)
        
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_pnl[emotion].append(t.pnl or 0)
        
        emotion_avg_pnl = {
            emotion: sum(pnls) / len(pnls)
            for emotion, pnls in emotion_pnl.items()
        }
        
        if not emotion_avg_pnl:
            return {'state': 'calm', 'avg_pnl': 0}
        
        best = max(emotion_avg_pnl.items(), key=lambda x: x[1])
        
        return {
            'state': best[0],
            'state_name': self.EMOTION_TYPES.get(best[0], {}).get('name', best[0]),
            'avg_pnl': round(best[1], 2),
            'trades': len(emotion_pnl[best[0]])
        }
    
    def _identify_worst_state(self, trades: List) -> Dict:
        """识别最差心理状态"""
        emotion_pnl = defaultdict(list)
        
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_pnl[emotion].append(t.pnl or 0)
        
        emotion_avg_pnl = {
            emotion: sum(pnls) / len(pnls)
            for emotion, pnls in emotion_pnl.items()
        }
        
        if not emotion_avg_pnl:
            return {'state': 'unknown', 'avg_pnl': 0}
        
        worst = min(emotion_avg_pnl.items(), key=lambda x: x[1])
        
        return {
            'state': worst[0],
            'state_name': self.EMOTION_TYPES.get(worst[0], {}).get('name', worst[0]),
            'avg_pnl': round(worst[1], 2),
            'trades': len(emotion_pnl[worst[0]])
        }
    
    def _generate_recommendations(self, trades: List) -> List[str]:
        """生成心理改进建议"""
        recommendations = []
        
        # 冲动交易
        impulse_ratio = len([t for t in trades if t.decision_reason == 'impulse']) / len(trades)
        if impulse_ratio > 0.2:
            recommendations.append("冲动交易比例超过 20%，建议交易前深呼吸 3 次")
        
        # 高情绪强度
        high_intensity = len([t for t in trades if (t.emotion_intensity or 3) > 7])
        if high_intensity > len(trades) * 0.3:
            recommendations.append("30% 以上交易情绪强度过高，建议降低交易频率")
        
        # 信心过高
        overconfident = len([t for t in trades if (t.confidence_entry or 5) >= 9])
        if overconfident > len(trades) * 0.2:
            recommendations.append("过度自信交易较多，建议保持谦逊")
        
        # 止损违规
        violations = len([
            t for t in trades
            if t.stop_loss_planned and t.stop_loss_planned > 0 and not t.stop_loss_executed
        ])
        if violations > 0:
            recommendations.append(f"出现{violations}次止损违规，需加强纪律性")
        
        # 最佳状态建议
        best_state = self._identify_best_state(trades)
        if best_state['state'] != 'calm':
            recommendations.append(f"你在{best_state['state_name']}状态下表现最佳，尝试保持这种心态")
        
        if not recommendations:
            recommendations.append("心理状态良好，继续保持")
        
        return recommendations
    
    def get_daily_psychology_stats(self, trader_id: str, date: str) -> Dict:
        """获取每日心理统计"""
        trades = self.trade_recorder.get_records_by_date(date)
        
        if not trades:
            return {'date': date, 'message': '无交易'}
        
        emotion_counts = defaultdict(int)
        for t in trades:
            emotion_counts[t.emotion_state or 'calm'] += 1
        
        return {
            'date': date,
            'trader_id': trader_id,
            'total_trades': len(trades),
            'emotion_counts': dict(emotion_counts),
            'avg_intensity': round(sum(t.emotion_intensity or 3 for t in trades) / len(trades), 2),
            'avg_confidence': round(sum(t.confidence_entry or 5 for t in trades) / len(trades), 2)
        }
    
    def save_psychology_stats(self, stats_data: Dict) -> str:
        """保存心理统计到数据库"""
        return self.review_db.save_psychology_stats(stats_data)


# 便捷函数
def analyze_psychology(period: str = 'last_30_days') -> Dict:
    """分析心理状态"""
    analyzer = PsychologyAnalyzer()
    return analyzer.analyze(period)
