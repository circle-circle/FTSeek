"""
TWAP 执行器 — 大单按时间均匀拆分以降低市场冲击

设计要点:
- 应用层实现 (broker 层只接受单个 Order)
- 每片提交后记录成交价, 计算实际 TWAP 价
- 任意一片失败 → 剩余合并为一个市价单 (降级)
- 支持外部 cancel(): 止损/异常时取消未发出的剩余片
- 同时提供 sync 和 async 两种 API, 便于在不同上下文使用

参考: backups/.../v4/execution/smart_execution.py
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.core.models import Direction, Order, OrderState, OrderType

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
#  数据结构
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TWAPSlice:
    """单片执行记录"""
    sequence: int
    quantity: int
    scheduled_at: float                  # 计划发送时间 (epoch)
    side: str                            # BUY / SELL
    sent_at: Optional[float] = None
    filled_at: Optional[float] = None
    filled_quantity: float = 0.0
    filled_price: float = 0.0
    state: str = "PENDING"               # PENDING / SENT / FILLED / FAILED / CANCELED / DEGRADED
    futu_order_id: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.sequence,
            "qty": self.quantity,
            "scheduled_at": datetime.fromtimestamp(self.scheduled_at).strftime("%H:%M:%S"),
            "side": self.side,
            "filled_qty": self.filled_quantity,
            "filled_price": round(self.filled_price, 4),
            "state": self.state,
            "futu_order_id": self.futu_order_id,
            "error": self.error,
        }


@dataclass
class TWAPResult:
    """TWAP 执行结果汇总"""
    symbol: str
    direction: str
    total_qty: int
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0          # 实际 TWAP 价 (按成交量加权)
    signal_price: float = 0.0
    slippage_pct: float = 0.0            # (avg - signal) / signal * 100
    started_at: float = 0.0
    finished_at: float = 0.0
    latency_ms: int = 0
    slices: List[TWAPSlice] = field(default_factory=list)
    canceled: bool = False
    degraded: bool = False               # 是否降级为市价单
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "total_qty": self.total_qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": round(self.avg_fill_price, 4),
            "signal_price": round(self.signal_price, 4),
            "slippage_pct": round(self.slippage_pct, 4),
            "latency_ms": self.latency_ms,
            "canceled": self.canceled,
            "degraded": self.degraded,
            "error": self.error,
            "slices": [s.to_dict() for s in self.slices],
        }


# ──────────────────────────────────────────────────────────────────────────────
#  全局执行注册 (供外部按 symbol 取消)
# ──────────────────────────────────────────────────────────────────────────────

_active_executors: Dict[str, "TWAPExecutor"] = {}
_active_lock = threading.Lock()


def cancel_active_twap(symbol: str, reason: str = "external_cancel") -> bool:
    """外部触发取消 (例如止损命中)。Returns True if a TWAP was canceled."""
    with _active_lock:
        executor = _active_executors.get(symbol)
    if executor is not None:
        executor.cancel(reason)
        logger.warning(f"[TWAP·external-cancel] {symbol} canceled by '{reason}'")
        return True
    return False


def get_active_twap_symbols() -> List[str]:
    with _active_lock:
        return list(_active_executors.keys())


# ──────────────────────────────────────────────────────────────────────────────
#  TWAPExecutor
# ──────────────────────────────────────────────────────────────────────────────

class TWAPExecutor:
    """将大单按时间均匀拆分执行

    Args:
        broker_submit: 单片提交函数, 签名 (order: Order) -> Order (含 fill_price/state)
        slices: 拆分片数 (3-5)
        interval_seconds: 每片之间间隔 (秒)
        signal_price: 信号生成时的参考价, 用于滑点计算
        signal_time: 信号生成 epoch, 用于延迟计算
    """

    def __init__(
        self,
        broker_submit: Callable[[Order], Order],
        slices: int = 4,
        interval_seconds: float = 30.0,
        signal_price: float = 0.0,
        signal_time: Optional[float] = None,
    ):
        self.broker_submit = broker_submit
        self.slices = max(2, min(int(slices), 8))   # 安全边界
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.signal_price = float(signal_price or 0.0)
        self.signal_time = float(signal_time) if signal_time else time.time()

        self._cancel_flag = threading.Event()
        self._cancel_reason: Optional[str] = None
        self._symbol: Optional[str] = None

    # ------------------------------------------------------------------ control

    def cancel(self, reason: str = "external_cancel") -> None:
        """请求取消, 已发出的片不会撤回, 但剩余片不再发出"""
        self._cancel_reason = reason
        self._cancel_flag.set()

    @property
    def is_canceled(self) -> bool:
        return self._cancel_flag.is_set()

    # ------------------------------------------------------------------ planning

    def _split_quantity(self, total_qty: int) -> List[int]:
        """均匀拆分, 余数加在最后一片 (最少 1 股 / 片)"""
        n = self.slices
        base = total_qty // n
        rem = total_qty - base * n
        if base <= 0:
            # 单量太少, 不值得拆分 — 退化为 1 片
            return [total_qty]
        out = [base] * n
        out[-1] += rem
        return [q for q in out if q > 0]

    def _build_slice_order(
        self,
        symbol: str,
        direction: Direction,
        qty: int,
        order_type: OrderType,
        price: float,
        client_order_id_prefix: str,
        seq: int,
        signal_id: str = "",
        trace_id: str = "",
    ) -> Order:
        order = Order(
            client_order_id=f"{client_order_id_prefix}_TWAP{seq}_{uuid.uuid4().hex[:6]}",
            signal_id=signal_id,
            trace_id=trace_id,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            quantity=float(qty),
            price=float(price),
        )
        # 标记 metadata 用于下游归因
        order.metadata = {
            "execution": "TWAP",
            "twap_seq": seq,
            "twap_total_slices": self.slices,
        }
        return order

    # ------------------------------------------------------------------ execute

    def execute_sync(
        self,
        symbol: str,
        direction: Direction,
        total_qty: int,
        price_limit: Optional[float] = None,
        signal_id: str = "",
        trace_id: str = "",
    ) -> TWAPResult:
        """同步执行 (block 当前线程, 时长 ≈ slices × interval_seconds)。"""
        self._symbol = symbol
        result = TWAPResult(
            symbol=symbol,
            direction=direction.value if hasattr(direction, "value") else str(direction),
            total_qty=int(total_qty),
            signal_price=self.signal_price or float(price_limit or 0.0),
            started_at=time.time(),
        )

        # 注册全局, 供外部 cancel
        with _active_lock:
            _active_executors[symbol] = self

        try:
            quantities = self._split_quantity(int(total_qty))
            now = time.time()
            client_prefix = f"{symbol}_{int(now)}"
            order_type = OrderType.LIMIT if price_limit and price_limit > 0 else OrderType.MARKET
            slice_price = float(price_limit or 0.0)

            total_filled_qty = 0.0
            total_filled_value = 0.0

            logger.info(
                f"[TWAP·start] {symbol} {result.direction} qty={total_qty} "
                f"slices={len(quantities)}({quantities}) interval={self.interval_seconds:.0f}s "
                f"signal_price=${result.signal_price:.4f}"
            )

            for idx, qty in enumerate(quantities):
                # 取消检查
                if self.is_canceled:
                    logger.warning(
                        f"[TWAP·canceled] {symbol} 在 slice {idx+1}/{len(quantities)} 前取消 "
                        f"(reason={self._cancel_reason}), 已成交 {total_filled_qty}/{total_qty}"
                    )
                    result.canceled = True
                    result.error = f"canceled:{self._cancel_reason}"
                    break

                slice_rec = TWAPSlice(
                    sequence=idx + 1,
                    quantity=int(qty),
                    scheduled_at=time.time(),
                    side="BUY" if direction == Direction.LONG else "SELL",
                )
                result.slices.append(slice_rec)

                # 提交本片
                try:
                    order = self._build_slice_order(
                        symbol, direction, int(qty), order_type, slice_price,
                        client_prefix, idx + 1, signal_id, trace_id,
                    )
                    slice_rec.sent_at = time.time()
                    slice_rec.state = "SENT"
                    submitted = self.broker_submit(order)

                    # 提取成交信息
                    fp = float(getattr(submitted, "fill_price", 0) or
                               getattr(submitted, "avg_fill_price", 0) or
                               getattr(submitted, "price", 0) or 0)
                    fq = float(getattr(submitted, "filled_quantity", 0) or 0)
                    if fq <= 0 and getattr(submitted, "state", None) == OrderState.FILLED:
                        fq = float(qty)
                    if fp <= 0:
                        fp = slice_price or self.signal_price

                    slice_rec.filled_at = time.time()
                    slice_rec.filled_quantity = fq
                    slice_rec.filled_price = fp
                    slice_rec.state = "FILLED" if fq > 0 else "FAILED"
                    slice_rec.futu_order_id = getattr(submitted, "futu_order_id", "") or ""

                    if fq > 0:
                        total_filled_qty += fq
                        total_filled_value += fq * fp
                        logger.info(
                            f"[TWAP·slice] {symbol} #{idx+1}/{len(quantities)} "
                            f"qty={fq} fill=${fp:.4f} (futu_id={slice_rec.futu_order_id})"
                        )
                    else:
                        raise RuntimeError("slice filled_qty=0")

                except Exception as e:
                    slice_rec.state = "FAILED"
                    slice_rec.error = str(e)
                    logger.error(
                        f"[TWAP·slice-failed] {symbol} #{idx+1}/{len(quantities)} qty={qty} "
                        f"error={e} → 触发降级合并"
                    )
                    # 降级: 剩余片合并为一个市价单
                    remaining = sum(quantities[idx:]) - int(slice_rec.filled_quantity)
                    if remaining > 0 and not self.is_canceled:
                        try:
                            degr = TWAPSlice(
                                sequence=idx + 100,  # 100+ 标记降级
                                quantity=remaining,
                                scheduled_at=time.time(),
                                side=slice_rec.side,
                                state="DEGRADED",
                            )
                            result.slices.append(degr)
                            degr_order = self._build_slice_order(
                                symbol, direction, remaining,
                                OrderType.MARKET, 0.0,
                                client_prefix, 999, signal_id, trace_id,
                            )
                            degr_order.metadata["execution"] = "TWAP_DEGRADED"
                            degr.sent_at = time.time()
                            submitted = self.broker_submit(degr_order)
                            fp = float(getattr(submitted, "fill_price", 0) or
                                       getattr(submitted, "avg_fill_price", 0) or
                                       slice_price or self.signal_price)
                            fq = float(getattr(submitted, "filled_quantity", 0) or remaining)
                            degr.filled_at = time.time()
                            degr.filled_quantity = fq
                            degr.filled_price = fp
                            degr.state = "FILLED"
                            degr.futu_order_id = getattr(submitted, "futu_order_id", "") or ""
                            total_filled_qty += fq
                            total_filled_value += fq * fp
                            result.degraded = True
                            logger.warning(
                                f"[TWAP·degraded] {symbol} 剩余 {remaining} 股以市价合并成交 @ ${fp:.4f}"
                            )
                        except Exception as e2:
                            logger.error(f"[TWAP·degrade-failed] {symbol} 降级单也失败: {e2}")
                            result.error = f"degrade_failed: {e2}"
                    break

                # 等待下一片 (最后一片不等)
                if idx < len(quantities) - 1:
                    self._sleep_with_cancel(self.interval_seconds)

            # 汇总
            result.filled_qty = total_filled_qty
            if total_filled_qty > 0:
                result.avg_fill_price = total_filled_value / total_filled_qty
            else:
                result.avg_fill_price = 0.0
            result.finished_at = time.time()
            result.latency_ms = int((result.finished_at - self.signal_time) * 1000)

            if result.signal_price > 0 and result.avg_fill_price > 0:
                slip = (result.avg_fill_price - result.signal_price) / result.signal_price * 100.0
                # 对 SELL: 实际价 < 参考价 = 不利, 取符号反转
                if direction == Direction.SHORT:
                    slip = -slip
                result.slippage_pct = slip

            logger.info(
                f"[TWAP·done] {symbol} filled={result.filled_qty}/{total_qty} "
                f"avg=${result.avg_fill_price:.4f} signal=${result.signal_price:.4f} "
                f"slippage={result.slippage_pct:+.3f}% latency={result.latency_ms}ms "
                f"degraded={result.degraded} canceled={result.canceled}"
            )
            return result
        finally:
            with _active_lock:
                if _active_executors.get(symbol) is self:
                    _active_executors.pop(symbol, None)

    async def execute(
        self,
        symbol: str,
        direction: Direction,
        total_qty: int,
        price_limit: Optional[float] = None,
        signal_id: str = "",
        trace_id: str = "",
    ) -> TWAPResult:
        """异步执行版本 — 内部仍委托同步实现, 在 executor 中跑避免阻塞 event loop"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.execute_sync(
                symbol=symbol,
                direction=direction,
                total_qty=int(total_qty),
                price_limit=price_limit,
                signal_id=signal_id,
                trace_id=trace_id,
            ),
        )

    # ------------------------------------------------------------------ helpers

    def _sleep_with_cancel(self, seconds: float) -> None:
        """可被 cancel() 中断的 sleep"""
        # 步进检查, 最长 0.5s 一格, 兼顾响应性
        end = time.time() + seconds
        while time.time() < end:
            if self.is_canceled:
                return
            time.sleep(min(0.5, max(0.0, end - time.time())))


# ──────────────────────────────────────────────────────────────────────────────
#  便捷函数: 阈值判断
# ──────────────────────────────────────────────────────────────────────────────

def should_use_twap(notional_usd: float, threshold_usd: float) -> bool:
    """订单金额超过阈值时启用 TWAP"""
    try:
        return float(notional_usd) > float(threshold_usd)
    except Exception:
        return False
