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
from app.review.source_attribution import aggregate_source_direction, playbook_source_sql
from app.core.trade_identity import summarize_trade_identity
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
    
    def generate(self, date: str = None, account_type: str = None,
                 final_refresh: bool = False) -> Dict[str, Any]:
        """生成每日复盘报告 (v2.0)"""
        if date is None:
            try:
                from app.utils.timezone import now_et as _now_et
                date = _now_et().strftime('%Y-%m-%d')
            except Exception:
                date = datetime.now().strftime('%Y-%m-%d')
        
        logger.info(f"生成每日复盘报告 v2.0：{date}")
        
        # 从 ftseek_us.db 读取交易记录
        # 2026-08-01: 黑名单改 D3 正向白名单 (DEB-20260730) — 黑名单会被未来新引擎静默穿透
        # (跨引擎拼接事故第四课); 今日两者等价, 结构性防呆。
        trades = self._query(
            # DEB-20260904 #208: ① 补出场日匹配 (同 #131 collect_daily 修复) —
            #   原仅按 trade_date(开仓日) 取数, 昨开今平的隔夜仓不进当日复盘;
            # ② `exit_reason != '...'` 在 SQLite 下 NULL(未平仓)行被一并过滤
            #   (NULL != 常量 = NULL) → open_trades 恒空/过夜持仓段永不渲染,
            #   改为 NULL 保留式排除幽灵对账行。
            "SELECT * FROM trade_records WHERE (trade_date = ? OR substr(exit_time, 1, 10) = ?) "
            "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
            "AND (exit_reason IS NULL OR exit_reason != 'PHANTOM_RECONCILE_VOID') "
            # DEB-20260903 #127: 删除 trade_mode != 'live' 硬排除 —
            # 实盘模式下 playbook_live 交易 trade_mode='live' 全被过滤 → 复盘报告为空
            "ORDER BY created_at",
            (date, date)
        )
        closed_trades = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]

        # 2026-11-03 复查修复 (11-02 实证): open_trades 口径从"当日进场未平"扩到
        #   "全部未平仓" — 原 trades 查询按 trade_date/exit_date=当日过滤, 跨日
        #   遗留仓 (如 MRK/LMT 10-30 开仓持有到第 3 个交易日) 在日报"过夜持仓"段
        #   完全隐形 (当日 postmarket 只列了 ACN)。此处单独查全量未平仓再按 id
        #   去重合并; 不进 trades/closed_trades ⇒ 当日胜率/笔数/PnL 口径不变。
        _open_same_day = [t for t in trades
                          if not (t.get('exit_price') and t.get('exit_price') > 0)]
        _open_all = self._query(
            "SELECT * FROM trade_records "
            "WHERE (exit_price IS NULL OR exit_price <= 0) "
            "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
            "AND (exit_reason IS NULL OR exit_reason != 'PHANTOM_RECONCILE_VOID') "
            "ORDER BY trade_date, id"
        )
        _seen_open_ids = {t.get('id') for t in _open_same_day}
        open_trades = _open_same_day + [
            t for t in _open_all if t.get('id') not in _seen_open_ids
        ]

        # ── 数据源采集 ──
        # ── 数据源采集 ──
        signal_funnel = self._get_signal_funnel(date)
        rejection_analysis = self._get_rejection_analysis(date)
        capital_state = self._get_capital_state(date)
        attribution_data = self._analyze_attribution(date)       # Phase 4: 新增
        plan_vs_actual = self._get_plan_vs_actual(date)          # Phase 4: 新增
        overnight_risk = self._analyze_overnight_risk(date, trades)
        
        # ── Phase 5: 逐笔交易深度分析 (新增 2026-05-02) ──
        trade_analyses = self._analyze_per_trade(trades)
        
        # ── 6维度分析 ──
        report = {
            'report_type': 'daily',
            'report_version': '2.0',
            'report_date': date,
            'generated_at': datetime.now().isoformat(),
            'summary': self._generate_summary(trades, closed_trades, capital_state),
            # 2026-08-13: report 此前无 'trades' 键 → _save_report 取空列表 →
            # Obsidian postmarket 笔记正文恒为"无交易"。补上已平仓交易行
            # (_save_report 内会做 symbol→code / realized_pnl→pnl 映射)。
            'trades': closed_trades,
            'trade_identity': summarize_trade_identity(closed_trades),
            # 2026-08-27 收盘审计④: 当日进场但未平仓 (持夜) 的交易 — 供 postmarket
            #   笔记渲染"过夜持仓"段, 消除 total_trades(按进场计) 与明细(按已平计) 口径差。
            #   2026-11-03: 改为上方合并后的全量未平仓 (含跨日遗留仓)。
            'open_trades': open_trades,
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
            'overnight_risk': overnight_risk,
            
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

        self._save_report(report, date, overwrite_postmarket=final_refresh)
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
        2026-09-04 (M4): 3 模型投票已退役 — voting_predictions 停写于 2026-07-12,
          playbook 路径的 voting_data 查询 (playbook_service L1654) 恒 miss →
          final_strength/consensus_level 为化石列恒 0。原 note「请检查 voting_coordinator
          写入管线」是误报 (管线未坏, 上游退役)。信号强度/置信度已由 signal_quality
          维度覆盖 (signal_strength=confidence 镜像, 2026-07-16 P2)。
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
                'status': 'retired',
                'data_available': False,
                'note': '3模型投票已退役 (voting_predictions 停写于 2026-07-12), '
                        'final_strength/consensus_level 为化石列恒 0。'
                        '信号强度/置信度由 signal_quality 维度覆盖 '
                        '(signal_strength=confidence 镜像)。',
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
        # DEB-20260903 #128: 胜率改净口径 COALESCE(pnl_net, realized_pnl) —
        # 原毛口径与 :822 "pnl_net 为唯一对外口径" 矛盾, 毛赢净亏交易被误计为胜场
        winning = [t for t in trades
                   if ((t.get('pnl_net') if t.get('pnl_net') is not None else t.get('realized_pnl')) or 0) > 0]
        closed = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]
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
        """归因数据采集 (47列 → 双源)
        
        Phase 4 新增: 从 trade_attribution 提取高价值归因数据。
        2026-09-04 (M4, 根因修复): trade_attribution 表停写于 2026-06-26 —
          平仓路径已改为把 compute_attribution 结果写入 trade_records.metadata_json.attribution
          (pipeline L6415, 每笔必写), 复盘却还在读死表 → 恒「无当日数据」。
          修复: 主源改读当日平仓单 metadata_json.attribution; 旧表作为历史日期
          (≤2026-06-26) 补充。注意量纲: 新源 dimension score ∈ [-1,1], 与旧表
          signal_score 等不同量纲, 故不硬填旧键 (防污染), 新增 dimension_scores 等新键。
        """
        result = {
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
        sources = []
        try:
            # ── 主源: 当日平仓单 metadata_json.attribution (2026-06-26 起每笔必写) ──
            rows = self._query(
                "SELECT metadata_json FROM trade_records WHERE trade_date = ? "
                "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
                "AND exit_price IS NOT NULL AND exit_price > 0",
                (date,)
            )
            dim_scores: Dict[str, List[float]] = {}
            n_attr = 0
            for r in rows:
                try:
                    md = json.loads(r.get('metadata_json') or '{}') if isinstance(r.get('metadata_json'), str) \
                        else (r.get('metadata_json') or {})
                except (json.JSONDecodeError, TypeError):
                    continue
                attr = md.get('attribution') if isinstance(md, dict) else None
                if not isinstance(attr, dict):
                    continue
                n_attr += 1
                if attr.get('overall_score') is not None:
                    result['overall_scores'].append(attr['overall_score'])
                for d in (attr.get('dimensions') or []):
                    if not isinstance(d, dict):
                        continue
                    name, score, verdict, label = d.get('name'), d.get('score'), d.get('verdict'), d.get('label')
                    if score is not None:
                        dim_scores.setdefault(name, []).append(float(score))
                    if verdict == '✅' and label:
                        result['success_factors'].append(label)
                    elif verdict in ('⚠️', '❌') and label:
                        result['failure_factors'].append(label)
            if n_attr:
                sources.append('trade_records.metadata')
                result['attribution_source'] = 'trade_records.metadata'
                result['total_records'] = n_attr
                result['dimension_scores'] = {
                    k: round(sum(v) / len(v), 3) for k, v in sorted(dim_scores.items())
                }
                result['dimension_counts'] = {k: len(v) for k, v in sorted(dim_scores.items())}
                # 方向正确率 (direction 维 score>0 即方向对)
                _dir = dim_scores.get('direction') or []
                if _dir:
                    result['direction_correct_rate'] = round(
                        sum(1 for s in _dir if s > 0) / len(_dir), 3)
        
        except Exception as e:
            # W4 纪律 (2026-10-15 修): except 留痕一律 warning+exc_info, debug-in-except 是棘轮违规
            logger.warning(f"[attribution] metadata 源读取失败: {e}", exc_info=True)

        try:
            # ── 补充源: 旧 trade_attribution 表 (历史日期 ≤2026-06-26 有数据) ──
            rows = self._query(
                "SELECT * FROM trade_attribution WHERE trade_date = ?",
                (date,)
            )
            if rows:
                sources.append('trade_attribution_table')
                result['total_records'] = result.get('total_records', 0) + len(rows)
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
            if not sources:
                result['total_records'] = 0
                result['note'] = '当日无归因数据 (metadata.attribution 与旧表均无)'
            else:
                result['attribution_source'] = '+'.join(sources)
        except Exception as e:
            logger.warning(f"[attribution] 分析失败: {e}")
            if not sources:
                result['total_records'] = 0
                result['note'] = f'归因读取异常: {e}'

        # ── Phase 6: 五因子归因融合 ──
        # 2026-08-01 断线 (做减法): 基线已退役 (旧持仓规则 vs 日内 playbook), 读数无效,
        # 且每日全量加载 30 天 trade_records 空耗。模块本体留墓碑待 08-07 后下线。
        five_factor = {
            'five_factor_available': False,
            'status': 'retired',
            'note': '五因子归因已退役 (基线不匹配日内 playbook), 计算已停用, 模块待 08-07 后下线。',
        }
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
                    'lookback_window': '30d',
                    'lookback_note': '五因子归因基于30天回看数据, 非当日',
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

        2026-08-01 改基准: 当日"计划" = playbook 指令集 (BUY/SELL_SHORT)。
        旧 battle plan 已退役 (标的多与 playbook 宇宙零交集 → "已执行0/计划外12"
        系口径化石, 执行质量维度被系统性打零)。新口径下:
          已执行 = 有成交的指令 | 错过 = 未成交指令 (门拦/未触发) | 计划外 = 无指令成交 (应恒0, >0 即链路 bug)
        """
        result = {
            'plan_available': False,
            'planned_targets': [],
            'executed_targets': [],
            'missed_opportunities': [],
            'unplanned_trades': [],
            'planned_details': [],
            'direction_provenance': {},
        }

        try:
            instructions = self._query(
                "SELECT symbol, direction FROM playbook_instructions "
                "WHERE date = ? AND (direction IN ('BUY','SELL_SHORT') "
                "OR skip_reason = 'entry_confirm_reversed')",
                (date,)
            )
            planned_symbols = sorted({r['symbol'] for r in instructions})
            if not planned_symbols:
                return result

            result['plan_available'] = True
            result['plan_source'] = 'playbook_instructions'
            result['plan_date'] = date
            result['planned_targets'] = planned_symbols

            # 对比实际交易 (D3 白名单, 与主查询同口径)
            # DEB-20260903 #127: 同步删除 trade_mode != 'live' (同主查询)
            # DEB-20260904 #209: 补 PHANTOM_RECONCILE_VOID 排除 (同主查询,
            #   NULL 保留式 — 未平仓单仍计为已执行; 原缺 → 幽灵对账冲销行
            #   被当作真实成交, 可把未执行指令误判为"已执行"/制造假 unplanned)
            trades = self._query(
                "SELECT symbol, direction FROM trade_records WHERE trade_date = ? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_reason IS NULL OR exit_reason != 'PHANTOM_RECONCILE_VOID')",
                (date,)
            )
            # DEB-20260903 #129: 按 (symbol, direction) 匹配 — 原仅按 symbol 集合交集,
            # 计划 BUY 而实际 SELL_SHORT 同标的会被误判为已执行 (方向错误无法检出)
            _dir_to_plan = {"LONG": "BUY", "SHORT": "SELL_SHORT"}
            planned_pairs = {(r['symbol'], r['direction']) for r in instructions}
            actual_pairs = {(t.get('symbol', ''),
                             _dir_to_plan.get(t.get('direction', ''), t.get('direction', '')))
                            for t in trades}
            actual_symbols = {s for s, _ in actual_pairs}

            executed_pairs = planned_pairs & actual_pairs
            # 方向错配检测 (同 symbol 但方向不一致)
            _mismatch = sorted(
                f"{s}(计划{pd}/实际{ad})"
                for (s, pd) in planned_pairs for (s2, ad) in actual_pairs
                if s == s2 and pd != ad
            )
            if _mismatch:
                result['direction_mismatch'] = _mismatch

            # 已执行的计划内交易 (方向一致)
            result['executed_targets'] = sorted({s for s, _ in executed_pairs})
            # 错过的计划内交易 (计划方向未执行)
            result['missed_opportunities'] = sorted({s for s, _ in planned_pairs - executed_pairs})
            # 计划外交易
            result['unplanned_trades'] = sorted(actual_symbols - set(planned_symbols))

            # 执行率
            result['execution_rate'] = round(len(result['executed_targets']) / len(planned_symbols), 4)

            confirm_rows = self._load_entry_confirm_directions(date)
            detailed_rows = self._query(
                "SELECT id, symbol, direction, executed, execution_state, skip_reason, "
                "decision_id, raw_instruction_json FROM playbook_instructions "
                "WHERE date = ? AND (direction IN ('BUY','SELL_SHORT') "
                "OR skip_reason = 'entry_confirm_reversed') ORDER BY id",
                (date,)
            )
            reverse_abstain_symbols = []
            for row in detailed_rows:
                try:
                    raw_instruction = json.loads(row.get('raw_instruction_json') or '{}')
                except (TypeError, ValueError):
                    raw_instruction = {}
                if raw_instruction.get('source') != 'deep_analysis':
                    continue
                original_direction = row.get('direction')
                if original_direction not in ('BUY', 'SELL_SHORT'):
                    confirm = confirm_rows.get(row.get('decision_id') or '', {})
                    original_direction = confirm.get('direction')
                if original_direction not in ('BUY', 'SELL_SHORT'):
                    original_direction = 'UNKNOWN'

                ue_direction = raw_instruction.get('ue_direction') or ''
                normalized_ue_direction = str(ue_direction).upper()
                ue_da_reverse = (
                    original_direction == 'BUY'
                    and normalized_ue_direction in ('SHORT', 'SELL_SHORT')
                ) or (
                    original_direction == 'SELL_SHORT'
                    and normalized_ue_direction in ('LONG', 'BUY')
                )
                challenger_abstain = '弃权' in str(raw_instruction.get('debate_outcome') or '')
                risk_tags = []
                if ue_da_reverse:
                    risk_tags.append('UE_DA_REVERSE')
                if challenger_abstain:
                    risk_tags.append('CHALLENGER_ABSTAIN')
                if ue_da_reverse and challenger_abstain:
                    risk_tags.append('UE_DA_REVERSE_CHALLENGER_ABSTAIN')
                    reverse_abstain_symbols.append(row['symbol'])

                final_state = (
                    'EXECUTED' if row.get('executed')
                    else (row.get('skip_reason') or row.get('execution_state') or 'PENDING')
                )
                result['planned_details'].append({
                    'symbol': row['symbol'],
                    'decision_id': row.get('decision_id') or '',
                    'source': raw_instruction.get('source'),
                    'original_direction': original_direction,
                    'final_direction': row['direction'],
                    'final_state': final_state,
                    'gate_reason': row.get('skip_reason') or '',
                    'ue_direction': ue_direction,
                    'ue_da_reverse': ue_da_reverse,
                    'challenger_abstain': challenger_abstain,
                    'risk_tags': risk_tags,
                })

            result['direction_provenance'] = {
                'original_buy': sum(
                    1 for row in result['planned_details']
                    if row['original_direction'] == 'BUY'
                ),
                'original_sell_short': sum(
                    1 for row in result['planned_details']
                    if row['original_direction'] == 'SELL_SHORT'
                ),
                'original_direction_unknown': sum(
                    1 for row in result['planned_details']
                    if row['original_direction'] == 'UNKNOWN'
                ),
                'original_direction_unknown_symbols': sorted({
                    row['symbol'] for row in result['planned_details']
                    if row['original_direction'] == 'UNKNOWN'
                }),
                'executed': sum(
                    1 for row in result['planned_details']
                    if row['final_state'] == 'EXECUTED'
                ),
                'entry_confirm_abandoned': sum(
                    1 for row in result['planned_details']
                    if row['gate_reason'] == 'entry_confirm_reversed'
                ),
                'other_blocked': sum(
                    1 for row in result['planned_details']
                    if row['final_state'] != 'EXECUTED'
                    and row['gate_reason'] != 'entry_confirm_reversed'
                ),
                'ue_da_reverse_challenger_abstain': len(reverse_abstain_symbols),
                'ue_da_reverse_challenger_abstain_symbols': sorted(
                    set(reverse_abstain_symbols)
                ),
                'policy': 'record-only; DA direction and execution unchanged',
            }

        except Exception as e:
            logger.debug(f"[plan_vs_actual] 获取失败: {e}")

        return result

    def _load_entry_confirm_directions(
        self, date: str, ledger_path: Optional[Path] = None
    ) -> Dict[str, Dict[str, str]]:
        """Read original directions for entry-confirm SKIP rows without DB writes."""
        direction_mapping = {
            'LONG': 'BUY',
            'SHORT': 'SELL_SHORT',
            'BUY': 'BUY',
            'SELL_SHORT': 'SELL_SHORT',
        }
        path = ledger_path or (
            Path(__file__).resolve().parents[2]
            / 'data' / 'shadow' / 'entry_confirm_forward.jsonl'
        )
        if not path.exists():
            return {}
        rows = {}
        try:
            with path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError) as _bad_e:
                        # 2026-10-28 except:pass 清剿: 坏行跳过后方向映射会静默缺样本。
                        logger.info("[daily_review] entry_confirm 坏行跳过: %s", _bad_e)
                        continue
                    if row.get('date') != date or not row.get('decision_id'):
                        continue
                    # RFC-141 §5.10: 跳过 v2 弃单后观测行 (无 outcome 字段,
                    # 否则会把主判定行的 outcome='ABANDONED' 覆盖成 '')。
                    # 只排这个类型, 不用白名单 — 历史行可能无 type 键 (兼容)。
                    if row.get('type') == 'entry_confirm_post':
                        continue
                    raw_direction = str(row.get('direction') or '').upper()
                    rows[str(row['decision_id'])] = {
                        'direction': direction_mapping.get(raw_direction, raw_direction),
                        'outcome': row.get('outcome') or '',
                    }
        except OSError as error:
            logger.debug(f"[plan_vs_actual] entry-confirm ledger read failed: {error}")
        return rows

    def _analyze_overnight_risk(self, date: str, trades: List[Dict]) -> Dict[str, Any]:
        """Summarize legacy positions closed today; observation only."""
        def net_pnl(trade: Dict) -> float:
            value = trade.get('pnl_net')
            if value is None:
                value = trade.get('realized_pnl', 0) or 0
            return float(value or 0)

        legacy = [
            trade for trade in trades
            if str(trade.get('exit_time') or '').startswith(date)
            and trade.get('trade_date') != date
        ]
        gap_stop = [
            trade for trade in legacy
            if trade.get('exit_reason') == 'OVERNIGHT_GAP_STOP'
        ]
        worst = min(legacy, key=net_pnl) if legacy else None
        return {
            'legacy_exit_count': len(legacy),
            'legacy_exit_net_pnl': round(sum(net_pnl(trade) for trade in legacy), 2),
            'overnight_gap_stop_count': len(gap_stop),
            'overnight_gap_stop_net_pnl': round(
                sum(net_pnl(trade) for trade in gap_stop), 2
            ),
            'worst_legacy_symbol': worst.get('symbol') if worst else '',
            'worst_legacy_net_pnl': round(net_pnl(worst), 2) if worst else 0.0,
            'attention_required': bool(gap_stop or (legacy and sum(
                net_pnl(trade) for trade in legacy
            ) < 0)),
            'policy': 'record-only; overnight exit rules unchanged',
        }
    
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
        
        # 净口径 (2026-08-01, DEB-20260730 D1: pnl_net 为唯一对外口径)
        # 旧行无 pnl_net → 回退 realized_pnl (与 exit_scorecard 同 COALESCE 语义)
        net_pnls = [(t.get('pnl_net') if t.get('pnl_net') is not None else (t.get('realized_pnl', 0) or 0))
                    for t in closed_trades]
        total_pnl = sum(net_pnls)
        winning = [pnl for pnl in net_pnls if pnl > 0]
        fees_total = sum(t.get('fees_est', 0) or 0 for t in closed_trades)
        fake_wins = sum(1 for t, p in zip(closed_trades, net_pnls)
                        if (t.get('realized_pnl', 0) or 0) > 0 >= p)

        # MFE 捕获效率: 退出引擎是利润核心, 衡量实际落袋占最大有利波动的比例。
        #   单位对齐: pnl_pct 存小数(0.0174=1.74%), mfe_pct 存百分数(1.7383=1.74%) → pnl_pct*100。
        #   每笔 = clamp(pnl_pct*100 / mfe_pct, 0, 1) — 止损单(负pnl)记0, 不奖励 overshoot 伪影。
        #   仅统计有可用 mfe_pct(>0) 的已平仓交易; 无样本则 None。
        _cap_ratios = []
        for t in closed_trades:
            _mfe = t.get('mfe_pct') or 0
            if _mfe > 0:
                _pp = (t.get('pnl_pct') or 0) * 100.0
                _cap_ratios.append(max(0.0, min(1.0, _pp / _mfe)))

        return {
            'total_trades': len(all_trades),
            'closed_trades': len(closed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(closed_trades) - len(winning),
            'total_pnl': round(total_pnl, 2),
            'win_rate': round(len(winning) / len(closed_trades), 4),
            'avg_pnl': round(total_pnl / len(closed_trades), 2),
            'pnl_basis': 'net: COALESCE(pnl_net, realized_pnl) (DEB-20260730 D1)',
            'fees_total': round(fees_total, 2),
            'total_pnl_net': round(total_pnl, 2),
            'winning_trades_net': len(winning),
            'win_rate_net': round(len(winning) / len(closed_trades), 4),
            'fake_wins': fake_wins,
            'avg_holding_period': round(sum(t.get('holding_hours') or 0 for t in closed_trades) / len(closed_trades), 1) if closed_trades else 0,
            'mfe_capture_efficiency': round(sum(_cap_ratios) / len(_cap_ratios), 4) if _cap_ratios else None,
            'mfe_capture_n': len(_cap_ratios),
            # 最大单笔持仓回撤 (峰→谷, **百分点**口径 — trade_records.max_drawdown 由
            #   pipeline._max_drawdown_from_mfe_mae(mfe_pct, mae_pct) 反算 = 100*(mfe+mae)/(100+mfe))。
            #   2026-09-03 复盘批新增: 盘后文档原"最大回撤"行读 summary['max_drawdown'] 而本函数
            #   从不产该键 → 恒 0.00% (常量退化); 且键名不带量纲 → 下游用 .2% 渲染会把 3.65pp
            #   印成 364.64% (量纲说谎)。故键名带 _pp 后缀, 并附非零样本数 _n 自证不是常量
            #   (AGENT.md 军规: 新增落库字段必带基数自检 + 单位/量纲声明)。
            'max_position_drawdown_pp': round(max((t.get('max_drawdown') or 0) for t in closed_trades), 2),
            'max_position_drawdown_symbol': max(closed_trades, key=lambda t: t.get('max_drawdown') or 0).get('symbol'),
            'max_position_drawdown_n': sum(1 for t in closed_trades if (t.get('max_drawdown') or 0) > 0),
        }
    
    # =========================================================================
    #  旧版方法 (向后兼容)
    # =========================================================================
    
    def _get_signal_funnel(self, date: str) -> Dict:
        """信号漏斗 (2026-08-01 改源: playbook_instructions)

        decisions.db 随旧引擎 07-12 停写 (playbook 为唯一交易路径但不写该表),
        total_decisions 恒 0 → 信号质量/漏斗诊断/综合评分全成伪读数。
        真实决策源 = 当日 playbook 指令 (含 SKIP), 键保持兼容。
        """
        try:
            rows = self._query(
                f"SELECT direction, executed, ({playbook_source_sql()}) AS source, "
                "COUNT(*) as cnt FROM playbook_instructions "
                "WHERE date = ? GROUP BY direction, executed, source",
                (date,)
            )
            total = sum(r['cnt'] for r in rows)
            by_direction = {}
            for r in rows:
                suffix = '' if r['executed'] else '(未执行)'
                key = f"{r['direction']}{suffix}"
                by_direction[key] = by_direction.get(key, 0) + r['cnt']
            return {
                'total_decisions': total,
                'by_direction_strength': by_direction,
                'by_source_direction': aggregate_source_direction(rows),
                'source': 'playbook_instructions',
                'source_attribution_version': 'rfc117_v3',
            }
        except Exception as e:
            return {'total_decisions': 0, 'error': str(e)}
    
    def _get_rejection_analysis(self, date: str) -> Dict:
        """拒绝分析 (从 rejections.db)
        2026-08-01 退役标注: rejections.db 06-29 停写 (playbook 路径不接 rejection_logger),
        此后每日 total 恒 0 且无告警 — 伪读数。playbook 时代的"拒绝"等价物 =
        信号漏斗里的 未执行指令 (executed=0), 已在 funnel 维度呈现。
        """
        return {
            'total': 0,
            'status': 'retired',
            'note': '拒绝分析已退役: rejections.db 停写于 2026-06-29 (playbook 路径不落拒绝)。'
                    '未执行指令请看信号漏斗的 未执行 分布。',
        }
    
    def _get_capital_state(self, date: str) -> Dict:
        """资金状态 (从 ftseek_us.db capital_state)"""
        try:
            # DEB-20260903 #130: last_update 为 CST 裸时间戳 (capital_safety.py:174 datetime.now()),
            # 服务器 CST 下美股盘中 12:00-16:00 ET 对应 CST 次日 00:00-04:00 →
            # date(last_update)=ET交易日 系统性不匹配. 改为取最近 N 条后 Python 侧 CST→ET 转换匹配
            rows = self._query(
                "SELECT initial_capital, current_equity, peak_equity, cash, "
                "buying_power, drawdown_pct, max_drawdown_pct, water_level, last_update "
                "FROM capital_state ORDER BY last_update DESC LIMIT 10",
                ()
            )
            rows = list(rows or [])
            from datetime import timezone as _tz, timedelta as _td
            from app.utils.timezone import ET as _ET
            _cst = _tz(_td(hours=8))

            def _et_date(_row):
                _dt = datetime.fromisoformat(str(_row.get('last_update', '')).replace('Z', ''))
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=_cst)  # CST 裸时间戳
                return _dt.astimezone(_ET).strftime("%Y-%m-%d")

            _matched = None
            if rows:
                for _r in rows:
                    try:
                        if _et_date(_r) == date:
                            _matched = _r
                            break
                    except Exception as _e:
                        logger.debug(f"[DailyReview] 快照行时间戳解析失败, 跳过: {_e}")
                        continue
            # DEB-20260904 #210: 未匹配到当日时原静默回退 rows[:1](最新快照) —
            # 历史日期复盘会把"今天"的回撤/水位当该日状态 → 风控维度/评分失真。
            # 改为 as-of 口径: 取 ET 日 <= 复盘日 的最新快照; 一个都没有则记 warning
            # 并标记维度不可用 (不带 drawdown_pct 键 → 下游按"数据不可用"处理),
            # 不再静默挪用最新快照。
            if _matched is None:
                _cst_cutoff = (datetime.strptime(date, "%Y-%m-%d") + _td(days=1)
                               ).strftime("%Y-%m-%dT13:00:00")  # 超集上限: ET D 23:59:59 ≤ CST D+1 12:59:59 (EST), 下方 _et_date <= date 精确过滤
                _older = self._query(
                    "SELECT initial_capital, current_equity, peak_equity, cash, "
                    "buying_power, drawdown_pct, max_drawdown_pct, water_level, last_update "
                    "FROM capital_state WHERE last_update <= ? "
                    "ORDER BY last_update DESC LIMIT 10",
                    (_cst_cutoff,)
                )
                for _r in (_older or []):
                    try:
                        if _et_date(_r) <= date:
                            _matched = _r
                            break
                    except Exception as _e:
                        logger.debug(f"[DailyReview] 快照行时间戳解析失败, 跳过: {_e}")
                        continue
            if _matched is None:
                logger.warning(f"[DailyReview] {date} 无 as-of 资金快照, 风控维度标记不可用")
                return {'note': f'No capital state as-of {date}',
                        'data_available': False, 'unavailable': True}
            rows = [_matched]
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
        
        # attribution 数据质量 (2026-09-17 修: 旧条件读 attribution['data_available'] —
        #   该键 _analyze_attribution 从无处写入 ⇒ `not None` 恒真, 只要当日有归因记录就误报
        #   "评分数据全空, 请修复归因写入管线"; 实测 9/16 sources=trade_records.metadata 已产出
        #   dimension_scores/direction_correct_rate。改按"是否真提取到任一带分数据"判定。
        _attr_scored = bool(
            attribution.get('overall_scores') or attribution.get('dimension_scores')
            or attribution.get('signal_scores') or attribution.get('execution_scores')
            or attribution.get('risk_scores') or attribution.get('timing_scores')
            or attribution.get('slippage_values')
        )
        if attribution.get('total_records', 0) > 0 and not _attr_scored:
            suggestions.append("当日归因记录存在但未提取到任何评分数据，请检查归因写入")
        
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
                if not t.get('exit_price') or t.get('exit_price') == 0:
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
                # 2026-08-28 口径统一 (同 intraday_heartbeat._query_today_trades):
                # ① substr 而非 date() — date() 把行内 -04:00 归一到 UTC, 20:00 ET
                #    后的出场会被挪一天 (全仓约定 = ET 墙钟日前 10 位);
                # ② 净额 COALESCE(pnl_net, realized_pnl) — 毛额每笔少计 ~$5 费。
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(pnl_net, realized_pnl)),0), "
                "COALESCE(AVG(CASE WHEN COALESCE(pnl_net, realized_pnl)>0 "
                "THEN 1.0 ELSE 0.0 END),0) "
                "FROM trade_records WHERE substr(exit_time,1,10)=? "
                "AND exit_price > 0 AND COALESCE(pnl_net, realized_pnl) IS NOT NULL",
                (date,)
            ).fetchone()
            # REMOVED (2026-06-19): shadow_trades 变体测试不再使用
            shadow_rows = []
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
            # DEB-20260904 #211: date(...,'localtime') 按服务器 CST 日期匹配,
            # 与 ET 复盘日错位 (盘中 ET 12:00-16:00 = CST 次日) — 改 DST 感知的
            # ET 日界 epoch 区间 (与 #130 capital_state 修复同口径)
            from datetime import datetime as _dt, timedelta as _td, time as _time
            from app.utils.timezone import ET as _ET, _localize
            _d0 = _dt.strptime(date, "%Y-%m-%d").date()
            _lo = int(_localize(_dt.combine(_d0, _time.min), _ET).timestamp())
            _hi = int(_localize(_dt.combine(_d0 + _td(days=1), _time.min), _ET).timestamp())
            rows = conn.execute(
                "SELECT symbol, realized_pnl, z_score, rolling_mean, rolling_std "
                "FROM pnl_outliers WHERE detected_at >= ? AND detected_at < ? "
                "ORDER BY ABS(z_score) DESC LIMIT 10",
                (_lo, _hi)
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
                # 2026-08-28: substr 而非 date() — 后者把 -04:00 归一到 UTC,
                # 20:00 ET 后的出场被挪一天 (全仓"当日"=ET 墙钟日)
                "SELECT COALESCE(owner_strategy, '<NULL>'), COUNT(*) "
                "FROM trade_records WHERE substr(exit_time,1,10)=? GROUP BY 1", (date,)
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
                # DEB-20260904 #211: date(...,'localtime') 按 CST 匹配与 ET 复盘日错位 —
                # 改 DST 感知 ET 日界 epoch 区间, Python 侧聚合 (与 #130 同口径)
                from datetime import datetime as _dt, timedelta as _td, time as _time
                from app.utils.timezone import ET as _ET, _localize
                _d0 = _dt.strptime(date, "%Y-%m-%d").date()
                _lo = int(_localize(_dt.combine(_d0, _time.min), _ET).timestamp())
                _hi = int(_localize(_dt.combine(_d0 + _td(days=1), _time.min), _ET).timestamp())
                _arb_rows = arb_conn.execute(
                    "SELECT timestamp, n_candidates, winner_strategy FROM signal_arbitration "
                    "WHERE timestamp >= ? AND timestamp < ?",
                    (_lo, _hi)
                ).fetchall()
                total = len(_arb_rows)
                multi = sum(1 for r in _arb_rows if (r[1] or 0) > 1)
                winners = {}
                for r in _arb_rows:
                    winners[r[2]] = winners.get(r[2], 0) + 1
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

    def _save_report(self, report: Dict, date: str,
                     overwrite_postmarket: bool = False):
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
            
            # 2026-08-13 修复三处映射缺陷:
            #   ① symbol 已含 US. 前缀, 旧写法 f"US.{symbol}" → US.US.CSCO
            #   ② pnl 口径统一为净 (pnl_net = 毛-费): total/win_rate/明细行同口径,
            #      否则明细加总 ≠ frontmatter 总数 (毛/费差可达 $30+/日)
            # 2026-08-27 收盘审计④: 补过夜仓行 (进场时间 + 隔夜臂台账水深, 若有)。
            _open_rows = []
            for t in report.get('open_trades', []) or []:
                _arm = {}
                try:
                    _arm = (json.loads(t.get('metadata_json') or '{}') or {}).get('overnight_arm') or {}
                except Exception:
                    _arm = {}
                _open_rows.append({'code': str(t.get('symbol', '') or ''),
                                   'id': str(t.get('id', '') or ''),
                                   'entry': str(t.get('entry_time', '') or ''),
                                   'underwater': _arm.get('underwater_pct_net')})
            writer.write_daily_postmarket({
                'date': date,
                'market': 'US',
                'total_trades': summary.get('total_trades', 0),
                'win_rate': summary.get('win_rate_net', summary.get('win_rate', 0)),
                'total_pnl': summary.get('total_pnl_net', summary.get('total_pnl', 0)),
                # 量纲声明: pp = 百分点 (源字段口径), 不是小数 — 渲染端不得再套百分比格式
                'max_drawdown_pp': summary.get('max_position_drawdown_pp'),
                'max_drawdown_symbol': summary.get('max_position_drawdown_symbol'),
                'max_drawdown_n': summary.get('max_position_drawdown_n', 0),
                'open_trades': _open_rows,
                'direction_provenance': report.get('plan_vs_actual', {}).get(
                    'direction_provenance', {}
                ),
                'overnight_risk': report.get('overnight_risk', {}),
                'trades': [{'code': str(t.get('symbol', '') or ''),
                            # DEB-20260904 #212: pnl_net 键存在但为 NULL 时
                            # dict.get 的 default 不生效 — 与 846 行同款的 NULL 感知回退
                            'pnl': t.get('pnl_net') if t.get('pnl_net') is not None else (t.get('realized_pnl', 0) or 0),
                            'id': str(t.get('id', i))}
                           for i, t in enumerate(trades)],
            }, overwrite=overwrite_postmarket)
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


def generate_daily_review(date: str = None, account_type: str = None,
                          final_refresh: bool = False) -> Dict:
    generator = DailyReviewGenerator()
    return generator.generate(date, account_type, final_refresh=final_refresh)
