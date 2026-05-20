"""
FTSeek-US 核心调度器 - 兼容导入层

此文件保持向后兼容，将导入重定向到拆分后的模块。
新代码应该直接从 app.pipeline.* 或 scheduler_core、scheduler_triggers 导入。

架构:
- scheduler_core.py    - 核心调度逻辑
- app/pipeline/*        - 信号管线、订单执行、持仓监控、LLM 策略
- scheduler_triggers.py - 触发器
- scheduler.py          - 兼容导入层 (本文件)
"""
import logging
from pathlib import Path
import sys

# 路径设置
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger(__name__)

# =============================================================================
# 从拆分后的模块导入核心类
# =============================================================================

from .core.scheduler_core import (
    TradingScheduler,
    PositionStore,
    ET,
)

from app.pipeline.signal_pipeline import (
    scan_loop,
    run_signal_scan,
    process_symbol,
    _build_signal_from_vote,
)
from app.pipeline.order_executor import (
    _submit_order,
    _submit_put_option,
    _get_order_qty,
    _get_current_price,
    _close_position,
    _notify_order,
    _notify_exit,
)
from app.pipeline.position_monitor import (
    monitor_loop,
    restore_positions_from_db,
    _check_positions,
    _monitor_option_positions,
    _get_option_price,
    _close_option_position,
    _execute_option_close_order,
    _notify_risk_blocked,
)

from .core.scheduler_triggers import (
    is_market_open,
    is_in_cooldown,
    set_cooldown,
    check_daily_review,
)

# =============================================================================
# 向后兼容导出
# =============================================================================

__all__ = [
    # 核心类
    "TradingScheduler",
    "PositionStore",
    
    # 常量
    "ET",
    
    # 任务函数
    "scan_loop",
    "run_signal_scan",
    "process_symbol",
    "monitor_loop",
    "restore_positions_from_db",
    
    # 内部函数（保持兼容）
    "_check_positions",
    "_monitor_option_positions",
    "_get_current_price",
    "_get_option_price",
    "_close_position",
    "_close_option_position",
    "_execute_option_close_order",
    "_submit_order",
    "_submit_put_option",
    "_build_signal_from_vote",
    "_get_order_qty",
    "_notify_order",
    "_notify_exit",
    "_notify_risk_blocked",
    
    # 触发器函数
    "is_market_open",
    "is_in_cooldown",
    "set_cooldown",
    "check_daily_review",
]

# =============================================================================
# 版本信息
# =============================================================================

__version__ = "3.0.0"
__version_info__ = {
    "split_date": "2026-04-29",
    "reason": "P2-2 模块拆分完成：pipeline 模块化 + scheduler_tasks 瘦身",
    "modules": {
        "scheduler_core": "核心调度逻辑和主类",
        "pipeline/signal_pipeline": "信号扫描 + 决策管线",
        "pipeline/order_executor": "下单执行 + 仓位管理",
        "pipeline/position_monitor": "持仓监控 + 止损止盈 + 对账",
        "pipeline/llm_pipeline": "LLM 策略 + 盘前计划 + 盘后推荐",
        "scheduler_triggers": "触发器（时间判断、冷却）",
        "scheduler": "兼容导入层",
    }
}

logger.info(f"[scheduler] 模块已加载 (拆分版本 {__version__})")
