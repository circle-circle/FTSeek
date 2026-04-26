"""
执行质量监控 (Execution Quality Monitor)

用途:
监控实盘/回测中的订单执行效率，发现滑点过大或系统延迟问题。
为策略迭代提供真实的执行成本数据。

更新时间: 2026-04-14
"""

import time
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import numpy as np

logger = logging.getLogger("ftseek.monitoring.execution_quality")

@dataclass
class OrderRecord:
    """单笔订单执行记录"""
    order_id: str
    code: str
    signal_time: float
    decision_price: float
    amount: float
    executed_amount: float = 0.0
    exec_price: float = 0.0
    exec_time: Optional[float] = None
    status: str = "pending" # pending, filled, rejected, partial
    slippage_pct: float = 0.0
    latency_ms: float = 0.0

class ExecutionQualityMonitor:
    def __init__(self, slippage_alert_pct: float = 0.005, # 0.5%
                 latency_alert_ms: float = 2000):         # 2s
        """
        初始化监控器
        
        Args:
            slippage_alert_pct: 滑点告警阈值 (百分比)
            latency_alert_ms: 延迟告警阈值 (毫秒)
        """
        self.orders: Dict[str, OrderRecord] = {}
        self.slippage_alert_pct = slippage_alert_pct
        self.latency_alert_ms = latency_alert_ms
        self.alerts: List[str] = []

    def record_signal(self, order_id: str, code: str, price: float, amount: float):
        """
        记录信号生成时刻 (理想状态)
        应在 DataSubscriber 或 Strategy 发出信号时调用
        """
        self.orders[order_id] = OrderRecord(
            order_id=order_id,
            code=code,
            signal_time=time.time(),
            decision_price=price,
            amount=amount,
        )

    def record_fill(self, order_id: str, exec_price: float, 
                    executed_amount: float, status: str = "filled"):
        """
        记录实际成交 (现实状态)
        应在 TradeExecutor 收到成交回报时调用
        """
        if order_id not in self.orders:
            # 可能是非本系统生成的订单 (如手动交易)，忽略或仅记录
            return

        order = self.orders[order_id]
        order.exec_price = exec_price
        order.executed_amount = executed_amount
        order.exec_time = time.time()
        order.status = status

        # 计算滑点 (绝对值百分比)
        if order.decision_price > 0:
            order.slippage_pct = abs(exec_price - order.decision_price) / order.decision_price
        
        # 计算延迟
        if order.signal_time > 0:
            order.latency_ms = (order.exec_time - order.signal_time) * 1000

        # 触发告警
        self._check_alerts(order)

    def _check_alerts(self, order: OrderRecord):
        """检查是否触发告警"""
        if order.slippage_pct > self.slippage_alert_pct:
            msg = f"⚠️ 高滑点: {order.code} 滑点 {order.slippage_pct:.2%} (阈值 {self.slippage_alert_pct:.2%})"
            self.alerts.append(msg)
            logger.warning(msg)
            
        if order.latency_ms > self.latency_alert_ms:
            msg = f"⚠️ 高延迟: {order.code} 延迟 {order.latency_ms:.0f}ms (阈值 {self.latency_alert_ms:.0f}ms)"
            self.alerts.append(msg)
            logger.warning(msg)

    def get_summary(self) -> Dict:
        """获取统计摘要"""
        filled = [o for o in self.orders.values() if o.status in ('filled', 'partial')]
        if not filled:
            return {"count": 0, "orders": self.orders}
        
        slippages = [o.slippage_pct for o in filled if o.slippage_pct > 0]
        latencies = [o.latency_ms for o in filled if o.latency_ms > 0]
        
        avg_slip = np.mean(slippages) if slippages else 0.0
        avg_lat = np.mean(latencies) if latencies else 0.0
        
        return {
            "total_orders": len(self.orders),
            "filled_orders": len(filled),
            "avg_slippage_pct": f"{avg_slip:.3%}",
            "avg_latency_ms": f"{avg_lat:.0f}ms",
            "partial_fills": sum(1 for o in filled if o.status == 'partial'),
            "recent_alerts": self.alerts[-5:], # 最近 5 条告警
            "max_slippage": f"{max(slippages):.3%}" if slippages else "0.000%",
            "max_latency_ms": f"{max(latencies):.0f}ms" if latencies else "0ms"
        }

# 全局单例
_monitor: Optional[ExecutionQualityMonitor] = None

def get_execution_monitor() -> ExecutionQualityMonitor:
    """获取全局执行质量监控器"""
    global _monitor
    if _monitor is None:
        _monitor = ExecutionQualityMonitor()
    return _monitor
