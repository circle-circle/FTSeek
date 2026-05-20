#!/usr/bin/env python3
"""
完整进化循环模拟 — 用模拟盘数据验证假设引擎

目标：用现有历史交易数据，驱动假设从 pending → confirming → confirmed/REFUTED
证明管线不是"有架构无价值"，而是真正能累积可执行规则
"""
import sys, os, json
os.chdir('/opt/ftseek/FTSeek')
sys.path.insert(0, '/opt/ftseek/FTSeek')

from pathlib import Path
from collections import Counter
from app.review.pattern_miner import get_pattern_miner
from app.review.hypothesis_engine import get_hypothesis_engine, HypothesisStatus

print("=" * 70)
print("模拟盘进化循环验证 — 用历史数据驱动假设引擎")
print("=" * 70)

# ═══════════════════════════════════════════════════════
# Step 1: 加载历史交易数据
# ═══════════════════════════════════════════════════════
print("\n📂 Step 1: 加载历史交易数据")
trades = []
for f in Path('/opt/ftseek/FTSeek/data/trade_records').glob('*.json'):
    with open(f) as fh:
        data = json.load(fh)
    trades.extend(data)

# 去重（同业务键保留最新）
seen = {}
for t in trades:
    key = (t.get('code',''), t.get('entry_price',0), t.get('action',''), t.get('strategy',''))
    if t.get('exit_price') is not None and t.get('pnl') is not None:
        if key not in seen or t.get('timestamp','') > seen[key].get('timestamp',''):
            seen[key] = t

closed_trades = list(seen.values())
print(f"   唯一已平仓交易: {len(closed_trades)} 笔")

by_code = Counter(t.get('code','') for t in closed_trades)
by_strategy = Counter(t.get('strategy','') for t in closed_trades)
print(f"   标的分布: {by_code.most_common(5)}")
print(f"   策略分布: {by_strategy.most_common(5)}")

# ═══════════════════════════════════════════════════════
# Step 2: PatternMiner 挖掘模式
# ═══════════════════════════════════════════════════════
print("\n📊 Step 2: PatternMiner 挖掘模式")
miner = get_pattern_miner()
patterns = miner.mine(days=90)
for p in patterns:
    print(f"   [{p['severity']}] {p['pattern']}")
    print(f"         证据: {p['evidence_count']} 次")

# ═══════════════════════════════════════════════════════
# Step 3: 从模式生成假设
# ═══════════════════════════════════════════════════════
print(f"\n🧪 Step 3: 生成假设")
engine = get_hypothesis_engine()

# 清空旧数据，从零开始
if os.path.exists(engine.db.db_path):
    os.remove(engine.db.db_path)
engine = get_hypothesis_engine()  # 重新初始化

hypotheses = engine.generate_from_patterns(patterns)
for h in hypotheses:
    print(f"   [{h.status}] {h.statement[:70]}...")
    print(f"         初始证据={h.evidence_count}, 需要{engine.CONFIRM_THRESHOLD}次确认")

# ═══════════════════════════════════════════════════════
# Step 4: 用历史交易驱动假设进化
# ═══════════════════════════════════════════════════════
print(f"\n🔄 Step 4: 用 {len(closed_trades)} 笔历史交易驱动假设进化")

# 记录进化轨迹
evolution_log = []
for i, t in enumerate(closed_trades):
    trade_info = {
        'code': t.get('code', ''),
        'pnl': t.get('pnl', 0),
        'exit_reason': t.get('exit_reason', ''),
        'strategy': t.get('strategy', ''),
    }
    result = engine.evaluate_trade(trade_info)
    if result['matched'] > 0:
        for update in result['updates']:
            evolution_log.append(update)
    
    # 检查是否有假设状态变化
    for h in engine.db.load_all():
        if h.status == HypothesisStatus.CONFIRMED and not hasattr(h, '_confirmed_logged'):
            h._confirmed_logged = True
            print(f"   ✅ 假设已确认 [{h.id[:12]}]: {h.statement[:60]}...")
            print(f"      支持={h.confirm_count}/反驳={h.refute_count} → 规则: {h.actionable_rule}")
        elif h.status == HypothesisStatus.REFUTED and not hasattr(h, '_refuted_logged'):
            h._refuted_logged = True
            print(f"   ❌ 假设被推翻 [{h.id[:12]}]: {h.statement[:60]}...")
            print(f"      支持={h.confirm_count}/反驳={h.refute_count}")

# ═══════════════════════════════════════════════════════
# Step 5: 进化结果
# ═══════════════════════════════════════════════════════
print(f"\n📈 Step 5: 进化结果")
summary = engine.get_evolution_summary()
print(f"   总假设: {summary['total']}")
print(f"   待验证: {summary['pending']}")
print(f"   验证中: {summary['verifying']}")
print(f"   已确认: {summary['confirmed']}")
print(f"   已推翻: {summary['refuted']}")

print(f"\n📋 所有假设状态:")
for h in engine.db.load_all():
    icon = "✅" if h.status == "confirmed" else "❌" if h.status == "refuted" else "🔬" if h.status == "verifying" else "⏳"
    print(f"   {icon} [{h.status:10}] {h.statement[:65]}...")
    print(f"        支持={h.confirm_count}/反驳={h.refute_count}/总={h.evidence_count} | 规则: {h.actionable_rule[:50]}...")

# ═══════════════════════════════════════════════════════
# Step 6: 已确认规则 → 可执行门禁
# ═══════════════════════════════════════════════════════
confirmed_rules = summary.get('confirmed_rules', [])
if confirmed_rules:
    print(f"\n🔒 Step 6: 已确认规则（次日盘前注入）")
    for r in confirmed_rules:
        print(f"   {r['type'].upper()}: {r['rule']}")
        print(f"        证据: {r['evidence']}")
        print(f"        置信度: {r['confidence']:.0%}")
else:
    print(f"\n⏳ 尚无已确认规则（需要更多交易数据或降低确认阈值）")

# ═══════════════════════════════════════════════════════
# Step 7: 模拟盘前注入效果
# ═══════════════════════════════════════════════════════
print(f"\n🌅 Step 7: 模拟次日盘前注入效果")
from app.review.hypothesis_engine import get_hypothesis_engine
engine2 = get_hypothesis_engine()

parts = []

# 模式
miner2 = get_pattern_miner()
patterns2 = miner2.mine(days=60)
if patterns2:
    important = [p for p in patterns2 if p.get('severity') in ('HIGH', 'MEDIUM')]
    if important:
        parts.append("\n【⚠️ 历史交易模式（必须遵守）】")
        for p in important[:3]:
            sev = "🔴" if p.get('severity') == 'HIGH' else "🟡"
            parts.append(f"  {sev} {p['pattern']}")
            parts.append(f"     → {p['actionable_rule']}")

# 规则
rules = engine2.get_confirmed_rules()
if rules:
    parts.append("\n【🔒 已验证交易规则（不可违反）】")
    for r in rules:
        icon = "🚫" if r['type'] == 'block' else "⚠️"
        parts.append(f"  {icon} {r['rule']}")
        parts.append(f"     证据: {r['evidence']}, 置信度: {r['confidence']:.0%}")

# 活跃假设
active = engine2.db.load_active()
if active:
    parts.append("\n【🔬 正在验证的假设（谨慎对待）】")
    for h in active:
        parts.append(f"  🔬 {h.statement}")
        parts.append(f"     证据: {h.confirm_count}支持/{h.refute_count}反驳 (总{h.evidence_count}次)")

if parts:
    print("\n".join(parts))
else:
    print("  （暂无可注入内容，数据量不足）")

print(f"\n{'=' * 70}")
print("✅ 模拟盘进化循环验证完成")
print(f"{'=' * 70}")
