"""FTSeek-US Pipeline 模块

从 scheduler_tasks.py 拆分而来的核心管线:
- signal_pipeline: 信号扫描 + 决策管线
- order_executor: 下单执行 + 仓位管理
- position_monitor: 持仓监控 + 止损止盈 + 对账
- llm_pipeline: LLM 策略 + 盘前计划 + 盘后推荐
"""

from app.pipeline.signal_pipeline import (
    scan_loop, run_signal_scan, process_symbol,
    _run_signal_scan_impl,
)
from app.pipeline.order_executor import (
    _submit_order, _close_position, _get_current_price,
    _get_order_qty,
    _close_position_for_conflict, _close_short_for_long, _close_long_for_short,
    _try_direct_short_only, _try_direct_short_with_fallback,
    _update_trade_attribution, _insert_trade_attribution,
    _notify_order, _notify_exit,
)
from app.pipeline.position_monitor import (
    monitor_loop, restore_positions_from_db,
    _notify_risk_blocked, _update_capital_safety,
    _check_positions,
)
from app.pipeline.llm_pipeline import (
    run_llm_strategy_scan, run_llm_backtest,
    run_after_hours_llm_scan, run_premarket_planning, run_open_checker,
    _fallback_to_finnhub_news, _generate_degraded_recommendation, _send_rsshub_alert,
    _merge_agent_plan_into_battle_plan, _deduplicate_strings,
)

__all__ = [
    # signal_pipeline
    "scan_loop", "run_signal_scan", "process_symbol",
    "_run_signal_scan_impl", "_generate_technical_signal",
    # order_executor
    "_submit_order", "_close_position", "_get_current_price",
    "_get_order_qty",
    "_close_position_for_conflict", "_close_short_for_long", "_close_long_for_short",
    "_try_direct_short_only", "_try_direct_short_with_fallback",
    "_update_trade_attribution", "_insert_trade_attribution",
    "_notify_order", "_notify_exit",
    # position_monitor
    "monitor_loop", "restore_positions_from_db",
    "_notify_risk_blocked", "_update_capital_safety",
    "_check_positions",
    # llm_pipeline
    "run_llm_strategy_scan", "run_llm_backtest",
    "run_after_hours_llm_scan", "run_premarket_planning", "run_open_checker",
    "_fallback_to_finnhub_news", "_generate_degraded_recommendation", "_send_rsshub_alert",
    "_merge_agent_plan_into_battle_plan", "_deduplicate_strings",
]
