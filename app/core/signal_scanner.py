"""
FTSeek-US 信号扫描模块 (P2-2 委托到 app.pipeline)

职责:
- 信号扫描主循环 (scan_loop)
- 自适应信号扫描 (轮询批次控制)
- 单只股票处理 (行情 → 投票 → 决策 → 下单)
- 订单构建和提交 (集成 7 层风控门禁)
- 持仓冲突处理 (反向持仓自动平仓)
- 仓位计算 (基于资本安全水位)

函数列表:
  scan_loop                - 信号扫描主循环 (带巡检与熔断)
  run_signal_scan          - 外部调用入口
  process_symbol           - 处理单只股票入口
  _run_signal_scan_impl    - 自适应信号扫描实现
  _process_symbol_impl     - 单只股票完整处理流程
  _build_signal_from_vote  - 投票结果 → SignalScore
  _submit_order            - 订单构建和提交 (核心)
  _get_order_qty           - 动态仓位计算
  _submit_put_option       - SHORT 信号: 买入 ATM Put
  _close_position_for_conflict - 持仓冲突平仓
  _close_short_for_long    - 平空转多
  _close_long_for_short    - 平多转空
  _try_direct_short_only   - 直接卖空
  _try_direct_short_with_fallback - 卖空+Put fallback
"""

# P2-2: 委托到 pipeline 模块 (实现已迁移)
from app.pipeline.signal_pipeline import (
    scan_loop,
    run_signal_scan,
    process_symbol,
    _run_signal_scan_impl,
    _process_symbol_impl,
    _build_signal_from_vote,
)
from app.pipeline.order_executor import (
    _submit_order,
    _get_order_qty,
    _submit_put_option,
    _close_position_for_conflict,
    _close_short_for_long,
    _close_long_for_short,
    _try_direct_short_only,
    _try_direct_short_with_fallback,
)

__all__ = [
    "scan_loop",
    "run_signal_scan",
    "process_symbol",
    "_run_signal_scan_impl",
    "_process_symbol_impl",
    "_build_signal_from_vote",
    "_submit_order",
    "_get_order_qty",
    "_submit_put_option",
    "_close_position_for_conflict",
    "_close_short_for_long",
    "_close_long_for_short",
    "_try_direct_short_only",
    "_try_direct_short_with_fallback",
]
