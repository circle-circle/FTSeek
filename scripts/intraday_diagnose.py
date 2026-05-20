#!/usr/bin/env python3
"""
FTSeek 盘中实时诊断系统 (Layer 1)

每 30 分钟读取 signal_tracker JSON，分析管线健康状态，
检测异常模式并推送告警。

设计原则：
- Overlay 模式，不修改现有代码
- 读取已有 signal_tracker JSON 数据
- 复用 signal_funnel 的分类逻辑
- 异常检测 + 根因分类 + 修复建议
"""
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ftseek.diagnose")

SIGNAL_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/signal_records')

# ─── 拒绝原因分类体系 (复用 signal_funnel 逻辑) ───
REJECT_CATEGORIES = {
    # 风控层拦截
    'circuit_breaker': '🔴 熔断器',
    'max_concurrent_positions': '📦 并发持仓上限',
    'daily_trade_limit': '📊 日内交易限额',
    'max_position': '📦 持仓上限',
    # 信号质量拦截
    'quality_rejected': '🔬 质量过滤',
    'weak_signal': '📉 信号强度不足',
    'weak_signal_error': '❌ 信号错误',
    'mtf_rejected': '🔀 多周期不一致',
    'mtf_unaligned': '🔀 多周期未对齐',
    'adx_inconsistent': '📐 ADX 不一致',
    # 时间窗口拦截
    'non_trading_hours': '🌙 非交易时段',
    'noise_hours': '⚡ 噪声时段',
    'midday_close': '🍱 午间收盘',
    'late_session_gate': '⏰ 尾盘门禁',
    # 去重/冷却拦截
    'open_dedup': '🔄 开仓去重',
    'open_dedup_map_missing': '🔄 去重映射缺失',
    'open_dedup_error': '❌ 去重错误',
    'signal_cooldown': '⏳ 信号冷却',
    'symbol_loss_cooldown': '💔 标的亏损冷却',
    'trailing_cooldown': '📉 追踪冷却',
    'min_hold_time': '⏱️ 最短持仓时间',
    # 策略/计划拦截
    'watchlist': '📝 非自选股',
    'existing_position': '📦 已有持仓',
    'decision_buffer_rejected': '📋 决策队列满',
    'risk_reward': '⚖️ 风险收益比不足',
}


def load_today_records(date: str = None) -> List[Dict]:
    """加载当日信号记录"""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    
    records_file = SIGNAL_RECORDS_DIR / f'signals_{date}.json'
    if not records_file.exists():
        return []
    
    with open(records_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def analyze_pipeline(records: List[Dict], date: str = None) -> Dict[str, Any]:
    """分析管线健康状态"""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    
    total = len(records)
    executed = [r for r in records if r.get('executed')]
    rejected = [r for r in records if r.get('reject_reason') and not r.get('executed')]
    pending = [r for r in records if not r.get('executed') and not r.get('reject_reason')]
    
    # 拒绝原因统计
    reason_counts = Counter()
    for r in rejected:
        reason = r.get('reject_reason', 'unknown')
        categorized = False
        for key, label in REJECT_CATEGORIES.items():
            if key in reason.lower() or key in reason:
                reason_counts[label] += 1
                categorized = True
                break
        if not categorized:
            reason_counts['❓ 其他'] += 1
    
    # TOP 拒绝原因（原始）
    raw_reasons = Counter(r.get('reject_reason', 'unknown') for r in rejected)
    top_reason = raw_reasons.most_common(1)[0] if raw_reasons else ('none', 0)
    
    # 时段分布
    hourly = Counter()
    for r in records:
        ts = r.get('timestamp', '')
        if ts:
            try:
                hour = int(ts[11:13])
                hourly[hour] += 1
            except (IndexError, ValueError):
                pass
    
    # 策略分布
    strategy_counts = Counter(r.get('strategy', 'unknown') for r in records)
    
    rejected_count = len(rejected) if rejected else 1  # 防止除零
    
    stats = {
        'date': date,
        'total': total,
        'executed': len(executed),
        'rejected': len(rejected),
        'pending': len(pending),
        'execution_rate': len(executed) / total if total > 0 else 0,
        'top_reason': top_reason[0],
        'top_reason_count': top_reason[1],
        'top_reason_pct': top_reason[1] / rejected_count,
        'reason_categories': dict(reason_counts.most_common()),
        'raw_reasons': dict(raw_reasons.most_common(10)),
        'hourly': dict(sorted(hourly.items())),
        'strategies': dict(strategy_counts.most_common(5)),
        # 各原因占比（用于异常检测）
        'weak_signal_error_pct': raw_reasons.get('weak_signal_error', 0) / rejected_count,
        'noise_hours_pct': raw_reasons.get('noise_hours', 0) / rejected_count,
        'signal_cooldown_pct': raw_reasons.get('signal_cooldown', 0) / rejected_count,
        'mtf_rejected_pct': (raw_reasons.get('mtf_rejected', 0) + raw_reasons.get('mtf_unaligned', 0)) / rejected_count,
        'late_session_pct': raw_reasons.get('late_session_gate', 0) / rejected_count,
        'max_concurrent_pct': raw_reasons.get('max_concurrent_positions', 0) / rejected_count,
        'is_trading_hours': is_trading_hours(),
    }
    
    return stats


def is_trading_hours() -> bool:
    """判断当前是否为港股交易时段"""
    now = datetime.now()
    time_str = now.strftime('%H:%M')
    if '09:30' <= time_str <= '12:00' or '13:00' <= time_str <= '16:00':
        return True
    return False


def detect_anomalies(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """检测异常模式"""
    anomalies = []
    s = stats  # 短名
    
    # 规则 1: 执行率过低
    if s['executed'] / max(s['total'], 1) < 0.05:
        anomalies.append({
            'name': '执行率过低',
            'severity': 'high',
            'message': f'执行率 {s["execution_rate"]*100:.1f}% <5%，管线几乎全拦截',
            'suggest': '检查 max_concurrent_positions 阈值是否过低',
        })
    
    # 规则 2: 并发持仓上限主导
    if s['top_reason'] == 'max_concurrent_positions' and s['max_concurrent_pct'] > 0.3:
        anomalies.append({
            'name': '并发持仓上限主导',
            'severity': 'high',
            'message': f'并发持仓上限拦截占 {s["max_concurrent_pct"]:.0%}，可能配置过低',
            'suggest': '检查 unified_config.yaml: risk_limits.max_concurrent_positions',
        })
    
    # 规则 3: 信号错误突增
    if s['weak_signal_error_pct'] > 0.2:
        anomalies.append({
            'name': '信号错误突增',
            'severity': 'critical',
            'message': f'weak_signal_error 占 {s["weak_signal_error_pct"]:.0%} >20%，策略代码可能有 bug',
            'suggest': '检查因子策略日志，确认是否有异常抛出',
        })
    
    # 规则 4: 噪声时段信号过多
    if s['noise_hours_pct'] > 0.15:
        anomalies.append({
            'name': '噪声时段信号过多',
            'severity': 'medium',
            'message': f'噪声时段信号占 {s["noise_hours_pct"]:.0%}，浪费算力',
            'suggest': '检查 time_gate 配置，确认噪声时段定义',
        })
    
    # 规则 5: 信号冷却频繁
    if s['signal_cooldown_pct'] > 0.1:
        anomalies.append({
            'name': '信号冷却频繁',
            'severity': 'medium',
            'message': f'信号冷却占 {s["signal_cooldown_pct"]:.0%}，可能阈值过严',
            'suggest': '检查 signal_cooldown 配置，考虑从 120s 调整为 60s',
        })
    
    # 规则 6: 多周期不一致
    if s['mtf_rejected_pct'] > 0.15:
        anomalies.append({
            'name': '多周期不一致',
            'severity': 'medium',
            'message': f'多周期不一致占 {s["mtf_rejected_pct"]:.0%}',
            'suggest': '检查多周期数据源是否同步',
        })
    
    # 规则 7: 尾盘信号过多
    if s['late_session_pct'] > 0.1:
        anomalies.append({
            'name': '尾盘信号过多',
            'severity': 'low',
            'message': f'尾盘信号占 {s["late_session_pct"]:.0%}',
            'suggest': '检查 time_gate 尾盘门禁配置',
        })
    
    # 规则 8: 交易时段零信号
    if s['total'] == 0 and s['is_trading_hours']:
        anomalies.append({
            'name': '交易时段零信号',
            'severity': 'critical',
            'message': '交易时段内零信号，策略引擎可能异常',
            'suggest': '检查 OpenD 连接、数据订阅、策略状态',
        })
    
    return anomalies


def format_diagnosis_report(stats: Dict[str, Any], anomalies: List[Dict]) -> str:
    """格式化为钉钉推送 Markdown"""
    severity_emoji = {
        'critical': '🔴',
        'high': '🟠',
        'medium': '🟡',
        'low': '🟢',
    }
    
    lines = []
    lines.append(f"### 📊 FTSeek 盘中诊断 ({stats['date']} {datetime.now().strftime('%H:%M')})")
    lines.append("")
    
    # 管线概览
    lines.append("**管线概览**")
    lines.append(f"- 总信号: {stats['total']} | 执行: {stats['executed']} | 拒绝: {stats['rejected']}")
    exec_rate = stats['execution_rate'] * 100
    rate_emoji = '🟢' if exec_rate > 10 else ('🟡' if exec_rate > 5 else '🔴')
    lines.append(f"- 执行率: {rate_emoji} {exec_rate:.1f}%")
    lines.append(f"- 待处理: {stats['pending']}")
    lines.append("")
    
    # TOP 拒绝原因
    if stats['raw_reasons']:
        lines.append("**拒绝原因 TOP 5**")
        for reason, count in list(stats['raw_reasons'].items())[:5]:
            pct = count / stats['rejected'] * 100 if stats['rejected'] > 0 else 0
            lines.append(f"- `{reason}`: {count} ({pct:.0f}%)")
        lines.append("")
    
    # 时段分布
    if stats['hourly']:
        lines.append("**时段分布**")
        for hour, count in sorted(stats['hourly'].items()):
            bar = '█' * min(count // 2, 20)
            lines.append(f"- {hour:02d}:00 {count:3d} {bar}")
        lines.append("")
    
    # 异常告警
    if anomalies:
        lines.append("### ⚠️ 异常检测")
        for a in anomalies:
            emoji = severity_emoji.get(a['severity'], '⚪')
            lines.append(f"- {emoji} **{a['name']}**: {a['message']}")
            lines.append(f"  - 💡 {a['suggest']}")
        lines.append("")
    else:
        lines.append("### ✅ 异常检测：无异常")
        lines.append("")
    
    # 策略分布
    if stats['strategies']:
        lines.append("**活跃策略 TOP 3**")
        for strategy, count in list(stats['strategies'].items())[:3]:
            lines.append(f"- {strategy}: {count}")
    
    return '\n'.join(lines)


def main():
    """主入口"""
    logging.basicConfig(level=logging.INFO)
    
    date = sys.argv[1] if len(sys.argv) > 1 else None
    records = load_today_records(date)
    
    if not records:
        date_str = date or datetime.now().strftime('%Y-%m-%d')
        print(json.dumps({
            'status': 'no_data',
            'date': date_str,
            'message': f'未找到 {date_str} 的信号记录',
        }, ensure_ascii=False, indent=2))
        return
    
    stats = analyze_pipeline(records, date)
    anomalies = detect_anomalies(stats)
    report = format_diagnosis_report(stats, anomalies)
    
    # 输出 JSON + Markdown
    output = {
        'status': 'ok',
        'date': stats['date'],
        'stats': stats,
        'anomalies': anomalies,
        'report': report,
        'anomaly_count': len(anomalies),
        'max_severity': max((a['severity'] for a in anomalies), key=lambda s: {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}.get(s, 0)) if anomalies else 'none',
    }
    
    print(json.dumps(output, ensure_ascii=False, indent=2))
    
    # 同时输出 Markdown 报告
    print("\n" + "="*60)
    print("MARKDOWN_REPORT")
    print("="*60)
    print(report)


if __name__ == '__main__':
    main()
