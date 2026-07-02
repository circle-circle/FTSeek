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
    assert reason == "SL_FIXED_PCT", f"SL 未触发, got {reason}"
    print(f"✅ SL 触发: {reason}")

    # TIME_EXIT 已禁用: 持仓>2h 且亏损, 但没有其他规则触发, 应该不退出
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.5,
        initial_sl=90, peak_price=99.5, meta=meta,
        held_minutes=180, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "", f"TIME_EXIT 应被禁用, got {reason}"
    print(f"✅ TIME_EXIT 已禁用 (无退出): {reason!r}")

    # breakeven ratchet 已禁用: 价格触及 peak_profit=1.5% 后的 SL 不应上抬
    # 即 initial_sl 保持 90, 当前价 99 (亏损 1%), 不应触发 breakeven 上移的止损
    # 由于 breakeven ratchet 关闭, effective_sl = initial_sl = 90, 当前价 99 > 90, 不触发 SL
    reason, exit_price, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.0,
        initial_sl=90, peak_price=101.5, meta=meta,
        held_minutes=30, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "", f"breakeven ratchet 应被禁用, got {reason}"
    print(f"✅ breakeven ratchet 已禁用 (无退出): {reason!r}")

    print("\n✅ Playbook 退出规则开关验证通过")


if __name__ == "__main__":
    test_exit_rules()
