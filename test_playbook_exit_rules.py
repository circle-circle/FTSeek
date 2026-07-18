#!/usr/bin/env python3
"""快速验证 ADR-008 在 Playbook 退出规则中的开关 (2026-06-28)"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.playbook.pipeline import _check_playbook_exit_rules


def test_exit_rules():
    meta = {"_log_tag": "test", "_symbol": "TEST", "cohort": "A", "horizon": "short"}

    # SL 应该始终触发
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=97.5,
        initial_sl=98, peak_price=100, meta=meta,
        held_minutes=180, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    # 2026-07-14: SL_FIXED_PCT 拆分为 SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK.
    # 无 breakeven (peak 未达 0.5%) → 命中原始 SL → SL_INITIAL.
    assert reason == "SL_INITIAL", f"SL 未触发, got {reason}"
    print(f"✅ SL 触发: {reason}")

    # TIME_EXIT 已重新启用 (2026-07-07): PLAYBOOK 仓位由 Pipeline 自管 time_exit.
    # 2026-07-16: default time_exit_hours 已升到 6h, 且 time_exit_only_if_loss=True.
    # 持仓 7h (>6h) 且亏损 (99.5<100) → 触发 TIME_EXIT.
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.5,
        initial_sl=90, peak_price=99.5, meta=meta,
        held_minutes=420, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "TIME_EXIT", f"TIME_EXIT 应被启用, got {reason}"
    print(f"✅ TIME_EXIT 已启用: {reason}")

    # breakeven ratchet 已启用 (factor_exit_rules 默认 breakeven_at_pct=0.5):
    # peak_profit=1.5% >= 0.5%, effective_sl 被上提到净保本线之上 (drawdown lock).
    # current_price=99 触及上提后的 SL → SL_TRAIL_LOCK (2026-07-14 标签拆分).
    reason, exit_price, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.0,
        initial_sl=90, peak_price=101.5, meta=meta,
        held_minutes=30, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "SL_TRAIL_LOCK", f"breakeven ratchet 应上提 effective_sl 触发止损, got {reason}"
    print(f"✅ breakeven ratchet 已启用: {reason}")

    print("\n✅ Playbook 退出规则开关验证通过")


if __name__ == "__main__":
    test_exit_rules()
