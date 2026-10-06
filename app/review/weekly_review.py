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
from app.review.source_attribution import aggregate_source_direction, playbook_source_sql

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
        
        # 从 ftseek_us.db 读取周内交易 (2026-08-01: 补 D3 正向白名单 — 原查询无任何
        # source_type 过滤, SHADOW/退休引擎行会混入周度统计)
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date >= ? AND trade_date <= ? "
            "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND trade_mode != 'live' "
            "ORDER BY created_at",
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

        # RFC-061 cohort A/B 退出实验度量 (OKR KR2.1/KR2.2 增量)
        cohort_ab = {}
        try:
            from app.review.cohort_ab import compute_cohort_report
            cohort_ab = compute_cohort_report(since=week_start)
        except Exception as e:
            logger.warning(f"cohort A/B 度量失败 (不影响周复盘): {e}")
            cohort_ab = {"error": str(e)}

        # 2026-08-01 (议题四尺子固化): 方向×regime 交叉 + 周度方向稳定性 — 与周六
        #   direction_hit_scorecard 共用 app/review/direction_axes 同一口径源 (防两处 SQL 漂移).
        #   窗口固定 trailing 35d 而非本周, 保证 5 周稳定性有序列可读.
        direction_axes_block = {}
        try:
            from app.review.direction_axes import direction_regime_cross, weekly_direction_stability
            from app.storage.db_pool import get_db
            _ax_conn = get_db("ftseek_us").connection()
            _since35 = (datetime.now() - timedelta(days=35)).strftime('%Y-%m-%d')
            direction_axes_block = {
                "cross_exec": direction_regime_cross(_ax_conn, _since35, executed_only=True),
                "stability_exec": weekly_direction_stability(_ax_conn, weeks=5, executed_only=True),
                "since": _since35,
            }
        except Exception as e:
            logger.warning(f"direction_axes 读取失败 (不影响周复盘): {e}")
            direction_axes_block = {"error": str(e)}

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
            'cohort_ab': cohort_ab,
            'direction_axes': direction_axes_block,
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
        """周度信号漏斗聚合 (2026-08-01 改源: playbook_instructions — decisions.db 随旧引擎
        07-12 停写, 周决策数恒 0 系伪读数)"""
        try:
            rows = self._query(
                "SELECT date, direction, executed, "
                f"({playbook_source_sql()}) AS source, COUNT(*) as cnt "
                "FROM playbook_instructions "
                "WHERE date >= ? AND date <= ? GROUP BY date, direction, executed, source",
                (week_start, week_end)
            )
            total = sum(r['cnt'] for r in rows)
            by_day = {}
            by_direction = {}
            for r in rows:
                by_day[r['date']] = by_day.get(r['date'], 0) + r['cnt']
                suffix = '' if r['executed'] else '(未执行)'
                key = f"{r['direction']}{suffix}"
                by_direction[key] = by_direction.get(key, 0) + r['cnt']

            return {
                'total_decisions': total,
                'by_day': by_day,
                'by_direction_strength': by_direction,
                'by_source_direction': aggregate_source_direction(rows),
                'source': 'playbook_instructions',
                'source_attribution_version': 'rfc117_v2',
            }
        except Exception as e:
            return {'total_decisions': 0, 'error': str(e)}
    
    def _get_weekly_rejection_trend(self, week_start: str, week_end: str) -> Dict:
        """周度拒绝趋势
        2026-08-01 退役标注: rejections.db 06-29 停写 (playbook 路径不接 rejection_logger),
        此后每周 total 恒 0 — 伪读数。未执行指令分布见信号漏斗。
        """
        return {
            'total': 0,
            'status': 'retired',
            'note': '拒绝趋势已退役: rejections.db 停写于 2026-06-29 (playbook 路径不落拒绝)。',
        }
    
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

        # 净口径 (2026-08-01, DEB-20260730 D1)
        net_pnls = [(t.get('pnl_net') if t.get('pnl_net') is not None else (t.get('realized_pnl', 0) or 0))
                    for t in closed_trades]
        winning_net = [p for p in net_pnls if p > 0]

        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4) if closed_trades else 0.0,
            'avg_pnl_per_trade': round(total_pnl / len(closed_trades), 2),
            'pnl_basis': 'net: COALESCE(pnl_net, realized_pnl) (DEB-20260730 D1)',
            'fees_total': round(sum(t.get('fees_est', 0) or 0 for t in closed_trades), 2),
            'total_pnl_net': round(sum(net_pnls), 2),
            'winning_trades_net': len(winning_net),
            'win_rate_net': round(len(winning_net) / len(closed_trades), 4) if closed_trades else 0.0,
            'fake_wins': sum(1 for t, p in zip(closed_trades, net_pnls)
                             if (t.get('realized_pnl', 0) or 0) > 0 >= p),
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
        """投票效果分析

        2026-10-06 修: 原判据用 `(t.get('final_strength') or 50)` fallback, 而
        final_strength/consensus_level 是**化石列恒 0** (3 模型投票 2026-07-12 退役,
        voting_predictions 停写) ⇒
          high_strength (>=70) : 恒 0 笔
          medium_strength      : 恒吞掉全部 335/335 笔
          low_strength  (<50)  : 恒 0 笔
          avg_signal_strength  : 恒等于常量 50.00  ← 不是测量值, 是 fallback 假数
        实测现行期 333 笔全落 medium 桶 ⇒ 本函数每周把一份**结构性假数据**推进周报
        (L132 voting_analysis 进主链, L441 亦消费)。
        处置同 daily_review._analyze_voting_consistency (2026-09-04 M4 已裁决):
        上游退役 ⇒ 明确标不可用, 不用 or-50 造出看似合理的数字。
        """
        if not trades:
            return {
                'data_available': False,
                'status': 'no_trades',
            }

        final_strengths = [t.get('final_strength') for t in trades]
        consensus_levels = [t.get('consensus_level') for t in trades]
        has_valid_strength = any(s and s > 0 for s in final_strengths)
        has_valid_consensus = any(c and c > 0 for c in consensus_levels)

        if not has_valid_strength and not has_valid_consensus:
            return {
                'data_available': False,
                'status': 'retired',
                'note': '3模型投票已退役 (voting_predictions 停写于 2026-07-12), '
                        'final_strength/consensus_level 为化石列恒 0 ⇒ 投票效果分析不可用。'
                        '此前 avg_signal_strength 恒 50.00 是 or-50 fallback 假数, 已停报。'
                        '信号质量见 signal_confidence (活字段) 与 daily_review 的 signal_quality 维度。',
                # 保留键位以免下游 KeyError; 值为 None 表示「不可用」而非 0。
                'avg_signal_strength': None,
                'high_strength_trades': 0,
                'medium_strength_trades': 0,
                'low_strength_trades': 0,
            }

        high_strength = [t for t in trades if (t.get('final_strength') or 0) >= 70]
        medium_strength = [t for t in trades if 50 <= (t.get('final_strength') or 0) < 70]
        low_strength = [t for t in trades if 0 < (t.get('final_strength') or 0) < 50]

        def calc_win_rate(trade_list):
            if not trade_list:
                return 0
            wins = sum(1 for t in trade_list if (t.get('realized_pnl', 0) or 0) > 0)
            return round(wins / len(trade_list), 4)

        return {
            'data_available': True,
            'status': 'ok',
            'avg_signal_strength': round(
                sum(t.get('final_strength', 0) or 0 for t in trades) / len(trades), 2),
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
        """一致性评分 - 交易风格是否稳定

        2026-10-06 修: 原判据 `[t.get('final_strength', 50) or 50 ...]` 在化石列
        (final_strength 恒 0, 3模型投票 2026-07-12 退役) 上 ⇒ 全部落 fallback 50 ⇒
        std_strength = 0.00 ⇒ consistency = 1 - 0/30 = **1.00 恒满分**,
        interpretation 恒 'high'。实测现行期 333 笔即如此。
        危害: 周报每周推送「🎯 一致性 🟢 1.00 (high)」= **假满分**, 比假 50.00 更误导 —
        它声称「交易风格非常稳定」, 而实际是零信息。
        处置沿用本仓既有约定 (scheduler_triggers._format_weekly_review 判
        `score is not None and 'note' not in consistency` 才渲染): 不可用时 score=None + note,
        格式化器自动跳过该行, 无需改推送端。
        """
        if len(trades) < 2:
            return {'score': None, 'note': '交易数不足，无法评估一致性'}

        strengths = [t.get('final_strength') for t in trades]
        # 化石列检测: 全 0/None ⇒ 无信息, 不得用 or-50 造出假满分
        if not any(s and s > 0 for s in strengths):
            return {
                'score': None,
                'note': 'final_strength 为化石列恒 0 (3模型投票已退役 2026-07-12) '
                        '⇒ 一致性无法评估。此前恒报 1.00/high 是 or-50 fallback 的假满分。',
            }

        vals = [s for s in strengths if s and s > 0]
        avg_strength = sum(vals) / len(vals)
        variance = sum((s - avg_strength) ** 2 for s in vals) / len(vals)
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
