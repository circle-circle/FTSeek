"""
每日复盘生成器 (美股版 v2.0)

修复 2026-04-25: 
- 原数据源 trade_records.db 无数据 (实际写入 ftseek_us.db)
- 0 笔交易时只输出空壳，无诊断价值
- 新增: 信号漏斗 + 拒绝分析 + 资金状态

修复 2026-05-02 (辩论部署 P0-P1):
- P0: 修复幽灵列引用 (stop_loss_planned, lessons_learned, review_notes 不存在)
- P0: 修复 voting_analysis 虚构数据 (final_strength=0, or 50 fallback)
- P0: 修复 _extract_learnings 读取不存在的列
- P1: 6维度重构为系统性能维度 (替代人工交易心理学)
- P1: 新增综合评分系统 (0-10分制)
- P1: 新增市场状态感知
- P1: 新增 trade_attribution 47列分析
- P1: 集成 plan_vs_actual 对比

生成每日交易复盘报告，包含 6 大维度分析
适配美股多 Agent 架构
"""
import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional

from app.core.paths import get_trade_records_db, get_decisions_db, get_rejections_db, get_reviews_dir
from pathlib import Path
import json

logger = logging.getLogger("ftseek_us.review.daily")
logger.propagate = False


class DailyReviewGenerator:
    """每日复盘生成器 (美股版 v2.0)
    
    6维度体系:
    ①信号质量 ②投票一致性 ③执行质量 ④风控合规 ⑤漏斗诊断 ⑥归因学习
    """
    
    def __init__(self):
        self.db_path = get_trade_records_db()
        self.decisions_db = get_decisions_db()
        self.rejections_db = get_rejections_db()
        self.output_dir = get_reviews_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _query(self, sql, params=(), db_path=None):
        """查询数据库"""
        target = db_path or self.db_path
        try:
            conn = sqlite3.connect(str(target))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"[daily_review] 查询失败 ({target.name}): {e}")
            return []
    
    def generate(self, date: str = None, account_type: str = None) -> Dict[str, Any]:
        """生成每日复盘报告 (v2.0)"""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"生成每日复盘报告 v2.0：{date}")
        
        # 从 ftseek_us.db 读取交易记录
        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date = ? ORDER BY created_at",
            (date,)
        )
        closed_trades = [t for t in trades if t.get('exit_price') is not None]
        
        # ── 数据源采集 ──
        signal_funnel = self._get_signal_funnel(date)
        rejection_analysis = self._get_rejection_analysis(date)
        capital_state = self._get_capital_state(date)
        attribution_data = self._analyze_attribution(date)       # Phase 4: 新增
        plan_vs_actual = self._get_plan_vs_actual(date)          # Phase 4: 新增
        
        # ── Phase 5: 逐笔交易深度分析 (新增 2026-05-02) ──
        trade_analyses = self._analyze_per_trade(trades)
        
        # ── 6维度分析 ──
        report = {
            'report_type': 'daily',
            'report_version': '2.0',
            'report_date': date,
            'generated_at': datetime.now().isoformat(),
            'summary': self._generate_summary(trades, closed_trades, capital_state),
            'signal_funnel': signal_funnel,
            'rejection_analysis': rejection_analysis,
            'capital_state': capital_state,

            # Phase 6: 五因子归因数据 (供 _format_daily_review 使用)
            'attribution_data': attribution_data,

            # Phase 5: 逐笔交易分析
            'trade_analyses': trade_analyses,
            
            # Phase 3: 6维度系统性能分析 (替代旧版人工交易维度)
            'dimensions': {
                'signal_quality': self._analyze_signal_quality(trades, signal_funnel, attribution_data),
                'voting_consistency': self._analyze_voting_consistency(closed_trades),
                'execution_quality': self._analyze_execution_quality(closed_trades, attribution_data),
                'risk_compliance': self._analyze_risk_compliance(closed_trades, capital_state),
                'funnel_diagnosis': self._analyze_funnel_diagnosis(trades, signal_funnel, rejection_analysis),
                'attribution_learning': self._analyze_attribution_learning(attribution_data, closed_trades),
            },
            
            # 兼容旧字段 (向后兼容)
            'decision_analysis': self._analyze_field(closed_trades, 'decision_reason', 'signal_confidence'),
            'risk_analysis': self._analyze_risk_compliance(closed_trades, capital_state),
            'voting_analysis': self._analyze_voting_consistency(closed_trades),
            'error_patterns': self._detect_errors(closed_trades),
            
            # Phase 3: 综合评分
            'review_score': self._calculate_review_score({}, signal_funnel, capital_state, attribution_data, closed_trades),
            'market_regime': self._detect_market_regime(capital_state, attribution_data),
            
            # Phase 4: 计划对比
            'plan_vs_actual': plan_vs_actual,
            
            # 旧版字段保留
            'key_learnings': self._extract_learnings_v2(attribution_data, closed_trades),
            'improvement_suggestions': self._generate_suggestions_v2(
                closed_trades, signal_funnel, rejection_analysis, capital_state, attribution_data
            ),

            # F22 (2026-05-14): exit_reason × pnl 分布
            'exit_reason_pnl': self._analyze_exit_reason_pnl(window_days=30),

            # Phase A (2026-05-14): coordinator 状态汇总
            'coordinator_summary': self._analyze_coordinator(date),

            # Z4 (2026-05-15): 复盘能力补全
            'shadow_comparison': self._analyze_shadow_vs_real(date),
            'pnl_outliers': self._analyze_pnl_outliers(date),
            'symbol_loss_guard_triggers': self._analyze_loss_guard(date),
        }

        # 回填评分到 report
        report['review_score'] = self._calculate_review_score(
            report['dimensions'], signal_funnel, capital_state, attribution_data, closed_trades
        )

        self._save_report(report, date)
        logger.info(
            f"每日复盘报告 v2.0 生成完成：{len(closed_trades)} 笔交易, "
            f"{signal_funnel.get('total_decisions', 0)} 条决策, "
            f"评分: {report['review_score']['overall_score']:.1f}/10"
        )
        return report
    
    # =========================================================================
    #  Phase 3: 6维度系统性能分析
    # =========================================================================
    
    def _analyze_signal_quality(self, trades: List, funnel: Dict, attribution: Dict) -> Dict:
        """维度 ①: 信号质量分析"""
        if not trades:
            return {
                'score': 5.0,
                'total_signals': funnel.get('total_decisions', 0),
                'signal_to_trade_ratio': 0,
                'strength_distribution': {},
                'assessment': 'no_trades'
            }
        
        # 信号强度分布
        high = len([t for t in trades if (t.get('signal_strength') or 0) >= 70])
        medium = len([t for t in trades if 50 <= (t.get('signal_strength') or 0) < 70])
        low = len([t for t in trades if (t.get('signal_strength') or 0) < 50])
        
        # 从 attribution 提取信号评分
        attr_scores = attribution.get('signal_scores', [])
        avg_attr_signal = sum(attr_scores) / len(attr_scores) if attr_scores else None
        
        total_signals = funnel.get('total_decisions', 0)
        ratio = len(trades) / total_signals if total_signals > 0 else 0
        
        # 质量评估
        if high > len(trades) * 0.5:
            assessment = 'high_quality'
        elif low > len(trades) * 0.5:
            assessment = 'low_quality'
        else:
            assessment = 'mixed'
        
        return {
            'score': round(avg_attr_signal, 2) if avg_attr_signal is not None else None,
            'total_signals': total_signals,
            'total_trades': len(trades),
            'signal_to_trade_ratio': round(ratio, 4),
            'strength_distribution': {'high': high, 'medium': medium, 'low': low},
            'avg_signal_strength': round(sum(t.get('signal_strength') or 0 for t in trades) / len(trades), 2),
            'assessment': assessment,
        }
    
    def _analyze_voting_consistency(self, trades: List) -> Dict:
        """维度 ②: 投票一致性分析
        
        Phase 2 修复: 不再使用 or 50 fallback。数据为0时明确标记不可用。
        """
        if not trades:
            return {
                'score': None,
                'status': 'no_trades',
                'data_available': False
            }
        
        final_strengths = [t.get('final_strength') for t in trades]
        consensus_levels = [t.get('consensus_level') for t in trades]
        
        # 检查数据是否有效 (不能全为0或None)
        has_valid_strength = any(s and s > 0 for s in final_strengths)
        has_valid_consensus = any(c and c > 0 for c in consensus_levels)
        
        if not has_valid_strength and not has_valid_consensus:
            return {
                'score': None,
                'status': 'data_not_populated',
                'data_available': False,
                'note': 'final_strength 和 consensus_level 均为 0/NULL，投票数据未写入。'
                        '请检查 voting_coordinator 写入管线。',
                'raw_strengths': final_strengths,
                'raw_consensus': consensus_levels,
            }
        
        result = {
            'score': None,
            'status': 'partial' if (has_valid_strength != has_valid_consensus) else 'available',
            'data_available': True,
        }
        
        if has_valid_strength:
            valid_s = [s for s in final_strengths if s and s > 0]
            result['avg_signal_strength'] = round(sum(valid_s) / len(valid_s), 2)
            result['high_strength_trades'] = len([s for s in valid_s if s >= 70])
            result['medium_strength_trades'] = len([s for s in valid_s if 50 <= s < 70])
            result['low_strength_trades'] = len([s for s in valid_s if s < 50])
        
        if has_valid_consensus:
            valid_c = [c for c in consensus_levels if c and c > 0]
            result['avg_consensus_level'] = round(sum(valid_c) / len(valid_c), 4)
        
        return result
    
    def _analyze_execution_quality(self, trades: List, attribution: Dict) -> Dict:
        """维度 ③: 执行质量分析"""
        if not trades:
            return {'score': None, 'status': 'no_trades', 'data_available': False}
        
        attr_exec = attribution.get('execution_scores', [])
        attr_slippage = attribution.get('slippage_values', [])
        
        result = {
            'total_trades': len(trades),
            'data_available': bool(attr_exec) or bool(attr_slippage),
        }
        
        if attr_exec:
            result['avg_execution_score'] = round(sum(attr_exec) / len(attr_exec), 2)
        
        if attr_slippage:
            valid_slippage = [s for s in attr_slippage if s is not None]
            if valid_slippage:
                result['avg_slippage_bps'] = round(sum(valid_slippage) / len(valid_slippage), 2)
        
        # 从 trade_records 推断执行质量 (attribution 未填充时)
        executed = len([t for t in trades if t.get('exit_price')])
        if executed > 0:
            result['execution_rate'] = round(executed / len(trades), 4)
        
        if not result.get('data_available'):
            result['note'] = 'trade_attribution 执行数据未填充，执行质量分析受限。'
        
        return result
    
    def _analyze_risk_compliance(self, trades: List, capital: Dict) -> Dict:
        """维度 ④: 风控合规分析
        
        Phase 1 修复: 不再引用不存在的 stop_loss_planned/stop_loss_executed 列。
        改用 capital_state 的真实数据。
        """
        result = {
            'data_available': True,
        }
        
        # 从 capital_state 获取真实风控数据
        if capital and 'drawdown_pct' in capital:
            dd = capital['drawdown_pct']
            result['current_drawdown_pct'] = round(dd, 2)
            result['max_drawdown_pct'] = round(capital.get('max_drawdown_pct', 0), 2)
            result['water_level'] = capital.get('water_level', 'unknown')
            
            # 风控评分: 回撤越小越好
            if abs(dd) < 5:
                result['risk_score'] = 9.0
            elif abs(dd) < 10:
                result['risk_score'] = 7.0
            elif abs(dd) < 15:
                result['risk_score'] = 5.0
            elif abs(dd) < 20:
                result['risk_score'] = 3.0
            else:
                result['risk_score'] = 1.0
        else:
            result['risk_score'] = None
            result['note'] = 'capital_state 数据不可用'
        
        # 交易级风控
        if trades:
            max_single_loss = max(
                [abs(t.get('realized_pnl') or 0) for t in trades if (t.get('realized_pnl') or 0) < 0],
                default=0
            )
            result['max_single_trade_loss'] = round(max_single_loss, 2)
            
            losing_trades = [t for t in trades if (t.get('realized_pnl') or 0) < 0]
            result['losing_trade_count'] = len(losing_trades)
            result['losing_trade_ratio'] = round(len(losing_trades) / len(trades), 4) if trades else 0
        
        return result
    
    def _analyze_funnel_diagnosis(self, trades: List, funnel: Dict, rejections: Dict) -> Dict:
        """维度 ⑤: 漏斗诊断分析"""
        total_decisions = funnel.get('total_decisions', 0)
        total_trades = len(trades)
        total_rejections = rejections.get('total', 0)
        
        if total_decisions == 0:
            return {
                'score': None,
                'status': 'no_decisions',
                'total_decisions': 0,
                'total_trades': 0,
                'total_rejections': 0,
            }
        
        # 转化率
        trade_rate = total_trades / total_decisions if total_decisions > 0 else 0
        rejection_rate = total_rejections / total_decisions if total_decisions > 0 else 0
        
        # 健康度评估
        if trade_rate > 0.1 and rejection_rate < 0.8:
            health = 'healthy'
        elif trade_rate < 0.01 and total_decisions > 50:
            health = 'overly_restrictive'
        elif rejection_rate > 0.95:
            health = 'almost_all_rejected'
        else:
            health = 'normal'
        
        return {
            'score': None,  # 诊断维度，不直接评分
            'status': health,
            'total_decisions': total_decisions,
            'total_trades': total_trades,
            'total_rejections': total_rejections,
            'trade_conversion_rate': round(trade_rate, 4),
            'rejection_rate': round(rejection_rate, 4),
            'top_rejector': rejections.get('top_rejector', 'none'),
            'by_direction_strength': funnel.get('by_direction_strength', {}),
        }
    
    def _analyze_attribution_learning(self, attribution: Dict, trades: List) -> Dict:
        """维度 ⑥: 归因学习分析"""
        result = {
            'data_available': False,
            'lessons_count': 0,
        }

        # Phase 6: 五因子归因数据注入
        five_factor = attribution.get('five_factor', {})
        if five_factor.get('five_factor_available'):
            result['data_available'] = True
            result['five_factor'] = five_factor
            score = five_factor.get('behavioral_score', {})
            if score:
                result['behavioral_score'] = score
                result['lessons_count'] += 1

        # 从 attribution 提取学习信号
        success_factors = attribution.get('success_factors', [])
        failure_factors = attribution.get('failure_factors', [])

        if success_factors or failure_factors:
            result['data_available'] = True
            result['success_patterns'] = list(set(success_factors))[:10]
            result['failure_patterns'] = list(set(failure_factors))[:10]
            result['lessons_count'] += len(result['success_patterns']) + len(result['failure_patterns'])
        
        # 从 error_patterns 补充
        if trades:
            try:
                from app.review.error_pattern_detector import detect_error_patterns
                patterns = detect_error_patterns(days=1)
                today_ids = set(str(t.get('id')) for t in trades if t.get('id'))
                filtered = [
                    p for p in patterns
                    if any(tid in p.affected_trades for tid in today_ids)
                ]
                if filtered:
                    result['error_lessons'] = [
                        {'pattern': p.pattern_name, 'severity': p.severity, 'suggestion': p.suggestion}
                        for p in filtered[:5]
                    ]
                    result['lessons_count'] += len(filtered)
            except Exception as e:
                logger.debug(f"error_pattern 提取失败: {e}")
        
        return result
    
    # =========================================================================
    #  Phase 3: 综合评分 (0-10分制)
    # =========================================================================
    
    def _calculate_review_score(
        self, dimensions: Dict, funnel: Dict, capital: Dict, 
        attribution: Dict, trades: List
    ) -> Dict:
        """综合评分系统 (0-10分制)
        
        加权维度:
        - 胜率表现 (25%): 实际交易胜率
        - 信号质量 (20%): 信号强度分布
        - 风控合规 (20%): 回撤控制
        - 漏斗效率 (15%): 决策→交易转化率
        - 归因学习 (10%): 可提取的教训数量
        - 计划执行 (10%): 计划内交易比例
        """
        if not trades:
            return {
                'overall_score': 5.0,
                'max_score': 10.0,
                'status': 'no_trades',
                'breakdown': {},
            }
        
        breakdown = {}
        
        # 1. 胜率表现 (25%)
        winning = [t for t in trades if (t.get('realized_pnl') or 0) > 0]
        closed = [t for t in trades if t.get('exit_price') is not None]
        win_rate = len(winning) / len(closed) if closed else 0
        # 映射: 0%→0, 50%→5, 70%→8, 90%+→10
        wr_score = min(10, win_rate * 12)
        breakdown['win_rate_score'] = round(wr_score, 2)
        breakdown['win_rate'] = round(win_rate, 4)
        breakdown['win_rate_weight'] = 0.25
        
        # 2. 信号质量 (20%)
        if trades:
            avg_strength = sum(t.get('signal_strength') or 0 for t in trades) / len(trades)
            # 映射: 0→0, 50→5, 70→7, 90→9, 100→10
            sq_score = min(10, avg_strength / 10)
        else:
            sq_score = 5.0
        breakdown['signal_quality_score'] = round(sq_score, 2)
        breakdown['avg_signal_strength'] = round(avg_strength if trades else 0, 2)
        breakdown['signal_quality_weight'] = 0.20
        
        # 3. 风控合规 (20%)
        if capital and 'drawdown_pct' in capital:
            dd = abs(capital['drawdown_pct'])
            # 回撤 0%→10, 5%→8, 10%→6, 15%→4, 20%+→2
            rc_score = max(1, 10 - dd * 0.5)
        else:
            rc_score = 5.0
        breakdown['risk_compliance_score'] = round(rc_score, 2)
        breakdown['drawdown_pct'] = round(capital.get('drawdown_pct', 0), 2) if capital else 0
        breakdown['risk_compliance_weight'] = 0.20
        
        # 4. 漏斗效率 (15%)
        total_decisions = funnel.get('total_decisions', 0)
        if total_decisions > 0:
            conversion = len(trades) / total_decisions
            # 映射: 0→2, 5%→5, 10%→7, 20%+→10
            fe_score = min(10, max(2, conversion * 50))
        else:
            fe_score = 5.0
        breakdown['funnel_efficiency_score'] = round(fe_score, 2)
        breakdown['conversion_rate'] = round(len(trades) / total_decisions, 4) if total_decisions > 0 else 0
        breakdown['funnel_efficiency_weight'] = 0.15
        
        # 5. 归因学习 (10%)
        lessons_count = len(attribution.get('success_factors', [])) + len(attribution.get('failure_factors', []))
        al_score = min(10, lessons_count * 2)
        breakdown['attribution_learning_score'] = round(al_score, 2)
        breakdown['lessons_count'] = lessons_count
        breakdown['attribution_learning_weight'] = 0.10
        
        # 6. 计划执行 (10%)
        # 简化: 如果有 plan_vs_actual 数据
        planned = attribution.get('planned_trades', 0)
        executed_planned = attribution.get('executed_planned', 0)
        if planned > 0:
            pe_score = (executed_planned / planned) * 10
        else:
            pe_score = 5.0  # 无计划时中性
        breakdown['plan_execution_score'] = round(pe_score, 2)
        breakdown['plan_execution_weight'] = 0.10
        
        # 加权总分
        overall = (
            wr_score * 0.25 +
            sq_score * 0.20 +
            rc_score * 0.20 +
            fe_score * 0.15 +
            al_score * 0.10 +
            pe_score * 0.10
        )
        
        # 状态标签
        if overall >= 8:
            status = 'excellent'
        elif overall >= 6:
            status = 'good'
        elif overall >= 4:
            status = 'needs_improvement'
        else:
            status = 'critical'
        
        return {
            'overall_score': round(overall, 1),
            'max_score': 10.0,
            'status': status,
            'breakdown': breakdown,
        }
    
    def _detect_market_regime(self, capital: Dict, attribution: Dict) -> Dict:
        """市场状态感知
        
        从 trade_attribution 或 capital_state 推断市场状态。
        如果 trade_records.market_regime 有值则直接使用。
        """
        regime = {
            'state': 'unknown',
            'confidence': 'low',
            'indicators': {},
        }
        
        # 优先从 attribution 的场景标签获取
        scene_tags = attribution.get('scene_tags', [])
        if scene_tags:
            from collections import Counter
            most_common = Counter(scene_tags).most_common(1)
            if most_common:
                regime['state'] = most_common[0][0]
                regime['confidence'] = 'medium'
        
        # 从资本状态推断
        if capital and 'drawdown_pct' in capital:
            dd = abs(capital['drawdown_pct'])
            regime['indicators']['drawdown_pct'] = round(dd, 2)
            if dd > 15:
                regime['state'] = regime.get('state') or 'HIGH_VOLATILITY'
                regime['indicators']['inferred'] = 'high_drawdown'
        
        return regime
    
    # =========================================================================
    #  Phase 4: trade_attribution 分析
    # =========================================================================
    
    def _analyze_attribution(self, date: str) -> Dict:
        """分析 trade_attribution 表 (47列)
        
        Phase 4 新增: 从 trade_attribution 提取高价值归因数据。
        Phase 6 (2026-05-03): 新增五因子归因分解融合
        """
        try:
            rows = self._query(
                "SELECT * FROM trade_attribution WHERE trade_date = ?",
                (date,)
            )
            
            if not rows:
                result = {
                    'total_records': 0,
                    'data_available': False,
                    'note': 'trade_attribution 无当日数据',
                }
            else:
                result = {
                    'total_records': len(rows),
                    'data_available': True,
                    'signal_scores': [],
                    'execution_scores': [],
                    'risk_scores': [],
                    'timing_scores': [],
                    'overall_scores': [],
                    'slippage_values': [],
                    'success_factors': [],
                    'failure_factors': [],
                    'scene_tags': [],
                }
                
                for r in rows:
                    if r.get('signal_score', 0):
                        result['signal_scores'].append(r['signal_score'])
                    if r.get('execution_score', 0):
                        result['execution_scores'].append(r['execution_score'])
                    if r.get('risk_score', 0):
                        result['risk_scores'].append(r['risk_score'])
                    if r.get('timing_score', 0):
                        result['timing_scores'].append(r['timing_score'])
                    if r.get('overall_score', 0):
                        result['overall_scores'].append(r['overall_score'])
                    if r.get('slippage_bps') is not None:
                        result['slippage_values'].append(r['slippage_bps'])
                    if r.get('success_factors'):
                        try:
                            factors = json.loads(r['success_factors']) if isinstance(r['success_factors'], str) else r['success_factors']
                            if isinstance(factors, list):
                                result['success_factors'].extend(factors)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if r.get('failure_factors'):
                        try:
                            factors = json.loads(r['failure_factors']) if isinstance(r['failure_factors'], str) else r['failure_factors']
                            if isinstance(factors, list):
                                result['failure_factors'].extend(factors)
                        except (json.JSONDecodeError, TypeError):
                            pass

            # ── Phase 6: 五因子归因融合 ──
            try:
                five_factor = self._get_five_factor_attribution(date)
            except Exception as ff_err:
                logger.debug(f"[五因子归因] 调用异常: {ff_err}")
                five_factor = {'five_factor_available': False, 'error': str(ff_err)}
            result['five_factor'] = five_factor

            # 统计摘要 (安全访问)
            for key in ['signal_scores', 'execution_scores', 'risk_scores', 'timing_scores', 'overall_scores']:
                vals = result.get(key, [])
                if vals:
                    result[f'{key}_avg'] = round(sum(vals) / len(vals), 2)
                    result[f'{key}_max'] = max(vals)
                    result[f'{key}_min'] = min(vals)
                else:
                    result[f'{key}_avg'] = None

            return result

        except Exception as e:
            logger.warning(f"[attribution] 分析失败: {e}")
            return {
                'total_records': 0,
                'data_available': False,
                'error': str(e),
                'signal_scores': [],
                'execution_scores': [],
                'risk_scores': [],
                'timing_scores': [],
                'overall_scores': [],
                'slippage_values': [],
                'success_factors': [],
                'failure_factors': [],
                'scene_tags': [],
            }

    def _get_five_factor_attribution(self, date: str) -> Dict:
        """
        Phase 6 新增: 调用五因子归因分解 (降级安全)

        从 trade_records 读取最近 30 天数据, 执行五因子归因
        """
        try:
            from app.diagnostics.shadow_account import create_shadow_diagnostic
            diag = create_shadow_diagnostic()

            # P0 修复 (2026-05-31): 用 Python ET 时间替代 SQLite datetime('now')
            from datetime import timedelta as _td
            from app.utils.timezone import now_et
            cutoff_30d = (now_et() - _td(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

            # 先检查是否有数据
            trades = self._query(
                "SELECT * FROM trade_records WHERE exit_time IS NOT NULL AND exit_time > ? ORDER BY exit_time DESC",
                (cutoff_30d,)
            )
            if len(trades) < 3:
                return {'five_factor_available': False, 'reason': f'样本不足 (n={len(trades)})'}

            # 降级到启发式规则 (速度优先)
            result = diag.execute(
                db_path=str(self.db_path),
                lookback_days=30,
                use_kmeans=False,
                use_attribution=True,
                has_trades=True,  # 跳过 is_applicable 检查
            )
            if result.status == "success":
                attr = result.data.get("attribution", {})
                score = result.data.get("behavioral_score", {})
                rules = result.data.get("rules", [])
                return {
                    'five_factor_available': True,
                    'noise_trades_pnl': attr.get("noise_trades_pnl", 0),
                    'early_exit_pnl': attr.get("early_exit_pnl", 0),
                    'late_exit_pnl': attr.get("late_exit_pnl", 0),
                    'overtrading_pnl': attr.get("overtrading_pnl", 0),
                    'missed_signals_pnl': attr.get("missed_signals_pnl", 0),
                    'behavioral_score': score,
                    'rules': rules,
                    'disposition_effect': result.data.get("disposition_effect", {}),
                    'chasing': result.data.get("chasing", {}),
                    'anchoring': result.data.get("anchoring", {}),
                }
            else:
                return {'five_factor_available': False, 'reason': result.summary}
        except Exception as e:
            logger.debug(f"[五因子归因] 调用失败 (不影响主流程): {e}")

        return {'five_factor_available': False}
    
    def _get_plan_vs_actual(self, date: str) -> Dict:
        """获取计划对比数据
        
        Phase 4 新增: 从 battle_plan_manager 获取当日作战计划，
        对比实际执行。
        """
        result = {
            'plan_available': False,
            'planned_targets': [],
            'executed_targets': [],
            'missed_opportunities': [],
            'unplanned_trades': [],
        }
        
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            mgr = USBattlePlanManager()
            plan = mgr.get_plan() or {}
            
            if not plan:
                return result
            
            result['plan_available'] = True
            result['plan_date'] = plan.get('date', 'unknown')
            result['planned_targets'] = plan.get('targets', []) or plan.get('watchlist', [])
            
            # 对比实际交易
            trades = self._query(
                "SELECT symbol, entry_reason, realized_pnl FROM trade_records WHERE trade_date = ?",
                (date,)
            )
            actual_symbols = set(t.get('symbol', '') for t in trades)
            planned_symbols = set()
            for t in result.get('planned_targets', []):
                if isinstance(t, dict):
                    planned_symbols.add(t.get('symbol', t.get('code', '')))
                elif isinstance(t, str):
                    planned_symbols.add(t)
            
            # 已执行的计划内交易
            result['executed_targets'] = list(actual_symbols & planned_symbols)
            # 错过的计划内交易
            result['missed_opportunities'] = list(planned_symbols - actual_symbols)
            # 计划外交易
            result['unplanned_trades'] = list(actual_symbols - planned_symbols)
            
            # 执行率
            if planned_symbols:
                result['execution_rate'] = round(len(result['executed_targets']) / len(planned_symbols), 4)
            
        except Exception as e:
            logger.debug(f"[plan_vs_actual] 获取失败: {e}")
        
        return result
    
    # =========================================================================
    #  基础统计
    # =========================================================================
    
    def _generate_summary(self, all_trades: List, closed_trades: List, capital: Dict = None) -> Dict:
        """生成基础统计摘要"""
        if not closed_trades:
            base = {
                'total_trades': len(all_trades),
                'closed_trades': 0,
                'total_pnl': 0,
                'win_rate': 0,
                'avg_pnl': 0,
            }
            if capital:
                base['equity_snapshot'] = {
                    'current_equity': capital.get('current_equity'),
                    'drawdown_pct': capital.get('drawdown_pct'),
                }
            return base
        
        total_pnl = sum(t.get('realized_pnl', 0) or 0 for t in closed_trades)
        winning = [t for t in closed_trades if (t.get('realized_pnl', 0) or 0) > 0]
        
        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(closed_trades) - len(winning),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4),
            'avg_pnl': round(total_pnl / len(closed_trades), 2),
            'avg_holding_period': round(sum(t.get('holding_hours') or 0 for t in closed_trades) / len(closed_trades), 1) if closed_trades else 0,
        }
    
    # =========================================================================
    #  旧版方法 (向后兼容)
    # =========================================================================
    
    def _get_signal_funnel(self, date: str) -> Dict:
        """信号漏斗分析 (从 decisions.db)"""
        try:
            if not self.decisions_db.exists():
                return {'total_decisions': 0, 'error': 'decisions.db not found'}
            conn = sqlite3.connect(str(self.decisions_db))
            total = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE date(created_at) = ?", (date,)
            ).fetchone()[0]
            
            by_direction = {}
            rows = conn.execute(
                "SELECT direction, final_strength, COUNT(*) as cnt FROM decisions "
                "WHERE date(created_at) = ? GROUP BY direction, final_strength",
                (date,)
            ).fetchall()
            for r in rows:
                key = f"{r[0]}(s={r[1]})"
                by_direction[key] = r[2]
            conn.close()
            
            return {
                'total_decisions': total,
                'by_direction_strength': by_direction,
            }
        except Exception as e:
            return {'total_decisions': 0, 'error': str(e)}
    
    def _get_rejection_analysis(self, date: str) -> Dict:
        """拒绝分析 (从 rejections.db)"""
        try:
            if not self.rejections_db.exists():
                return {'total': 0, 'note': 'rejection_logger 未初始化'}
            conn = sqlite3.connect(str(self.rejections_db))
            total = conn.execute(
                "SELECT COUNT(*) FROM rejections WHERE date(timestamp) = ?", (date,)
            ).fetchone()[0]
            
            by_reason = {}
            rows = conn.execute(
                "SELECT reason_category, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) = ? GROUP BY reason_category ORDER BY cnt DESC",
                (date,)
            ).fetchall()
            for r in rows:
                by_reason[r[0]] = r[1]
            
            by_symbol = {}
            rows = conn.execute(
                "SELECT symbol, COUNT(*) as cnt FROM rejections "
                "WHERE date(timestamp) = ? GROUP BY symbol ORDER BY cnt DESC LIMIT 5",
                (date,)
            ).fetchall()
            for r in rows:
                by_symbol[r[0]] = r[1]
            conn.close()
            
            return {
                'total': total,
                'by_reason': by_reason,
                'by_symbol': by_symbol,
                'top_rejector': max(by_reason, key=by_reason.get) if by_reason else 'none'
            }
        except Exception as e:
            return {'total': 0, 'error': str(e)}
    
    def _get_capital_state(self, date: str) -> Dict:
        """资金状态 (从 ftseek_us.db capital_state)"""
        try:
            rows = self._query(
                "SELECT initial_capital, current_equity, peak_equity, cash, "
                "buying_power, drawdown_pct, max_drawdown_pct, water_level, last_update "
                "FROM capital_state WHERE date(last_update) = ? ORDER BY last_update DESC LIMIT 1",
                (date,)
            )
            if rows:
                r = rows[0]
                return {
                    'current_equity': round(r.get('current_equity', 0), 0),
                    'cash': round(r.get('cash', 0), 0),
                    'buying_power': round(r.get('buying_power', 0), 0),
                    'drawdown_pct': round(r.get('drawdown_pct', 0) * 100, 2),
                    'max_drawdown_pct': round(r.get('max_drawdown_pct', 0) * 100, 2),
                    'water_level': r.get('water_level', 'unknown'),
                }
            return {'note': 'No capital state for this date'}
        except Exception as e:
            return {'error': str(e)}
    
    def _analyze_field(self, trades: List, reason_field: str, confidence_field: str) -> Dict:
        """决策字段分析 (兼容旧版)"""
        if not trades:
            return {}
        
        counts = {}
        for t in trades:
            reason = t.get(reason_field, 'unknown') or 'unknown'
            counts[reason] = counts.get(reason, 0) + 1
        
        # 检查是否有真实数据
        valid_confidences = [t.get(confidence_field) for t in trades if t.get(confidence_field)]
        if valid_confidences:
            avg_conf = sum(valid_confidences) / len(valid_confidences)
        else:
            avg_conf = None
        
        result = {
            'distribution': counts,
            'avg_confidence': round(avg_conf, 2) if avg_conf is not None else None,
        }
        if avg_conf is None:
            result['note'] = f'{confidence_field} 数据未填充，所有值为 NULL/0'
        
        return result
    
    def _detect_errors(self, trades: List) -> Dict:
        """检测错误模式"""
        if not trades:
            return {}
        
        try:
            from app.review.error_pattern_detector import detect_error_patterns
            patterns = detect_error_patterns(days=1)
            
            today_ids = [str(t.get('id')) for t in trades if t.get('id')]
            filtered = []
            for p in patterns:
                affected = p.affected_trades if hasattr(p, 'affected_trades') else []
                if any(tid in affected for tid in today_ids):
                    filtered.append({
                        'pattern': p.pattern_name,
                        'severity': p.severity,
                        'count': p.count,
                        'suggestion': p.suggestion,
                    })
            
            return {
                'detected': len(filtered) > 0,
                'patterns': filtered,
            }
        except Exception as e:
            logger.debug(f"错误检测失败：{e}")
            return {}
    
    def _extract_learnings_v2(self, attribution: Dict, trades: List) -> List[str]:
        """提取关键学习 (v2.0 修复: 不再读幽灵列)
        
        Phase 1 修复: 旧版读 lessons_learned/review_notes (不存在)。
        新版从 error_patterns 和 trade_attribution 提取。
        """
        learnings = set()
        
        # 从 attribution 提取
        for factor in attribution.get('success_factors', []):
            learnings.add(f"成功模式: {factor}")
        for factor in attribution.get('failure_factors', []):
            learnings.add(f"失败模式: {factor}")
        
        # 从 error_patterns 提取
        if trades:
            try:
                from app.review.error_pattern_detector import detect_error_patterns
                patterns = detect_error_patterns(days=1)
                today_ids = set(str(t.get('id')) for t in trades if t.get('id'))
                for p in patterns:
                    affected = p.affected_trades if hasattr(p, 'affected_trades') else []
                    if any(tid in affected for tid in today_ids):
                        learnings.add(f"错误模式: {p.pattern_name} - {p.suggestion}")
            except Exception:
                pass
        
        return list(learnings)[:10]
    
    def _generate_suggestions_v2(
        self, trades: List, funnel: Dict, rejections: Dict, 
        capital: Dict, attribution: Dict
    ) -> List[str]:
        """生成改进建议 (v2.0)"""
        suggestions = []
        
        # 有交易时的建议
        if trades:
            summary = self._generate_summary(trades, trades)
            if summary.get('win_rate', 0) < 0.4:
                suggestions.append("胜率低于 40%，建议重新评估策略")
            
            voting = self._analyze_voting_consistency(trades)
            if voting.get('data_available') and voting.get('low_strength_trades', 0) > 0:
                suggestions.append(f"有 {voting['low_strength_trades']} 笔低信号强度交易，建议提高入场门槛")
            
            # 亏损分析
            losing = [t for t in trades if (t.get('realized_pnl') or 0) < 0]
            if len(losing) > len(trades) * 0.6:
                suggestions.append(f"亏损交易占比 {len(losing)/len(trades):.0%}，建议收紧入场条件")
        
        # 信号漏斗建议
        total_decisions = funnel.get('total_decisions', 0)
        if total_decisions > 0 and not trades:
            suggestions.append(f"产生 {total_decisions} 条决策但 0 笔执行，检查门禁/风控是否过严")
        elif total_decisions > 100 and len(trades) < 2:
            suggestions.append(f"决策/交易比异常高 ({total_decisions}:{len(trades)})，信号质量可能偏低")
        
        # 拒绝分析建议
        if rejections.get('total', 0) > 0:
            total_rej = rejections['total']
            top_reason = rejections.get('top_rejector', 'unknown')
            suggestions.append(f"今日 {total_rej} 次信号被拒绝，TOP 原因: {top_reason}")
        
        # 资金状态建议
        if capital and 'drawdown_pct' in capital:
            dd = abs(capital.get('drawdown_pct', 0))
            if dd > 15:
                suggestions.append(f"当前回撤 {dd:.1f}%，建议降低仓位或暂停交易")
            water_level = capital.get('water_level', '')
            if water_level in ('WARNING', 'CRITICAL'):
                suggestions.append(f"资金水位: {water_level}，触发风控警报")
        
        # attribution 数据质量
        if attribution.get('total_records', 0) > 0 and not attribution.get('data_available'):
            suggestions.append("trade_attribution 记录存在但评分数据全空，请修复归因写入管线")
        
        if not suggestions:
            if not trades:
                suggestions.append("今日无交易，系统运行正常，等待交易信号")
            else:
                suggestions.append("今日交易执行正常，继续保持")
        
        return suggestions
    
    def _analyze_per_trade(self, trades: List) -> List[Dict]:
        """Phase 5: 逐笔交易深度分析"""
        try:
            from app.review.trade_analyzer import TradeAnalyzer
            analyzer = TradeAnalyzer()
            
            results = []
            for t in trades:
                if t.get('exit_price') is None:
                    continue
                analysis = analyzer._analyze_single_trade(dict(t), {})
                results.append(analysis.to_dict())
            
            return results
        except Exception as e:
            logger.warning(f"[Phase5] 逐笔交易分析失败: {e}")
            return []
    
    def _analyze_shadow_vs_real(self, date: str) -> Dict:
        """Z4: shadow vs real 1:1 对照"""
        try:
            import sqlite3 as _s
            from app.core.paths import get_trade_records_db
            conn = _s.connect(str(get_trade_records_db()))
            real_today = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(realized_pnl),0), "
                "COALESCE(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END),0) "
                "FROM trade_records WHERE date(exit_time)=? AND realized_pnl IS NOT NULL",
                (date,)
            ).fetchone()
            shadow_rows = conn.execute(
                "SELECT strategy, COUNT(*), COALESCE(SUM(realized_pnl),0), "
                "COALESCE(AVG(CASE WHEN realized_pnl>0 THEN 1.0 ELSE 0.0 END),0) "
                "FROM shadow_trades WHERE date(exit_time)=? AND closed=1 GROUP BY strategy",
                (date,)
            ).fetchall()
            conn.close()
            return {
                "real": {
                    "n": real_today[0], "total_pnl": real_today[1],
                    "win_rate": real_today[2],
                },
                "shadow_by_variant": [
                    {"variant": r[0], "n": r[1], "total_pnl": r[2], "win_rate": r[3]}
                    for r in shadow_rows
                ],
            }
        except Exception as e:
            logger.debug(f"[shadow_vs_real] 失败: {e}")
            return {"error": str(e)}

    def _analyze_pnl_outliers(self, date: str) -> Dict:
        """Z4: 当日 PnL 异常值"""
        try:
            import sqlite3 as _s
            from app.core.paths import get_trade_records_db
            conn = _s.connect(str(get_trade_records_db()))
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pnl_outliers'"
            ).fetchone()
            if not cur:
                conn.close()
                return {"note": "pnl_outliers 表尚未创建"}
            rows = conn.execute(
                "SELECT symbol, realized_pnl, z_score, rolling_mean, rolling_std "
                "FROM pnl_outliers WHERE date(detected_at,'unixepoch','localtime')=? "
                "ORDER BY ABS(z_score) DESC LIMIT 10",
                (date,)
            ).fetchall()
            conn.close()
            return {
                "n": len(rows),
                "outliers": [
                    {"symbol": r[0], "pnl": r[1], "z_score": r[2],
                     "rolling_mean": r[3], "rolling_std": r[4]}
                    for r in rows
                ],
            }
        except Exception as e:
            return {"error": str(e)}

    def _analyze_loss_guard(self, date: str) -> Dict:
        """Z4: SymbolDailyLossGuard 当日触发"""
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            snap = get_symbol_daily_loss_guard().snapshot()
            blocked = [s for s in snap if s.get("blocked")]
            return {
                "total_symbols_tracked": len(snap),
                "blocked_count": len(blocked),
                "blocked": blocked[:10],
            }
        except Exception as e:
            return {"error": str(e)}

    def _analyze_coordinator(self, date: str) -> Dict:
        """
        Phase A (2026-05-14): coordinator 当日状态汇总.

        - owner_strategy 分布 (today trade_records)
        - signal_arbitration 数量 (今日)
        - 多候选仲裁占比 (n_candidates > 1)
        - winner_strategy 分布
        """
        try:
            import sqlite3 as _s
            from app.core.paths import get_trade_records_db, get_decisions_db
            out: Dict[str, Any] = {"window_date": date}

            # 1) owner_strategy 分布
            tr_conn = _s.connect(str(get_trade_records_db()))
            owner_dist = {}
            for r in tr_conn.execute(
                "SELECT COALESCE(owner_strategy, '<NULL>'), COUNT(*) "
                "FROM trade_records WHERE date(exit_time)=? GROUP BY 1", (date,)
            ).fetchall():
                owner_dist[r[0]] = r[1]
            out["owner_strategy_distribution"] = owner_dist

            # 2) 当前活跃持仓 owner 分布
            active = {}
            for r in tr_conn.execute(
                "SELECT COALESCE(owner_strategy, '<NULL>'), COUNT(*) "
                "FROM open_positions WHERE is_filled=1 AND COALESCE(is_anomalous,0)=0 GROUP BY 1"
            ).fetchall():
                active[r[0]] = r[1]
            out["active_positions_by_owner"] = active
            tr_conn.close()

            # 3) signal_arbitration 统计
            arb_conn = _s.connect(str(get_decisions_db()))
            cur = arb_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_arbitration'"
            ).fetchone()
            if cur:
                total = arb_conn.execute(
                    "SELECT COUNT(*) FROM signal_arbitration WHERE date(timestamp,'unixepoch','localtime')=?",
                    (date,)
                ).fetchone()[0]
                multi = arb_conn.execute(
                    "SELECT COUNT(*) FROM signal_arbitration "
                    "WHERE date(timestamp,'unixepoch','localtime')=? AND n_candidates > 1",
                    (date,)
                ).fetchone()[0]
                winners = {}
                for r in arb_conn.execute(
                    "SELECT winner_strategy, COUNT(*) FROM signal_arbitration "
                    "WHERE date(timestamp,'unixepoch','localtime')=? GROUP BY winner_strategy",
                    (date,)
                ).fetchall():
                    winners[r[0]] = r[1]
                out["arbitration"] = {
                    "total": total,
                    "multi_candidate": multi,
                    "winners": winners,
                }
            else:
                out["arbitration"] = {"note": "signal_arbitration 表尚未创建"}
            arb_conn.close()

            return out
        except Exception as e:
            logger.warning(f"[Coordinator·summary] 分析失败: {e}")
            return {"error": str(e)}

    def _analyze_exit_reason_pnl(self, window_days: int = 30) -> Dict:
        """
        F22 (2026-05-14): exit_reason × pnl 分布
        复用 scripts/exit_reason_pnl_report.py 的核心聚合
        """
        try:
            import sys as _sys
            from pathlib import Path as _P
            _proj = _P(__file__).resolve().parent.parent.parent
            if str(_proj) not in _sys.path:
                _sys.path.insert(0, str(_proj))
            from scripts.exit_reason_pnl_report import _load_trades, _aggregate, _recommendation
            from app.core.paths import get_trade_records_db
            trades = _load_trades(str(get_trade_records_db()), window_days)
            stats = _aggregate(trades)
            # 浓缩: 只输出 n>=3 的项 + recommendation, 减小 JSON 体积
            return {
                'window_days': window_days,
                'total_reasons': len(stats),
                'by_reason': {
                    r: {
                        'n': s['n'],
                        'win_rate': round(s['win_rate'], 3),
                        'avg_pnl_pct': round(s['avg_pnl_pct'], 5),
                        'sharpe_proxy': round(s['sharpe_proxy'], 3),
                        'recommendation': _recommendation(s),
                    }
                    for r, s in stats.items() if s['n'] >= 3
                },
            }
        except Exception as e:
            logger.warning(f"[F22] exit_reason×pnl 分析失败: {e}")
            return {'error': str(e)}

    def _save_report(self, report: Dict, date: str):
        """保存报告"""
        filename = self.output_dir / f"daily_review_{date}.json"
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        # Phase 1: Obsidian 双写 — 美股盘后复盘
        try:
            from app.core.obsidian_writer import get_obsidian_writer
            from app.core.paths import get_obsidian_vault

            writer = get_obsidian_writer(vault_path=str(get_obsidian_vault()))
            summary = report.get('summary', {})
            trades = report.get('trades', [])
            
            writer.write_daily_postmarket({
                'date': date,
                'market': 'US',
                'total_trades': summary.get('total_trades', 0),
                'win_rate': summary.get('win_rate', 0),
                'total_pnl': summary.get('total_pnl', 0),
                'max_drawdown': summary.get('max_drawdown', 0),
                'trades': [{'code': f"US.{t.get('symbol', '')}", 'pnl': t.get('realized_pnl', 0), 'id': str(i)}
                           for i, t in enumerate(trades)],
            })
            logger.info(f"[Obsidian] 美股盘后复盘已写入")
        except Exception as _e:
            logger.debug(f"[Obsidian] 美股复盘写入失败: {_e}")
    
    def get_report(self, date: str) -> Optional[Dict]:
        """获取已生成的报告"""
        filename = self.output_dir / f"daily_review_{date}.json"
        if not filename.exists():
            return None
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)


def generate_daily_review(date: str = None, account_type: str = None) -> Dict:
    generator = DailyReviewGenerator()
    return generator.generate(date, account_type)
