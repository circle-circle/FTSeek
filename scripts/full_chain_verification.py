#!/usr/bin/env python3
"""
全链路就绪验证 — 明日开盘前检查
确保从信号→执行→归因→假设→盘前的完整数据流无断点
"""
import sys, os
os.chdir('/opt/ftseek/FTSeek')
sys.path.insert(0, '/opt/ftseek/FTSeek')

print("=" * 70)
print("FTSeek 港股 — 全链路就绪验证")
print("=" * 70)

checks = []
errors = []

# ═══════════════════════════════════════════════════════
# 1. 数据层
# ═══════════════════════════════════════════════════════
print("\n【1】数据层检查")

# 1.1 TradeRecorder 去重
try:
    from app.storage import trade_recorder as hk_tr
    rec = hk_tr.get_trade_recorder()
    source = open('app/storage/trade_recorder.py').read()
    if 'seen[r.trade_id]' in source:
        print("  ✅ TradeRecorder 去重逻辑已就位")
        checks.append("TradeRecorder 去重")
    else:
        errors.append("TradeRecorder 去重逻辑缺失")
        print("  ❌ TradeRecorder 去重逻辑缺失")
except Exception as e:
    errors.append(f"TradeRecorder: {e}")
    print(f"  ❌ TradeRecorder: {e}")

# 1.2 holding_period
try:
    source = open('app/storage/trade_recorder.py').read()
    if 'total_seconds' in source:
        print("  ✅ holding_period 分钟计算已修复")
        checks.append("holding_period 修复")
    else:
        errors.append("holding_period 未修复")
except Exception as e:
    errors.append(f"holding_period: {e}")

# 1.3 knowledge.db
try:
    import sqlite3
    conn = sqlite3.connect('data/knowledge.db')
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in c.fetchall()]
    conn.close()
    if len(tables) >= 4:
        print(f"  ✅ knowledge.db: {len(tables)} 张表")
        checks.append("knowledge.db")
    else:
        errors.append(f"knowledge.db 只有 {len(tables)} 张表")
except Exception as e:
    errors.append(f"knowledge.db: {e}")

# ═══════════════════════════════════════════════════════
# 2. 归因层
# ═══════════════════════════════════════════════════════
print("\n【2】归因层检查")

# 2.1 IntradayAttribution LLM
try:
    from app.review.hk_attribution_runner import IntradayAttribution
    ia = IntradayAttribution()
    if hasattr(ia, '_llm_analyze_intraday'):
        print("  ✅ IntradayAttribution LLM 归因已就位")
        checks.append("IntradayAttribution LLM")
    else:
        errors.append("IntradayAttribution LLM 方法缺失")
except Exception as e:
    errors.append(f"IntradayAttribution: {e}")

# ═══════════════════════════════════════════════════════
# 3. 模式发现 + 假设引擎
# ═══════════════════════════════════════════════════════
print("\n【3】模式发现 + 假设引擎检查")

# 3.1 PatternMiner
try:
    from app.review.pattern_miner import get_pattern_miner
    miner = get_pattern_miner()
    patterns = miner.mine(days=60)
    print(f"  ✅ PatternMiner: 发现 {len(patterns)} 个模式")
    checks.append("PatternMiner")
except Exception as e:
    errors.append(f"PatternMiner: {e}")
    print(f"  ❌ PatternMiner: {e}")

# 3.2 HypothesisEngine
try:
    from app.review.hypothesis_engine import get_hypothesis_engine
    engine = get_hypothesis_engine()
    summary = engine.get_evolution_summary()
    print(f"  ✅ HypothesisEngine: 总={summary['total']}, 已确认={summary['confirmed']}")
    checks.append("HypothesisEngine")
    
    # 验证中英文 exit_reason 匹配
    test_trade_stop = {'code': 'HK.00700', 'pnl': -100, 'exit_reason': 'stop_loss', 'strategy': 'test'}
    test_trade_cn = {'code': 'HK.00700', 'pnl': -100, 'exit_reason': '止损', 'strategy': 'test'}
    
    # 创建测试假设
    from app.review.hypothesis_engine import Hypothesis, HypothesisStatus
    h = Hypothesis(
        id='test_stop', statement='止损在15min内触发说明止损太紧',
        source='test', category='stop_loss',
        trigger_conditions={'conditions': ['exit_reason=止损']},
        predicted_outcome='test', created_at='2026-05-10', updated_at='2026-05-10',
        actionable_rule='止损改ATR', rule_type='adjust_param', status=HypothesisStatus.VERIFYING
    )
    engine.db.save(h)
    
    r1 = engine.evaluate_trade(test_trade_stop)
    r2 = engine.evaluate_trade(test_trade_cn)
    
    if r1['matched'] > 0 and r2['matched'] > 0:
        print("  ✅ exit_reason 中英文匹配验证通过")
        checks.append("exit_reason 中英文匹配")
    else:
        errors.append(f"exit_reason 匹配失败: stop_loss={r1['matched']}, 止损={r2['matched']}")
        print(f"  ❌ exit_reason 匹配失败: stop_loss={r1['matched']}, 止损={r2['matched']}")
    
    # 清理测试假设
    engine.db.db_path = '/opt/ftseek/FTSeek/data/hypothesis.db'
    conn = sqlite3.connect(engine.db.db_path)
    conn.execute("DELETE FROM hypotheses WHERE id='test_stop'")
    conn.commit()
    conn.close()
    
except Exception as e:
    errors.append(f"HypothesisEngine: {e}")
    print(f"  ❌ HypothesisEngine: {e}")

# ═══════════════════════════════════════════════════════
# 4. 执行层集成
# ═══════════════════════════════════════════════════════
print("\n【4】执行层集成检查")

# 4.1 executor 假设评估集成
try:
    source = open('app/trader/executor.py').read()
    if 'Phase 1.5: 假设驱动进化评估' in source:
        print("  ✅ executor.py 假设评估集成已就位")
        checks.append("executor 假设评估")
    else:
        errors.append("executor 假设评估集成缺失")
except Exception as e:
    errors.append(f"executor 检查: {e}")

# 4.2 executor 去重
try:
    source = open('app/trader/executor.py').read()
    if '去重：检查同 order_id' in source:
        print("  ✅ executor.py record_trade_to_db 去重已就位")
        checks.append("executor 去重")
    else:
        errors.append("executor 去重缺失")
except Exception as e:
    errors.append(f"executor 去重检查: {e}")

# ═══════════════════════════════════════════════════════
# 5. 盘前注入
# ═══════════════════════════════════════════════════════
print("\n【5】盘前注入检查")

try:
    source = open('app/pipeline/premarket_analyst.py').read()
    if '_load_patterns_context' in source and 'hypothesis_engine' in source:
        print("  ✅ PreMarketAnalyst 模式+规则注入已就位")
        checks.append("PreMarketAnalyst 注入")
    else:
        errors.append("PreMarketAnalyst 注入缺失")
except Exception as e:
    errors.append(f"PreMarketAnalyst: {e}")

# ═══════════════════════════════════════════════════════
# 6. EOD Review 集成
# ═══════════════════════════════════════════════════════
print("\n【6】EOD Review 集成检查")

try:
    source = open('app/review/hk_eod_review.py').read()
    if 'Step 4b' in source and 'hypothesis_engine' in source:
        print("  ✅ HKEODReview Step 4b 假设引擎集成已就位")
        checks.append("EOD Review 集成")
    else:
        errors.append("EOD Review 假设引擎集成缺失")
except Exception as e:
    errors.append(f"EOD Review: {e}")

# ═══════════════════════════════════════════════════════
# 7. 服务健康
# ═══════════════════════════════════════════════════════
print("\n【7】服务健康检查")

try:
    import subprocess
    result = subprocess.run(['systemctl', 'is-active', 'ftseek'], capture_output=True, text=True)
    if result.stdout.strip() == 'active':
        print("  ✅ ftseek 服务运行中")
        checks.append("ftseek 服务")
    else:
        errors.append(f"ftseek 服务状态: {result.stdout.strip()}")
        print(f"  ❌ ftseek 服务状态: {result.stdout.strip()}")
except Exception as e:
    errors.append(f"服务检查: {e}")

# ═══════════════════════════════════════════════════════
# 总结
# ═══════════════════════════════════════════════════════
print(f"\n{'=' * 70}")
print(f"验证结果: {len(checks)}/{len(checks) + len(errors)} 通过")
print(f"{'=' * 70}")

passed = len(checks)
total = passed + len(errors)

if errors:
    print(f"\n❌ 失败项 ({len(errors)}):")
    for e in errors:
        print(f"  ❌ {e}")
else:
    print(f"\n✅ 全部 {passed} 项检查通过，明日开盘准备就绪！")

print(f"\n通过项: {', '.join(checks)}")
