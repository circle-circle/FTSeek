"""
每月复盘生成器 (美股版)

生成每月交易复盘报告，包含策略归因、收益分析、风险评估
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import json
import sqlite3

from app.storage.trade_recorder import get_trade_recorder
from app.storage.review_tables import get_review_database

logger = logging.getLogger("ftseek_us.review.monthly")
logger.propagate = False


class MonthlyReviewGenerator:
    """每月复盘生成器 (美股版)"""
    
    def __init__(self):
        from app.core.paths import get_trade_records_db, get_monthly_reviews_dir
        self.trade_recorder = get_trade_recorder()
        # Fix: Override to use the active trading DB (trade_recorder defaults to legacy trade_records.db)
        self.trade_recorder.db_path = get_trade_records_db()
        self.review_db = get_review_database()
        self.output_dir = get_monthly_reviews_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate(self, year: int = None, month: int = None, account_type: str = None) -> Dict[str, Any]:
        """
        生成每月复盘报告
        
        Args:
            year: 年份，默认当前年
            month: 月份，默认上个月
            account_type: 账户类型筛选
        
        Returns:
            月报数据
        """
        now = datetime.now()
        if year is None:
            year = now.year
        if month is None:
            # 默认上个月
            if now.month == 1:
                year = now.year - 1
                month = 12
            else:
                month = now.month - 1
        
        # 计算月份范围
        month_start = datetime(year, month, 1)
        if month == 12:
            month_end = datetime(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = datetime(year, month + 1, 1) - timedelta(days=1)
        
        logger.info(f"生成每月复盘报告：{year}年{month}月")
        
        # 获取当月交易记录
        trades = self._get_trades_by_month(year, month, account_type)
        
        # 生成报告
        report = {
            'report_type': 'monthly',
            'year': year,
            'month': month,
            'period_start': month_start.strftime('%Y-%m-%d'),
            'period_end': month_end.strftime('%Y-%m-%d'),
            'generated_at': datetime.now().isoformat(),
            'account_type': account_type or 'all',
            
            # 1. 收益统计
            'performance': self._calculate_performance(trades),
            
            # 2. 风险指标
            'risk_metrics': self._calculate_risk_metrics(trades),
            
            # 3. 策略归因
            'strategy_attribution': self._analyze_strategies(trades),
            
            # 4. 多 Agent 投票分析
            'voting_analysis': self._analyze_voting(trades),
            
            # 5. 决策质量分析
            'decision_quality': self._analyze_decisions(trades),
            
            # 6. 最佳/最差交易
            'best_worst_trades': self._get_best_worst_trades(trades),
            
            # 7. 错误模式识别
            'error_patterns': self._identify_error_patterns(trades),
            
            # 8. 改进行动计划
            'improvement_actions': self._generate_improvement_actions(trades),
            
            # 9. 下月计划
            'next_month_plan': self._generate_next_month_plan(trades),
        }
        
        self._save_report(report, year, month)
        logger.info(f"每月复盘报告生成完成：{len(trades)} 笔交易")
        return report
    
    def _get_trades_by_month(self, year: int, month: int, account_type: str = None) -> List[Dict]:
        """获取指定月份的交易记录"""
        try:
            conn = sqlite3.connect(str(self.trade_recorder.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            start_date = f"{year}-{month:02d}-01"
            if month == 12:
                end_date = f"{year + 1}-01-01"
            else:
                end_date = f"{year}-{month + 1:02d}-01"
            
            query = """
                SELECT * FROM trade_records
                WHERE trade_date >= ? AND trade_date < ?
            """
            params = [start_date, end_date]
            
            if account_type:
                query += " AND account_type = ?"
                params.append(account_type)
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            conn.close()
            
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"获取交易记录失败：{e}")
            return []
    
    def _calculate_performance(self, trades: List[Dict]) -> Dict:
        """计算收益统计"""
        if not trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'profit_factor': 0,
                'sharpe_ratio': 0,
                'avg_pnl_pct': 0,
            }
        
        total_pnl = sum(t.get('realized_pnl', 0) or 0 for t in trades)
        winning = [t for t in trades if (t.get('realized_pnl', 0) or 0) > 0]
        losing = [t for t in trades if (t.get('realized_pnl', 0) or 0) < 0]
        
        avg_win = sum(t.get('realized_pnl', 0) or 0 for t in winning) / len(winning) if winning else 0
        avg_loss = sum(t.get('realized_pnl', 0) or 0 for t in losing) / len(losing) if losing else 0
        
        # 计算盈利因子
        gross_profit = sum(t.get('realized_pnl', 0) or 0 for t in winning)
        gross_loss = abs(sum(t.get('realized_pnl', 0) or 0 for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
        
        # 计算夏普比率 (简化版)
        returns = [t.get('pnl_pct', 0) or 0 for t in trades]
        avg_return = sum(returns) / len(returns) if returns else 0
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5 if returns else 0
        sharpe_ratio = (avg_return * 12) / (std_return * (12 ** 0.5)) if std_return > 0 else 0  # 年化
        
        return {
            'total_trades': len(trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': round(len(winning) / len(trades), 4) if trades else 0,
            'total_pnl': round(total_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'sharpe_ratio': round(sharpe_ratio, 2),
            'avg_pnl_pct': round(avg_return, 4),
        }
    
    def _calculate_risk_metrics(self, trades: List[Dict]) -> Dict:
        """计算风险指标"""
        if not trades:
            return {
                'max_single_loss': 0,
                'max_drawdown': 0,
                'avg_holding_hours': 0,
                'stop_loss_execution_rate': 0,
            }
        
        # 最大单笔亏损
        max_loss = min(t.get('realized_pnl', 0) or 0 for t in trades)
        
        # 最大回撤 (简化计算)
        cumulative_pnl = 0
        peak = 0
        max_drawdown = 0
        for t in sorted(trades, key=lambda x: x.get('exit_time', '')):
            cumulative_pnl += t.get('realized_pnl', 0) or 0
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            drawdown = peak - cumulative_pnl
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        # 平均持仓时间
        holding_hours = [t.get('holding_hours', 0) or 0 for t in trades]
        avg_holding = sum(holding_hours) / len(holding_hours) if holding_hours else 0
        
        # 止损执行率
        sl_trades = [t for t in trades if t.get('stop_loss_planned')]
        sl_executed = len([t for t in sl_trades if t.get('stop_loss_executed', True)])
        sl_rate = sl_executed / len(sl_trades) if sl_trades else 1.0
        
        return {
            'max_single_loss': round(max_loss, 2),
            'max_drawdown': round(max_drawdown, 2),
            'avg_holding_hours': round(avg_holding, 1),
            'stop_loss_execution_rate': round(sl_rate, 4),
        }
    
    def _analyze_strategies(self, trades: List[Dict]) -> Dict:
        """策略归因分析"""
        if not trades:
            return {}
        
        strategies = {}
        for t in trades:
            strategy = t.get('strategy_type', 'voting_llm') or 'voting_llm'
            if strategy not in strategies:
                strategies[strategy] = {
                    'trades': 0,
                    'wins': 0,
                    'total_pnl': 0,
                    'pnls': [],
                }
            strategies[strategy]['trades'] += 1
            pnl = t.get('realized_pnl', 0) or 0
            strategies[strategy]['total_pnl'] += pnl
            strategies[strategy]['pnls'].append(pnl)
            if pnl > 0:
                strategies[strategy]['wins'] += 1
        
        # 计算各策略指标
        result = {}
        for strategy, stats in strategies.items():
            result[strategy] = {
                'trades': stats['trades'],
                'win_rate': round(stats['wins'] / stats['trades'], 4) if stats['trades'] else 0,
                'total_pnl': round(stats['total_pnl'], 2),
                'avg_pnl': round(stats['total_pnl'] / stats['trades'], 2) if stats['trades'] else 0,
            }
        
        return result
    
    def _analyze_voting(self, trades: List[Dict]) -> Dict:
        """多 Agent 投票分析"""
        if not trades:
            return {}
        
        # 分析信号强度与实际收益的关系
        strength_bins = {
            'high (70-100)': {'trades': 0, 'wins': 0, 'pnl': 0},
            'medium (50-70)': {'trades': 0, 'wins': 0, 'pnl': 0},
            'low (<50)': {'trades': 0, 'wins': 0, 'pnl': 0},
        }
        
        for t in trades:
            strength = t.get('signal_strength', 50) or 50
            pnl = t.get('realized_pnl', 0) or 0
            
            if strength >= 70:
                bin_key = 'high (70-100)'
            elif strength >= 50:
                bin_key = 'medium (50-70)'
            else:
                bin_key = 'low (<50)'
            
            strength_bins[bin_key]['trades'] += 1
            strength_bins[bin_key]['pnl'] += pnl
            if pnl > 0:
                strength_bins[bin_key]['wins'] += 1
        
        # 计算各档位的胜率
        for bin_key in strength_bins:
            trades_count = strength_bins[bin_key]['trades']
            if trades_count > 0:
                strength_bins[bin_key]['win_rate'] = round(
                    strength_bins[bin_key]['wins'] / trades_count, 4
                )
                strength_bins[bin_key]['avg_pnl'] = round(
                    strength_bins[bin_key]['pnl'] / trades_count, 2
                )
            else:
                strength_bins[bin_key]['win_rate'] = 0
                strength_bins[bin_key]['avg_pnl'] = 0
        
        return {
            'strength_analysis': strength_bins,
            'avg_signal_strength': round(
                sum(t.get('signal_strength', 50) or 50 for t in trades) / len(trades), 2
            ) if trades else 0,
            'avg_signal_confidence': round(
                sum(t.get('signal_confidence', 0.5) or 0.5 for t in trades) / len(trades), 4
            ) if trades else 0,
        }
    
    def _analyze_decisions(self, trades: List[Dict]) -> Dict:
        """决策质量分析"""
        if not trades:
            return {}
        
        # 按决策原因分析
        reasons = {}
        for t in trades:
            reason = t.get('decision_reason', 'unknown') or 'unknown'
            if reason not in reasons:
                reasons[reason] = {'trades': 0, 'wins': 0, 'pnl': 0}
            reasons[reason]['trades'] += 1
            pnl = t.get('realized_pnl', 0) or 0
            reasons[reason]['pnl'] += pnl
            if pnl > 0:
                reasons[reason]['wins'] += 1
        
        # 计算各决策原因的准确率
        for reason in reasons:
            trades_count = reasons[reason]['trades']
            reasons[reason]['win_rate'] = round(reasons[reason]['wins'] / trades_count, 4) if trades_count else 0
            reasons[reason]['avg_pnl'] = round(reasons[reason]['pnl'] / trades_count, 2) if trades_count else 0
        
        return {
            'by_reason': reasons,
            'avg_decision_confidence': round(
                sum(t.get('decision_confidence', 5) or 5 for t in trades) / len(trades), 2
            ) if trades else 0,
        }
    
    def _get_best_worst_trades(self, trades: List[Dict]) -> Dict:
        """获取最佳/最差交易"""
        if not trades:
            return {}
        
        sorted_trades = sorted(trades, key=lambda x: x.get('realized_pnl', 0) or 0)
        
        best = sorted_trades[-1] if sorted_trades else None
        worst = sorted_trades[0] if sorted_trades else None
        
        return {
            'best_trade': {
                'symbol': best.get('symbol') if best else None,
                'pnl': best.get('realized_pnl') if best else None,
                'pnl_pct': best.get('pnl_pct') if best else None,
                'exit_reason': best.get('exit_reason') if best else None,
            } if best else None,
            'worst_trade': {
                'symbol': worst.get('symbol') if worst else None,
                'pnl': worst.get('realized_pnl') if worst else None,
                'pnl_pct': worst.get('pnl_pct') if worst else None,
                'exit_reason': worst.get('exit_reason') if worst else None,
            } if worst else None,
        }
    
    def _identify_error_patterns(self, trades: List[Dict]) -> List[Dict]:
        """识别错误模式"""
        patterns = []
        
        # 1. 冲动交易
        impulse = [t for t in trades if t.get('decision_reason') == 'impulse']
        if len(impulse) >= 3:
            patterns.append({
                'pattern': 'impulse_trading',
                'count': len(impulse),
                'total_loss': sum(t.get('realized_pnl', 0) or 0 for t in impulse),
                'suggestion': '减少冲动交易，等待高质量信号',
            })
        
        # 2. 止损违规
        violations = [t for t in trades if t.get('stop_loss_planned') and not t.get('stop_loss_executed', True)]
        if violations:
            patterns.append({
                'pattern': 'stop_loss_violation',
                'count': len(violations),
                'total_loss': sum(t.get('realized_pnl', 0) or 0 for t in violations),
                'suggestion': '严格执行止损纪律',
            })
        
        # 3. 低信号强度交易
        low_strength = [t for t in trades if (t.get('signal_strength', 50) or 50) < 50]
        if len(low_strength) >= 3:
            patterns.append({
                'pattern': 'low_signal_trading',
                'count': len(low_strength),
                'total_loss': sum(t.get('realized_pnl', 0) or 0 for t in low_strength),
                'suggestion': '提高信号强度阈值至 50 以上',
            })
        
        return patterns
    
    def _generate_improvement_actions(self, trades: List[Dict]) -> List[Dict]:
        """生成改进行动"""
        actions = []
        
        performance = self._calculate_performance(trades)
        risk = self._calculate_risk_metrics(trades)
        errors = self._identify_error_patterns(trades)
        
        # 基于胜率
        if performance.get('win_rate', 0) < 0.5:
            actions.append({
                'action': '提高入场标准',
                'priority': 'high',
                'detail': f"当前胜率 {performance.get('win_rate', 0)*100:.1f}%，建议提高信号强度阈值",
            })
        
        # 基于盈利因子
        if performance.get('profit_factor', 0) < 1.5:
            actions.append({
                'action': '优化止盈止损比例',
                'priority': 'high',
                'detail': f"当前盈利因子 {performance.get('profit_factor', 0):.2f}，建议调整止盈目标",
            })
        
        # 基于错误模式
        for error in errors:
            actions.append({
                'action': error['suggestion'],
                'priority': 'medium',
                'detail': f"发现 {error['pattern']} 问题 {error['count']} 次",
            })
        
        return actions
    
    def _generate_next_month_plan(self, trades: List[Dict]) -> List[str]:
        """生成下月计划"""
        plans = []
        
        performance = self._calculate_performance(trades)
        
        if performance.get('win_rate', 0) >= 0.6:
            plans.append("✅ 胜率达标，继续保持当前策略")
        else:
            plans.append("⚠️ 胜率偏低，需要优化入场条件")
        
        if performance.get('sharpe_ratio', 0) >= 1.5:
            plans.append("✅ 夏普比率达标，风险调整后收益良好")
        else:
            plans.append("⚠️ 夏普比率偏低，需要提高收益稳定性")
        
        plans.append(f"📌 目标胜率: 60%+ | 当前: {performance.get('win_rate', 0)*100:.1f}%")
        plans.append(f"📌 目标盈利因子: 1.5+ | 当前: {performance.get('profit_factor', 0):.2f}")
        
        return plans
    
    def _save_report(self, report: Dict, year: int, month: int):
        """保存报告"""
        filename = self.output_dir / f"monthly_review_{year}_{month:02d}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    
    def get_report(self, year: int, month: int) -> Optional[Dict]:
        """获取已保存的报告"""
        filename = self.output_dir / f"monthly_review_{year}_{month:02d}.json"
        if not filename.exists():
            return None
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


def generate_monthly_review(year: int = None, month: int = None, account_type: str = None) -> Dict:
    """生成每月复盘报告"""
    generator = MonthlyReviewGenerator()
    return generator.generate(year, month, account_type)