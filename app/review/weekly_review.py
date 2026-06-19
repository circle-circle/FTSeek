"""
每周复盘生成器 (美股版)

修复 2026-04-25:
- 原数据源 trade_records.db 无数据 (实际写入 ftseek_us.db)
- 原只有 4 个维度，现升级为 12 维度 (与日报对齐 + 周度特有分析)
- 新增: 信号漏斗聚合 + 拒绝趋势 + 资金周曲线 + 逐日对比 + 一致性指标
- 导入了 DailyReviewGenerator 但从未使用 → 现聚合每日报告做周度分析

周度特有分析:
- 逐日交易分布 (哪天交易最多/最少)
- 资金周曲线 (周一到周五净值变化)
- 一致性评分 (交易风格是否一致)
- 周度拒绝趋势 (拒绝数逐日变化)
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
import json

from app.core.paths import get_trade_records_db

logger = logging.getLogger("ftseek_us.review.weekly")
logger.propagate = False


class WeeklyReviewGenerator:
    """每周复盘生成器 (美股版)
    
    修复 2026-04-25: 从 ftseek_us.db 直接读取数据，聚合每日报告做周度分析
    """
    
    def __init__(self):
        self.db_path = get_trade_records_db()
        from app.core.paths import get_weekly_reviews_dir
        self.output_dir = get_weekly_reviews_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _query(self, sql, params=()):
        """查询 ftseek_us.db"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[weekly_review] 查询失败: {e}")
            return []
    
    def generate(self, week_start: str = None, week_end: str = None, account_type: str = None) -> Dict:
        """生成每周复盘报告"""
        if week_start is None:
            today = datetime.now()
            week_start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        if week_end is None:
            week_end = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"生成每周复盘报告：{week_start} 至 {week_end}")
        
        # 从 ftseek_us.db 读取周内交易
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date >= ? AND trade_date <= ? ORDER BY created_at",
            (week_start, week_end)
        )
        closed_trades = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]
        
        if account_type:
            trades = [t for t in trades if t.get('account_type') == account_type]
            closed_trades = [t for t in closed_trades if t.get('account_type') == account_type]
        
        # 聚合周内每日报告
        daily_reviews = self._aggregate_daily_reviews(week_start, week_end)
        
        # 信号漏斗聚合
        signal_funnel = self._get_weekly_signal_funnel(week_start, week_end)
        
        # 拒绝趋势分析
        rejection_trend = self._get_weekly_rejection_trend(week_start, week_end)
        
        # 资金周曲线
        capital_curve = self._get_weekly_capital_curve(week_start, week_end)
        
        # 逐日对比分析
        daily_comparison = self._get_daily_comparison(week_start, week_end, trades)
        
        # 生成报告
        report = {
            'report_type': 'weekly',
            'period_start': week_start,
            'period_end': week_end,
            'generated_at': datetime.now().isoformat(),
            'summary': self._generate_summary(trades, closed_trades),
            'daily_comparison': daily_comparison,
            'signal_funnel': signal_funnel,
            'rejection_trend': rejection_trend,
            'capital_curve': capital_curve,
            'strategy_performance': self._analyze_strategies(closed_trades),
            'voting_analysis': self._analyze_voting(closed_trades),
            'risk_analysis': self._analyze_risk(closed_trades),
            'consistency_score': self._calculate_consistency(closed_trades),
            'key_learnings': self._extract_learnings(closed_trades),
            'improvement_plan': self._generate_plan(closed_trades, signal_funnel, rejection_trend),
            'daily_reviews_summary': daily_reviews,
        }
        
        self._save_report(report, week_start, week_end)
        logger.info(f"每周复盘报告生成完成：{len(closed_trades)} 笔交易, {len(trades)} 条记录")
        return report
    
    def _aggregate_daily_reviews(self, week_start: str, week_end: str) -> Dict:
        """聚合周内每日复盘报告"""
        reviews = []
        from app.core.paths import get_reviews_dir
        daily_dir = get_reviews_dir()
        
        current = datetime.strptime(week_start, '%Y-%m-%d')
        end = datetime.strptime(week_end, '%Y-%m-%d')
        
        while current <= end:
            date_str = current.strftime('%Y-%m-%d')
            filename = daily_dir / f"daily_review_{date_str}.json"
            if filename.exists():
                try:
                    with open(filename, 'r', encoding='utf-8') as f:
                        review = json.load(f)
                        summary = review.get('summary') or {}
                        reviews.append({
                            'date': date_str,
                            'trades': summary.get('total_trades') or 0,
                            'pnl': summary.get('total_pnl') or 0,
                            'win_rate': summary.get('win_rate') or 0,
                            'suggestions': review.get('improvement_suggestions') or [],
                        })
                except Exception as e:
                    logger.debug(f"读取日报失败 {date_str}: {e}")
            current += timedelta(days=1)
        
        return {
            'daily_count': len(reviews),
            'days_with_trades': len([r for r in reviews if r['trades'] > 0]),
            'reviews': reviews,
        }
    
    def _get_weekly_signal_funnel(self, week_start: str, week_end: str) -> Dict:
        """周度信号漏斗聚合"""
        try:
            from app.core.paths import get_decisions_db
            db = get_decisions_db()
            if not db.exists():
                return {'total_decisions': 0, 'error': 'decisions.db not found'}
            conn = sqlite3.connect(str(db))
            
            total = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE date(created_at) >= ? AND date(created_at) <= ?",
                (week_start, week_end)
            ).fetchone()[0]
            
            by_day = {}
            rows = conn.execute(
                "SELECT date(created_at) as day, COUNT(*) as cnt FROM decisions "
                "WHERE date(created_at) >= ? AND date(created_at) <= ? GROUP BY day",
                (week_start, week_end)
            ).fetchall()
            for r in rows:
                by_day[r[0]] = r[1]
            
            by_direction = {}
            rows = conn.execute(
                "SELECT direction, final_strength, COUNT(*) as cnt FROM decisions "
                "WHERE date(created_at) >= ? AND date(created_at) <= ? GROUP BY direction, final_strength",
                (week_start, week_end)
            ).fetchall()
            for r in rows:
                key = f"{r[0]}(s={r[1]})"
                by_direction[key] = r[2]
            
            conn.close()
            
            return {
                'total_decisions': total,
                'by_day': by_day,
                'by_direction_strength': by_direction,
            }
        except Exception as e:
            return {'total_decisions': 0, 'error': str(e)}
    
    def _get_weekly_rejection_trend(self, week_start: str, week_end: str) -> Dict:
        """周度拒绝趋势"""
        try:
            from app.core.paths import get_rejections_db
            db = get_rejections_db()
            if not db.exists():
                return {'total': 0, 'note': 'rejection_logger 未初始化'}
            conn = sqlite3.connect(str(db))
            
            total = conn.execute(
                "SELECT COUNT(*) FROM rejections WHERE date(timestamp) >= ? AND date(timestamp) <= ?",
                (week_start, week_end)
            ).fetchone()[0]
            
            by_day = {}
            rows = conn.execute(
                "SELECT date(timestamp) as day, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) >= ? AND date(timestamp) <= ? GROUP BY day",
                (week_start, week_end)
            ).fetchall()
            for r in rows:
                by_day[r[0]] = r[1]
            
            by_reason = {}
            rows = conn.execute(
                "SELECT reason_category, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) >= ? AND date(timestamp) <= ? GROUP BY reason_category ORDER BY cnt DESC",
                (week_start, week_end)
            ).fetchall()
            for r in rows:
                by_reason[r[0]] = r[1]
            
            by_symbol = {}
            rows = conn.execute(
                "SELECT symbol, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) >= ? AND date(timestamp) <= ? GROUP BY symbol ORDER BY cnt DESC LIMIT 5",
                (week_start, week_end)
            ).fetchall()
            for r in rows:
                by_symbol[r[0]] = r[1]
            
            conn.close()
            
            # 计算趋势
            day_values = list(by_day.values())
            trend = "stable"
            if len(day_values) >= 2:
                if day_values[-1] > day_values[0] * 1.5:
                    trend = "increasing"
                elif day_values[-1] < day_values[0] * 0.5:
                    trend = "decreasing"
            
            return {
                'total': total,
                'by_day': by_day,
                'by_reason': by_reason,
                'by_symbol': by_symbol,
                'trend': trend,
                'top_rejector': max(by_reason, key=by_reason.get) if by_reason else 'none',
            }
        except Exception as e:
            return {'total': 0, 'error': str(e)}
    
    def _get_weekly_capital_curve(self, week_start: str, week_end: str) -> Dict:
        """资金周曲线"""
        try:
            rows = self._query(
                "SELECT date(last_update) as day, current_equity, cash, drawdown_pct, water_level "
                "FROM capital_state WHERE date(last_update) >= ? AND date(last_update) <= ? "
                "ORDER BY last_update DESC",
                (week_start, week_end)
            )
            
            # 去重 (每天取最新)
            seen = set()
            unique = []
            for r in rows:
                day = r.get('day')
                if day and day not in seen:
                    seen.add(day)
                    unique.append(r)
            unique.reverse()  # 按时间正序
            
            if not unique:
                return {'note': 'No capital state for this week'}
            
            start_equity = unique[0].get('current_equity', 0)
            end_equity = unique[-1].get('current_equity', 0)
            weekly_pnl = end_equity - start_equity
            weekly_return = weekly_pnl / start_equity if start_equity else 0
            
            max_dd = max([r.get('drawdown_pct', 0) or 0 for r in unique], default=0)
            
            return {
                'start_equity': round(start_equity, 0),
                'end_equity': round(end_equity, 0),
                'weekly_pnl': round(weekly_pnl, 2),
                'weekly_return_pct': round(weekly_return * 100, 2),
                'max_drawdown_pct': round(max_dd * 100, 2),
                'daily_points': [
                    {
                        'day': r.get('day'),
                        'equity': round(r.get('current_equity', 0), 0),
                        'cash': round(r.get('cash', 0), 0),
                        'drawdown': round((r.get('drawdown_pct', 0) or 0) * 100, 2),
                    }
                    for r in unique
                ],
            }
        except Exception as e:
            return {'error': str(e)}
    
    def _get_daily_comparison(self, week_start: str, week_end: str, trades: List) -> Dict:
        """逐日对比分析"""
        daily_stats = {}
        for t in trades:
            day = t.get('trade_date', '')[:10]
            if not day:
                continue
            if day not in daily_stats:
                daily_stats[day] = {'trades': 0, 'wins': 0, 'pnl': 0}
            daily_stats[day]['trades'] += 1
            if (t.get('realized_pnl') or 0) > 0:
                daily_stats[day]['wins'] += 1
            daily_stats[day]['pnl'] += t.get('realized_pnl', 0) or 0
        
        # 四舍五入
        for d in daily_stats:
            daily_stats[d]['pnl'] = round(daily_stats[d]['pnl'], 2)
        
        # 找出最佳/最差交易日
        best_day = max(daily_stats, key=lambda d: daily_stats[d]['pnl']) if daily_stats else None
        worst_day = min(daily_stats, key=lambda d: daily_stats[d]['pnl']) if daily_stats else None
        
        return {
            'daily_stats': daily_stats,
            'best_day': best_day,
            'worst_day': worst_day,
            'trading_days': len(daily_stats),
        }
    
    def _generate_summary(self, all_trades: List, closed_trades: List) -> Dict:
        """周度汇总"""
        if not closed_trades:
            return {
                'total_trades': len(all_trades),
                'closed_trades': 0,
                'total_pnl': 0,
                'win_rate': 0,
            }
        
        total_pnl = sum(t.get('realized_pnl', 0) or 0 for t in closed_trades)
        winning = [t for t in closed_trades if (t.get('realized_pnl', 0) or 0) > 0]
        
        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4) if closed_trades else 0.0,
            'avg_pnl_per_trade': round(total_pnl / len(closed_trades), 2),
        }
    
    def _analyze_strategies(self, trades: List) -> Dict:
        """策略表现分析"""
        if not trades:
            return {'strategy_breakdown': {}}
        
        stats = {}
        for t in trades:
            strategy = t.get('strategy', 'unknown') or 'unknown'
            if strategy not in stats:
                stats[strategy] = {'trades': 0, 'wins': 0, 'pnl': 0}
            stats[strategy]['trades'] += 1
            pnl = t.get('realized_pnl', 0) or 0
            if pnl > 0:
                stats[strategy]['wins'] += 1
            stats[strategy]['pnl'] += pnl
        
        # 计算胜率
        for s in stats:
            stats[s]['win_rate'] = round(stats[s]['wins'] / stats[s]['trades'], 4) if stats[s]['trades'] > 0 else 0
            stats[s]['pnl'] = round(stats[s]['pnl'], 2)
        
        return {'strategy_breakdown': stats}
    
    def _analyze_voting(self, trades: List) -> Dict:
        """投票效果分析"""
        if not trades:
            return {}
        
        high_strength = [t for t in trades if (t.get('final_strength') or 50) >= 70]
        medium_strength = [t for t in trades if 50 <= (t.get('final_strength') or 50) < 70]
        low_strength = [t for t in trades if (t.get('final_strength') or 50) < 50]
        
        def calc_win_rate(trade_list):
            if not trade_list:
                return 0
            wins = sum(1 for t in trade_list if (t.get('realized_pnl', 0) or 0) > 0)
            return round(wins / len(trade_list), 4)
        
        return {
            'avg_signal_strength': round(sum(t.get('final_strength', 50) or 50 for t in trades) / len(trades), 2),
            'high_strength_trades': len(high_strength),
            'high_strength_win_rate': calc_win_rate(high_strength),
            'medium_strength_trades': len(medium_strength),
            'medium_strength_win_rate': calc_win_rate(medium_strength),
            'low_strength_trades': len(low_strength),
            'low_strength_win_rate': calc_win_rate(low_strength),
        }
    
    def _analyze_risk(self, trades: List) -> Dict:
        """风险分析"""
        if not trades:
            return {}
        
        sl_trades = [t for t in trades if t.get('stop_loss_planned') and t['stop_loss_planned'] > 0]
        executed = len([t for t in sl_trades if t.get('stop_loss_executed', True)])
        max_dd = max([t.get('max_drawdown', 0) or 0 for t in trades], default=0)
        
        return {
            'stop_loss_execution_rate': round(executed / len(sl_trades), 4) if sl_trades else 1.0,
            'max_drawdown': round(max_dd, 4),
        }
    
    def _calculate_consistency(self, trades: List) -> Dict:
        """一致性评分 - 交易风格是否稳定"""
        if len(trades) < 2:
            return {'score': 1.0, 'note': '交易数不足，无法评估一致性'}
        
        # 信号强度标准差 (越低越一致)
        strengths = [t.get('final_strength', 50) or 50 for t in trades]
        avg_strength = sum(strengths) / len(strengths)
        variance = sum((s - avg_strength) ** 2 for s in strengths) / len(strengths)
        std_strength = variance ** 0.5
        
        # 归一化到 0-1 (0=完全不一致, 1=完全一致)
        # 标准差 < 10 → 非常一致, > 30 → 很不一致
        consistency = max(0, min(1, 1 - (std_strength / 30)))
        
        return {
            'score': round(consistency, 2),
            'avg_strength': round(avg_strength, 2),
            'std_strength': round(std_strength, 2),
            'interpretation': 'high' if consistency > 0.7 else 'medium' if consistency > 0.4 else 'low',
        }
    
    def _extract_learnings(self, trades: List) -> List[str]:
        """提取关键教训"""
        learnings = set()
        for t in trades:
            if t.get('lessons_learned'):
                learnings.add(t['lessons_learned'])
            if t.get('review_notes'):
                learnings.add(t['review_notes'])
        return list(learnings)[:10]
    
    def _generate_plan(self, trades: List, signal_funnel: Dict = None, rejection_trend: Dict = None) -> List:
        """生成改进计划"""
        plan = []
        
        if trades:
            summary = self._generate_summary(trades, trades)
            if summary.get('win_rate', 0) < 0.4:
                plan.append({'action': '胜率低于 40%，建议重新评估策略', 'priority': 'high'})
            
            risk = self._analyze_risk(trades)
            if risk.get('stop_loss_execution_rate', 1) < 0.8:
                plan.append({'action': '止损执行率低于 80%，需严格执行纪律', 'priority': 'high'})
            
            voting = self._analyze_voting(trades)
            if voting.get('low_strength_trades', 0) > 0:
                plan.append({
                    'action': f"有 {voting['low_strength_trades']} 笔低信号强度交易，建议提高入场门槛",
                    'priority': 'medium'
                })
            
            consistency = self._calculate_consistency(trades)
            if consistency.get('interpretation') == 'low':
                plan.append({
                    'action': f"交易一致性低 (评分 {consistency['score']})，建议收敛策略范围",
                    'priority': 'medium'
                })
        
        # 信号漏斗建议
        if signal_funnel and signal_funnel.get('total_decisions', 0) > 0:
            total = signal_funnel['total_decisions']
            executed = len(trades)
            if total > 0 and executed == 0:
                plan.append({
                    'action': f"本周产生 {total} 条决策但 0 笔执行，检查门禁/风控是否过严",
                    'priority': 'high'
                })
        
        # 拒绝趋势建议
        if rejection_trend and rejection_trend.get('total', 0) > 0:
            total_rej = rejection_trend['total']
            trend = rejection_trend.get('trend', 'stable')
            top_reason = rejection_trend.get('top_rejector', 'unknown')
            plan.append({
                'action': f"本周 {total_rej} 次信号被拒绝 (趋势: {trend})，TOP 原因: {top_reason}",
                'priority': 'medium'
            })
        
        if not plan:
            if not trades:
                plan.append({'action': '本周无交易，系统运行正常，等待交易信号', 'priority': 'low'})
            else:
                plan.append({'action': '本周交易执行正常，继续保持', 'priority': 'low'})
        
        return plan
    
    def _save_report(self, report: Dict, week_start: str, week_end: str):
        """保存报告"""
        filename = self.output_dir / f"weekly_review_{week_start}_to_{week_end}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    
    def get_report(self, week_start: str, week_end: str) -> Optional[Dict]:
        """获取已保存的报告"""
        filename = self.output_dir / f"weekly_review_{week_start}_to_{week_end}.json"
        if not filename.exists():
            return None
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


def generate_weekly_review(week_start: str = None, week_end: str = None) -> Dict:
    generator = WeeklyReviewGenerator()
    return generator.generate(week_start, week_end)
