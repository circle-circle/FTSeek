#!/usr/bin/env python3
"""
OKR Query Interface for External Agents
========================================
外部 agent 可以通过此脚本查询 OKR 状态、历史记录和趋势分析。

用法:
  python scripts/okr_query.py status          # 当前状态
  python scripts/okr_query.py history [days]  # 历史记录 (默认7天)
  python scripts/okr_query.py trend           # 趋势分析
  python scripts/okr_query.py gap             # 差距分析和行动建议
  python scripts/okr_query.py json            # 纯JSON输出 (供程序解析)
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

def load_latest():
    """加载最新 OKR 状态"""
    path = Path(__file__).parent.parent / "data" / "okr_latest.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)

def load_history(days=7):
    """加载历史记录"""
    path = Path(__file__).parent.parent / "data" / "okr_history.jsonl"
    if not path.exists():
        return []
    
    cutoff = datetime.now() - timedelta(days=days)
    history = []
    with open(path) as f:
        for line in f:
            try:
                record = json.loads(line)
                ts = datetime.fromisoformat(record['timestamp'])
                if ts >= cutoff:
                    history.append(record)
            except:
                continue
    return history

def cmd_status():
    """显示当前状态"""
    data = load_latest()
    if not data:
        print("❌ 无最新数据，请先运行 scripts/okr_status.py")
        return
    
    print("=" * 65)
    print(f"  OKR STATUS — {data['timestamp'][:16]}")
    print("=" * 65)
    print(f"\n🎯 总体进度: {data['overall_score']}%\n")
    
    kr = data['kr_scores']
    for key, score in kr.items():
        bar = "█" * int(score/5) + "░" * (20 - int(score/5))
        print(f"  {key:<20} {bar} {score}%")
    
    p = data['pipeline']
    print(f"\n📊 管道指标:")
    print(f"  指令总数: {p['total']} (active: {p['active']}, skip: {p['skip']})")
    print(f"  有入场价: {p['with_entry']}/{p['active']} ({p['with_entry']/max(p['active'],1)*100:.0f}%)")
    print(f"  触发: {p['triggered']}/{p['active']} ({p['triggered']/max(p['active'],1)*100:.1f}%)")
    print(f"  执行: {p['executed']}")
    print(f"  胜率: {p['win_rate']}% (W:{p['wins']} L:{p['losses']})")
    print(f"  总PnL: ${p['total_pnl']:.2f}")
    
    ic = data['ic']
    print(f"\n📈 IC 指标:")
    print(f"  IC(T+1): {ic['t1']} (n={data['pipeline'].get('sample_t1', 0)})")
    print(f"  IC(T+3): {ic['t3']} (n={data['pipeline'].get('sample_t3', 0)})")
    print(f"  IC(T+5): {ic['t5']} (n={data['pipeline'].get('sample_t5', 0)})")
    print(f"  毕业状态: {ic['graduation']}")
    
    cfg = data['config']
    print(f"\n⚙️  配置:")
    for k, v in cfg.items():
        print(f"  {k}: {v}")

def cmd_history(days=7):
    """显示历史记录"""
    history = load_history(days)
    if not history:
        print(f"❌ 无最近 {days} 天的历史数据")
        return
    
    print(f"📜 OKR 历史记录 (最近 {days} 天, {len(history)} 条)\n")
    print(f"{'时间':<16} {'进度':>6} {'胜率':>6} {'PnL':>10} {'触发率':>7} {'IC(T+3)':>8}")
    print("-" * 65)
    
    for record in history[-20:]:  # 最多显示20条
        ts = record['timestamp'][:16]
        score = record['overall_score']
        p = record['pipeline']
        wr = p['win_rate']
        pnl = p['total_pnl']
        trigger = p['triggered'] / max(p['active'], 1) * 100
        ic = record['ic']['t3']
        ic_str = f"{ic:.3f}" if ic else "N/A"
        
        print(f"{ts:<16} {score:>5}% {wr:>5}% ${pnl:>8.2f} {trigger:>6.1f}% {ic_str:>8}")

def cmd_trend():
    """趋势分析"""
    history = load_history(7)
    if len(history) < 2:
        print("❌ 需要至少2条记录才能分析趋势")
        return
    
    latest = history[-1]
    oldest = history[0]
    
    print("📈 OKR 趋势分析 (7天)\n")
    
    metrics = [
        ('overall_score', '总体进度', '%'),
        ('pipeline.win_rate', '胜率', '%'),
        ('pipeline.total_pnl', '总PnL', '$'),
        ('pipeline.triggered', '触发数', ''),
        ('ic.t3', 'IC(T+3)', ''),
    ]
    
    print(f"{'指标':<15} {'起始':>10} {'当前':>10} {'变化':>10} {'趋势':>6}")
    print("-" * 60)
    
    for path, name, unit in metrics:
        def get_val(record, p):
            keys = p.split('.')
            val = record
            for k in keys:
                val = val.get(k, {}) if isinstance(val, dict) else None
            return val if val is not None else 0
        
        old_val = get_val(oldest, path)
        new_val = get_val(latest, path)
        delta = new_val - old_val
        
        if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
            old_str = f"{old_val:.2f}{unit}" if unit == '$' else f"{old_val:.1f}{unit}"
            new_str = f"{new_val:.2f}{unit}" if unit == '$' else f"{new_val:.1f}{unit}"
            delta_str = f"{delta:+.2f}" if unit == '$' else f"{delta:+.1f}"
            trend = "📈" if delta > 0 else "📉" if delta < 0 else "➡️"
            print(f"{name:<15} {old_str:>10} {new_str:>10} {delta_str:>10} {trend:>6}")

def cmd_gap():
    """差距分析和行动建议"""
    data = load_latest()
    if not data:
        print("❌ 无最新数据")
        return
    
    print("🎯 OKR 差距分析与行动建议\n")
    
    targets = {
        'win_rate': ('胜率', 55, '%'),
        'weekly_pnl': ('周PnL', 1000, '$'),
        'sample': ('样本数', 30, ''),
        'ic': ('IC(T+3)', 0.05, ''),
        'trigger': ('触发率', 50, '%'),
    }
    
    kr = data['kr_scores']
    p = data['pipeline']
    ic = data['ic']
    
    gaps = []
    
    # KR1: 胜率
    wr = p['win_rate']
    gap_wr = 55 - wr
    if gap_wr > 0:
        gaps.append(('kr1_win_rate', f"胜率差距 {gap_wr:.1f}%", [
            "检查低胜率策略，考虑禁用或优化",
            "分析失败交易的共同特征",
            "提升信号质量阈值"
        ]))
    
    # KR2: 周PnL
    # 简化计算：假设总PnL / 周数
    total_pnl = p['total_pnl']
    # 这里简化处理，实际应该计算滚动周PnL
    gap_pnl = 1000 - total_pnl
    if gap_pnl > 0:
        gaps.append(('kr2_weekly_pnl', f"周PnL差距 ${gap_pnl:.2f}", [
            "增加触发率和执行率",
            "优化仓位大小",
            "改进止盈止损策略"
        ]))
    
    # KR3: 样本数
    sample = p['wins'] + p['losses']
    gap_sample = 30 - sample
    if gap_sample > 0:
        gaps.append(('kr3_sample', f"样本数差距 {gap_sample}", [
            "增加 watchlist 规模",
            "放宽筛选条件",
            "确保 auto_execute 正常运行"
        ]))
    
    # KR4: IC
    ic_t3 = ic['t3'] if ic['t3'] else 0
    gap_ic = 0.05 - ic_t3
    if gap_ic > 0:
        gaps.append(('kr4_ic', f"IC(T+3)差距 {gap_ic:.3f}", [
            "分析哪些信号预测力强",
            "禁用负IC策略",
            "优化信号组合权重"
        ]))
    
    # KR5: 触发率
    trigger = p['triggered'] / max(p['active'], 1) * 100
    gap_trigger = 50 - trigger
    if gap_trigger > 0:
        gaps.append(('kr5_trigger', f"触发率差距 {gap_trigger:.1f}%", [
            "检查 entry_price 缺失问题",
            "调整触发阈值 (当前 ±3%)",
            "验证数据收集器正常工作"
        ]))
    
    if not gaps:
        print("✅ 所有 KR 已达标！\n")
    else:
        for kr_name, gap_desc, actions in gaps:
            score = kr[kr_name]
            print(f"【{kr_name}】 {gap_desc} (当前进度: {score}%)")
            for i, action in enumerate(actions, 1):
                print(f"  {i}. {action}")
            print()

def cmd_json():
    """纯JSON输出"""
    data = load_latest()
    if data:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({"error": "no data"}))

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'status':
        cmd_status()
    elif cmd == 'history':
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        cmd_history(days)
    elif cmd == 'trend':
        cmd_trend()
    elif cmd == 'gap':
        cmd_gap()
    elif cmd == 'json':
        cmd_json()
    else:
        print(f"❌ 未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)
