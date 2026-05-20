"""
改进建议生成模块 (美股版)
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from pathlib import Path
import json

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database
from .error_pattern_detector import ErrorPatternDetector

logger = logging.getLogger("ftseek_us.review.improvement")
logger.propagate = False


class ImprovementGenerator:
    """改进建议生成器 (美股版)"""
    
    TEMPLATES = {
        'impulse_trading': {'title': '减少冲动交易', 'category': '决策', 'actions': ['创建检查清单', '设置冷静期']},
        'stop_loss_discipline': {'title': '提高止损执行力', 'category': '风控', 'actions': ['设置自动止损', '不随意调整']},
        'position_sizing': {'title': '优化仓位管理', 'category': '风控', 'actions': ['单笔不超过 20%', '分散风险']},
        'emotion_management': {'title': '加强情绪管理', 'category': '心理', 'actions': ['交易前冥想', '亏损后休息']}
    }
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
        self.error_detector = ErrorPatternDetector()
        from app.core.paths import get_improvement_plans_dir
        self.output_dir = get_improvement_plans_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self, trader_id: str = 'user_001', period: str = 'last_30_days') -> Dict:
        logger.info(f"生成改进建议：{trader_id}")
        
        errors = self.error_detector.detect(period, min_occurrences=2)
        
        suggestions = []
        for error in errors:
            template = self.TEMPLATES.get(error['pattern_id'])
            if template:
                suggestions.append({
                    'priority': 'high' if error['count'] >= 3 else 'medium',
                    'title': template['title'],
                    'category': template['category'],
                    'actions': template['actions']
                })
        
        plan = {
            'trader_id': trader_id,
            'period': period,
            'generated_at': datetime.now().isoformat(),
            'suggestions': suggestions,
            'action_plan': [
                {
                    'step': i + 1,
                    'action': s['title'],
                    'target_date': (datetime.now() + timedelta(days=(i+1)*3)).strftime('%Y-%m-%d')
                }
                for i, s in enumerate(suggestions[:5])
            ]
        }
        
        # 保存到数据库
        for s in suggestions:
            self.review_db.add_improvement_action({
                'trader_id': trader_id,
                'action_title': s['title'],
                'action_category': s['category'],
                'priority': s['priority'],
                'status': 'pending',
                'created_date': datetime.now().strftime('%Y-%m-%d')
            })
        
        self._save_plan(plan, trader_id, period)
        return plan
    
    def _save_plan(self, plan: Dict, trader_id: str, period: str):
        filename = self.output_dir / f"improvement_plan_{trader_id}_{period}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False, default=str)
    
    def get_pending_actions(self, trader_id: str) -> List:
        return self.review_db.get_pending_actions(trader_id)


def generate_improvements(trader_id: str = 'user_001', period: str = 'last_30_days') -> Dict:
    generator = ImprovementGenerator()
    return generator.generate(trader_id, period)
