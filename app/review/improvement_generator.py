"""
改进建议生成模块

基于复盘数据生成个性化的改进行动计划
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import json

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database
from .error_pattern_detector import ErrorPatternDetector
from .psychology_analyzer import PsychologyAnalyzer
from .strategy_analysis import StrategyAnalyzer

logger = logging.getLogger("ftseek.review.improvement")
logger.propagate = False


class ImprovementGenerator:
    """改进建议生成器"""
    
    # 改进建议模板库
    IMPROVEMENT_TEMPLATES = {
        'impulse_trading': {
            'title': '减少冲动交易',
            'category': '决策',
            'description': '建立交易前检查清单，强制等待 5 分钟',
            'actions': [
                '创建交易前检查清单 (信号确认、风险计算、情绪检查)',
                '设置 5 分钟冷静期，确认后再执行',
                '记录每次冲动交易的原因和结果'
            ],
            'metrics': ['冲动交易比例', '冲动交易胜率']
        },
        'stop_loss_discipline': {
            'title': '提高止损执行力',
            'category': '风控',
            'description': '设置自动止损，减少人为干预',
            'actions': [
                '入场时立即设置止损单',
                '不随意调整止损位',
                '记录每次止损执行情况'
            ],
            'metrics': ['止损执行率', '止损调整后盈亏']
        },
        'position_sizing': {
            'title': '优化仓位管理',
            'category': '风控',
            'description': '单笔交易仓位不超过 20%',
            'actions': [
                '设定单笔最大仓位限制 (20%)',
                '根据风险调整仓位大小',
                '避免同时持有过多仓位'
            ],
            'metrics': ['平均仓位', '仓位集中度']
        },
        'timing_improvement': {
            'title': '改善入场时机',
            'category': '时机',
            'description': '等待确认信号，避免追高杀跌',
            'actions': [
                '增加确认信号 (如成交量、多周期共振)',
                '设置价格提醒，不追单',
                '记录最佳入场时机特征'
            ],
            'metrics': ['入场时机评分', '追单比例']
        },
        'exit_strategy': {
            'title': '优化出场策略',
            'category': '时机',
            'description': '严格执行目标价位，避免过早/过晚出场',
            'actions': [
                '设定明确的止盈目标',
                '分批出场，锁定部分利润',
                '使用移动止损保护利润'
            ],
            'metrics': ['出场时机评分', '止盈执行率']
        },
        'emotion_management': {
            'title': '加强情绪管理',
            'category': '心理',
            'description': '交易前冥想，保持冷静',
            'actions': [
                '交易前冥想 5 分钟',
                '亏损后强制休息 30 分钟',
                '记录情绪日志'
            ],
            'metrics': ['平均情绪强度', '高情绪交易比例']
        },
        'overtrading': {
            'title': '减少过度交易',
            'category': '决策',
            'description': '设定每日最大交易次数',
            'actions': [
                '设定每日最大交易次数 (3 笔)',
                '只交易高质量信号',
                '每周设定交易预算'
            ],
            'metrics': ['日均交易次数', '交易质量评分']
        },
        'strategy_refinement': {
            'title': '优化交易策略',
            'category': '策略',
            'description': '复盘亏损交易，优化策略参数',
            'actions': [
                '分析所有亏损交易的共同特征',
                '优化入场/出场条件',
                '回测验证改进效果'
            ],
            'metrics': ['策略胜率', '策略盈亏比']
        }
    }
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
        self.error_detector = ErrorPatternDetector()
        self.psychology_analyzer = PsychologyAnalyzer()
        self.strategy_analyzer = StrategyAnalyzer()
        self.output_dir = Path('/opt/ftseek/FTSeek/data/improvement_plans')
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self,
                 trader_id: str = 'user_001',
                 period: str = 'last_30_days',
                 account_type: str = None) -> Dict[str, Any]:
        """
        生成改进建议
        
        Args:
            trader_id: 交易者 ID
            period: 时间周期
            account_type: 账户类型
        
        Returns:
            改进计划
        """
        logger.info(f"生成改进建议：{trader_id} ({period})")
        
        # 获取交易记录
        trades = self.trade_recorder.get_records_with_pnl()
        trades = self._filter_by_period(trades, period)
        
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        if not trades:
            return {'message': '无符合条件的交易记录'}
        
        # 错误模式分析
        error_patterns = self.error_detector.detect(period, min_occurrences=2, account_type=account_type)
        
        # 心理分析
        psychology_report = self.psychology_analyzer.analyze(period, account_type)
        
        # 策略分析
        strategy_report = self.strategy_analyzer.analyze(period=period)
        
        # 生成改进建议
        suggestions = []
        
        # 基于错误模式
        for pattern in error_patterns:
            template = self._get_template_for_pattern(pattern['pattern_id'])
            if template:
                suggestions.append({
                    'source': 'error_pattern',
                    'pattern_id': pattern['pattern_id'],
                    'priority': 'high' if pattern['severity'] in ['critical', 'high'] else 'medium',
                    'title': template['title'],
                    'category': template['category'],
                    'description': template['description'],
                    'actions': template['actions'],
                    'metrics': template['metrics'],
                    'evidence': {
                        'count': pattern['count'],
                        'impact': pattern.get('total_pnl_impact', 0)
                    }
                })
        
        # 基于心理分析
        psych_recommendations = psychology_report.get('recommendations', [])
        for rec in psych_recommendations:
            if '冲动' in rec:
                suggestions.append(self._create_suggestion('impulse_trading', 'psychology', 'medium'))
            if '情绪' in rec:
                suggestions.append(self._create_suggestion('emotion_management', 'psychology', 'medium'))
            if '止损' in rec:
                suggestions.append(self._create_suggestion('stop_loss_discipline', 'psychology', 'high'))
        
        # 基于策略分析
        if isinstance(strategy_report, dict) and 'weaknesses' in strategy_report:
            weaknesses = strategy_report.get('weaknesses', [])
            for weakness in weaknesses:
                if '胜率' in weakness:
                    suggestions.append(self._create_suggestion('strategy_refinement', 'strategy', 'high'))
                if '风险收益比' in weakness:
                    suggestions.append(self._create_suggestion('exit_strategy', 'strategy', 'medium'))
        
        # 去重和排序
        suggestions = self._deduplicate_suggestions(suggestions)
        suggestions.sort(key=lambda s: {'high': 0, 'medium': 1, 'low': 2}.get(s['priority'], 3))
        
        # 生成行动计划
        improvement_plan = {
            'trader_id': trader_id,
            'period': period,
            'generated_at': datetime.now().isoformat(),
            'total_suggestions': len(suggestions),
            'priority_breakdown': {
                'high': len([s for s in suggestions if s['priority'] == 'high']),
                'medium': len([s for s in suggestions if s['priority'] == 'medium']),
                'low': len([s for s in suggestions if s['priority'] == 'low'])
            },
            'category_breakdown': self._categorize_suggestions(suggestions),
            'suggestions': suggestions,
            'action_plan': self._create_action_plan(suggestions),
            'success_metrics': self._define_success_metrics(suggestions)
        }
        
        # 保存到数据库
        for suggestion in suggestions:
            self._save_to_database(trader_id, suggestion)
        
        # 保存文件
        self._save_plan(improvement_plan, trader_id, period)
        
        logger.info(f"改进建议生成完成：{len(suggestions)} 条")
        return improvement_plan
    
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
    
    def _get_template_for_pattern(self, pattern_id: str) -> Optional[Dict]:
        """根据错误模式获取改进模板"""
        mapping = {
            'impulse_trading': 'impulse_trading',
            'stop_loss_violation': 'stop_loss_discipline',
            'oversized_position': 'position_sizing',
            'poor_timing': 'timing_improvement',
            'early_exit': 'exit_strategy',
            'overtrading': 'overtrading',
            'revenge_trading': 'emotion_management'
        }
        
        template_id = mapping.get(pattern_id)
        if template_id:
            return self.IMPROVEMENT_TEMPLATES.get(template_id)
        return None
    
    def _create_suggestion(self, template_id: str, source: str, priority: str) -> Dict:
        """创建改进建议"""
        template = self.IMPROVEMENT_TEMPLATES.get(template_id, {})
        
        return {
            'source': source,
            'template_id': template_id,
            'priority': priority,
            'title': template.get('title', '改进建议'),
            'category': template.get('category', 'general'),
            'description': template.get('description', ''),
            'actions': template.get('actions', []),
            'metrics': template.get('metrics', [])
        }
    
    def _deduplicate_suggestions(self, suggestions: List[Dict]) -> List[Dict]:
        """去重"""
        seen = set()
        unique = []
        
        for s in suggestions:
            key = s.get('template_id') or s.get('pattern_id') or s['title']
            if key not in seen:
                seen.add(key)
                unique.append(s)
        
        return unique
    
    def _categorize_suggestions(self, suggestions: List[Dict]) -> Dict:
        """按类别分组"""
        categories = defaultdict(int)
        for s in suggestions:
            categories[s.get('category', 'other')] += 1
        return dict(categories)
    
    def _create_action_plan(self, suggestions: List[Dict]) -> List[Dict]:
        """创建具体行动计划"""
        plan = []
        
        for i, suggestion in enumerate(suggestions[:5], 1):  # 最多 5 个优先级最高的
            plan.append({
                'step': i,
                'action': suggestion['title'],
                'category': suggestion['category'],
                'priority': suggestion['priority'],
                'specific_actions': suggestion.get('actions', [])[:2],
                'target_date': (datetime.now() + timedelta(days=i * 3)).strftime('%Y-%m-%d'),
                'status': 'pending'
            })
        
        return plan
    
    def _define_success_metrics(self, suggestions: List[Dict]) -> Dict:
        """定义成功指标"""
        all_metrics = set()
        for s in suggestions:
            all_metrics.update(s.get('metrics', []))
        
        return {
            'metrics': list(all_metrics),
            'review_date': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d'),
            'baseline': '当前值将在下次评估时记录',
            'target': '改进 20% 以上'
        }
    
    def _save_to_database(self, trader_id: str, suggestion: Dict):
        """保存到数据库"""
        action_data = {
            'trader_id': trader_id,
            'action_title': suggestion['title'],
            'action_description': suggestion['description'],
            'action_category': suggestion.get('category', 'general'),
            'priority': suggestion['priority'],
            'source': suggestion.get('source', 'generated'),
            'status': 'pending',
            'created_date': datetime.now().strftime('%Y-%m-%d'),
            'target_date': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
        }
        
        self.review_db.add_improvement_action(action_data)
    
    def _save_plan(self, plan: Dict, trader_id: str, period: str):
        """保存计划到文件"""
        filename = self.output_dir / f"improvement_plan_{trader_id}_{period}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(plan, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"改进计划已保存：{filename}")
    
    def get_pending_actions(self, trader_id: str) -> List[Dict]:
        """获取待处理改进行动"""
        return self.review_db.get_pending_actions(trader_id)
    
    def complete_action(self, action_id: str, effectiveness_score: int = None, notes: str = None) -> bool:
        """标记行动为完成"""
        success = self.review_db.update_action_status(action_id, 'completed')
        
        if effectiveness_score is not None:
            # 更新效果评分
            pass  # 需要额外方法
        
        return success


# 便捷函数
def generate_improvements(trader_id: str = 'user_001', period: str = 'last_30_days') -> Dict:
    """生成改进建议"""
    generator = ImprovementGenerator()
    return generator.generate(trader_id, period)
