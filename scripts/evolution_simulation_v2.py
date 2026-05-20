#!/usr/bin/env python3
"""
模拟盘进化循环验证 v2 — 主动生成模拟交易驱动假设确认

由于当前只有 3 笔唯一已平仓交易，我们用模拟交易来充分验证假设引擎的
pending → verifying → confirmed 完整进化路径。
"""
import sys, os, json, time
os.chdir('/opt/ftseek/FTSeek')
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.review.hypothesis_engine import get_hypothesis_engine, HypothesisStatus, Hypothesis

print("=" * 70)
print("模拟盘进化循环验证 v2 — 主动生成交易驱动假设确认")
print("=" * 70)

# 清空旧假设 DB，从零开始
db_path = '/opt/ftseek/FTSeek/data/hypothesis.db'
if os.path.exists(db_path):
    os.remove(db_path)

engine = get_hypothesis_engine()

# ═══════════════════════════════════════════════════════
# Step 1: 手动创建假设（模拟 PatternMiner 产出）
# ═══════════════════════════════════════════════════════
print("\n🧪 Step 1: 创建假设")
now = "2026-05-10 16:30:00"

hypotheses_data = [
    {
        "id": "hyp_001",
        "statement": "HK.00981 午后(13:00+)追高(RSI>65)买入胜率低",
        "source": "pattern_mining",
        "category": "stock_time",
        "trigger_conditions": {"conditions": ["code=HK.00981", "time=afternoon", "RSI>65"]},
        "predicted_outcome": "胜率<30%, 平均亏损>2%",
        "evidence_count": 0,
        "confidence": 0.5,
        "status": HypothesisStatus.PENDING,
        "actionable_rule": "HK.00981 午后 RSI>65 时禁止开仓",
        "rule_type": "block",
    },
    {
        "id": "hyp_002",
        "statement": "策略 test 胜率 0% 应立即暂停",
        "source": "pattern_mining",
        "category": "strategy",
        "trigger_conditions": {"conditions": ["strategy=test"]},
        "predicted_outcome": "胜率0%, 累计亏损-762600",
        "evidence_count": 0,
        "confidence": 0.5,
        "status": HypothesisStatus.PENDING,
        "actionable_rule": "策略 test 禁止使用",
        "rule_type": "block",
    },
    {
        "id": "hyp_003",
        "statement": "止损在入场后 15min 内触发说明止损太紧",
        "source": "pattern_mining",
        "category": "stop_loss",
        "trigger_conditions": {"conditions": ["exit_reason=止损", "holding<15min"]},
        "predicted_outcome": "100% 止损在 15min 内触发",
        "evidence_count": 0,
        "confidence": 0.5,
        "status": HypothesisStatus.PENDING,
        "actionable_rule": "止损从固定%改为 ATR 止损",
        "rule_type": "adjust_param",
    },
    {
        "id": "hyp_004",
        "statement": "恒指跌>1%的日子做多胜率仅18%",
        "source": "llm_analysis",
        "category": "market_regime",
        "trigger_conditions": {"conditions": ["HSI_change<-1%", "action=BUY"]},
        "predicted_outcome": "胜率<20%",
        "evidence_count": 0,
        "confidence": 0.5,
        "status": HypothesisStatus.PENDING,
        "actionable_rule": "恒指跌>1%时禁止做多",
        "rule_type": "block",
    },
]

for hd in hypotheses_data:
    h = Hypothesis(created_at=now, updated_at=now, **{k: v for k, v in hd.items() if k != 'id'})
    h.id = hd['id']
    engine.db.save(h)
    print(f"   [{h.status}] {h.statement[:60]}...")

print(f"   共 {len(hypotheses_data)} 个假设，状态: PENDING")

# ═══════════════════════════════════════════════════════
# Step 2: 生成模拟交易数据驱动进化
# ═══════════════════════════════════════════════════════
print(f"\n🔄 Step 2: 生成模拟交易驱动假设进化")
print(f"   (CONFIRM_THRESHOLD={engine.CONFIRM_THRESHOLD}, REFUTE_THRESHOLD={engine.REFUTE_THRESHOLD})")

# 生成 30 笔模拟交易，覆盖 4 个假设的触发场景
import random
random.seed(42)

simulated_trades = []

# hyp_001: HK.00981 午后追高 — 生成 6 笔亏损交易（应确认）
for i in range(6):
    simulated_trades.append({
        'code': 'HK.00981',
        'pnl': random.uniform(-300, -50),
        'exit_reason': '止损',
        'strategy': 'confluence_v2',
        'note': f'午后追高#{i+1}'
    })

# hyp_002: test 策略 — 生成 6 笔亏损交易（应确认）
for i in range(6):
    simulated_trades.append({
        'code': random.choice(['HK.00700', 'HK.02269', 'HK.09988']),
        'pnl': random.uniform(-500, -100),
        'exit_reason': '止损',
        'strategy': 'test',
        'note': f'test策略#{i+1}'
    })

# hyp_003: 止损太紧 — 生成 6 笔止损交易（应确认）
for i in range(6):
    simulated_trades.append({
        'code': random.choice(['HK.00700', 'HK.01810', 'HK.00941']),
        'pnl': random.uniform(-200, -30),
        'exit_reason': '止损',
        'strategy': 'confluence_v2',
        'note': f'止损触发#{i+1}'
    })

# hyp_004: 恒指弱势日做多 — 生成 3 笔亏损 + 3 笔盈利（应推翻）
for i in range(3):
    simulated_trades.append({
        'code': random.choice(['HK.00700', 'HK.02269']),
        'pnl': random.uniform(-400, -80),
        'exit_reason': '止损',
        'strategy': 'confluence_v2',
        'note': f'恒指弱势做多#{i+1}'
    })
for i in range(3):
    simulated_trades.append({
        'code': random.choice(['HK.00700', 'HK.02269']),
        'pnl': random.uniform(50, 300),
        'exit_reason': '止盈',
        'strategy': 'confluence_v2',
        'note': f'恒指弱势做多盈利#{i+1}'
    })

# 额外：不匹配任何假设的交易（不应影响假设）
for i in range(5):
    simulated_trades.append({
        'code': 'HK.00941',
        'pnl': random.uniform(50, 200),
        'exit_reason': '止盈',
        'strategy': 'confluence_v2',
        'note': f'正常盈利#{i+1}'
    })

print(f"   生成 {len(simulated_trades)} 笔模拟交易")

# ═══════════════════════════════════════════════════════
# Step 3: 驱动进化循环
# ═══════════════════════════════════════════════════════
print(f"\n🔄 Step 3: 逐笔交易驱动假设进化")
print("-" * 70)

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
# Step 4: 进化结果
# ═══════════════════════════════════════════════════════
print(f"\n📈 Step 4: 进化结果")
summary = engine.get_evolution_summary()
print(f"   总假设: {summary['total']}")
print(f"   待验证: {summary['pending']}")
print(f"   验证中: {summary['verifying']}")
print(f"   已确认: {summary['confirmed']}")
print(f"   已推翻: {summary['refuted']}")

print(f"\n📋 假设详情:")
for h in engine.db.load_all():
    if h.status == "confirmed":
        icon = "✅"
    elif h.status == "refuted":
        icon = "❌"
    elif h.status == "verifying":
        icon = "🔬"
    else:
        icon = "⏳"
    print(f"   {icon} [{h.status:10}] {h.statement[:55]}...")
    print(f"        支持={h.confirm_count}/反驳={h.refute_count}/总={h.evidence_count} | 规则: {h.actionable_rule[:40]}...")

# ═══════════════════════════════════════════════════════
# Step 5: 已确认规则 → 盘前注入
# ═══════════════════════════════════════════════════════
confirmed_rules = summary.get('confirmed_rules', [])
if confirmed_rules:
    print(f"\n🔒 Step 5: 已确认规则（次日盘前注入）")
    for r in confirmed_rules:
        icon = "🚫" if r['type'] == 'block' else "⚠️" if r['type'] == 'warn' else "🔧"
        print(f"   {icon} {r['rule']}")
        print(f"        证据: {r['evidence']}, 置信度: {r['confidence']:.0%}")
else:
    print(f"\n⏳ 尚无已确认规则")

# ═══════════════════════════════════════════════════════
# Step 6: 模拟次日盘前计划注入
# ═══════════════════════════════════════════════════════
print(f"\n🌅 Step 6: 模拟次日盘前计划注入内容")
print("-" * 70)

parts = []
if confirmed_rules:
    parts.append("\n【🔒 已验证交易规则（不可违反）】")
    for r in confirmed_rules:
        icon = "🚫" if r['type'] == 'block' else "🔧"
        parts.append(f"  {icon} {r['rule']}")
        parts.append(f"     证据: {r['evidence']}, 置信度: {r['confidence']:.0%}")

refuted = engine.db.load_all(status=HypothesisStatus.REFUTED)
if refuted:
    parts.append("\n【❌ 已推翻假设（可忽略）】")
    for h in refuted:
        parts.append(f"  ❌ {h.statement}")
        parts.append(f"     被 {h.refute_count} 次交易反驳")

verifying = engine.db.load_all(status=HypothesisStatus.VERIFYING)
if verifying:
    parts.append("\n【🔬 正在验证的假设（谨慎对待）】")
    for h in verifying:
        parts.append(f"  🔬 {h.statement}")
        parts.append(f"     证据: {h.confirm_count}支持/{h.refute_count}反驳 (需{engine.CONFIRM_THRESHOLD}次确认)")

if parts:
    print("\n".join(parts))

print(f"\n{'=' * 70}")
print("✅ 模拟盘进化循环验证完成")
print(f"   已确认 {summary['confirmed']} 条规则，已推翻 {summary['refuted']} 条假设")
print(f"{'=' * 70}")
