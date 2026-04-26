"""
Position Conflict Resolver - End-to-End Tests

Tests all decision scenarios:
1. Empty positions -> APPROVE
2. Full + extremely strong signal -> REPLACE
3. Full + medium signal + insufficient gap -> DEFER
4. Full + stop loss triggered -> FORCE REPLACE
5. Weak signal -> REJECT
6. Near take profit -> PARTIAL SELL
7. Daily replace limit -> DEFER
8. Available slot -> APPROVE

Run: python3 scripts/test_position_conflict_resolver.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.factors.position_conflict_resolver import (
    PositionConflictResolver, ConflictDecision,
    score_position_from_data, score_new_signal_from_data,
    format_conflict_alert,
)


def make_positions(**overrides):
    """Helper to create standard 4-position portfolio"""
    defaults = {'take_profit_pct': 20.0, 'stop_loss_pct': -8.0}
    defaults.update(overrides)
    return [
        score_position_from_data('HK.00883', '中国海洋石油', -5.0, 25.0, 26.3, 48,
            'MEDIUM', 35, -1.2, **defaults),
        score_position_from_data('HK.02318', '中国平安', 2.0, 63.8, 62.5, 12,
            'HIGH', 60, 0.8, **defaults),
        score_position_from_data('HK.09988', '阿里巴巴-W', 8.0, 130.7, 121.0, 6,
            'HIGH', 75, 2.5, **defaults),
        score_position_from_data('HK.00700', '腾讯控股', 12.0, 509.0, 454.5, 4,
            'HIGH', 85, 3.0, **defaults),
    ]


def run_tests():
    results = []
    r = PositionConflictResolver(max_positions=4, max_replace_per_day=2)

    def test(name, expected, actual, details=""):
        ok = actual == expected
        results.append((name, ok))
        status = "✅" if ok else "❌"
        print(f'{status} {name}: {actual.value} (expected={expected.value})')
        if details:
            print(f'   {details}')
        return ok

    print("=" * 60)
    print("Position Conflict Resolver - E2E Tests")
    print("=" * 60)

    # Test 1: Empty -> approve
    new = score_new_signal_from_data('HK.00700', '腾讯', 'STRONG_BUY', 'HIGH', 'IMMEDIATE',
        'AI', 0.9, 180, 'core', 'US.NVDA', True)
    result = r.resolve(new, [])
    test("空仓批准", ConflictDecision.HOLD, result.decision,
         f"reason: {result.reason}")

    # Test 2: Full + extremely strong -> replace
    positions = make_positions()
    new2 = score_new_signal_from_data('HK.01810', '小米', 'STRONG_BUY', 'HIGH', 'IMMEDIATE',
        'AI', 0.95, 180, 'core', 'US.NVDA', True)
    result2 = r.resolve(new2, positions)
    test("满仓+极强替换", ConflictDecision.REPLACE, result2.decision,
         f"new={result2.new_signal_score:.1f} vs weakest={result2.weakest_position.code}({result2.weakest_position_score:.1f})")

    # Test 3: Full + medium + gap insufficient -> defer
    positions3 = make_positions()
    new3 = score_new_signal_from_data('HK.02020', '安踏', 'BUY', 'MEDIUM', 'WAIT_CONFIRM',
        'consumer', 0.6, 120, 'satellite')
    result3 = r.resolve(new3, positions3)
    test("满仓+差距不够延迟", ConflictDecision.DEFER, result3.decision,
         f"gap={result3.new_signal_score - result3.weakest_position_score:.1f}")

    # Test 4: Stop loss -> force replace (any signal strength)
    positions4 = make_positions()
    # Replace first position with stop-loss-hit one
    positions4[0] = score_position_from_data('HK.01211', '比亚迪', -10.0, 180.0, 200.0, 72,
        'LOW', 20, -3.0, stop_loss_pct=-8.0, take_profit_pct=20.0)
    new4 = score_new_signal_from_data('HK.01024', '快手', 'BUY', 'LOW', 'MONITOR_ONLY',
        'tech', 0.3, 60, 'satellite')
    result4 = r.resolve(new4, positions4)
    test("止损强制替换", ConflictDecision.REPLACE, result4.decision,
         f"stop_loss={result4.weakest_position.stop_loss_hit}, signal={result4.new_signal_score:.1f}")

    # Test 5: Weak signal -> reject
    new5 = score_new_signal_from_data('HK.01888', '建滔', 'BUY', 'LOW', 'MONITOR_ONLY',
        'minor', 0.2, 60, 'satellite', news_age_minutes=90)
    result5 = r.resolve(new5, positions)
    test("信号太弱拒绝", ConflictDecision.REJECT, result5.decision,
         f"score={result5.new_signal_score:.1f}")

    # Test 6: Near take profit -> partial sell
    positions6 = make_positions()
    positions6[0] = score_position_from_data('HK.00883', '中海油', 17.0, 29.25, 25.0, 24,
        'HIGH', 80, 1.5, take_profit_pct=20.0)
    new6 = score_new_signal_from_data('HK.01810', '小米', 'STRONG_BUY', 'HIGH', 'IMMEDIATE',
        'product', 0.85, 180, 'core', 'US.AAPL', True)
    result6 = r.resolve(new6, positions6)
    test("接近止盈部分减持", ConflictDecision.PARTIAL_SELL, result6.decision,
         f"target={result6.weakest_position.code} PnL={result6.weakest_position.pnl_pct}%")

    # Test 7: Daily replace limit -> defer
    r2 = PositionConflictResolver(max_positions=4, max_replace_per_day=1)
    # Manually increment to simulate daily limit hit
    r2._replace_count_today = 1
    positions7 = make_positions()
    new7 = score_new_signal_from_data('HK.01810', '小米', 'STRONG_BUY', 'HIGH', 'IMMEDIATE',
        'AI', 0.95, 180, 'core', 'US.NVDA', True)
    result7 = r2.resolve(new7, positions7)
    test("每日替换上限", ConflictDecision.DEFER, result7.decision,
         f"replaced_today={r2._replace_count_today}, limit={r2.max_replace_per_day}")

    # Test 8: Available slot -> approve
    r3 = PositionConflictResolver(max_positions=4)
    positions8 = make_positions()[:2]  # Only 2 positions
    new8 = score_new_signal_from_data('HK.01810', '小米', 'BUY', 'MEDIUM', 'WAIT_CONFIRM',
        'product', 0.7, 120, 'satellite')
    result8 = r3.resolve(new8, positions8)
    test("有空余仓位", ConflictDecision.HOLD, result8.decision,
         f"positions={len(positions8)}/4")

    # Test 9: Gap exactly at threshold -> replace
    # Need to craft positions where gap is exactly 15
    positions9 = [
        score_position_from_data('HK.00883', '中海油', 0.0, 25.0, 25.0, 24, 'MEDIUM', 40, 0.0, take_profit_pct=20.0),
        score_position_from_data('HK.02318', '平安', 5.0, 66.0, 62.8, 12, 'HIGH', 65, 1.0, take_profit_pct=20.0),
        score_position_from_data('HK.09988', '阿里', 7.0, 130.0, 121.5, 6, 'HIGH', 70, 1.5, take_profit_pct=20.0),
        score_position_from_data('HK.00700', '腾讯', 10.0, 505.0, 459.0, 4, 'HIGH', 80, 2.0, take_profit_pct=20.0),
    ]
    new9 = score_new_signal_from_data('HK.01810', '小米', 'BUY', 'HIGH', 'IMMEDIATE',
        'AI', 0.85, 180, 'core', 'US.NVDA', True)
    result9 = r.resolve(new9, positions9)
    gap = result9.new_signal_score - result9.weakest_position_score
    test(f"差距刚好达标 (gap={gap:.1f})", ConflictDecision.REPLACE, result9.decision,
         f"new={result9.new_signal_score:.1f} vs weakest={result9.weakest_position_score:.1f}")

    # Summary
    passed = sum(1 for _, ok in results if ok)
    print(f'\n{"=" * 60}')
    print(f'结果: {passed}/{len(results)} 通过')
    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f'  {status} {name}')

    if passed == len(results):
        print(f'\n🎉 全部通过!')
        return 0
    else:
        print(f'\n⚠️ {len(results) - passed} 个测试失败')
        return 1


if __name__ == "__main__":
    sys.exit(run_tests())
