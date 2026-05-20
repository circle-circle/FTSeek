"""
信号漏斗报告 — 每日信号生成→共识→风控→执行的全链路统计

从 signal_tracker 的每日 JSON 读取所有信号（含被拒），
生成漏斗视图：信号总数 → 各关卡拦截数 → 最终执行数。

输出格式适合钉钉 Markdown 推送。
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
from collections import Counter

logger = logging.getLogger("ftseek.signal_funnel")

SIGNAL_RECORDS_DIR = Path('/opt/ftseek/FTSeek/data/signal_records')


def build_signal_funnel(date: str = None) -> Dict[str, Any]:
    """
    构建当日信号漏斗报告
    
    Args:
        date: 日期 YYYY-MM-DD，默认今天
        
    Returns:
        漏斗报告字典
    """
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    
    records_file = SIGNAL_RECORDS_DIR / f'signals_{date}.json'
    if not records_file.exists():
        return {
            'date': date,
            'total_signals': 0,
            'funnel': {},
            'rejection_reasons': {},
            'top_rejected_stocks': [],
            'confidence_distribution': {},
            'hourly_distribution': {},
            'pipeline_health': 'no_data',
        }
    
    with open(records_file, 'r', encoding='utf-8') as f:
        records = json.load(f)
    
    if not records:
        return {
            'date': date,
            'total_signals': 0,
            'funnel': {},
            'rejection_reasons': {},
            'top_rejected_stocks': [],
            'confidence_distribution': {},
            'hourly_distribution': {},
            'pipeline_health': 'empty',
        }
    
    total = len(records)
    executed = [r for r in records if r.get('executed')]
    rejected = [r for r in records if r.get('reject_reason') and not r.get('executed')]
    pending = [r for r in records if not r.get('executed') and not r.get('reject_reason')]
    
    # ── 漏斗分层 ──
    funnel = {
        'total_signals': total,
        'generated': total,
        'rejected': len(rejected),
        'pending': len(pending),
        'executed': len(executed),
        'execution_rate': round(len(executed) / total * 100, 1) if total > 0 else 0,
    }
    
    # ── 拒绝原因分类聚合 ──
    reason_categories = {
        'circuit_breaker': '🔴 熔断器',
        'decision_buffer_rejected': '📋 决策队列',
        'noise_hours': '🌙 噪声时段',
        'non_trading_hours': '🌙 非交易时段',
        'watchlist': '📝 非自选股',
        'existing_position': '📦 已有持仓',
        'quality_rejected': '🔬 质量过滤',
    }
    
    raw_reasons = Counter()
    category_counts = Counter()
    
    for r in rejected:
        reason = r.get('reject_reason', 'unknown')
        raw_reasons[reason] += 1
        
        # 分类映射
        categorized = False
        for key, label in reason_categories.items():
            if key in reason:
                category_counts[label] += 1
                categorized = True
                break
        if not categorized:
            if '趋势过滤' in reason or 'trend' in reason.lower():
                category_counts['📉 趋势过滤'] += 1
            elif '价格检查' in reason or '追高' in reason:
                category_counts['⚡ 追高拦截'] += 1
            elif '计划拦截' in reason or '计划外' in reason:
                category_counts['🎯 盘前计划拦截'] += 1
            elif 'confidence' in reason.lower() or '置信度' in reason:
                category_counts['📊 置信度不足'] += 1
            elif '持仓限制' in reason or 'max_position' in reason.lower():
                category_counts['📦 持仓上限'] += 1
            elif '开仓去重' in reason or 'open_dedup' in reason:
                category_counts['🔄 开仓去重'] += 1
            else:
                category_counts['❓ 其他'] += 1
    
    # ── 被拒最多的股票 ──
    stock_reject_counts = Counter()
    for r in rejected:
        code = r.get('code', 'unknown')
        stock_reject_counts[code] += 1
    top_rejected = stock_reject_counts.most_common(10)
    
    # ── 置信度分布 ──
    confs = [r.get('confidence', 0) for r in records if r.get('confidence')]
    conf_distribution = {
        'min': round(min(confs), 2) if confs else 0,
        'max': round(max(confs), 2) if confs else 0,
        'avg': round(sum(confs) / len(confs), 2) if confs else 0,
    }
    
    # ── 时段分布 ──
    hourly = Counter()
    for r in records:
        ts = r.get('timestamp', '')
        if ts:
            try:
                hour = int(ts[11:13])
                hourly[hour] += 1
            except (IndexError, ValueError):
                pass
    
    hourly_sorted = dict(sorted(hourly.items()))
    
    # ── 管线健康判断 ──
    if total == 0:
        health = 'no_data'
    elif len(executed) > 0:
        health = 'healthy'
    elif any('circuit_breaker' in r.get('reject_reason', '') for r in rejected):
        health = 'circuit_breaker_active'
    elif len(rejected) == total and total > 50:
        health = 'all_rejected_high_volume'
    elif len(pending) > 0:
        health = 'pending_signals'
    else:
        health = 'all_rejected'
    
    return {
        'date': date,
        'total_signals': total,
        'funnel': funnel,
        'rejection_categories': dict(category_counts.most_common()),
        'rejection_reasons_raw': dict(raw_reasons.most_common(15)),
        'top_rejected_stocks': top_rejected,
        'confidence_distribution': conf_distribution,
        'hourly_distribution': hourly_sorted,
        'pipeline_health': health,
    }


def format_funnel_markdown(report: Dict[str, Any]) -> str:
    """将漏斗报告格式化为 Markdown (钉钉推送用)"""
    funnel = report['funnel']
    total = funnel['total_signals']
    
    if total == 0:
        return "### 📊 信号漏斗 (今日无信号)\n今日未产生任何信号，请检查数据源和策略引擎状态。\n"
    
    lines = []
    
    # ── 漏斗概览 ──
    health_icon = {
        'healthy': '✅',
        'circuit_breaker_active': '🔴',
        'all_rejected_high_volume': '⚠️',
        'pending_signals': '⏳',
        'all_rejected': '❌',
        'no_data': '❓',
        'empty': '➡️',
    }
    icon = health_icon.get(report['pipeline_health'], '❓')
    
    lines.append(f"### {icon} 信号漏斗 (今日共 {total} 个信号)")
    lines.append(f"- **生成**: {funnel['generated']}")
    lines.append(f"- **被拒**: {funnel['rejected']}")
    if funnel['pending'] > 0:
        lines.append(f"- **待处理**: {funnel['pending']}")
    lines.append(f"- **执行**: {funnel['executed']}")
    lines.append(f"- **执行率**: {funnel['execution_rate']}%")
    lines.append("")
    
    # ── 拒绝原因分类 ──
    if report['rejection_categories']:
        lines.append("#### 🚫 拒绝原因分类")
        for cat, count in report['rejection_categories'].items():
            pct = round(count / total * 100, 1)
            bar_len = min(20, int(pct / 5))
            bar = '█' * bar_len + '░' * (20 - bar_len)
            lines.append(f"- {cat}: {count} ({pct}%) `{bar}`")
        lines.append("")
    
    # ── Top 被拒股票 ──
    if report['top_rejected_stocks']:
        lines.append("#### 📉 被拒最多的股票 (Top 5)")
        for code, count in report['top_rejected_stocks'][:5]:
            lines.append(f"- {code}: {count} 次")
        lines.append("")
    
    # ── 置信度分布 ──
    conf = report['confidence_distribution']
    if conf.get('avg', 0) > 0:
        lines.append(f"#### 📊 信号置信度")
        lines.append(f"- 范围: {conf['min']} ~ {conf['max']}")
        lines.append(f"- 均值: {conf['avg']}")
        lines.append("")
    
    # ── 时段分布 ──
    if report['hourly_distribution']:
        lines.append("#### ⏰ 信号时段分布")
        for hour, count in report['hourly_distribution'].items():
            lines.append(f"- {hour:02d}:00 → {count} 个信号")
        lines.append("")
    
    # ── 健康建议 ──
    health = report['pipeline_health']
    if health == 'circuit_breaker_active':
        lines.append("> ⚠️ 熔断器已激活，部分信号被批量拦截。请检查市场波动和连续拒绝情况。")
    elif health == 'all_rejected_high_volume' and funnel['executed'] == 0:
        lines.append("> ⚠️ 信号量大但执行率为0。可能原因：置信度阈值过高 / 市场条件不满足 / 持仓数已达上限。")
    elif health == 'healthy':
        lines.append("> ✅ 管线运行正常，信号生成和执行链路通畅。")
    
    return "\n".join(lines)


def format_full_daily_report(date: str = None) -> str:
    """
    完整的每日收盘推送报告
    
    合并:
    1. 信号漏斗 (今日所有信号 + 拦截原因)
    2. 持仓状态 (当前持仓列表 + 浮盈浮亏)
    3. 交易记录 (今日实际成交)
    4. 系统健康 (数据源状态)
    """
    from datetime import datetime
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    
    lines = []
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    # ═══════ 标题 ═══════
    lines.append(f"## 📊 FTSeek 港股收盘报告 | {date}")
    lines.append(f"_生成时间: {now_str}_")
    lines.append("")
    
    # ═══════ 1. 信号漏斗 ═══════
    try:
        funnel_report = build_signal_funnel(date)
        funnel_md = format_funnel_markdown(funnel_report)
        lines.append(funnel_md)
    except Exception as e:
        lines.append(f"### ❌ 信号漏斗读取失败: {e}")
        lines.append("")
    
    # ═══════ 2. 今日实际交易 ═══════
    try:
        import sqlite3
        db_path = Path('/opt/ftseek/FTSeek/data/ftseek.db')
        with sqlite3.connect(str(db_path)) as conn:
            c = conn.cursor()
            
            c.execute(f"SELECT code, action, entry_price, exit_price, pnl, pnl_percent, strategy, entry_time, exit_reason, status FROM trades WHERE date(entry_time) = '{date}' ORDER BY entry_time DESC")
            trades = c.fetchall()
            
            if trades:
                lines.append("### 💹 今日成交记录")
                for t in trades:
                    code, action, entry, exit_p, pnl, pnl_pct, strategy, entry_time, exit_reason, status = t
                    pnl_str = f"{pnl:+.2f}" if pnl else "N/A"
                    pct_str = f"({pnl_pct:+.1%})" if pnl_pct else ""
                    status_icon = "📈" if action == 'BUY' else "📉"
                    if status == 'CLOSED':
                        status_icon = "✅" if pnl and pnl > 0 else "❌" if pnl and pnl < 0 else "➡️"
                    lines.append(
                        f"- {status_icon} **{code}** {action} @ {entry} | "
                        f"策略: {strategy} | "
                        f"状态: {status}"
                        + (f" | 盈亏: {pnl_str} {pct_str}" if pnl is not None else "")
                        + (f" | 原因: {exit_reason}" if exit_reason and exit_reason != '' else "")
                    )
            else:
                lines.append("### 💹 今日成交记录")
                lines.append("- 今日无实际成交")
            
            # 统计 trade_chains
            c.execute(f"SELECT COUNT(*) FROM trade_chains WHERE date(created_at) = '{date}'")
            chain_count = c.fetchone()[0]
            lines.append(f"- trade_chains 记录: {chain_count} 条")
    except Exception as e:
        lines.append(f"### ❌ 交易记录读取失败: {e}")
    lines.append("")
    
    # ═══════ 3. 当前持仓 ═══════
    try:
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            c = conn.cursor()
            c.execute("SELECT code, action, quantity, entry_price, stop_loss, take_profit, status FROM positions WHERE status='OPEN' ORDER BY code")
            positions = c.fetchall()
            
            lines.append("### 📦 当前持仓")
            if positions:
                for p in positions:
                    code, action, qty, entry, sl, tp, status = p
                    lines.append(
                        f"- **{code}** {action} × {qty}股 | "
                        f"成本: {entry} | "
                        f"止损: {sl} | "
                        f"止盈: {tp}"
                    )
            else:
                lines.append("- 当前无持仓")
    except Exception as e:
        lines.append(f"### ❌ 持仓读取失败: {e}")
    lines.append("")
    
    # ═══════ 4. 系统健康 ═══════
    try:
        import subprocess
        result = subprocess.run(['systemctl', 'is-active', 'ftseek.service'],
                              capture_output=True, text=True, timeout=5)
        hk_status = result.stdout.strip()
        
        # 检查 OpenD
        result2 = subprocess.run(['pgrep', '-f', 'FutuOpenD'],
                                capture_output=True, text=True, timeout=5)
        opend_status = '✅' if result2.returncode == 0 else '❌'
        
        lines.append("### 🏥 系统健康")
        lines.append(f"- 港股服务: {'✅' if hk_status == 'active' else '❌ ' + hk_status}")
        lines.append(f"- OpenD: {opend_status}")
        
        # 检查 signal_records 文件
        sig_file = SIGNAL_RECORDS_DIR / f'signals_{date}.json'
        if sig_file.exists():
            with open(sig_file) as f:
                sig_data = json.load(f)
            lines.append(f"- 信号记录: ✅ {len(sig_data)} 条")
        else:
            lines.append(f"- 信号记录: ❌ 今日文件不存在")
    except Exception as e:
        lines.append(f"### ❌ 系统健康检查失败: {e}")
    lines.append("")
    
    # ═══════ 5. 优化建议 ═══════
    lines.append("### 💡 建议")
    if funnel_report.get('pipeline_health') == 'all_rejected_high_volume':
        lines.append("- 信号被大量拦截，建议检查置信度阈值和 market regime 参数")
    if funnel_report.get('funnel', {}).get('executed', 0) == 0 and funnel_report.get('total_signals', 0) > 0:
        lines.append("- 今日执行率 0%，建议审查拒绝原因分布，调整 gating 条件")
    lines.append("- 美股今晚 21:30 开盘，请关注 FTSeek-US 盘前推送")
    lines.append("")
    
    return "\n".join(lines)
