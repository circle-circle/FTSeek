"""
心理状态分析模块 (美股版)
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder

logger = logging.getLogger("ftseek_us.review.psychology")
logger.propagate = False


class PsychologyAnalyzer:
    """心理状态分析器 (美股版)"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
    
    def analyze(self, period: str = 'last_30_days') -> Dict:
        trades = self.trade_recorder.get_records_with_pnl()
        trades = self._filter_by_period(trades, period)
        
        if not trades:
            return {'message': '无交易记录'}
        
        # 情绪分布
        emotion_counts = defaultdict(int)
        for t in trades:
            emotion = t.get('emotion_state', 'calm') or 'calm'
            emotion_counts[emotion] += 1
        
        avg_intensity = sum(t.get('emotion_intensity', 3) or 3 for t in trades) / len(trades)
        
        # 情绪与表现关联
        emotion_pnl = defaultdict(list)
        for t in trades:
            emotion = t.get('emotion_state', 'calm') or 'calm'
            emotion_pnl[emotion].append(t.get('realized_pnl', 0))
        
        emotion_avg_pnl = {e: sum(p)/len(p) for e, p in emotion_pnl.items()}
        
        return {
            'emotion_distribution': dict(emotion_counts),
            'avg_emotion_intensity': round(avg_intensity, 2),
            'emotion_performance': emotion_avg_pnl,
            'best_state': max(emotion_avg_pnl.items(), key=lambda x: x[1])[0] if emotion_avg_pnl else 'calm',
            'recommendations': self._generate_recommendations(trades)
        }
    
    def _filter_by_period(self, trades: List, period: str) -> List:
        if period == 'all':
            return trades
        cutoff = datetime.now() - timedelta(days=30)
        return [t for t in trades if datetime.fromisoformat(t.get('entry_time', '')[:19]) >= cutoff]
    
    def _generate_recommendations(self, trades: List) -> List[str]:
        recs = []
        
        impulse = len([t for t in trades if t.get('decision_reason') == 'impulse'])
        if impulse > len(trades) * 0.2:
            recs.append("冲动交易比例高，建议交易前深呼吸")
        
        high_intensity = len([t for t in trades if (t.get('emotion_intensity') or 3) > 7])
        if high_intensity > len(trades) * 0.3:
            recs.append("情绪强度过高，建议降低交易频率")
        
        if not recs:
            recs.append("心理状态良好")
        
        return recs


def analyze_psychology(period: str = 'last_30_days') -> Dict:
    analyzer = PsychologyAnalyzer()
    return analyzer.analyze(period)
