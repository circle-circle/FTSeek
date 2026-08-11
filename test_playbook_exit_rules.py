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
    # 2026-07-24 (规则3.4): 此场景 (持仓7h, peak浮盈仅-0.5%从未达0.5%, 当前水下)
    #   原由 MFE_STALL_EXIT 在 150min 处接管.
    # 2026-08-08 (规则3.3): DEAD_SIGNAL_EXIT (60min, peak<0.2%) 更早接管 —
    #   死信号退出优先于 MFE_STALL, 语义一致 (方向从未兑现).
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.5,
        initial_sl=90, peak_price=99.5, meta=meta,
        held_minutes=420, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "DEAD_SIGNAL_EXIT", f"低MFE死单应被死信号退出接管, got {reason}"
    print(f"✅ 死信号退出接管低MFE死单: {reason}")

    # 规则3.3 只抓 MFE<0.2% 极端死信号; peak 0.3% (≥0.2% 但 <0.5%) 时 60min 不触发,
    #   150min 处 MFE_STALL 仍接管 — 但注意: 浅亏(>-1%) 单被 8/4 浅亏持有转 SL/PRE_CLOSE,
    #   深亏(< -1%) 才立即退. 用深亏场景验证.
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=98.5,
        initial_sl=90, peak_price=100.3, meta=meta,
        held_minutes=200, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "MFE_STALL_EXIT", f"peak 0.3% 深亏死单应由停滞退出接管, got {reason}"
    print(f"✅ MFE停滞退出接管 peak0.3% 深亏死单: {reason}")
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=100.5,
        initial_sl=90, peak_price=101.0, meta=meta,
        held_minutes=420, minutes_to_close=30, minutes_from_open=20, entry_time_str=""
    )
    assert reason == "", f"保本线上方盈利仓不应退出, got {reason}"
    print(f"✅ 盈利仓不受停滞/时间退出影响")

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

    # ── RFC-064 (2026-08-11): TIME_EXIT 隔夜每日重置 ──
    # 隔离其他规则: peak 0.3% (≥0.2 避 DEAD_SIGNAL; <0.5 避 breakeven),
    # 浅亏 -0.1% (>-1%, MFE_STALL 浅亏递延), initial_sl 远 (不触 SL).
    from datetime import datetime, timedelta
    from app.playbook.pipeline import _now_et as _et_tz
    _yesterday_et = (datetime.now(_et_tz()) - timedelta(days=1)) \
        .replace(hour=12, minute=41, second=0, microsecond=0).isoformat()

    # ① 隔夜仓开盘 (8/11 TSM 复盘场景): wall-clock 已 1249min > 6h,
    #    但重置钟仅 1min → 不得 TIME_EXIT; 其余规则均不命中 → 不退出.
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.9,
        initial_sl=90, peak_price=100.3, meta=dict(meta),
        held_minutes=1249, minutes_to_close=300, minutes_from_open=1,
        entry_time_str=_yesterday_et,
    )
    assert reason == "", f"RFC-064 隔夜仓开盘预算重置, 不应退出, got {reason}"
    print("✅ RFC-064 隔夜仓开盘不秒杀 (重置钟 1min < 6h)")

    # ② 隔夜仓当日预算耗尽: 开盘以来 400min > 6h 且水下 → TIME_EXIT.
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.9,
        initial_sl=90, peak_price=100.3, meta=dict(meta),
        held_minutes=1249, minutes_to_close=90, minutes_from_open=400,
        entry_time_str=_yesterday_et,
    )
    assert reason == "TIME_EXIT", f"RFC-064 隔夜仓当日预算耗尽应 TIME_EXIT, got {reason}"
    print("✅ RFC-064 隔夜仓当日预算耗尽仍 TIME_EXIT")

    # ③ 日内单对照: 非隔夜 → wall-clock 不变 (held 420min > 6h 水下 → TIME_EXIT).
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100, current_price=99.9,
        initial_sl=90, peak_price=100.3, meta=dict(meta),
        held_minutes=420, minutes_to_close=90, minutes_from_open=20, entry_time_str="",
    )
    assert reason == "TIME_EXIT", f"日内单 wall-clock 行为不变, got {reason}"
    print("✅ RFC-064 日内单 wall-clock 行为不变")

    print("\n✅ Playbook 退出规则开关验证通过")


if __name__ == "__main__":
    test_exit_rules()
