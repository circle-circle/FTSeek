"""
FTSeek-US 持仓监控模块 (P2-2 委托到 app.pipeline)

职责:
- 持仓监控主循环 (monitor_loop)
- 资本安全水位更新
- 止损止盈检查 (股票 + 期权)
- 平仓执行和记录
- 通知推送

函数列表:
  monitor_loop           - 持仓监控主循环
  _update_capital_safety - 资本安全水位更新
  _check_positions       - 股票持仓止损止盈检查
  _get_current_price     - 获取当前价格 (Futu → Yahoo fallback)
  _close_position        - 通用平仓函数
  _update_trade_attribution - 更新交易归因
  _insert_trade_attribution - 插入归因基准记录
  _notify_order          - 下单通知
  _notify_exit           - 平仓通知
  _notify_risk_blocked   - 风控拦截通知
"""

# P2-2: 委托到 pipeline 模块 (实现已迁移)
from app.pipeline.position_monitor import (
    monitor_loop,
    _update_capital_safety,
    _check_positions,
    _notify_risk_blocked,
)
from app.pipeline.order_executor import (
    _get_current_price,
    _close_position,
    _update_trade_attribution,
    _insert_trade_attribution,
    _notify_order,
    _notify_exit,
)

__all__ = [
    "monitor_loop",
    "_update_capital_safety",
    "_check_positions",
    "_get_current_price",
    "_close_position",
    "_update_trade_attribution",
    "_insert_trade_attribution",
    "_notify_order",
    "_notify_exit",
    "_notify_risk_blocked",
]
