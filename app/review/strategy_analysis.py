"""
策略表现分析模块

分析各策略的历史表现，识别优劣势
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek.review.strategy")
logger.propagate = False


class StrategyAnalyzer:
    """策略表现分析器"""
    
    def __init__(self):
        self.trade_recorder = get_trade_recorder()
        self.review_db = get_review_database()
    
    def analyze(self,
                strategy_name: str = None,
                period: str = 'last_30_days',
                account_type: str = None) -> Dict[str, Any]:
        """
        分析策略表现
        
        Args:
            strategy_name: 策略名称，None 表示分析所有策略
            period: 时间周期 last_7_days/last_30_days/last_90_days/all
            account_type: 账户类型
        
        Returns:
            分析报告
        """
        logger.info(f"分析策略表现：{strategy_name or 'all'} ({period})")
        
        # 获取交易记录
        trades = self.trade_recorder.get_records_with_pnl()
        
        # 时间过滤
        trades = self._filter_by_period(trades, period)
        
        # 策略过滤
        if strategy_name:
            trades = [t for t in trades if t.strategy == strategy_name]
        
        # 账户类型过滤
        if account_type:
            trades = [t for t in trades if t.account_type == account_type]
        
        if not trades:
            return {'message': '无符合条件的交易记录'}
        
        # 按策略分组
        if strategy_name:
            return self._analyze_single_strategy(strategy_name, trades)
        else:
            return self._analyze_all_strategies(trades)
    
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
    
    def _analyze_single_strategy(self, strategy_name: str, trades: List) -> Dict:
        """分析单个策略"""
        if not trades:
            return {'strategy': strategy_name, 'message': '无交易记录'}
        
        # 基础统计
        total_pnl = sum(t.pnl for t in trades if t.pnl)
        winning = [t for t in trades if t.pnl and t.pnl > 0]
        losing = [t for t in trades if t.pnl and t.pnl < 0]
        
        win_rate = len(winning) / len(trades) if trades else 0
        
        # 盈亏比
        avg_win = sum(t.pnl for t in winning) / len(winning) if winning else 0
        avg_loss = abs(sum(t.pnl for t in losing) / len(losing)) if losing else 0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')
        
        # 持仓时间
        avg_holding = sum(t.holding_period or 0 for t in trades) / len(trades)
        
        # 风险收益比
        avg_rr = sum(t.risk_reward_actual or 0 for t in trades) / len(trades)
        
        # 最大回撤
        cumulative = 0
        peak = 0
        max_drawdown = 0
        for t in sorted(trades, key=lambda x: x.timestamp):
            cumulative += t.pnl or 0
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_drawdown = max(max_drawdown, drawdown)
        
        # 决策质量
        avg_decision_confidence = sum(t.decision_confidence or 5 for t in trades) / len(trades)
        avg_decision_quality = sum(t.decision_quality_score or 5 for t in trades) / len(trades)
        
        # 时机评分
        avg_entry_timing = sum(t.entry_timing_score or 5 for t in trades) / len(trades)
        avg_exit_timing = sum(t.exit_timing_score or 5 for t in trades) / len(trades)
        
        # 止损执行
        stop_loss_trades = [t for t in trades if t.stop_loss_planned and t.stop_loss_planned > 0]
        stop_loss_executed = len([t for t in stop_loss_trades if t.stop_loss_executed])
        stop_loss_rate = stop_loss_executed / len(stop_loss_trades) if stop_loss_trades else 1.0
        
        return {
            'strategy': strategy_name,
            'period': 'custom',
            'total_trades': len(trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': round(win_rate, 4),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl': round(total_pnl / len(trades), 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'N/A',
            'avg_holding_period': round(avg_holding, 1),
            'avg_risk_reward_ratio': round(avg_rr, 2),
            'max_drawdown': round(max_drawdown, 2),
            'decision_metrics': {
                'avg_confidence': round(avg_decision_confidence, 2),
                'avg_quality_score': round(avg_decision_quality, 2)
            },
            'timing_metrics': {
                'avg_entry_score': round(avg_entry_timing, 2),
                'avg_exit_score': round(avg_exit_timing, 2),
                'timing_gap': round(avg_entry_timing - avg_exit_timing, 2)
            },
            'risk_metrics': {
                'stop_loss_execution_rate': round(stop_loss_rate, 4),
                'avg_position_size': round(sum(t.position_size_actual or 0 for t in trades) / len(trades), 4)
            },
            'strengths': self._identify_strengths(trades),
            'weaknesses': self._identify_weaknesses(trades),
            'recommendations': self._generate_recommendations(trades)
        }
    
    def _analyze_all_strategies(self, trades: List) -> Dict:
        """分析所有策略"""
        # 按策略分组
        strategy_trades = defaultdict(list)
        for t in trades:
            strategy = t.strategy or 'unknown'
            strategy_trades[strategy].append(t)
        
        # 分析每个策略
        strategy_reports = {}
        for strategy, strat_trades in strategy_trades.items():
            strategy_reports[strategy] = self._analyze_single_strategy(strategy, strat_trades)
        
        # 排序
        sorted_strategies = sorted(
            strategy_reports.items(),
            key=lambda x: x[1].get('total_pnl', 0),
            reverse=True
        )
        
        return {
            'total_strategies': len(strategy_trades),
            'total_trades': len(trades),
            'strategy_ranking': [
                {
                    'rank': i + 1,
                    'strategy': name,
                    'total_pnl': report.get('total_pnl', 0),
                    'win_rate': report.get('win_rate', 0),
                    'total_trades': report.get('total_trades', 0)
                }
                for i, (name, report) in enumerate(sorted_strategies)
            ],
            'best_strategy': sorted_strategies[0][0] if sorted_strategies else None,
            'worst_strategy': sorted_strategies[-1][0] if sorted_strategies else None,
            'detailed_reports': strategy_reports
        }
    
    def _identify_strengths(self, trades: List) -> List[str]:
        """识别策略优势"""
        strengths = []
        
        if not trades:
            return strengths
        
        win_rate = len([t for t in trades if t.pnl and t.pnl > 0]) / len(trades)
        if win_rate > 0.6:
            strengths.append(f"高胜率 ({win_rate:.1%})")
        
        avg_rr = sum(t.risk_reward_actual or 0 for t in trades) / len(trades)
        if avg_rr > 2:
            strengths.append(f"优秀的风险收益比 ({avg_rr:.2f})")
        
        stop_loss_trades = [t for t in trades if t.stop_loss_planned and t.stop_loss_planned > 0]
        if stop_loss_trades:
            stop_loss_rate = len([t for t in stop_loss_trades if t.stop_loss_executed]) / len(stop_loss_trades)
            if stop_loss_rate > 0.9:
                strengths.append(f"严格的止损执行 ({stop_loss_rate:.1%})")
        
        avg_timing = sum(t.entry_timing_score or 5 for t in trades) / len(trades)
        if avg_timing > 7:
            strengths.append(f"精准的入场时机 ({avg_timing:.1f}/10)")
        
        return strengths
    
    def _identify_weaknesses(self, trades: List) -> List[str]:
        """识别策略劣势"""
        weaknesses = []
        
        if not trades:
            return weaknesses
        
        win_rate = len([t for t in trades if t.pnl and t.pnl > 0]) / len(trades)
        if win_rate < 0.4:
            weaknesses.append(f"胜率偏低 ({win_rate:.1%})")
        
        avg_rr = sum(t.risk_reward_actual or 0 for t in trades) / len(trades)
        if avg_rr < 1:
            weaknesses.append(f"风险收益比不佳 ({avg_rr:.2f})")
        
        avg_holding = sum(t.holding_period or 0 for t in trades) / len(trades)
        if avg_holding < 1 and win_rate < 0.5:
            weaknesses.append("持仓时间过短，可能存在过度交易")
        
        impulse_trades = len([t for t in trades if t.decision_reason == 'impulse'])
        if impulse_trades > len(trades) * 0.2:
            weaknesses.append(f"冲动交易比例高 ({impulse_trades/len(trades):.1%})")
        
        return weaknesses
    
    def _generate_recommendations(self, trades: List) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        weaknesses = self._identify_weaknesses(trades)
        
        if any("胜率偏低" in w for w in weaknesses):
            recommendations.append("优化入场条件，提高选股标准")
        
        if any("风险收益比不佳" in w for w in weaknesses):
            recommendations.append("调整止盈止损比例，目标 R:R >= 2:1")
        
        if any("过度交易" in w for w in weaknesses):
            recommendations.append("减少交易频率，等待高质量信号")
        
        if any("冲动交易" in w for w in weaknesses):
            recommendations.append("建立交易前检查清单")
        
        if not recommendations:
            recommendations.append("保持当前策略执行")
        
        return recommendations
    
    def compare_strategies(self,
                           strategy_a: str,
                           strategy_b: str,
                           period: str = 'last_30_days') -> Dict:
        """比较两个策略"""
        report_a = self.analyze(strategy_a, period)
        report_b = self.analyze(strategy_b, period)
        
        return {
            'strategy_a': {
                'name': strategy_a,
                'total_pnl': report_a.get('total_pnl', 0),
                'win_rate': report_a.get('win_rate', 0),
                'total_trades': report_a.get('total_trades', 0)
            },
            'strategy_b': {
                'name': strategy_b,
                'total_pnl': report_b.get('total_pnl', 0),
                'win_rate': report_b.get('win_rate', 0),
                'total_trades': report_b.get('total_trades', 0)
            },
            'comparison': {
                'pnl_difference': report_a.get('total_pnl', 0) - report_b.get('total_pnl', 0),
                'win_rate_difference': report_a.get('win_rate', 0) - report_b.get('win_rate', 0),
                'better_performer': strategy_a if report_a.get('total_pnl', 0) > report_b.get('total_pnl', 0) else strategy_b
            }
        }


# 便捷函数
def analyze_strategy(strategy_name: str = None, period: str = 'last_30_days') -> Dict:
    """分析策略表现"""
    analyzer = StrategyAnalyzer()
    return analyzer.analyze(strategy_name, period)
