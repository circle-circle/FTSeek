"""
每日复盘生成器

生成每日交易复盘报告，包含 6 大维度分析
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import json

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.daily")
logger.propagate = False


class DailyReviewGenerator:
    """每日复盘生成器"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
        self.output_dir = Path('/opt/ftseek/FTSeek/data/daily_reviews')
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self,
                 date: str = None,
                 account_type: str = None) -> Dict[str, Any]:
        """
        生成每日复盘报告
        
        Args:
            date: 日期 YYYY-MM-DD，默认今天
            account_type: 账户类型 real/simulation，默认全部
        
        Returns:
            复盘报告字典
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"生成每日复盘报告：{date}")
        
        # 获取当日交易记录
        trades = self.trade_recorder.get_records_by_date(date)
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        # 已平仓交易
        closed_trades = [t for t in trades if t.exit_price is not None]
        
        # 基础统计
        report = {
            'report_type': 'daily',
            'report_date': date,
            'generated_at': datetime.now().isoformat(),
            'account_type': account_type or 'all',
            'summary': self._generate_summary(trades, closed_trades),
            'decision_analysis': self._analyze_decisions(closed_trades),
            'timing_analysis': self._analyze_timing(closed_trades),
            'position_analysis': self._analyze_positions(closed_trades),
            'risk_analysis': self._analyze_risk(closed_trades),
            'psychology_analysis': self._analyze_psychology(closed_trades),
            'key_learnings': self._extract_learnings(closed_trades),
            'improvement_suggestions': self._generate_suggestions(closed_trades),
            'best_trade': self._find_best_trade(closed_trades),
            'worst_trade': self._find_worst_trade(closed_trades)
        }
        
        # 保存报告
        self._save_report(report, date)
        
        logger.info(f"每日复盘报告生成完成：{len(closed_trades)} 笔交易")
        return report
    
    def _generate_summary(self, all_trades: List, closed_trades: List) -> Dict:
        """生成基础统计摘要"""
        if not closed_trades:
            return {
                'total_trades': len(all_trades),
                'closed_trades': 0,
                'total_pnl': 0,
                'win_rate': 0,
                'avg_pnl': 0
            }
        
        total_pnl = sum(t.pnl for t in closed_trades if t.pnl)
        winning = [t for t in closed_trades if t.pnl and t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl and t.pnl < 0]
        
        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'breakeven_trades': len(closed_trades) - len(winning) - len(losing),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4) if closed_trades else 0,
            'avg_pnl': round(total_pnl / len(closed_trades), 2) if closed_trades else 0,
            'avg_holding_period': round(sum(t.holding_period or 0 for t in closed_trades) / len(closed_trades), 1)
        }
    
    def _analyze_decisions(self, trades: List) -> Dict:
        """决策维度分析"""
        if not trades:
            return {}
        
        # 决策原因分布
        decision_counts = {}
        for t in trades:
            reason = t.decision_reason or 'unknown'
            decision_counts[reason] = decision_counts.get(reason, 0) + 1
        
        # 决策信心统计
        avg_confidence = sum(t.decision_confidence or 5 for t in trades) / len(trades)
        avg_quality = sum(t.decision_quality_score or 5 for t in trades) / len(trades)
        
        # 冲动交易识别
        impulse_trades = [t for t in trades if t.decision_reason == 'impulse']
        
        return {
            'decision_reason_distribution': decision_counts,
            'avg_decision_confidence': round(avg_confidence, 2),
            'avg_decision_quality': round(avg_quality, 2),
            'impulse_trade_count': len(impulse_trades),
            'impulse_trade_ratio': round(len(impulse_trades) / len(trades), 4),
            'decision_quality_issues': [
                t for t in trades if (t.decision_quality_score or 5) < 5
            ]
        }
    
    def _analyze_timing(self, trades: List) -> Dict:
        """时机维度分析"""
        if not trades:
            return {}
        
        avg_entry_score = sum(t.entry_timing_score or 5 for t in trades) / len(trades)
        avg_exit_score = sum(t.exit_timing_score or 5 for t in trades) / len(trades)
        
        # 时机问题识别
        poor_entry = [t for t in trades if (t.entry_timing_score or 5) < 5]
        poor_exit = [t for t in trades if (t.exit_timing_score or 5) < 5]
        
        return {
            'avg_entry_timing_score': round(avg_entry_score, 2),
            'avg_exit_timing_score': round(avg_exit_score, 2),
            'timing_score_gap': round(avg_entry_score - avg_exit_score, 2),
            'poor_entry_count': len(poor_entry),
            'poor_exit_count': len(poor_exit),
            'timing_notes': [t.timing_notes for t in trades if t.timing_notes]
        }
    
    def _analyze_positions(self, trades: List) -> Dict:
        """持仓维度分析"""
        if not trades:
            return {}
        
        avg_position_size = sum(t.position_size_actual or 0 for t in trades) / len(trades)
        avg_adjustments = sum(t.position_adjustments or 0 for t in trades) / len(trades)
        
        # 仓位问题识别
        oversized = [t for t in trades if (t.position_size_actual or 0) > 0.3]
        frequent_adjustments = [t for t in trades if (t.position_adjustments or 0) > 2]
        
        return {
            'avg_position_size': round(avg_position_size, 4),
            'avg_position_adjustments': round(avg_adjustments, 2),
            'oversized_position_count': len(oversized),
            'frequent_adjustment_count': len(frequent_adjustments),
            'position_notes': [t.position_notes for t in trades if t.position_notes]
        }
    
    def _analyze_risk(self, trades: List) -> Dict:
        """风控维度分析"""
        if not trades:
            return {}
        
        # 止损执行率
        stop_loss_trades = [t for t in trades if t.stop_loss_planned and t.stop_loss_planned > 0]
        executed = [t for t in stop_loss_trades if t.stop_loss_executed]
        stop_loss_rate = len(executed) / len(stop_loss_trades) if stop_loss_trades else 1.0
        
        # 止损调整
        adjusted = [t for t in trades if t.stop_loss_adjusted]
        
        # 风险收益比
        avg_rr = sum(t.risk_reward_actual or 0 for t in trades) / len(trades)
        
        return {
            'stop_loss_planned_count': len(stop_loss_trades),
            'stop_loss_executed_count': len(executed),
            'stop_loss_execution_rate': round(stop_loss_rate, 4),
            'stop_loss_adjusted_count': len(adjusted),
            'avg_risk_reward_ratio': round(avg_rr, 2),
            'risk_notes': [t.risk_notes for t in trades if t.risk_notes]
        }
    
    def _analyze_psychology(self, trades: List) -> Dict:
        """心理维度分析"""
        if not trades:
            return {}
        
        # 情绪分布
        emotion_counts = {}
        for t in trades:
            emotion = t.emotion_state or 'calm'
            emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        
        # 情绪强度
        avg_intensity = sum(t.emotion_intensity or 3 for t in trades) / len(trades)
        
        # 信心变化
        avg_confidence_entry = sum(t.confidence_entry or 5 for t in trades) / len(trades)
        avg_confidence_exit = sum(t.confidence_exit or 5 for t in trades) / len(trades)
        
        # 心理备注
        psych_notes = [t.psychological_notes for t in trades if t.psychological_notes]
        
        return {
            'emotion_distribution': emotion_counts,
            'avg_emotion_intensity': round(avg_intensity, 2),
            'avg_confidence_entry': round(avg_confidence_entry, 2),
            'avg_confidence_exit': round(avg_confidence_exit, 2),
            'confidence_change': round(avg_confidence_exit - avg_confidence_entry, 2),
            'psychological_notes': psych_notes
        }
    
    def _extract_learnings(self, trades: List) -> List[str]:
        """提取关键学习"""
        learnings = []
        
        for t in trades:
            if t.lessons_learned:
                learnings.append(t.lessons_learned)
            if t.review_notes:
                learnings.append(t.review_notes)
        
        # 去重
        return list(set(learnings))[:10]
    
    def _generate_suggestions(self, trades: List) -> List[str]:
        """生成改进建议"""
        suggestions = []
        
        if not trades:
            return suggestions
        
        # 基于统计的建议
        summary = self._generate_summary(trades, trades)
        
        if summary['win_rate'] < 0.4:
            suggestions.append("胜率低于 40%，建议重新评估策略有效性")
        
        if summary.get('impulse_trade_ratio', 0) > 0.3:
            suggestions.append("冲动交易比例超过 30%，建议建立交易前检查清单")
        
        # 止损执行率
        risk_analysis = self._analyze_risk(trades)
        if risk_analysis.get('stop_loss_execution_rate', 1) < 0.8:
            suggestions.append("止损执行率低于 80%，需严格执行止损纪律")
        
        # 连续亏损
        losing_streak = 0
        max_losing_streak = 0
        for t in trades:
            if t.pnl and t.pnl < 0:
                losing_streak += 1
                max_losing_streak = max(max_losing_streak, losing_streak)
            else:
                losing_streak = 0
        
        if max_losing_streak >= 3:
            suggestions.append(f"出现连续{max_losing_streak}笔亏损，考虑暂停交易，调整心态")
        
        return suggestions
    
    def _find_best_trade(self, trades: List) -> Optional[Dict]:
        """找出最佳交易"""
        if not trades:
            return None
        
        profitable = [t for t in trades if t.pnl and t.pnl > 0]
        if not profitable:
            return None
        
        best = max(profitable, key=lambda t: t.pnl)
        return {
            'trade_id': best.trade_id,
            'code': best.code,
            'pnl': round(best.pnl, 2),
            'pnl_percent': round(best.pnl_percent, 4) if best.pnl_percent else 0,
            'highlight': best.lessons_learned or '严格执行策略'
        }
    
    def _find_worst_trade(self, trades: List) -> Optional[Dict]:
        """找出最差交易"""
        if not trades:
            return None
        
        losing = [t for t in trades if t.pnl and t.pnl < 0]
        if not losing:
            return None
        
        worst = min(losing, key=lambda t: t.pnl)
        return {
            'trade_id': worst.trade_id,
            'code': worst.code,
            'pnl': round(worst.pnl, 2),
            'pnl_percent': round(worst.pnl_percent, 4) if worst.pnl_percent else 0,
            'issue': worst.lessons_learned or worst.review_notes or '需要复盘分析'
        }
    
    def _save_report(self, report: Dict, date: str):
        """保存报告到文件"""
        filename = self.output_dir / f"daily_review_{date}.json"
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"报告已保存：{filename}")
    
    def get_report(self, date: str) -> Optional[Dict]:
        """获取已生成的报告"""
        filename = self.output_dir / f"daily_review_{date}.json"
        
        if not filename.exists():
            return None
        
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


# 便捷函数
def generate_daily_review(date: str = None, account_type: str = None) -> Dict:
    """生成每日复盘报告"""
    generator = DailyReviewGenerator()
    return generator.generate(date, account_type)
