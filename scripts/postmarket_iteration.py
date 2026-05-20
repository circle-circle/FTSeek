#!/usr/bin/env python3
"""
FTSeek 收盘后持续迭代分析 (Layer 3)

每日收盘后自动执行：
1. 分析当日信号漏斗 + 拒绝原因分布
2. 对比历史数据，识别趋势性变化
3. 生成优化建议（参数调整/配置修改/代码修复）
4. 输出到知识库文件，积累历史经验

设计原则：
- Overlay 模式，不修改现有代码
- 读取已有 signal_tracker + strategy_health 数据
- 输出结构化 JSON + 人类可读 Markdown
"""
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ftseek.postmarket")

SIGNAL_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/signal_records')
KNOWLEDGE_DIR = Path('/opt/ftseek/FTSeek/data/diagnosis_knowledge')
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

# ─── 优化建议规则 ───
OPTIMIZATION_RULES = [
    {
        'name': 'max_concurrent_positions 过低',
        'check': lambda s: s.get('max_concurrent_pct', 0) > 0.3,
        'severity': 'high',
        'suggestion': 'max_concurrent_positions 拦截占 {pct:.0%}，建议从当前值提高到 5-8',
        'config_path': 'unified_config.yaml → risk_limits.max_concurrent_positions',
    },
    {
        'name': 'weak_signal 过多',
        'check': lambda s: s.get('weak_signal_pct', 0) > 0.2,
        'severity': 'medium',
        'suggestion': 'weak_signal 拦截占 {pct:.0%}，可能 consensus_threshold 过高或因子权重不合理',
        'config_path': 'unified_config.yaml → strategy.consensus.threshold',
    },
    {
        'name': 'noise_hours 信号过多',
        'check': lambda s: s.get('noise_hours_pct', 0) > 0.15,
        'severity': 'medium',
        'suggestion': '噪声时段信号占 {pct:.0%}，建议收窄 time_gate 或调整 noise_hours 定义',
        'config_path': 'unified_config.yaml → strategy.time_gate',
    },
    {
        'name': 'signal_cooldown 过严',
        'check': lambda s: s.get('signal_cooldown_pct', 0) > 0.1,
        'severity': 'low',
        'suggestion': '信号冷却占 {pct:.0%}，建议从 120s 调整为 60s',
        'config_path': 'unified_config.yaml → strategy.cooldown.signal_cooldown',
    },
    {
        'name': 'mtf_rejected 过多',
        'check': lambda s: s.get('mtf_rejected_pct', 0) > 0.15,
        'severity': 'medium',
        'suggestion': '多周期不一致占 {pct:.0%}，检查多周期数据源同步',
        'config_path': 'data_subscriber.py → _regime_detector 更新逻辑',
    },
    {
        'name': '执行率过低',
        'check': lambda s: s.get('execution_rate', 1) < 0.05,
        'severity': 'high',
        'suggestion': '执行率仅 {rate:.1f}%，管线几乎全拦截，需全面检查风控阈值',
        'config_path': '多个配置项需综合调整',
    },
]


def load_records(date: str) -> List[Dict]:
    """加载指定日期的信号记录"""
    records_file = SIGNAL_RECORDS_DIR / f'signals_{date}.json'
    if not records_file.exists():
        return []
    with open(records_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_historical_dates(days: int = 7) -> List[str]:
    """获取最近 N 天的日期列表"""
    dates = []
    for i in range(days):
        d = datetime.now() - timedelta(days=i)
        dates.append(d.strftime('%Y-%m-%d'))
    return dates


def analyze_day(records: List[Dict], date: str) -> Dict[str, Any]:
    """分析单日数据"""
    total = len(records)
    executed = sum(1 for r in records if r.get('executed'))
    rejected = [r for r in records if r.get('reject_reason') and not r.get('executed')]
    pending = total - executed - len(rejected)
    
    raw_reasons = Counter(r.get('reject_reason', 'unknown') for r in rejected)
    rejected_count = len(rejected) if rejected else 1
    
    return {
        'date': date,
        'total': total,
        'executed': executed,
        'rejected': len(rejected),
        'pending': pending,
        'execution_rate': executed / total if total > 0 else 0,
        # 各原因占比
        'max_concurrent_pct': raw_reasons.get('max_concurrent_positions', 0) / rejected_count,
        'weak_signal_pct': raw_reasons.get('weak_signal', 0) / rejected_count,
        'weak_signal_error_pct': raw_reasons.get('weak_signal_error', 0) / rejected_count,
        'noise_hours_pct': raw_reasons.get('noise_hours', 0) / rejected_count,
        'signal_cooldown_pct': raw_reasons.get('signal_cooldown', 0) / rejected_count,
        'mtf_rejected_pct': (raw_reasons.get('mtf_rejected', 0) + raw_reasons.get('mtf_unaligned', 0)) / rejected_count,
        'late_session_pct': raw_reasons.get('late_session_gate', 0) / rejected_count,
        # TOP 原因
        'top_reasons': dict(raw_reasons.most_common(5)),
    }


def detect_trends(daily_stats: List[Dict]) -> List[Dict[str, Any]]:
    """检测趋势性变化"""
    trends = []
    if len(daily_stats) < 2:
        return trends
    
    # 比较最近两天
    latest = daily_stats[0]
    previous = daily_stats[1] if len(daily_stats) > 1 else None
    
    if not previous:
        return trends
    
    # 执行率趋势
    exec_rate_change = latest['execution_rate'] - previous['execution_rate']
    if exec_rate_change < -0.05:
        trends.append({
            'name': '执行率下降',
            'severity': 'high',
            'message': f'执行率从 {previous["execution_rate"]*100:.1f}% 降至 {latest["execution_rate"]*100:.1f}% ({exec_rate_change*100:+.1f}pp)',
        })
    elif exec_rate_change > 0.05:
        trends.append({
            'name': '执行率上升',
            'severity': 'low',
            'message': f'执行率从 {previous["execution_rate"]*100:.1f}% 升至 {latest["execution_rate"]*100:.1f}% ({exec_rate_change*100:+.1f}pp)',
        })
    
    # 信号量趋势
    signal_change = latest['total'] - previous['total']
    if abs(signal_change) > previous['total'] * 0.3 and previous['total'] > 0:
        trends.append({
            'name': '信号量异常变化',
            'severity': 'medium',
            'message': f'信号量从 {previous["total"]} 变为 {latest["total"]} ({signal_change:+d})',
        })
    
    return trends


def generate_optimization_suggestions(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """生成优化建议"""
    suggestions = []
    for rule in OPTIMIZATION_RULES:
        if rule['check'](stats):
            pct = stats.get('max_concurrent_pct', 0)
            if 'max_concurrent' in rule['name']:
                pct = stats.get('max_concurrent_pct', 0)
            elif 'weak_signal' in rule['name']:
                pct = stats.get('weak_signal_pct', 0)
            elif 'noise' in rule['name']:
                pct = stats.get('noise_hours_pct', 0)
            elif 'cooldown' in rule['name']:
                pct = stats.get('signal_cooldown_pct', 0)
            elif 'mtf' in rule['name']:
                pct = stats.get('mtf_rejected_pct', 0)
            else:
                pct = stats.get('execution_rate', 0)
            
            suggestion_text = rule['suggestion'].format(pct=pct, rate=stats.get('execution_rate', 0)*100)
            suggestions.append({
                'name': rule['name'],
                'severity': rule['severity'],
                'suggestion': suggestion_text,
                'config_path': rule['config_path'],
            })
    return suggestions


def format_postmarket_report(date: str, stats: Dict, trends: List, suggestions: List) -> str:
    """格式化为 Markdown 报告"""
    lines = []
    lines.append(f"### 📊 FTSeek 盘后分析 ({date})")
    lines.append("")
    
    lines.append("**管线概览**")
    lines.append(f"- 总信号: {stats['total']} | 执行: {stats['executed']} | 拒绝: {stats['rejected']}")
    rate = stats['execution_rate'] * 100
    emoji = '🟢' if rate > 10 else ('🟡' if rate > 5 else '🔴')
    lines.append(f"- 执行率: {emoji} {rate:.1f}%")
    lines.append("")
    
    if stats['top_reasons']:
        lines.append("**拒绝原因 TOP 5**")
        for reason, count in stats['top_reasons'].items():
            pct = count / stats['rejected'] * 100 if stats['rejected'] > 0 else 0
            lines.append(f"- `{reason}`: {count} ({pct:.0f}%)")
        lines.append("")
    
    if trends:
        lines.append("### 📈 趋势变化")
        for t in trends:
            emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(t['severity'], '⚪')
            lines.append(f"- {emoji} {t['name']}: {t['message']}")
        lines.append("")
    
    if suggestions:
        lines.append("### 💡 优化建议")
        for s in suggestions:
            emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(s['severity'], '⚪')
            lines.append(f"- {emoji} **{s['name']}**: {s['suggestion']}")
            lines.append(f"  - 📁 {s['config_path']}")
        lines.append("")
    else:
        lines.append("### ✅ 无优化建议")
        lines.append("")
    
    return '\n'.join(lines)


def save_knowledge(date: str, report: Dict):
    """保存诊断结果到知识库"""
    knowledge_file = KNOWLEDGE_DIR / f'diagnosis_{date}.json'
    with open(knowledge_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"知识库已保存: {knowledge_file}")


def main():
    """主入口"""
    logging.basicConfig(level=logging.INFO)
    
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime('%Y-%m-%d')
    
    # 加载当日数据
    records = load_records(date)
    if not records:
        print(json.dumps({
            'status': 'no_data',
            'date': date,
            'message': f'未找到 {date} 的信号记录',
        }, ensure_ascii=False, indent=2))
        return
    
    # 分析当日
    stats = analyze_day(records, date)
    
    # 加载历史数据（检测趋势）
    historical_dates = load_historical_dates(7)
    daily_stats = []
    for d in historical_dates:
        hist_records = load_records(d)
        if hist_records:
            daily_stats.append(analyze_day(hist_records, d))
    
    # 检测趋势
    trends = detect_trends(daily_stats)
    
    # 生成优化建议
    suggestions = generate_optimization_suggestions(stats)
    
    # 格式化报告
    report_markdown = format_postmarket_report(date, stats, trends, suggestions)
    
    # 输出
    output = {
        'status': 'ok',
        'date': date,
        'stats': stats,
        'trends': trends,
        'suggestions': suggestions,
        'suggestion_count': len(suggestions),
        'max_severity': max((s['severity'] for s in suggestions), key=lambda s: {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}.get(s, 0)) if suggestions else 'none',
        'report': report_markdown,
    }
    
    print(json.dumps(output, ensure_ascii=False, indent=2))
    
    # 保存知识库
    save_knowledge(date, output)
    
    # 输出 Markdown
    print("\n" + "="*60)
    print("POSTMARKET_REPORT")
    print("="*60)
    print(report_markdown)


if __name__ == '__main__':
    main()
