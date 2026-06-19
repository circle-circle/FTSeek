"""
FTSeek-US 核心调度器 - 任务管理 (P2-2 已迁移至 app.pipeline)

⚠️ DEPRECATED: 本模块所有实现已迁移到 app/pipeline/ 目录。
本文件保留为向后兼容桩，所有导入自动重定向到 pipeline。

迁移映射:
  - signal_pipeline.py      # 信号扫描 + 决策管线 (scan_loop, _process_symbol_impl, ...)
  - order_executor.py       # 下单执行 + 仓位管理 (_submit_order, _close_position, ...)
  - position_monitor.py     # 持仓监控 + 止损止盈 + 对账 (monitor_loop, _check_positions, ...)
  - llm_pipeline.py         # LLM 策略 + 盘前计划 + 盘后推荐 (run_llm_strategy_scan, ...)

新代码应从 app.pipeline.* 导入。
"""

import logging
import warnings

logger = logging.getLogger(__name__)

warnings.warn(
    "scheduler_tasks 已废弃，请使用 app.pipeline.* 导入",
    DeprecationWarning,
    stacklevel=2,
)

# ── signal_pipeline ──────────────────────────────────────────────────
from app.pipeline.signal_pipeline import (
    scan_loop,
    run_signal_scan,
    process_symbol,
    _run_signal_scan_impl,
    _generate_technical_signal,
)

# ── order_executor ───────────────────────────────────────────────────
from app.pipeline.order_executor import (
    _submit_order,
    _close_position,
    _get_current_price,
    _get_order_qty,
    _close_position_for_conflict,
    _close_short_for_long,
    _close_long_for_short,
    _try_direct_short_only,
    _try_direct_short_with_fallback,
    _update_trade_attribution,
    _insert_trade_attribution,
    _notify_order,
    _notify_exit,
)

# ── position_monitor ─────────────────────────────────────────────────
from app.pipeline.position_monitor import (
    monitor_loop,
    restore_positions_from_db,
    _update_capital_safety,
    _check_positions,
    _notify_risk_blocked,
)

# ── llm_pipeline ─────────────────────────────────────────────────────
from app.pipeline.llm_pipeline import (
    run_llm_strategy_scan,
    run_llm_backtest,
    run_after_hours_llm_scan,
    run_premarket_planning,
    run_open_checker,
    _fallback_to_finnhub_news,
    _generate_degraded_recommendation,
    _send_rsshub_alert,
    _merge_agent_plan_into_battle_plan,
    _deduplicate_strings,
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
    "_update_capital_safety", "_check_positions",
    "_notify_risk_blocked",
    # llm_pipeline
    "run_llm_strategy_scan", "run_llm_backtest",
    "run_after_hours_llm_scan", "run_premarket_planning", "run_open_checker",
    "_fallback_to_finnhub_news", "_generate_degraded_recommendation",
    "_send_rsshub_alert",
    "_merge_agent_plan_into_battle_plan", "_deduplicate_strings",
]
