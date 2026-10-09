"""
FTSeek-US 逐笔交易分析引擎 (TradeAnalyzer v1.0)

架构:
  每笔交易 → 多维度分析 (入场逻辑/退出逻辑/盈亏归因/时段匹配/风险合规)
      ↓
  合理性判定 (合理/待定观察/不合理)
      ↓
  周/月共性聚合 → 自动改进方案
      ↓
  辩论触发 (争议交易) → 推送通知

设计原则:
  1. 每笔交易必须深入分析，不只看盈亏
  2. 少数不确定的标记为"待定观察"，基于周/月统计做最终结论
  3. 争议交易触发双 Agent 辩论后再部署改进方案
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek_us.trade_analyzer")

# ============================================================================
#  数据类
# ============================================================================

class TradeVerdict:
    """交易合理性判定"""
    REASONABLE = "合理"           # 决策逻辑正确，执行符合规则
    OBSERVE = "待定观察"          # 证据不足，需要更多数据
    UNREASONABLE = "不合理"       # 决策逻辑或执行有明显问题

@dataclass
class TradeAnalysis:
    """单笔交易分析结果"""
    trade_id: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    realized_pnl: float
    pnl_pct: float
    exit_reason: str
    holding_hours: float
    signal_strength: int
    signal_confidence: float
    
    # 净口径 (2026-10-09, 对齐 7d9a800fa 净口径 SSOT): COALESCE(pnl_net, realized_pnl)
    # 辩论触发/盈亏归因/推送/lesson 一律用净额; 旧行 pnl_net=NULL 回退毛额
    net_pnl: float = 0.0
    
    # 分析结果
    verdict: str = ""             # 合理/待定观察/不合理
    entry_logic: str = ""         # 入场逻辑评估
    exit_logic: str = ""          # 退出逻辑评估
    pnl_attribution: str = ""     # 盈亏归因
    timing_assessment: str = ""   # 时段匹配评估
    risk_compliance: str = ""     # 风险合规评估
    
    # 详细证据
    evidence: Dict[str, Any] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    
    # 辩论标记
    needs_debate: bool = False    # 是否需要辩论
    debate_reason: str = ""       # 辩论原因
    
    def to_dict(self) -> Dict:
        return {
            'trade_id': self.trade_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'realized_pnl': round(self.realized_pnl, 2),
            'net_pnl': round(self.net_pnl, 2),
            'pnl_pct': round(self.pnl_pct, 6),  # 保留原始比率, 不×100 (格式化层负责×100)
            'exit_reason': self.exit_reason,
            'holding_hours': round(self.holding_hours, 1),
            'signal_strength': self.signal_strength,
            'signal_confidence': round(self.signal_confidence, 2),
            'verdict': self.verdict,
            'entry_logic': self.entry_logic,
            'exit_logic': self.exit_logic,
            'pnl_attribution': self.pnl_attribution,
            'timing_assessment': self.timing_assessment,
            'risk_compliance': self.risk_compliance,
            'evidence': self.evidence,
            'issues': self.issues,
            'suggestions': self.suggestions,
            'needs_debate': self.needs_debate,
            'debate_reason': self.debate_reason,
        }


# ============================================================================
#  TradeAnalyzer: 逐笔交易分析引擎
# ============================================================================

class TradeAnalyzer:
    """
    逐笔交易分析引擎
    
    对每笔已平仓交易进行深度分析:
    1. 入场逻辑评估 (信号强度/时段匹配/市场状态)
    2. 退出逻辑评估 (止损/止盈/时间止损/盘末强平)
    3. 盈亏归因 (滑点/信号质量/执行延迟/市场环境)
    4. 风险合规 (仓位占比/止损距离/最大回撤)
    5. 合理性判定 (合理/待定观察/不合理)
    """
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db")
        self.db_path = db_path
        
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def analyze_trades(self, date: str = None) -> List[TradeAnalysis]:
        """
        分析指定日期的所有已平仓交易
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认今天
        
        Returns:
            List[TradeAnalysis]: 每笔交易的分析结果
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        conn = self._get_conn()
        try:
            # 获取当日所有已平仓交易
            trades = conn.execute(
                "SELECT * FROM trade_records WHERE trade_date = ? AND exit_price > 0 ORDER BY id",
                (date,)
            ).fetchall()
            
            # 获取归因数据
            attribution_rows = conn.execute(
                "SELECT * FROM trade_attribution WHERE trade_date = ?",
                (date,)
            ).fetchall()
            attribution_map = {r['symbol']: dict(r) for r in attribution_rows}
            
            results = []
            for t in trades:
                trade_dict = dict(t)
                attr = attribution_map.get(t['symbol'], {})
                
                analysis = self._analyze_single_trade(trade_dict, attr)
                results.append(analysis)
            
            return results
        finally:
            conn.close()
    
    def _analyze_single_trade(self, trade: Dict, attribution: Dict) -> TradeAnalysis:
        """分析单笔交易"""
        # 净口径 SSOT (2026-10-09): pnl_net 存在用净额, NULL(旧行)回退毛额。
        # 0 是合法净额, 禁 `or` 回退 (会把净 0 当缺失)。
        _gross = trade.get('realized_pnl', 0) or 0
        _pnl_net = trade.get('pnl_net')
        _net = _pnl_net if _pnl_net is not None else _gross
        analysis = TradeAnalysis(
            trade_id=trade.get('id', 0),
            symbol=trade.get('symbol', ''),
            direction=trade.get('direction', ''),
            entry_price=trade.get('entry_price', 0),
            exit_price=trade.get('exit_price', 0),
            realized_pnl=_gross,
            net_pnl=_net,
            pnl_pct=trade.get('pnl_pct', 0) or 0,
            exit_reason=trade.get('exit_reason', ''),
            holding_hours=trade.get('holding_hours', 0) or 0,
            signal_strength=trade.get('signal_strength', 0) or 0,
            signal_confidence=trade.get('signal_confidence', 0) or 0,
        )
        
        # 1. 入场逻辑评估
        self._assess_entry_logic(analysis, trade, attribution)
        
        # 2. 退出逻辑评估
        self._assess_exit_logic(analysis, trade)
        
        # 3. 盈亏归因
        self._attribute_pnl(analysis, trade, attribution)
        
        # 4. 时段匹配评估
        self._assess_timing(analysis, trade)
        
        # 5. 风险合规评估
        self._assess_risk_compliance(analysis, trade)
        
        # 6. 综合合理性判定
        self._determine_verdict(analysis)
        
        # 7. 辩论触发判断
        self._check_debate_trigger(analysis)
        
        return analysis
    
    def _assess_entry_logic(self, analysis: TradeAnalysis, trade: Dict, attribution: Dict):
        """评估入场逻辑"""
        issues = []
        evidence = {}
        
        strength = analysis.signal_strength
        confidence = analysis.signal_confidence
        
        # 信号强度检查
        if strength == 0:
            issues.append("signal_strength=0 — 投票数据缺失或静默失败")
            evidence['signal_data_missing'] = True
        elif strength < 50:
            issues.append(f"信号强度偏低 (strength={strength})")
            evidence['weak_signal'] = True
        
        # 信心度检查
        if confidence == 0:
            issues.append("signal_confidence=0.0 — 投票数据缺失")
        elif confidence < 0.5:
            issues.append(f"信心度偏低 (confidence={confidence:.2f})")
        
        # 决策原因检查
        decision_reason = trade.get('decision_reason', '') or ''
        if not decision_reason:
            evidence['no_decision_reason'] = True
        
        if issues:
            analysis.entry_logic = f"存在问题: {'; '.join(issues[:3])}"
        else:
            analysis.entry_logic = f"入场逻辑正常 (strength={strength}, conf={confidence:.2f})"
        
        analysis.evidence.update(evidence)
        analysis.issues.extend(issues)
    
    def _assess_exit_logic(self, analysis: TradeAnalysis, trade: Dict):
        """评估退出逻辑"""
        exit_reason = analysis.exit_reason
        issues = []
        
        if exit_reason == 'PRE_CLOSE_EXIT':
            # 盘末强平分析
            if analysis.holding_hours < 1:
                issues.append(f"持仓极短 ({analysis.holding_hours:.1f}h) 即被盘末强平 — 可能入场时机不当")
            elif analysis.holding_hours >= 5:
                issues.append(f"持仓 {analysis.holding_hours:.1f}h 全程未触发止损/止盈 — 参数可能过宽")
            
            if analysis.pnl_pct > -0.01:
                analysis.exit_logic = f"盘末微盈/微亏退出 ({analysis.pnl_pct*100:.2f}%) — 止损未激活"
            else:
                analysis.exit_logic = f"盘末亏损退出 ({analysis.pnl_pct*100:.2f}%) — 止损未激活"
        
        elif exit_reason == 'FLASH_EXIT':
            if analysis.holding_hours < 0.5:
                analysis.exit_logic = f"闪崩退出 ({analysis.holding_hours*60:.0f}分钟) — 市场剧烈波动"
                analysis.evidence['flash_exit'] = True
        
        elif exit_reason == 'TRAILING_STOP':
            if analysis.pnl_pct > 0:
                analysis.exit_logic = f"追踪止盈获利退出 ({analysis.pnl_pct*100:.2f}%) — 正常"
            else:
                analysis.exit_logic = f"追踪止损亏损退出 ({analysis.pnl_pct*100:.2f}%) — 正常风控"
        
        elif exit_reason == 'EXTERNAL_CLOSE':
            analysis.exit_logic = "外部手动平仓 — 非系统决策"
            analysis.evidence['manual_close'] = True
        
        elif exit_reason == 'STRUCTURE_EXIT':
            analysis.exit_logic = "技术结构破位退出 — 形态失效"
            analysis.evidence['structure_breach'] = True
        
        else:
            analysis.exit_logic = f"退出原因: {exit_reason}"
        
        analysis.issues.extend(issues)
    
    def _attribute_pnl(self, analysis: TradeAnalysis, trade: Dict, attribution: Dict):
        """盈亏归因"""
        # 2026-10-09 净口径修复: 归因判定/金额用净额 (毛正净负单曾被归因为"盈利退出")。
        pnl = analysis.net_pnl
        pnl_pct = analysis.pnl_pct
        
        if pnl > 0:
            # 盈利归因
            if analysis.exit_reason == 'TRAILING_STOP':
                analysis.pnl_attribution = f"追踪止盈成功锁定利润 (+${pnl:.2f}, +{pnl_pct*100:.2f}%)"
            elif analysis.exit_reason == 'PRE_CLOSE_EXIT':
                analysis.pnl_attribution = f"盘末微利退出 (+${pnl:.2f}) — 非主动止盈"
            else:
                analysis.pnl_attribution = f"盈利退出 (+${pnl:.2f}, {analysis.exit_reason})"
        else:
            # 亏损归因
            if analysis.exit_reason == 'PRE_CLOSE_EXIT':
                analysis.pnl_attribution = f"盘末强平亏损 (-${abs(pnl):.2f}) — 止损未激活"
            elif analysis.exit_reason == 'FLASH_EXIT':
                analysis.pnl_attribution = f"闪崩退出亏损 (-${abs(pnl):.2f}) — 市场异常波动"
            elif analysis.exit_reason == 'STRUCTURE_EXIT':
                analysis.pnl_attribution = f"结构破位亏损 (-${abs(pnl):.2f}) — 技术形态失效"
            else:
                analysis.pnl_attribution = f"亏损退出 (-${abs(pnl):.2f}, {analysis.exit_reason})"
        
        # 信号质量关联
        if analysis.signal_strength > 60 and pnl < 0:
            analysis.suggestions.append("高信号强度但亏损 — 需检查信号有效性")
        elif analysis.signal_strength < 40 and pnl > 0:
            analysis.suggestions.append("低信号强度但盈利 — 可能是运气而非策略有效")
    
    def _assess_timing(self, analysis: TradeAnalysis, trade: Dict):
        """时段匹配评估"""
        entry_time = trade.get('entry_time', '')
        if not entry_time:
            analysis.timing_assessment = "无入场时间数据"
            return
        
        try:
            entry_dt = datetime.fromisoformat(entry_time)
            # CST → ET: 用 pytz 动态计算 (DST=12h, EST=13h)
            try:
                import pytz
                from datetime import timezone, timedelta
                et_tz = pytz.timezone('US/Eastern')
                # 如果 entry_dt 是 naive, 假设为服务器时间 CST(UTC+8)
                if entry_dt.tzinfo is None:
                    cst_tz = timezone(timedelta(hours=8))
                    entry_dt = entry_dt.replace(tzinfo=cst_tz)
                et_dt = entry_dt.astimezone(et_tz)
                et_hour = et_dt.hour
            except ImportError:
                # fallback: 硬编码 13h (DST期间), EST用14h
                et_hour = (entry_dt.hour - 13) % 24
            
            if 9 <= et_hour < 16:
                if et_hour == 9:
                    analysis.timing_assessment = f"开盘时段入场 (ET {et_hour}:xx) — 波动大，需确认"
                else:
                    analysis.timing_assessment = f"正常交易时段入场 (ET {et_hour}:xx)"
            elif 4 <= et_hour < 9:
                analysis.timing_assessment = f"⚠️ 盘前时段入场 (ET {et_hour}:xx) — 流动性差"
                analysis.issues.append("盘前入场 — 流动性差，滑点风险高")
            else:
                analysis.timing_assessment = f"非交易时段入场 (ET {et_hour}:xx)"
                analysis.issues.append("非交易时段入场")
        except (ValueError, TypeError):
            analysis.timing_assessment = "时间解析失败"
    
    def _assess_risk_compliance(self, analysis: TradeAnalysis, trade: Dict):
        """风险合规评估"""
        issues = []
        
        # 仓位大小检查 (基于 entry_price * quantity)
        position_value = analysis.entry_price * (trade.get('quantity', 0) or 0)
        if position_value > 200000:
            issues.append(f"仓位过大 (${position_value:,.0f})")
        
        # 最大回撤检查 (trade_records.max_drawdown = **百分点**口径, 由
        #   pipeline._max_drawdown_from_mfe_mae 反算 100*(mfe+mae)/(100+mfe); 例 5.5342 ≡ 5.53%)
        #   2026-09-17 单位勘误: 旧代码 `> 0.05` + `*100` 把 5.53% 打成 "553.4%", 阈值等价 0.05%
        #   ⇒ 每笔都误报"超过 5% 阈值"。与 daily_review.max_position_drawdown_pp (百分比口径) 对齐。
        max_dd = trade.get('max_drawdown', 0) or 0
        if max_dd > 5.0:
            issues.append(f"持仓期间最大回撤 {max_dd:.1f}% — 超过 5% 阈值")
        
        # 亏损幅度检查
        if analysis.pnl_pct < -0.05:
            issues.append(f"单笔亏损 {analysis.pnl_pct*100:.1f}% — 超过 5% 底限")
        
        if issues:
            analysis.risk_compliance = f"风险合规问题: {'; '.join(issues)}"
            analysis.issues.extend(issues)
        else:
            analysis.risk_compliance = "风险合规正常"
    
    def _determine_verdict(self, analysis: TradeAnalysis):
        """综合合理性判定"""
        issues = analysis.issues
        
        # 不合理判定条件
        unreasonable_conditions = [
            "signal_strength=0" in ' '.join(issues),
            "盘前入场" in ' '.join(issues),
            "持仓极短" in ' '.join(issues) and analysis.pnl_pct < 0,
            "单笔亏损" in ' '.join(issues) and analysis.pnl_pct < -0.05,
        ]
        
        # 合理判定条件
        reasonable_conditions = [
            analysis.exit_reason in ('TRAILING_STOP',),
            analysis.pnl_pct > 0 and not any("盘前入场" in i for i in issues),
            analysis.signal_strength >= 50 and analysis.pnl_pct > -0.01,
        ]
        
        if any(unreasonable_conditions):
            analysis.verdict = TradeVerdict.UNREASONABLE
        elif any(reasonable_conditions) and not any("盘前入场" in i for i in issues):
            analysis.verdict = TradeVerdict.REASONABLE
        else:
            analysis.verdict = TradeVerdict.OBSERVE
        
        # 记录判定原因
        analysis.evidence['verdict_reason'] = {
            'unreasonable_count': sum(1 for c in unreasonable_conditions if c),
            'reasonable_count': sum(1 for c in reasonable_conditions if c),
            'issue_count': len(issues),
        }
    
    def _check_debate_trigger(self, analysis: TradeAnalysis):
        """检查是否需要触发辩论"""
        # 触发辩论的条件:
        # 1. 判定为"不合理"但信号强度 > 60 (高信号却亏损)
        # 2. 判定为"待定观察"且亏损超过 $500
        # 3. 盘前入场但盈利 (可能是运气)
        # 2026-10-09 净口径修复: 判定 2/3 改用 net_pnl (费用后净额)。
        # 根因: id=1631 PEP 毛 +$1.13 / 净 -$3.20 (费 $4.33=毛利3.8x) 误触发
        # "盘前盈利待辩论"; 全仓 553 笔毛正里 78 笔 (14.1%) 实为净亏。
        if analysis.verdict == TradeVerdict.UNREASONABLE and analysis.signal_strength > 60:
            analysis.needs_debate = True
            analysis.debate_reason = f"高信号强度 (strength={analysis.signal_strength}) 但判定不合理 — 需要辩论确认信号有效性"
        
        elif analysis.verdict == TradeVerdict.OBSERVE and analysis.net_pnl < -500:
            analysis.needs_debate = True
            analysis.debate_reason = f"待定观察但净亏损较大 (净-${abs(analysis.net_pnl):.2f}, 毛${analysis.realized_pnl:+.2f}) — 需要辩论确认是否系统问题"
        
        elif "盘前入场" in ' '.join(analysis.issues) and analysis.net_pnl > 0:
            analysis.needs_debate = True
            analysis.debate_reason = f"盘前入场但净盈利 (+${analysis.net_pnl:.2f}) — 需要辩论确认是策略有效还是运气"


# ============================================================================
#  周/月共性聚合器
# ============================================================================

class WeeklyMonthlyAggregator:
    """
    周/月共性问题分析聚合器
    
    从逐笔交易分析中提取:
    1. 共性问题 (如: 多次盘前入场、止损参数过宽)
    2. 趋势变化 (胜率/盈亏比/退出原因分布)
    3. 自动改进方案建议
    """
    
    def aggregate_weekly(self, analyses: List[TradeAnalysis]) -> Dict:
        """聚合周度分析结果"""
        if not analyses:
            return {'has_data': False}
        
        result = {
            'has_data': True,
            'total_trades': len(analyses),
            'verdict_distribution': {},
            'exit_reason_distribution': {},
            'common_issues': [],
            'auto_fix_suggestions': [],
            'trends': {},
        }
        
        # 判定分布
        verdicts = {}
        for a in analyses:
            verdicts[a.verdict] = verdicts.get(a.verdict, 0) + 1
        result['verdict_distribution'] = verdicts
        
        # 退出原因分布
        exits = {}
        for a in analyses:
            exits[a.exit_reason] = exits.get(a.exit_reason, 0) + 1
        result['exit_reason_distribution'] = exits
        
        # 共性问题提取
        issue_counts = {}
        for a in analyses:
            for issue in a.issues:
                # 标准化问题 (提取关键词)
                key = issue.split('—')[0].split(':')[0].strip()
                issue_counts[key] = issue_counts.get(key, 0) + 1
        
        # 过滤出出现 >= 2 次的共性问题
        result['common_issues'] = [
            {'issue': k, 'count': v}
            for k, v in sorted(issue_counts.items(), key=lambda x: -x[1])
            if v >= 2
        ]
        
        # 自动修复建议
        result['auto_fix_suggestions'] = self._generate_fix_suggestions(analyses, result)
        
        return result
    
    def _generate_fix_suggestions(self, analyses: List[TradeAnalysis], weekly: Dict) -> List[Dict]:
        """基于周度分析生成自动修复建议"""
        suggestions = []
        
        # 1. 盘前入场过多
        premarket_count = sum(1 for a in analyses if "盘前入场" in ' '.join(a.issues))
        if premarket_count >= 2:
            suggestions.append({
                'type': 'phase_gate',
                'title': '盘前入场门禁过松',
                'description': f'本周 {premarket_count} 笔盘前入场交易',
                'action': 'enforce_trading_hours_gate',
                'priority': 'P0',
            })
        
        # 2. PRE_CLOSE_EXIT 过多
        preclose_count = weekly['exit_reason_distribution'].get('PRE_CLOSE_EXIT', 0)
        total = weekly['total_trades']
        if total > 0 and preclose_count / total > 0.3:
            suggestions.append({
                'type': 'exit_params',
                'title': '止损参数过宽导致大量盘末强平',
                'description': f'{preclose_count}/{total} 笔 ({preclose_count/total*100:.0f}%) 为 PRE_CLOSE_EXIT',
                'action': 'tighten_exit_params',
                'priority': 'P1',
            })
        
        # 3. 信号强度全零
        zero_signal = sum(1 for a in analyses if a.signal_strength == 0)
        if zero_signal >= 3:
            suggestions.append({
                'type': 'signal_data',
                'title': '投票数据大面积缺失',
                'description': f'{zero_signal}/{total} 笔交易 signal_strength=0',
                'action': 'fix_voting_data_pipeline',
                'priority': 'P0',
            })
        
        # 4. 不合理交易过多
        unreasonable = weekly['verdict_distribution'].get('不合理', 0)
        if unreasonable >= 3:
            suggestions.append({
                'type': 'trading_logic',
                'title': '不合理交易占比过高',
                'description': f'{unreasonable} 笔交易判定为不合理',
                'action': 'review_trading_pipeline',
                'priority': 'P1',
            })
        
        return suggestions


# ============================================================================
#  公共接口
# ============================================================================

def analyze_daily_trades(date: str = None) -> List[Dict]:
    """
    分析当日所有交易，返回分析结果列表
    
    Args:
        date: 日期字符串 (YYYY-MM-DD)
    
    Returns:
        List[Dict]: 每笔交易的分析结果
    """
    analyzer = TradeAnalyzer()
    analyses = analyzer.analyze_trades(date)
    return [a.to_dict() for a in analyses]


def aggregate_weekly_analyses(analyses: List[Dict]) -> Dict:
    """
    聚合周度分析结果
    
    Args:
        analyses: 逐笔交易分析结果列表
    
    Returns:
        Dict: 周度聚合报告
    """
    aggregator = WeeklyMonthlyAggregator()
    # Convert dicts back to TradeAnalysis objects
    trade_analyses = []
    for a in analyses:
        ta = TradeAnalysis(
            trade_id=a.get('trade_id', 0),
            symbol=a.get('symbol', ''),
            direction=a.get('direction', ''),
            entry_price=a.get('entry_price', 0),
            exit_price=a.get('exit_price', 0),
            realized_pnl=a.get('realized_pnl', 0),
            net_pnl=a.get('net_pnl', a.get('realized_pnl', 0)),
            pnl_pct=a.get('pnl_pct', 0),  # 不再/100, to_dict已保留原始比率
            exit_reason=a.get('exit_reason', ''),
            holding_hours=a.get('holding_hours', 0),
            signal_strength=a.get('signal_strength', 0),
            signal_confidence=a.get('signal_confidence', 0),
            verdict=a.get('verdict', ''),
            entry_logic=a.get('entry_logic', ''),
            exit_logic=a.get('exit_logic', ''),
            pnl_attribution=a.get('pnl_attribution', ''),
            timing_assessment=a.get('timing_assessment', ''),
            risk_compliance=a.get('risk_compliance', ''),
            evidence=a.get('evidence', {}),
            issues=a.get('issues', []),
            suggestions=a.get('suggestions', []),
            needs_debate=a.get('needs_debate', False),
            debate_reason=a.get('debate_reason', ''),
        )
        trade_analyses.append(ta)
    
    return aggregator.aggregate_weekly(trade_analyses)
