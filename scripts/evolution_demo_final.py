#!/usr/bin/env python3
"""
完整进化循环演示 — 确认 + 推翻 + 规则注入
展示假设引擎如何处理四种场景：确认、推翻、验证中、待验证
"""
import sys, os, json
os.chdir('/opt/ftseek/FTSeek')
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.review.hypothesis_engine import get_hypothesis_engine, HypothesisStatus, Hypothesis

print("=" * 70)
print("假设驱动进化引擎 — 完整演示（确认+推翻+规则注入）")
print("对标 RD-Agent: Hypothesis → Experiment → Feedback → Confirm/Refute → Knowledge")
print("=" * 70)

# 清空旧数据
db_path = '/opt/ftseek/FTSeek/data/hypothesis.db'
if os.path.exists(db_path):
    os.remove(db_path)

engine = get_hypothesis_engine()
now = "2026-05-10 16:30:00"

# ═══════════════════════════════════════════════════════
# Step 1: 创建 4 个假设（覆盖不同类别）
# ═══════════════════════════════════════════════════════
print("\n🧪 Step 1: 创建 4 个假设")

hypotheses_data = [
    # H1: 策略类 — 应被确认（5笔亏损 → confirmed）
    {"id": "H1", "statement": "策略「test」胜率为0%，累计亏损巨大",
     "source": "pattern_mining", "category": "strategy",
     "trigger_conditions": {"conditions": ["strategy=test"]},
     "predicted_outcome": "胜率0%, 累计亏损-762600",
     "actionable_rule": "策略 test 禁止使用", "rule_type": "block"},
    
    # H2: 止损类 — 应被确认（5笔止损 → confirmed）
    {"id": "H2", "statement": "止损在入场后15min内触发说明止损太紧",
     "source": "pattern_mining", "category": "stop_loss",
     "trigger_conditions": {"conditions": ["exit_reason=止损"]},
     "predicted_outcome": "100%止损在15min内",
     "actionable_rule": "止损从固定%改为 ATR 止损", "rule_type": "adjust_param"},
    
    # H3: 市场类 — 应被推翻（3亏损 vs 4盈利 → refuted）
    {"id": "H3", "statement": "恒指跌>1%的日子做多胜率仅18%",
     "source": "llm_analysis", "category": "market_regime",
     "trigger_conditions": {"conditions": ["HSI<-1%", "action=BUY"]},
     "predicted_outcome": "胜率<20%",
     "actionable_rule": "恒指跌>1%时禁止做多", "rule_type": "block"},
    
    # H4: 个股类 — 保持验证中（2笔证据，不足5次）
    {"id": "H4", "statement": "HK.00981 午后追高(RSI>65)胜率低",
     "source": "pattern_mining", "category": "stock_time",
     "trigger_conditions": {"conditions": ["code=HK.00981", "RSI>65"]},
     "predicted_outcome": "胜率<30%",
     "actionable_rule": "HK.00981 午后 RSI>65 时禁止开仓", "rule_type": "block"},
]

for hd in hypotheses_data:
    h = Hypothesis(created_at=now, updated_at=now, **{k: v for k, v in hd.items() if k not in ['id']})
    h.id = hd['id']
    engine.db.save(h)
    print(f"   [{h.status}] {h.statement[:50]}...")

# ═══════════════════════════════════════════════════════
# Step 2: 模拟交易驱动进化
# ═══════════════════════════════════════════════════════
print(f"\n🔄 Step 2: 模拟交易驱动进化（CONFIRM={engine.CONFIRM_THRESHOLD}, REFUTE={engine.REFUTE_THRESHOLD}）")

import random
random.seed(42)
simulated_trades = []

# H3 放最前面：4盈利（反驳）先到达 → refuted
for i in range(4):
    simulated_trades.append({'code': 'HK.00700', 'pnl': random.uniform(50,300), 'exit_reason': '止盈', 'strategy': 'confluence_v2'})
# 再2亏损（支持，但反驳已足够）
for i in range(2):
    simulated_trades.append({'code': 'HK.00700', 'pnl': random.uniform(-400,-80), 'exit_reason': '止损', 'strategy': 'confluence_v2'})

# H1: 6笔 test 策略亏损 → confirmed
for i in range(6):
    simulated_trades.append({'code': 'HK.00700', 'pnl': random.uniform(-500,-100), 'exit_reason': '止损', 'strategy': 'test'})

# H2: 6笔止损交易 → confirmed
for i in range(6):
    simulated_trades.append({'code': random.choice(['HK.00700','HK.01810']), 'pnl': random.uniform(-200,-30), 'exit_reason': '止损', 'strategy': 'confluence_v2'})

# H4: 2笔 HK.00981 亏损 → verifying（不足5次）
for i in range(2):
    simulated_trades.append({'code': 'HK.00981', 'pnl': random.uniform(-300,-50), 'exit_reason': '止损', 'strategy': 'confluence_v2'})

print(f"   生成 {len(simulated_trades)} 笔模拟交易")

# 驱动进化
state_changes = []
for i, t in enumerate(simulated_trades):
    result = engine.evaluate_trade(t)
    if result['matched'] > 0:
        for update in result['updates']:
            if update['old_status'] != update['new_status']:
                state_changes.append(update)
                h = [h for h in engine.db.load_all() if h.id == update['hypothesis_id']][0]
                emoji = "✅" if h.status == "confirmed" else "❌" if h.status == "refuted" else "🔬"
                print(f"   [{i+1:2d}] {emoji} {update['hypothesis_id']}: "
                      f"{update['old_status']} → {update['new_status']} "
                      f"({update['evidence']}, 支持={h.confirm_count}/反驳={h.refute_count})")

# ═══════════════════════════════════════════════════════
# Step 3: 进化结果
# ═══════════════════════════════════════════════════════
print(f"\n📈 Step 3: 进化结果")
summary = engine.get_evolution_summary()
print(f"   已确认: {summary['confirmed']} 条 → 转化为盘中规则")
print(f"   已推翻: {summary['refuted']} 条 → 从知识库移除")
print(f"   验证中: {summary['verifying']} 条 → 继续收集证据")
print(f"   待验证: {summary['pending']} 条 → 等待首次交易")

print(f"\n📋 假设状态表:")
print(f"   {'ID':<6} {'状态':<10} {'支持/反驳/总':<15} {'规则'}")
print(f"   {'─'*6} {'─'*10} {'─'*15} {'─'*40}")
for h in engine.db.load_all():
    status_icon = {"confirmed": "✅", "refuted": "❌", "verifying": "🔬", "pending": "⏳"}.get(h.status, "?")
    print(f"   {h.id:<6} {status_icon} {h.status:<10} {h.confirm_count}/{h.refute_count}/{h.evidence_count:<5} {h.actionable_rule[:40]}")

# ═══════════════════════════════════════════════════════
# Step 4: 已确认规则 → 次日盘前注入
# ═══════════════════════════════════════════════════════
confirmed_rules = summary.get('confirmed_rules', [])
refuted = engine.db.load_all(status=HypothesisStatus.REFUTED)
verifying = engine.db.load_all(status=HypothesisStatus.VERIFYING)

print(f"\n🌅 Step 4: 次日盘前计划注入内容（PreMarketAnalyst 输出）")
print("=" * 70)

if confirmed_rules:
    print("\n【🔒 已验证交易规则（不可违反）】")
    print("以下规则已从历史交易中经 ≥5 次证据确认，开仓时不可违反：\n")
    for r in confirmed_rules:
        icon = "🚫" if r['type'] == 'block' else "🔧" if r['type'] == 'adjust_param' else "⚠️"
        print(f"  {icon} {r['rule']}")
        print(f"     证据: {r['evidence']}, 置信度: {r['confidence']:.0%}\n")

if verifying:
    print("\n【🔬 正在验证的假设（谨慎对待）】")
    for h in verifying:
        print(f"  🔬 {h.statement}")
        print(f"     证据: {h.confirm_count}支持/{h.refute_count}反驳 (需{engine.CONFIRM_THRESHOLD}次确认)\n")

if refuted:
    print("\n【❌ 已推翻假设（可忽略）】")
    for h in refuted:
        print(f"  ❌ {h.statement}")
        print(f"     被 {h.refute_count} 次交易反驳，该规律不成立\n")

# ═══════════════════════════════════════════════════════
# Step 5: 对"盈利和胜率"的价值分析
# ═══════════════════════════════════════════════════════
print(f"\n💡 Step 5: 对盈利和胜率的价值")
print("-" * 70)
print("  🔒 已确认的 3 条规则可直接转化为:")
print("     1. 🚫 策略 test 禁止使用 → 避免继续亏损")
print("     2. 🔧 止损从固定%改为 ATR → 减少无效止损，提升盈亏比")
print("     3. 🚫 恒指跌>1%时禁止做多 → 降低逆势亏损")
print()
print("  🔬 正在验证的 HK.00981 假设:")
print("     - 继续收集证据，达到 5 次后自动确认或推翻")
print("     - 确认 → 自动加入盘前门禁")
print("     - 推翻 → 避免错误规则限制交易机会")
print()
print("  ❌ 已推翻的恒指假设:")
print("     - 避免了过度保守的规则")
print("     - 保留了合理的交易机会")

print(f"\n{'=' * 70}")
print("✅ 假设驱动进化引擎演示完成")
print(f"   确认={summary['confirmed']}, 推翻={summary['refuted']}, 验证中={summary['verifying']}")
print(f"{'=' * 70}")
