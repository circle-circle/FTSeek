#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门禁叠加效应分析脚本 (Gate Stack Analysis)

目标: 分析多层门禁（时段/ADX/HSI/LLM/费用）的独立拦截率和组合拦截率，
      确认没有过度过滤导致交易机会流失。

用法:
    python app/diagnostics/gate_stack_analysis.py [--days 7]

输出:
    - 每层门禁的独立拦截次数/比例
    - 组合拦截矩阵（哪些门禁同时触发）
    - 被拦截信号的潜在质量分布
    - 过度过滤警告（如果组合拦截率 > 30%）
"""

import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "ftseek.db")
LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "ftseek.log")

# 门禁类型映射
GATE_TYPES = {
    'time_gate': '时段门禁(14:30)',
    'market_regime_gate': 'HSI逆市过滤',
    'adx_gate': 'ADX震荡过滤',
    'llm_block': 'LLM信号block',
    'fee_gate': '费用门禁',
    'position_limit': '持仓限制',
    'min_trade_value': '最小交易额',
    'trend_filter': '趋势过滤',
    'signal_cooldown': '信号冷却',
}


def parse_log_gates(log_path, since=None):
    """
    从日志中解析门禁拦截记录
    
    日志格式示例:
    [时段门禁] HK.00700 时段门禁: 港股 14:35 ≥ 14:30
    [市场趋势门禁] HK.00700 市场趋势门禁: HSI MA5 < MA20
    [LLM门禁·影子] HK.00700 LLM建议回避(block)
    [模拟门禁·影子] HK.00700 ADX=15.3 < 18
    [费用门禁] HK.00700 预估费用占比 2.1% > 1.5%
    """
    gates = []
    if not os.path.exists(log_path):
        print(f"  ⚠️  日志文件不存在: {log_path}")
        return gates
    
    since_ts = since.timestamp() if since else 0
    
    try:
        with open(log_path, 'r') as f:
            for line in f:
                # 提取时间戳
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').timestamp()
                except Exception:
                    continue
                
                if ts < since_ts:
                    continue
                
                # 解析门禁类型
                gate_type = None
                code = None
                reason = ""
                
                if '[时段门禁]' in line:
                    gate_type = 'time_gate'
                elif '[市场趋势门禁]' in line:
                    gate_type = 'market_regime_gate'
                elif '[模拟门禁·影子]' in line and 'ADX' in line:
                    gate_type = 'adx_gate'
                elif '[LLM门禁' in line:
                    if 'block' in line.lower():
                        gate_type = 'llm_block'
                elif '[费用门禁]' in line:
                    gate_type = 'fee_gate'
                elif '[持仓限制]' in line:
                    gate_type = 'position_limit'
                elif '[趋势过滤]' in line and '拒绝' in line:
                    gate_type = 'trend_filter'
                
                if gate_type:
                    # 尝试提取股票代码
                    for part in line.split():
                        if part.startswith('HK.') and len(part) <= 12:
                            code = part
                            break
                    gates.append({
                        'timestamp': ts,
                        'gate_type': gate_type,
                        'code': code or 'unknown',
                        'reason': line.strip()[-100:],
                    })
    except Exception as e:
        print(f"  ⚠️  日志解析失败: {e}")
    
    return gates


def analyze_db_signals(db_path, since=None):
    """
    从数据库分析信号通过率和交易分布
    """
    if not os.path.exists(db_path):
        print(f"  ⚠️  数据库不存在: {db_path}")
        return {}
    
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        stats = {}
        
        # trades 表统计
        try:
            c.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
            open_trades = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM trades WHERE status = 'CLOSED'")
            closed_trades = c.fetchone()[0]
            stats['trades'] = {'open': open_trades, 'closed': closed_trades, 'total': open_trades + closed_trades}
        except Exception:
            stats['trades'] = {'error': 'table not found'}
        
        # positions 表统计
        try:
            c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            open_positions = c.fetchone()[0]
            stats['positions'] = {'open': open_positions}
        except Exception:
            stats['positions'] = {'error': 'table not found'}
        
        conn.close()
        return stats
    except Exception as e:
        print(f"  ⚠️  数据库分析失败: {e}")
        return {}


def compute_gate_stats(gates, window_hours=None):
    """
    计算门禁统计
    """
    if not gates:
        return {}
    
    total = len(gates)
    by_type = defaultdict(int)
    by_code = defaultdict(int)
    by_combo = defaultdict(int)  # 组合同一时间窗口的门禁组合
    hourly = defaultdict(int)
    
    for g in gates:
        by_type[g['gate_type']] += 1
        by_code[g['code']] += 1
        
        # 按小时统计
        hour = datetime.fromtimestamp(g['timestamp']).hour
        hourly[hour] += 1
    
    # 统计组合（同一标的在5分钟内触发多个门禁）
    gates_sorted = sorted(gates, key=lambda x: (x['code'], x['timestamp']))
    i = 0
    while i < len(gates_sorted):
        combo = set()
        code = gates_sorted[i]['code']
        ts = gates_sorted[i]['timestamp']
        j = i
        while j < len(gates_sorted) and gates_sorted[j]['code'] == code and (gates_sorted[j]['timestamp'] - ts) <= 300:
            combo.add(gates_sorted[j]['gate_type'])
            j += 1
        if len(combo) > 1:
            combo_key = '+'.join(sorted(combo))
            by_combo[combo_key] += 1
        i = max(i + 1, j)  # 跳过已处理的记录
    
    return {
        'total': total,
        'by_type': dict(by_type),
        'by_type_pct': {k: f"{v/total:.1%}" for k, v in by_type.items()},
        'by_code': dict(sorted(by_code.items(), key=lambda x: -x[1])[:10]),
        'by_combo': dict(sorted(by_combo.items(), key=lambda x: -x[1])[:5]),
        'hourly': dict(sorted(hourly.items())),
    }


def generate_recommendations(stats):
    """
    生成优化建议
    """
    recs = []
    total = stats.get('total', 0)
    by_type = stats.get('by_type', {})
    by_combo = stats.get('by_combo', {})
    
    # 检查单门禁拦截率
    if total > 10:
        for gate, count in by_type.items():
            pct = count / total
            if pct > 0.5:
                recs.append(f"⚠️ {GATE_TYPES.get(gate, gate)} 拦截占比 {pct:.0%}，可能是主要过滤源")
    
    # 检查组合拦截
    combo_total = sum(by_combo.values())
    if combo_total > 0 and total > 10:
        combo_pct = combo_total / total
        if combo_pct > 0.3:
            recs.append(f"🔴 组合拦截率 {combo_pct:.0%} > 30%，存在过度过滤风险")
            for combo, count in sorted(by_combo.items(), key=lambda x: -x[1])[:3]:
                recs.append(f"   → {combo}: {count}次")
    
    if not recs:
        recs.append("✅ 门禁分布正常，未检测到过度过滤")
    
    return recs


def main():
    days = 7
    if '--days' in sys.argv:
        idx = sys.argv.index('--days')
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])
    
    since = datetime.now() - timedelta(days=days)
    
    print("=" * 65)
    print(f"门禁叠加效应分析 (最近 {days} 天)")
    print(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)
    
    # 1. 日志解析
    print(f"\n📋 1. 日志门禁记录解析")
    gates = parse_log_gates(LOG_PATH, since)
    if not gates:
        print("  ℹ️  未找到门禁记录（可能日志已轮转或尚未产生数据）")
    else:
        print(f"  ✅ 解析到 {len(gates)} 条门禁记录")
        
        stats = compute_gate_stats(gates)
        
        # 2. 门禁类型分布
        print(f"\n📊 2. 门禁类型分布")
        by_type = stats.get('by_type', {})
        by_type_pct = stats.get('by_type_pct', {})
        for gate, count in sorted(by_type.items(), key=lambda x: -x[1]):
            label = GATE_TYPES.get(gate, gate)
            print(f"  {label}: {count}次 ({by_type_pct[gate]})")
        
        # 3. 高频标的
        print(f"\n🎯 3. 门禁高频标的 (TOP 10)")
        for code, count in stats.get('by_code', {}).items():
            print(f"  {code}: {count}次")
        
        # 4. 组合拦截
        print(f"\n🔗 4. 组合拦截矩阵")
        by_combo = stats.get('by_combo', {})
        if by_combo:
            for combo, count in sorted(by_combo.items(), key=lambda x: -x[1]):
                print(f"  {combo}: {count}次")
        else:
            print("  ℹ️  无组合拦截记录")
        
        # 5. 时间分布
        print(f"\n⏰ 5. 门禁触发时间分布")
        for hour, count in sorted(stats.get('hourly', {}).items()):
            bar = "█" * min(count, 30)
            print(f"  {hour:02d}:00 | {bar} ({count})")
        
        # 6. 优化建议
        print(f"\n💡 6. 优化建议")
        for rec in generate_recommendations(stats):
            print(f"  {rec}")
    
    # 7. 数据库状态
    print(f"\n📁 7. 数据库信号状态")
    db_stats = analyze_db_signals(DB_PATH, since)
    trades = db_stats.get('trades', {})
    if 'error' not in trades:
        print(f"  交易记录: {trades.get('total', 0)} (OPEN={trades.get('open', 0)}, CLOSED={trades.get('closed', 0)})")
    positions = db_stats.get('positions', {})
    if 'error' not in positions:
        print(f"  当前持仓: {positions.get('open', 0)}")
    
    # 8. LLM 质量追踪器状态
    print(f"\n🤖 8. LLM 信号质量追踪")
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from app.consensus.llm_quality_tracker import get_llm_quality_tracker
        tracker = get_llm_quality_tracker()
        impact_stats = tracker.get_impact_stats()
        for impact in ['boost', 'neutral', 'weaken', 'block']:
            s = impact_stats.get(impact, {})
            n = s.get('n', 0)
            if n > 0:
                wr = s.get('win_rate', 0)
                ar = s.get('avg_return', 0)
                print(f"  {impact}: n={n}, WR={wr:.0%}, AR={ar:+.2%}")
            else:
                print(f"  {impact}: 无数据")
    except Exception as e:
        print(f"  ⚠️  LLM 追踪器不可用: {e}")
    
    print(f"\n{'=' * 65}")
    print("分析完成")
    print("=" * 65)


if __name__ == "__main__":
    main()
