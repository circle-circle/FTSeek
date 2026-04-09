"""
每周策略复盘报告生成器

自动生成周度绩效复盘报告
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List

logger = logging.getLogger("ftseek.reports.weekly_review")
logger.propagate = False  # 防止日志重复

# 报告存储目录
REPORTS_DIR = Path('/opt/ftseek/FTSeek/data/reports')
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


class WeeklyReviewGenerator:
    """周报复盘生成器"""
    
    def __init__(self):
        pass
    
    def generate_report(self,
                       start_date: str,
                       end_date: str,
                       trade_records: List[Dict],
                       prediction_stats: Dict,
                       attribution_report: Dict) -> Dict:
        """
        生成周报复盘报告
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            trade_records: 交易记录列表
            prediction_stats: AI 预测统计
            attribution_report: 收益归因报告
        
        Returns:
            Dict: 周报数据
        """
        # 计算基础统计
        closed_trades = [t for t in trade_records if t.get('exit_price')]
        winning_trades = [t for t in closed_trades if (t.get('pnl') or 0) > 0]
        
        total_pnl = sum(t.get('pnl', 0) for t in closed_trades if t.get('pnl'))
        win_count = len(winning_trades)
        total_count = len(closed_trades)
        win_rate = win_count / total_count if total_count > 0 else 0
        
        # 按策略分组
        strategy_stats = self._group_by_strategy(closed_trades)
        
        # 生成改进建议
        recommendations = self._generate_recommendations(
            win_rate=win_rate,
            strategy_stats=strategy_stats,
            prediction_stats=prediction_stats,
            attribution_report=attribution_report
        )
        
        report = {
            'period': f'{start_date} ~ {end_date}',
            'generated_at': datetime.now().isoformat(),
            'summary': {
                'total_trades': total_count,
                'winning_trades': win_count,
                'losing_trades': total_count - win_count,
                'win_rate': round(win_rate, 4),
                'total_pnl': round(total_pnl, 2),
                'avg_pnl': round(total_pnl / total_count, 2) if total_count > 0 else 0
            },
            'by_strategy': strategy_stats,
            'ai_performance': prediction_stats,
            'attribution': attribution_report,
            'recommendations': recommendations,
            'next_week_focus': self._generate_next_week_focus(recommendations)
        }
        
        return report
    
    def _group_by_strategy(self, trades: List[Dict]) -> Dict:
        """按策略分组统计"""
        strategy_stats = {}
        
        for trade in trades:
            strategy = trade.get('strategy', 'unknown')
            pnl = trade.get('pnl', 0) or 0
            
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    'trades': 0,
                    'winning': 0,
                    'total_pnl': 0.0
                }
            
            strategy_stats[strategy]['trades'] += 1
            strategy_stats[strategy]['total_pnl'] += pnl
            
            if pnl > 0:
                strategy_stats[strategy]['winning'] += 1
        
        # 计算胜率
        for strategy, stats in strategy_stats.items():
            stats['win_rate'] = round(stats['winning'] / stats['trades'], 4) if stats['trades'] > 0 else 0
            stats['total_pnl'] = round(stats['total_pnl'], 2)
        
        return strategy_stats
    
    def _generate_recommendations(self,
                                 win_rate: float,
                                 strategy_stats: Dict,
                                 prediction_stats: Dict,
                                 attribution_report: Dict) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        # 基于胜率
        if win_rate > 0.60:
            recommendations.append('✅ 胜率优秀，保持当前策略')
        elif win_rate > 0.50:
            recommendations.append('✅ 胜率良好，继续优化')
        elif win_rate > 0.40:
            recommendations.append('⚠️ 胜率偏低，建议检查策略参数')
        else:
            recommendations.append('❌ 胜率较低，建议暂停交易并复盘')
        
        # 基于策略表现
        if 'fusion_v3' in strategy_stats:
            v3_stats = strategy_stats['fusion_v3']
            if v3_stats['win_rate'] > 0.65:
                recommendations.append('✅ FusionV3 (AI 增强) 表现优秀，可增加 AI 权重')
            elif v3_stats['win_rate'] < 0.45:
                recommendations.append('⚠️ FusionV3 表现不佳，建议降低 AI 权重')
        
        # 基于 AI 表现
        ai_accuracy = prediction_stats.get('accuracy', 0)
        if ai_accuracy > 0.65:
            recommendations.append(f'✅ AI 预测准确率 {ai_accuracy:.1%}，表现优秀')
        elif ai_accuracy < 0.45:
            recommendations.append(f'⚠️ AI 预测准确率 {ai_accuracy:.1%}，需要改进')
        
        # 基于 AI 贡献度
        ai_contrib = attribution_report.get('ai_contribution', {})
        ai_contrib_percent = ai_contrib.get('ai_contribution_percent', 0)
        if ai_contrib_percent > 10:
            recommendations.append(f'✅ AI 带来 +{ai_contrib_percent}% 额外收益')
        elif ai_contrib_percent < -10:
            recommendations.append(f'⚠️ AI 拖累收益 {ai_contrib_percent}%')
        
        if not recommendations:
            recommendations.append('📊 继续按当前策略执行，保持监控')
        
        return recommendations
    
    def _generate_next_week_focus(self, recommendations: List[str]) -> List[str]:
        """生成下周重点关注"""
        focus = []
        
        for rec in recommendations:
            if 'AI 权重' in rec:
                focus.append('调整 AI 权重参数')
            if '胜率偏低' in rec:
                focus.append('检查策略参数，优化入场条件')
            if 'AI 预测准确率' in rec:
                focus.append('改进 AI 提示词工程')
        
        if not focus:
            focus.append('继续执行当前策略')
            focus.append('监控交易质量')
        
        return focus
    
    def save_report(self, report: Dict) -> str:
        """保存报告"""
        week_start = report['period'].split(' ~ ')[0]
        filename = f'weekly_review_{week_start}.json'
        filepath = REPORTS_DIR / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"周报已保存：{filepath}")
        return str(filepath)
    
    def generate_markdown(self, report: Dict) -> str:
        """生成 Markdown 格式报告"""
        md = f"""# FTSeek 周报复盘

**周期**: {report['period']}  
**生成时间**: {report['generated_at'][:10]}

---

## 📊 交易汇总

| 指标 | 数值 |
|------|------|
| 总交易数 | {report['summary']['total_trades']} |
| 盈利交易 | {report['summary']['winning_trades']} |
| 亏损交易 | {report['summary']['losing_trades']} |
| 胜率 | {report['summary']['win_rate']:.1%} |
| 总收益 | HK${report['summary']['total_pnl']:,.2f} |
| 平均收益 | HK${report['summary']['avg_pnl']:,.2f} |

---

## 🎯 策略表现

| 策略 | 交易数 | 胜率 | 收益 |
|------|--------|------|------|
"""
        
        for strategy, stats in report['by_strategy'].items():
            md += f"| {strategy} | {stats['trades']} | {stats['win_rate']:.1%} | HK${stats['total_pnl']:,.2f} |\n"
        
        md += f"""
---

## 🤖 AI 表现

**预测准确率**: {report['ai_performance'].get('accuracy', 0):.1%}

---

## 💡 改进建议

"""
        
        for i, rec in enumerate(report['recommendations'], 1):
            md += f"{i}. {rec}\n"
        
        md += f"""
---

## 📋 下周重点关注

"""
        
        for i, focus in enumerate(report['next_week_focus'], 1):
            md += f"{i}. {focus}\n"
        
        md += "\n---\n\n*FTSeek 自动复盘系统*\n"
        
        return md


# 全局单例
_generator = None

def get_weekly_review_generator() -> WeeklyReviewGenerator:
    """获取周报复盘生成器单例"""
    global _generator
    if _generator is None:
        _generator = WeeklyReviewGenerator()
    return _generator
