"""
每周复盘生成器

生成每周交易复盘报告，包含趋势分析和周期性洞察
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import json

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database
from .daily_review import DailyReviewGenerator

logger = logging.getLogger("ftseek.review.weekly")
logger.propagate = False


class WeeklyReviewGenerator:
    """每周复盘生成器"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
        self.daily_generator = DailyReviewGenerator()
        self.output_dir = Path('/opt/ftseek/FTSeek/data/weekly_reviews')
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self,
                 week_start: str = None,
                 week_end: str = None,
                 account_type: str = None) -> Dict[str, Any]:
        """
        生成每周复盘报告
        
        Args:
            week_start: 周开始日期 YYYY-MM-DD，默认本周一
            week_end: 周结束日期 YYYY-MM-DD，默认本周日
            account_type: 账户类型
        
        Returns:
            复盘报告字典
        """
        if week_start is None:
            # 获取本周一
            today = datetime.now()
            week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        
        if week_end is None:
            week_end = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"生成每周复盘报告：{week_start} 至 {week_end}")
        
        # 获取周期内交易记录
        all_trades = self.trade_recorder.get_records_with_pnl()
        
        # 日期过滤
        period_trades = [
            t for t in all_trades
            if week_start <= t.timestamp[:10] <= week_end
        ]
        
        if account_type:
            period_trades = [t for t in period_trades if t.account_type == account_type]
        
        # 获取每日报告
        daily_reports = self._get_daily_reports(week_start, week_end)
        
        # 生成周报
        report = {
            'report_type': 'weekly',
            'period_start': week_start,
            'period_end': week_end,
            'generated_at': datetime.now().isoformat(),
            'account_type': account_type or 'all',
            'summary': self._generate_summary(period_trades),
            'trend_analysis': self._analyze_trends(daily_reports),
            'strategy_performance': self._analyze_strategies(period_trades),
            'error_patterns': self._identify_error_patterns(period_trades),
            'psychology_trends': self._analyze_psychology_trends(period_trades),
            'weekly_learnings': self._extract_weekly_learnings(period_trades),
            'improvement_plan': self._generate_improvement_plan(period_trades),
            'next_week_focus': self._suggest_next_week_focus(period_trades)
        }
        
        # 保存报告
        self._save_report(report, week_start, week_end)
        
        logger.info(f"每周复盘报告生成完成：{len(period_trades)} 笔交易")
        return report
    
    def _generate_summary(self, trades: List) -> Dict:
        """生成周统计摘要"""
        if not trades:
            return {'total_trades': 0, 'message': '本周无交易'}
        
        total_pnl = sum(t.pnl for t in trades if t.pnl)
        winning = [t for t in trades if t.pnl and t.pnl > 0]
        losing = [t for t in trades if t.pnl and t.pnl < 0]
        
        # 按交易日分组
        daily_pnl = {}
        for t in trades:
            date = t.timestamp[:10]
            daily_pnl[date] = daily_pnl.get(date, 0) + (t.pnl or 0)
        
        # 最佳/最差交易日
        best_day = max(daily_pnl.items(), key=lambda x: x[1]) if daily_pnl else None
        worst_day = min(daily_pnl.items(), key=lambda x: x[1]) if daily_pnl else None
        
        return {
            'total_trades': len(trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(trades), 4),
            'avg_pnl': round(total_pnl / len(trades), 2),
            'avg_holding_period': round(sum(t.holding_period or 0 for t in trades) / len(trades), 1),
            'trading_days': len(daily_pnl),
            'best_day': {'date': best_day[0], 'pnl': round(best_day[1], 2)} if best_day else None,
            'worst_day': {'date': worst_day[0], 'pnl': round(worst_day[1], 2)} if worst_day else None
        }
    
    def _get_daily_reports(self, start: str, end: str) -> List[Dict]:
        """获取周期内的每日报告"""
        daily_reports = []
        current = datetime.strptime(start, '%Y-%m-%d')
        end_date = datetime.strptime(end, '%Y-%m-%d')
        
        while current <= end_date:
            date_str = current.strftime('%Y-%m-%d')
            report = self.daily_generator.get_report(date_str)
            if report:
                daily_reports.append(report)
            current += timedelta(days=1)
        
        return daily_reports
    
    def _analyze_trends(self, daily_reports: List[Dict]) -> Dict:
        """分析趋势"""
        if not daily_reports:
            return {}
        
        # 胜率趋势
        win_rates = [r['summary'].get('win_rate', 0) for r in daily_reports if r.get('summary')]
        
        # 盈亏趋势
        daily_pnls = [r['summary'].get('total_pnl', 0) for r in daily_reports if r.get('summary')]
        
        # 情绪趋势
        emotion_intensity = []
        for r in daily_reports:
            psych = r.get('psychology_analysis', {})
            if psych:
                emotion_intensity.append(psych.get('avg_emotion_intensity', 3))
        
        return {
            'avg_win_rate': round(sum(win_rates) / len(win_rates), 4) if win_rates else 0,
            'win_rate_trend': 'improving' if len(win_rates) > 1 and win_rates[-1] > win_rates[0] else 'declining',
            'total_pnl': sum(daily_pnls),
            'pnl_volatility': round(max(daily_pnls) - min(daily_pnls), 2) if daily_pnls else 0,
            'avg_emotion_intensity': round(sum(emotion_intensity) / len(emotion_intensity), 2) if emotion_intensity else 0,
            'emotion_trend': 'calmer' if len(emotion_intensity) > 1 and emotion_intensity[-1] < emotion_intensity[0] else 'more_emotional'
        }
    
    def _analyze_strategies(self, trades: List) -> Dict:
        """策略表现分析"""
        if not trades:
            return {}
        
        # 按策略分组
        strategy_stats = {}
        for t in trades:
            strategy = t.strategy or 'unknown'
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    'trades': 0,
                    'wins': 0,
                    'total_pnl': 0
                }
            
            strategy_stats[strategy]['trades'] += 1
            if t.pnl and t.pnl > 0:
                strategy_stats[strategy]['wins'] += 1
            strategy_stats[strategy]['total_pnl'] += t.pnl or 0
        
        # 计算胜率
        for strategy, stats in strategy_stats.items():
            stats['win_rate'] = round(stats['wins'] / stats['trades'], 4) if stats['trades'] > 0 else 0
            stats['avg_pnl'] = round(stats['total_pnl'] / stats['trades'], 2) if stats['trades'] > 0 else 0
        
        return {
            'strategy_breakdown': strategy_stats,
            'best_strategy': max(strategy_stats.items(), key=lambda x: x[1]['total_pnl'])[0] if strategy_stats else None,
            'worst_strategy': min(strategy_stats.items(), key=lambda x: x[1]['total_pnl'])[0] if strategy_stats else None
        }
    
    def _identify_error_patterns(self, trades: List) -> List[Dict]:
        """识别错误模式"""
        patterns = []
        
        # 冲动交易
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        if len(impulse_trades) >= 2:
            patterns.append({
                'pattern': 'impulse_trading',
                'count': len(impulse_trades),
                'impact': sum(t.pnl or 0 for t in impulse_trades),
                'suggestion': '建立交易前检查清单，强制等待 5 分钟'
            })
        
        # 止损未执行
        non_stop_loss = [t for t in trades if t.stop_loss_planned and not t.stop_loss_executed]
        if non_stop_loss:
            patterns.append({
                'pattern': 'stop_loss_violation',
                'count': len(non_stop_loss),
                'impact': sum(t.pnl or 0 for t in non_stop_loss),
                'suggestion': '设置自动止损，减少人为干预'
            })
        
        # 过早出场
        early_exits = [t for t in trades if (t.exit_timing_score or 5) < 4 and t.pnl and t.pnl > 0]
        if len(early_exits) >= 2:
            patterns.append({
                'pattern': 'early_exit',
                'count': len(early_exits),
                'impact': '潜在利润损失',
                'suggestion': '严格执行目标价位，避免恐惧性出场'
            })
        
        # 仓位过大
        oversized = [t for t in trades if (t.position_size_actual or 0) > 0.3]
        if oversized:
            patterns.append({
                'pattern': 'oversized_position',
                'count': len(oversized),
                'impact': '风险集中',
                'suggestion': '单笔交易仓位不超过 20%'
            })
        
        return patterns
    
    def _analyze_psychology_trends(self, trades: List) -> Dict:
        """心理趋势分析"""
        if not trades:
            return {}
        
        # 情绪分布
        emotion_counts = {}
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        
        # 按情绪分组的盈亏
        emotion_pnl = {}
        for t in trades:
            emotion = t.emotion_state or 'calm'
            if emotion not in emotion_pnl:
                emotion_pnl[emotion] = []
            emotion_pnl[emotion].append(t.pnl or 0)
        
        emotion_avg_pnl = {
            emotion: round(sum(pnls) / len(pnls), 2)
            for emotion, pnls in emotion_pnl.items()
        }
        
        return {
            'emotion_distribution': emotion_counts,
            'emotion_pnl_correlation': emotion_avg_pnl,
            'best_emotion_state': max(emotion_avg_pnl.items(), key=lambda x: x[1])[0] if emotion_avg_pnl else None,
            'worst_emotion_state': min(emotion_avg_pnl.items(), key=lambda x: x[1])[0] if emotion_avg_pnl else None,
            'avg_emotion_intensity': round(sum(t.emotion_intensity or 3 for t in trades) / len(trades), 2)
        }
    
    def _extract_weekly_learnings(self, trades: List) -> List[str]:
        """提取周度学习"""
        learnings = set()
        
        for t in trades:
            if t.lessons_learned:
                learnings.add(t.lessons_learned)
            if t.review_notes:
                learnings.add(t.review_notes)
            if t.psychological_notes:
                learnings.add(t.psychological_notes)
        
        return list(learnings)[:15]
    
    def _generate_improvement_plan(self, trades: List) -> List[Dict]:
        """生成改进计划"""
        plan = []
        
        # 基于错误模式
        error_patterns = self._identify_error_patterns(trades)
        for pattern in error_patterns:
            plan.append({
                'action': pattern['suggestion'],
                'priority': 'high' if pattern['count'] >= 3 else 'medium',
                'category': 'risk_control' if 'stop_loss' in pattern['pattern'] else 'psychology',
                'target_date': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
            })
        
        # 基于统计
        summary = self._generate_summary(trades)
        if summary.get('win_rate', 0) < 0.5:
            plan.append({
                'action': '复盘所有亏损交易，识别共同特征',
                'priority': 'high',
                'category': 'strategy',
                'target_date': (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
            })
        
        return plan
    
    def _suggest_next_week_focus(self, trades: List) -> List[str]:
        """建议下周关注重点"""
        focus = []
        
        # 基于本周表现
        summary = self._generate_summary(trades)
        
        if summary.get('win_rate', 0) < 0.4:
            focus.append("降低交易频率，提高选股质量")
        
        if summary.get('avg_emotion_intensity', 3) > 6:
            focus.append("加强情绪管理，交易前冥想 5 分钟")
        
        error_patterns = self._identify_error_patterns(trades)
        if any(p['pattern'] == 'impulse_trading' for p in error_patterns):
            focus.append("严格执行交易计划，避免盘中临时决策")
        
        if not focus:
            focus.append("保持当前策略，继续执行")
        
        return focus
    
    def _save_report(self, report: Dict, week_start: str, week_end: str):
        """保存报告"""
        filename = self.output_dir / f"weekly_review_{week_start}_to_{week_end}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"周报已保存：{filename}")
    
    def get_report(self, week_start: str, week_end: str) -> Optional[Dict]:
        """获取已生成的报告"""
        filename = self.output_dir / f"weekly_review_{week_start}_to_{week_end}.json"
        
        if not filename.exists():
            return None
        
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


# 便捷函数
def generate_weekly_review(week_start: str = None, week_end: str = None, account_type: str = None) -> Dict:
    """生成每周复盘报告"""
    generator = WeeklyReviewGenerator()
    return generator.generate(week_start, week_end, account_type)
