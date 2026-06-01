"""
订单执行管线 — FTSeek-US 下单执行与仓位管理

从 scheduler_tasks.py 拆分而来。本模块包含完整的订单构建、风控门禁、
仓位计算、平仓逻辑和通知。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from app.utils.timezone import _et_trade_date, now_et

from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, ExitReason, AlertLevel,
)
from app.execution.options_selector import OptionSelector
from app.data.adaptive_polling import StockTier
from app.storage.rejection_logger import get_rejection_logger
from app.execution.twap_executor import (
    TWAPExecutor, TWAPResult, should_use_twap, cancel_active_twap,
)

logger = logging.getLogger(__name__)

# ── 全局常量 ──
DEFAULT_ORDER_QTY = 50  # 默认下单股数 (watchlist_us.json 可覆盖)


# ══════════════════════════════════════════════════════════════════
#  执行优化: TWAP 拆单 + 延迟/滑点监控 (Task #14, 2026-05-27)
# ══════════════════════════════════════════════════════════════════

def _load_execution_optimization_config() -> Dict[str, Any]:
    """从 unified_config.yaml 读取 TWAP / 延迟跟踪 配置"""
    defaults = {
        "twap_threshold_usd": 5000.0,
        "twap_slices": 4,
        "twap_interval_seconds": 30.0,
        "enable_latency_tracking": True,
        "enable_twap": True,
    }
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        return {
            "twap_threshold_usd": float(cfg.get_dotted("execution.twap_threshold_usd", defaults["twap_threshold_usd"])),
            "twap_slices": int(cfg.get_dotted("execution.twap_slices", defaults["twap_slices"])),
            "twap_interval_seconds": float(cfg.get_dotted("execution.twap_interval_seconds", defaults["twap_interval_seconds"])),
            "enable_latency_tracking": bool(cfg.get_dotted("execution.enable_latency_tracking", defaults["enable_latency_tracking"])),
            "enable_twap": bool(cfg.get_dotted("execution.enable_twap", defaults["enable_twap"])),
        }
    except Exception as e:
        logger.debug(f"[执行优化] 配置加载失败, 使用默认值: {e}")
        return defaults


def _record_execution_metrics(
    symbol: str,
    direction: str,
    signal_price: float,
    fill_price: float,
    signal_time: float,
    fill_time: float,
    twap_result: Optional[TWAPResult] = None,
) -> Dict[str, Any]:
    """计算执行延迟 + 滑点, 返回 metadata dict (供写入 trade_records.metadata_json)

    同时写日志: "Execution latency: {ms}ms for {symbol}"
    """
    latency_ms = int(max(0.0, (fill_time - signal_time)) * 1000)
    slippage_pct = 0.0
    if signal_price and signal_price > 0 and fill_price and fill_price > 0:
        slip = (float(fill_price) - float(signal_price)) / float(signal_price) * 100.0
        # SHORT 方向: 实际价 < 参考价 = 不利 → 反转符号使 “正 = 不利”
        slippage_pct = -slip if str(direction).upper() == "SHORT" else slip

    logger.info(f"Execution latency: {latency_ms}ms for {symbol}")

    metadata: Dict[str, Any] = {
        "signal_price": round(float(signal_price or 0.0), 4),
        "fill_price": round(float(fill_price or 0.0), 4),
        "slippage_pct": round(float(slippage_pct), 4),
        "execution_latency_ms": latency_ms,
        "signal_time": datetime.fromtimestamp(signal_time).isoformat() if signal_time else None,
        "fill_time": datetime.fromtimestamp(fill_time).isoformat() if fill_time else None,
        "execution_mode": "TWAP" if twap_result else "MARKET",
    }
    if twap_result is not None:
        metadata["twap"] = twap_result.to_dict()
        if twap_result.degraded:
            metadata["execution_mode"] = "TWAP_DEGRADED"
        if twap_result.canceled:
            metadata["execution_mode"] = "TWAP_CANCELED"
    return metadata


def _submit_order_optimized(
    scheduler,
    order: Order,
    signal_price: float,
    signal_time: float,
    direction: Direction,
    signal_id: str = "",
    trace_id: str = "",
) -> Tuple[Order, Dict[str, Any]]:
    """统一下单入口 — 金额 > 阈值 走 TWAP, 否则走市价; 返回 (order, exec_metadata)

    Args:
        order: 已准备好的订单 (quantity/price/direction 均已设置)
        signal_price: 信号生成时参考价
        signal_time: 信号生成 epoch (s)
        direction: 方向
        signal_id/trace_id: 归因

    Returns:
        (submitted_order, metadata) — metadata 含 slippage_pct/execution_latency_ms/twap 详情
    """
    cfg = _load_execution_optimization_config()
    notional = float(order.quantity) * float(order.price or signal_price or 0.0)
    use_twap = (
        cfg["enable_twap"]
        and should_use_twap(notional, cfg["twap_threshold_usd"])
        and scheduler.execution_engine is not None
    )

    if not use_twap:
        # 普通路径
        if scheduler.execution_engine:
            submitted = scheduler.execution_engine.submit_order(order)
        else:
            order.state = OrderState.FILLED
            order.filled_quantity = order.quantity
            order.avg_fill_price = signal_price or order.price
            order.fill_price = order.avg_fill_price
            order.filled_at = now_et()
            submitted = order

        fill_price = float(
            getattr(submitted, "fill_price", 0)
            or getattr(submitted, "avg_fill_price", 0)
            or order.price
            or signal_price
        )
        fill_time = time.time()
        meta = _record_execution_metrics(
            order.symbol, direction.value if hasattr(direction, "value") else str(direction),
            signal_price, fill_price, signal_time, fill_time, twap_result=None,
        )
        return submitted, meta

    # ── TWAP 路径 ──
    logger.info(
        f"[执行优化] {order.symbol} 订单金额 ≈ ${notional:,.0f} > 阈值 "
        f"${cfg['twap_threshold_usd']:,.0f} → 启用 TWAP 拆单 "
        f"({cfg['twap_slices']} 片 / {cfg['twap_interval_seconds']:.0f}s)"
    )

    def _broker_submit(slice_order: Order) -> Order:
        return scheduler.execution_engine.submit_order(slice_order)

    executor = TWAPExecutor(
        broker_submit=_broker_submit,
        slices=cfg["twap_slices"],
        interval_seconds=cfg["twap_interval_seconds"],
        signal_price=signal_price,
        signal_time=signal_time,
    )

    twap_result = executor.execute_sync(
        symbol=order.symbol,
        direction=direction,
        total_qty=int(order.quantity),
        price_limit=order.price if order.order_type == OrderType.LIMIT else None,
        signal_id=signal_id,
        trace_id=trace_id,
    )

    # 将 TWAP 结果回填到 order 对象, 保持与下游一致
    order.state = OrderState.FILLED if twap_result.filled_qty > 0 else OrderState.REJECTED
    order.filled_quantity = float(twap_result.filled_qty)
    order.avg_fill_price = float(twap_result.avg_fill_price)
    try:
        order.fill_price = float(twap_result.avg_fill_price)
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    order.filled_at = datetime.fromtimestamp(twap_result.finished_at) if twap_result.finished_at else now_et()
    order.sent_at = datetime.fromtimestamp(twap_result.started_at) if twap_result.started_at else now_et()
    # 首个成交片的 futu_order_id 作为代表 (供归因/查询)
    for s in twap_result.slices:
        if s.futu_order_id:
            order.futu_order_id = s.futu_order_id
            break

    fill_time = twap_result.finished_at or time.time()
    meta = _record_execution_metrics(
        order.symbol, direction.value if hasattr(direction, "value") else str(direction),
        signal_price, twap_result.avg_fill_price,
        signal_time, fill_time, twap_result=twap_result,
    )
    return order, meta


# ═══════════════════════════════════════════════════════════════════
#  工具函数: 价格获取
# ═══════════════════════════════════════════════════════════════════

def _get_current_price(scheduler, symbol: str) -> float:
    """获取当前价格（富途 → Yahoo fallback）"""
    # 优先从执行引擎（富途）获取
    if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
        adapter = scheduler.execution_engine.broker_adapter
        if adapter and adapter.is_connected():
            try:
                price = adapter.get_current_price(symbol)
                if price > 0:
                    return price
            except Exception as e:
                logger.debug(f"[价格获取] adapter.get_current_price({symbol}) 失败: {e}")

    # fallback：通过 MarketDataRouter 获取报价
    try:
        quote = scheduler.data_router.get_quote(symbol)
        if quote:
            price_val = quote.get("price", 0)
            # Defensive: price field could be a dict in edge cases (Push cache corruption)
            if isinstance(price_val, dict):
                price_val = price_val.get("price", price_val.get("last_price", 0))
            return float(price_val)
    except Exception as e:
        logger.debug(f"[价格获取] data_router.get_quote({symbol}) 失败: {e}")
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  持仓冲突处理
# ═══════════════════════════════════════════════════════════════════

def _close_position_for_conflict(scheduler, position: Position):
    """
    因持仓方向冲突而平仓。

    P0 修复 (2026-04-17): 当 position_store 与 broker 不同步时,
    自动关闭冲突持仓, 避免下单失败。
    """
    try:
        price = _get_current_price(scheduler, position.symbol)
        if price <= 0:
            logger.error(f"[持仓冲突] {position.symbol} 无法获取价格, 跳过平仓")
            return

        exit_reason = ExitReason.POSITION_CONFLICT
        _close_position(scheduler, position, price, exit_reason)
        logger.info(f"[持仓冲突] {position.symbol} 已平仓, 等待下轮扫描再开新仓")
        # 设置冷却期, 防止立即反向开仓
        scheduler._set_cooldown(position.symbol)
    except Exception as e:
        logger.error(f"[持仓冲突] {position.symbol} 平仓异常: {e}")


def _close_short_for_long(scheduler, symbol: str, qty: float, current_price: float) -> bool:
    """
    自动平仓空头以准备开多头。

    P1 修复 (2026-04-19): 增加作战计划门禁 — 如果作战计划明确禁止 LONG 方向,
    拒绝执行反向操作。
    P1 优化 (2026-04-17): 当 broker 有空头持仓但新信号为 LONG 时,
    自动以市价买入平仓, 避免信号丢失。
    
    Returns:
        True if close was attempted, False if skipped
    """
    from app.core.models import Order as OrderModel, Direction as Dir, OrderType as OT

    # P1 修复: 作战计划门禁 (防止与作战计划背离的反手)
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        # 检查作战计划是否允许此标的做多
        plan_info = mgr.get_target_info(symbol)
        if plan_info:
            plan_dir = plan_info.get("direction", "").upper()
            plan_status = plan_info.get("status", "PRE_MARKET")
            if plan_status == "CANCELLED":
                logger.warning(
                    f"🚫 [作战计划反手门禁] {symbol} 计划已取消, 拒绝平空转多"
                )
                return False
            if plan_dir and plan_dir not in ("LONG", "ANY"):
                logger.warning(
                    f"⚠️ [作战计划反手门禁] {symbol} 计划方向为 {plan_dir}, "
                    f"与反手 LONG 背离, 仅记录告警但不拦截 (允许执行)"
                )
    except Exception as e:
        logger.debug(f"[作战计划反手门禁] 检查异常 (放行): {e}")

    try:
        if current_price <= 0:
            current_price = _get_current_price(scheduler, symbol)
            if current_price <= 0:
                logger.error(f"[空头转多头] {symbol} 无法获取价格, 跳过平仓")
                return False

        # 构建平仓订单 (BUY 平空仓)
        order = OrderModel(
            client_order_id=f"{symbol}_CLOSE_SHORT_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            symbol=symbol,
            direction=Dir.LONG,  # 买入平空
            order_type=OT.MARKET,
            quantity=qty,
            price=current_price,
        )

        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.broker_adapter.submit_order(order)
                logger.info(
                    f"[空头转多头] {symbol} 空头平仓已提交: "
                    f"qty={qty} price=${current_price:.2f} result={result}"
                )
            except Exception as e:
                logger.error(f"[空头转多头] {symbol} 平仓提交失败: {e}")
                return False

        # 设置冷却期
        scheduler._set_cooldown(symbol)
        logger.info(f"[空头转多头] {symbol} 空头已平仓, 下轮扫描将尝试开多头")

        # 通知
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            msg = (
                f"🔄 FTSeek-US 持仓冲突自动处理\n\n"
                f"📌 标的: {symbol}\n"
                f"📉 操作: 平空仓 {int(qty)} 股 @ ${current_price:.2f}\n"
                f"📈 原因: 新信号 LONG, 自动清理反向空头\n"
                f"⏳ 下轮扫描将尝试开多头"
            )
            notifier.send_text(msg)
        except Exception as e:
            logger.warning(f"[空头转多头] 钉钉通知失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[空头转多头] {symbol} 处理异常: {e}")
        return False


def _close_long_for_short(scheduler, symbol: str, qty: float, current_price: float) -> bool:
    """
    自动平仓多头以准备开空头（通过期权）。

    P1 修复 (2026-04-19): 增加作战计划门禁 — 如果作战计划明确禁止 SHORT 方向,
    拒绝执行反向操作。
    P1 优化 (2026-04-17): 当 broker 有多头持仓但新信号为 SHORT 时,
    自动卖出平仓, 然后通过期权做空。
    
    Returns:
        True if close was attempted, False if skipped
    """
    from app.core.models import Order as OrderModel, Direction as Dir, OrderType as OT

    # P1 修复: 作战计划门禁
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        plan_info = mgr.get_target_info(symbol)
        if plan_info:
            plan_dir = plan_info.get("direction", "").upper()
            plan_status = plan_info.get("status", "PRE_MARKET")
            if plan_status == "CANCELLED":
                logger.warning(
                    f"🚫 [作战计划反手门禁] {symbol} 计划已取消, 拒绝平多转空"
                )
                return False
            if plan_dir and plan_dir not in ("SHORT", "ANY"):
                logger.warning(
                    f"⚠️ [作战计划反手门禁] {symbol} 计划方向为 {plan_dir}, "
                    f"与反手 SHORT 背离, 仅记录告警但不拦截 (允许执行)"
                )
    except Exception as e:
        logger.debug(f"[作战计划反手门禁] 检查异常 (放行): {e}")

    try:
        if current_price <= 0:
            current_price = _get_current_price(scheduler, symbol)
            if current_price <= 0:
                logger.error(f"[多头转空头] {symbol} 无法获取价格, 跳过平仓")
                return False

        # 构建平仓订单 (SELL 平多仓)
        order = OrderModel(
            client_order_id=f"{symbol}_CLOSE_LONG_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            symbol=symbol,
            direction=Dir.SHORT,  # 卖出平多
            order_type=OT.MARKET,
            quantity=qty,
            price=current_price,
        )

        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.broker_adapter.submit_order(order)
                logger.info(
                    f"[多头转空头] {symbol} 多头平仓已提交: "
                    f"qty={qty} price=${current_price:.2f}"
                )
            except Exception as e:
                logger.error(f"[多头转空头] {symbol} 平仓提交失败: {e}")
                return False

        scheduler._set_cooldown(symbol)
        logger.info(f"[多头转空头] {symbol} 多头已平仓, 下轮扫描将尝试开空头(期权)")

        # 通知
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            msg = (
                f"🔄 FTSeek-US 持仓冲突自动处理\n\n"
                f"📌 标的: {symbol}\n"
                f"📈 操作: 平多仓 {int(qty)} 股 @ ${current_price:.2f}\n"
                f"📉 原因: 新信号 SHORT, 自动清理反向多头\n"
                f"⏳ 下轮扫描将尝试期权做空"
            )
            notifier.send_text(msg)
        except Exception as e:
            logger.warning(f"[多头转空头] 钉钉通知失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[多头转空头] {symbol} 处理异常: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
#  通知函数
# ═══════════════════════════════════════════════════════════════════

def _notify_order(scheduler, symbol: str, signal: SignalScore, order: Order, position: Position, voting_result: Dict = None):
    """下单通知 (钉钉 + 飞书)

    P-Push (2026-05-16): 同时走 PushHub (聚合 + 防疲劳) 和旧 notifier.
    yaml `push.enabled=true` 时 PushHub 接管, 旧 notifier 仍发 (双路过渡期).
    收盘后 daily_review 看是否单一通道即可, 然后再删旧路径.

    Task #42 (P0, 2026-05-28): 投票→交易记录数据链路修复
      历史问题: signal_strength/voting_result 写入逻辑被嵌在 ``if scheduler.notifier:``
      块内, 当 notifier 未配置或失败时, position.metadata 永远拿不到投票数据,
      导致平仓时 trade_records 的 signal_strength / final_strength 全为 0.
      修复: 将 position.metadata 写入提到外层, 与 notifier 解耦, 确保投票结果
      始终落到持仓元数据上.
    """
    # ── Task #42: 始终先写 position.metadata, 不依赖 notifier 是否启用 ──
    try:
        # P0 修复 (2026-05-27): 推导 entry_reason 并写入 position.metadata
        # 修复 52.2% 交易 entry_reason 为空的归因断链问题
        from app.core.models import derive_entry_reason
        _entry_reason = derive_entry_reason(
            strategy_id=signal.strategy_id,
            source=(signal.metadata or {}).get("source", "") if hasattr(signal, "metadata") else "",
            metadata=signal.metadata if hasattr(signal, "metadata") else {},
        )

        # 信号 + 投票快照 (用于平仓时 trade_records 字段回填)
        # P1 修复 (2026-05-31): 添加 decision_id 到 metadata, 确保平仓时可关联到 signal_snapshots
        _decision_id = (voting_result or {}).get("decision_id", "") or ""
        _signal_snapshot = {
            'signal_strength': int(getattr(signal, 'strength', 0) or 0),
            'signal_confidence': float(getattr(signal, 'confidence', 0.0) or 0.0),
            'strategy_id': getattr(signal, 'strategy_id', '') or '',
            'voting_result': voting_result or {},
            'source_type': 'MAIN_PIPELINE',  # P1 修复：标注来源
            'entry_reason': _entry_reason,   # P0 归因修复
            'signal_id': getattr(signal, 'signal_id', '') or getattr(signal, 'trace_id', '') or '',  # P0 归因
            # P1 修复 (2026-05-31): decision_id 持久化 — 平仓时用于关联 signal_snapshots
            'decision_id': _decision_id,
            # P1 修复 (2026-05-31): decision_reason 归因 — 记录为什么做这笔交易
            'decision_reason': (
                f"strategy={getattr(signal, 'strategy_id', 'unknown')} "
                f"strength={getattr(signal, 'strength', 0)} "
                f"confidence={getattr(signal, 'confidence', 0):.2f} "
                f"regime={getattr(signal, 'market_regime', 'UNKNOWN')}"
            ),
            # Preserve quality scoring metadata
            "quality_factors": signal.metadata.get("quality_factors") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused": signal.metadata.get("fused") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused_count": signal.metadata.get("fused_count") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused_strategies": signal.metadata.get("fused_strategies") if hasattr(signal, "metadata") and signal.metadata else None,
        }

        if hasattr(position, 'metadata') and position.metadata:
            position.metadata.update(_signal_snapshot)
        else:
            position.metadata = dict(_signal_snapshot)
    except Exception as _meta_err:
        # metadata 写入失败不能阻断下单流程, 但要明确告警 (Task #42 要求)
        logger.warning(
            f"[Task#42] {symbol} 写 position.metadata 失败 (signal_strength 链路降级到 attribution/decisions 兜底): {_meta_err}"
        )

    if scheduler.notifier:
        try:
            scheduler.notifier.notify_order_filled(order, position, voting_result=voting_result)
        except Exception as e:
            logger.error(f"[通知] 下单通知失败: {e}", exc_info=True)

    # P-Push: 同步进入 PushHub (聚合 5min 窗口)
    try:
        from app.push import get_push_hub, trade_entry as _tx_entry
        _hub_msg = _tx_entry(
            symbol=symbol,
            direction=signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            qty=int(getattr(order, "quantity", 0) or 0),
            price=float(getattr(order, "price", 0) or 0),
            strategy=getattr(position, "owner_strategy", None) or "main",
            trace_id=getattr(signal, "trace_id", "") or getattr(position, "trace_id", ""),
        )
        get_push_hub().push(_hub_msg)
    except Exception as _pe:
        logger.debug(f"[PushHub·entry] 失败 (不影响旧通道): {_pe}")


def _notify_exit(scheduler, position: Position, reason: ExitReason, pnl: float, pnl_pct: float):
    """平仓通知 (钉钉 + 飞书)

    P-Push: 同时入 PushHub. 平仓小亏 (<$500) 进聚合, 大单立即推.
    """
    if scheduler.notifier:
        try:
            scheduler.notifier.notify_exit(position, reason, pnl)
        except Exception as e:
            logger.error(f"[通知] 平仓通知失败: {e}", exc_info=True)

    try:
        from app.push import get_push_hub, trade_exit as _tx_exit
        _reason_str = reason.value if hasattr(reason, "value") else str(reason)
        _hub_msg = _tx_exit(
            symbol=position.symbol,
            direction=position.direction.value if hasattr(position.direction, "value") else str(position.direction),
            pnl=float(pnl),
            exit_reason=_reason_str,
            strategy=getattr(position, "owner_strategy", None) or "main",
            trace_id=getattr(position, "trace_id", ""),
        )
        get_push_hub().push(_hub_msg)
    except Exception as _pe:
        logger.debug(f"[PushHub·exit] 失败 (不影响旧通道): {_pe}")


# ═══════════════════════════════════════════════════════════════════
#  动态仓位计算
# ═══════════════════════════════════════════════════════════════════

def _get_order_qty(scheduler, symbol: str, atr: float = 0, direction: str = "LONG", context: dict = None) -> int:
    """
    获取订单数量 (基于资本安全水位的动态仓位计算)

    P1-1 修复 (2026-04-17): 使用 _get_current_price 替代 get_full_market_context
    避免重复调用 TwelveData 浪费信用

    Livermore 原则: 根据账户净值、风险承受能力和当前水位动态调整仓位,
    永远不被破产, 永远不陷于绝境。
    """
    DEFAULT_QTY = DEFAULT_ORDER_QTY  # 局部别名，方便引用
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()

        # P0 修复 (2026-05-02): 开仓前实时刷新 buying_power
        # 旧行为: 依赖 position_monitor 每 30s 推送，开仓时 buying_power 可能过时
        # 新行为: 每次计算前刷新 (AccountRefresher 内部 30s 缓存，不增加 API 负担)
        try:
            from app.risk.account_refresher import get_account_refresher
            refresher = get_account_refresher()

            # 注入 broker_adapter (仅首次)
            if refresher._broker is None and scheduler.execution_engine:
                adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
                if adapter and adapter.is_connected():
                    refresher._broker = adapter

            # 刷新账户状态
            if refresher._broker and refresher._broker.is_connected():
                account_info = refresher.refresh()
                if account_info and account_info.get("buying_power", 0) > 0:
                    capital.update_account(
                        current_equity=account_info.get("total_assets", 0),
                        cash=account_info.get("cash", 0),
                        market_value=account_info.get("market_val", 0),
                        buying_power=account_info.get("buying_power", 0),
                    )
                    logger.debug(
                        f"[动态仓位] {symbol} 开仓前账户刷新: "
                        f"equity=${account_info.get('total_assets', 0):,.0f} | "
                        f"buying_power=${account_info.get('buying_power', 0):,.0f}"
                    )
        except Exception as e:
            logger.debug(f"[动态仓位] {symbol} 账户刷新失败 (继续计算): {e}")

        # P1-1 修复: 优先使用 Futu LV2 实时价格
        price = _get_current_price(scheduler, symbol)

        if price <= 0:
            logger.warning(f"[动态仓位] {symbol} 无法获取价格, 使用默认 {DEFAULT_QTY} 股")
            try:
                from app.utils.watchlist_manager import get_order_qty
                return get_order_qty(symbol, default_qty=DEFAULT_QTY)
            except Exception as e:
                logger.debug(f'[动态仓位] 获取订单数量失败: {e}，使用默认 {DEFAULT_QTY}')
                return DEFAULT_QTY

        # P0 + P1-2 修复 (2026-05-01): ATR 动态止损 + session-aware 入场
        # atr 参数由调用方传入 (来自 market context)，避免重复获取
        stop_loss_price = price * 0.90  # 默认 fallback: 做多 10%
        if atr and atr > 0:
            # P1-2: 使用 session_vol_ratio 使入场/退出止损标准一致
            try:
                from app.core.shared_kernel import get_shared_kernel
                kernel = get_shared_kernel()
                session = kernel.get_current_session()
                session_ratio = kernel.get_session_vol_ratio(session)
            except Exception:
                session_ratio = 1.0  # 降级

            effective_atr = atr * session_ratio
            # F32 修复 (2026-05-12): 从 exit_policy.yaml 读取, 不再硬编码
            atr_mult = 2.0  # exit_policy.yaml atr_stop.initial (默认)
            if hasattr(scheduler, 'position_store') and scheduler.position_store.has(symbol):
                atr_mult = 1.8  # 已有持仓 → 更紧止损 (加仓场景)

            # 入场方向: 做多止损在下方，做空止损在上方
            if hasattr(direction, 'value'):
                is_short = direction.value == "SHORT"
            elif isinstance(direction, str):
                is_short = direction.upper() == "SHORT"
            else:
                is_short = False
            if is_short:
                stop_loss_price = price + effective_atr * atr_mult
                hard_stop = price * 1.15
                stop_loss_price = min(stop_loss_price, hard_stop)
            else:
                stop_loss_price = price - effective_atr * atr_mult
                hard_stop = price * 0.85
                stop_loss_price = max(stop_loss_price, hard_stop)

            logger.debug(
                f"[动态仓位] {symbol} ATR=${atr:.2f} session_ratio={session_ratio:.2f} "
                f"effective_atr=${effective_atr:.2f}, 动态止损 ${stop_loss_price:.2f} ({atr_mult}x)"
            )
        else:
            logger.debug(f"[动态仓位] {symbol} ATR 不可用, 使用固定 10% 止损")

        # ── 动态计算仓位 (P3 2026-05-02: 接入 PositionSizer 纯函数 + 资金自适应) ──
        # 2026-05-27: 支持 VOLATILITY_ADJUSTED 方法
        from app.risk.position_sizer import calc_position_size, calc_volatility_adjusted_size, load_config_from_yaml, get_sizing_method
        tiers, water_levels, guards = load_config_from_yaml()
        sizing_method = get_sizing_method()

        if sizing_method == "volatility_adjusted" and atr and atr > 0:
            # 获取 ATR 历史均值 (20天)
            atr_mean = atr  # 默认等于当前值 (scale=1.0)
            try:
                from app.core.config_container import get_config
                _cfg = get_config()
                # 尝试从 market_data 获取历史 ATR 均值
                _atr_mean_from_ctx = (context or {}).get("atr_mean", 0)
                if _atr_mean_from_ctx > 0:
                    atr_mean = _atr_mean_from_ctx
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

            sizing_result = calc_volatility_adjusted_size(
                equity=capital._state.current_equity,
                buying_power=capital._state.buying_power,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                atr_current=atr,
                atr_mean=atr_mean,
                tiers=tiers,
                water_levels=water_levels,
                drawdown_pct=capital._state.drawdown_pct,
                guards=guards,
            )
        else:
            sizing_result = calc_position_size(
                equity=capital._state.current_equity,
                buying_power=capital._state.buying_power,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                tiers=tiers,
                water_levels=water_levels,
                drawdown_pct=capital._state.drawdown_pct,
                guards=guards,
            )

        size = sizing_result.shares
        logger.info(
            f"[动态仓位] {symbol} ${price:.2f} | "
            f"止损 ${stop_loss_price:.2f} | "
            f"仓位={size} 股 (${size*price:,.0f}) | "
            f"tier={sizing_result.tier} | wl={sizing_result.water_level} | "
            f"pct={sizing_result.position_pct*100:.1f}%"
        )

        # Task #13 (2026-05-27): 叠加 regime_adapter 仓位缩放 (HIGH_VOL=50% etc.)
        # 与 VOLATILITY_ADJUSTED 不冲突: ATR scale 描述个股波动, regime scale 描述全市场状态
        # regime 从 shared_kernel / VIX 代理推断 (与上游 pre_trade_check 一致)
        try:
            _regime = None
            try:
                from app.core.shared_kernel import get_shared_kernel
                _kernel = get_shared_kernel()
                _regime = (
                    getattr(_kernel, "current_regime", None)
                    or getattr(_kernel, "market_regime", None)
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            if not _regime:
                # 以 VIX 作为 fallback (与 pre_trade_check 同逻辑)
                try:
                    from app.analysis.market_state import MarketStateChecker
                    _vix_data = MarketStateChecker()._fetch_vix()
                    _vix = _vix_data[0] if isinstance(_vix_data, tuple) else _vix_data
                    if isinstance(_vix, (int, float)) and _vix >= 25:
                        _regime = "HIGH_VOL"
                except Exception as _silent_e:
                    logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            if _regime and size > 0:
                from app.strategy.regime_adapter import apply_regime_position_scale
                _adj_size, _scale, _note = apply_regime_position_scale(size, _regime)
                if _adj_size != size and _scale < 1.0:
                    logger.info(
                        f"[动态仓位·Regime] {symbol} {_note}"
                    )
                    size = _adj_size
        except Exception as _e:
            logger.debug(f"[动态仓位·Regime] 应用缩放失败 (忽略): {_e}")

        # P0 修复 (2026-05-31): 接入 TradingMode 风控倍数
        # 模拟盘 multiplier=1.0, 实盘保守=0.5, 实盘正常=0.8
        try:
            from app.core.trading_mode import TradingMode
            _tm = TradingMode()
            _tm_mult = _tm.get_risk_multiplier()
            if _tm_mult < 1.0 and size > 0:
                _orig_size = size
                size = max(1, int(size * _tm_mult))
                logger.info(
                    f"[动态仓位·TradingMode] {symbol} {_tm.mode_label}: "
                    f"{_orig_size} → {size} 股 (×{_tm_mult})"
                )
        except Exception as _tme:
            logger.debug(f"[动态仓位·TradingMode] 应用风控倍数失败 (忽略): {_tme}")

        # W5 修复 (2026-05-02): 仅在 shares==0 时 fallback，warning 仅记录不阻断
        if sizing_result.warnings:
            for w in sizing_result.warnings:
                logger.warning(f"[动态仓位·告警] {symbol}: {w}")

        if size <= 0:
            logger.warning(f"[动态仓位] {symbol} 计算仓位为 0, 使用默认 {DEFAULT_QTY} 股")
            try:
                from app.utils.watchlist_manager import get_order_qty
                return get_order_qty(symbol, default_qty=DEFAULT_QTY)
            except Exception as e:
                logger.debug(f'[动态仓位] 获取订单数量失败: {e}，使用默认 {DEFAULT_QTY}')
                return DEFAULT_QTY

        return size

    except Exception as e:
        logger.warning(f"[动态仓位] {symbol} 计算异常: {e}, 使用默认 {DEFAULT_QTY} 股")
        try:
            from app.utils.watchlist_manager import get_order_qty
            return get_order_qty(symbol, default_qty=DEFAULT_QTY)
        except Exception:
            return DEFAULT_QTY


# ═══════════════════════════════════════════════════════════════════
#  期权下单
# ═══════════════════════════════════════════════════════════════════

def _submit_put_option(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]):
    """SHORT 信号处理：买入 ATM Put 期权替代卖空"""
    try:
        from app.core.config_container import get_config
        _oc = get_config().get_section("options", {})
    except Exception:
        _oc = {}

    # P0 加固 (2026-05-24): 期权硬封锁 — options.enabled=False 时绝对禁止期权下单
    # 根因: 旧逻辑在 enabled=False + stock_short=True 时仍继续执行期权下单，
    #        导致 5/21-5/22 期间 SHORT_PROXY_OPTION 仍产生 $30,237 亏损。
    # 修复: enabled=False 直接 return，做空路由由上层调用方处理（正股做空路径）。
    if not _oc.get("ENABLED", True):
        logger.warning(
            f"[期权·硬封锁] {symbol} OPTIONS_ENABLED=False，禁止期权下单。"
            f"做空请使用正股做空路径。"
        )
        return

    price = context.get("price", 0)
    if price <= 0:
        logger.warning(f"[期权] {symbol} 价格为 0，取消期权下单")
        return

    # 资金 SSOT 修复 (2026-04-30): 优先使用百分比，自动适配模拟盘/实盘
    max_cost_pct = float(_oc.get("MAX_COST_PCT", 0.005))  # 默认 0.5% 总资金
    max_cost_abs = float(_oc.get("MAX_OPTION_COST", 2000))  # 向下兼容
    # 如果配置了百分比，从 capital_safety 获取总资金
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        total_equity = capital._state.current_equity if capital._state.current_equity > 0 else max_cost_abs / max_cost_pct
        max_cost = max(max_cost_abs, total_equity * max_cost_pct)
    except Exception:
        max_cost = max_cost_abs
    min_dte   = int(_oc.get("MIN_DTE", 7))
    max_dte   = int(_oc.get("MAX_DTE", 21))

    # 尝试注入 quote_ctx（execution_engine 已连接时）
    if (scheduler.option_selector.quote_ctx is None
            and scheduler.execution_engine
            and hasattr(scheduler.execution_engine, "broker_adapter")):
        adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
        if adapter and adapter.is_connected() and adapter.quote_ctx:
            scheduler.option_selector.quote_ctx = adapter.quote_ctx

    # 查询 ATM Put 合约
    contract = scheduler.option_selector.get_atm_put(
        symbol, price, min_dte=min_dte, max_dte=max_dte
    )
    if contract is None:
        logger.error(f"[期权] {symbol} 无法获取 Put 期权合约，放弃 SHORT 信号")
        return

    # 计算手数
    qty = OptionSelector.calc_option_cost(contract, max_cost)
    cost_total = qty * contract.last_price * 100

    logger.info(
        f"[期权] {symbol} 买入 Put {contract.option_code} "
        f"qty={qty} price=${contract.last_price:.2f} "
        f"K={contract.strike_price} expiry={contract.expiry_date} "
        f"DTE={contract.dte} cost≈${cost_total:.0f} "
        f"{'[模拟]' if contract.is_simulated else '[真实]'}"
    )

    # 下单
    try:
        adapter = None
        if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
            adapter = getattr(scheduler.execution_engine, "broker_adapter", None)

        if adapter:
            order_result = adapter.submit_option_order(
                option_code=contract.option_code,
                qty=qty,
                price=contract.ask,  # 以 ask 价买入（确保成交）
                direction="BUY",
            )
        else:
            order_result = {
                "order_id": f"OPT_{contract.option_code}_BUY",
                "futu_order_id": f"SIM_{contract.option_code[:20]}",
                "status": "simulated_fill",
            }

        logger.info(f"[期权] 下单结果：{order_result}")

        # 只有真实成交/提交成功才创建持仓，拒绝/失败时跳过防止幽灵仓
        if order_result.get("status") not in ("submitted", "filled", "simulated_fill"):
            logger.warning(f"[期权] {symbol} 下单未成功 (status={order_result.get('status')})，跳过持仓创建")
            return

    except Exception as e:
        logger.error(f"[期权] {symbol} 期权下单异常：{e}", exc_info=True)
        return

    # 写入 option_positions 表
    try:
        scheduler.trade_store.upsert_option_position({
            "symbol":             symbol,
            "option_code":        contract.option_code,
            "direction":          "SHORT_PROXY",
            "qty":                qty,
            "entry_price":        contract.last_price,
            "strike_price":       contract.strike_price,
            "expiry_date":        contract.expiry_date,
            "entry_time":         now_et().isoformat(),
            "cost_total":         cost_total,
            "signal_strength":    signal.strength,
            "signal_confidence":  signal.confidence,
            "is_simulated":       contract.is_simulated,
        })
    except Exception as e:
        logger.warning(f"[期权] 写入期权持仓记录失败：{e}")

    # 在 position_store 中登记占位持仓（供监控循环识别）
    try:
        position = Position(
            strategy_id="voting_llm_option",
            # Task #15 (2026-05-27): 期权路径补 trace_id / signal_id 以贯通全链路
            signal_id=getattr(signal, "signal_id", "") or "",
            trace_id=getattr(signal, "trace_id", "") or "",
            symbol=symbol,
            direction=Direction.SHORT,
            size=float(qty),
            avg_entry_price=contract.last_price,
            initial_sl=contract.last_price * (1 - float(_oc.get("STOP_LOSS_RATIO", 0.3))),
            initial_tp=contract.last_price * float(_oc.get("TAKE_PROFIT_RATIO", 2.0)),
            current_sl=contract.last_price * (1 - float(_oc.get("STOP_LOSS_RATIO", 0.3))),
            take_profit_levels=[
                {"ratio": 1.0, "price": contract.last_price * float(_oc.get("TAKE_PROFIT_RATIO", 2.0))},
            ],
            time_stop_hours=contract.dte * 24,
            market_regime=signal.market_regime,
        )
        position.entry_time = now_et()
        # 在 metadata 中标记为期权持仓 + 来源
        position.metadata = {
            "is_option":    True,
            "option_code":  contract.option_code,
            "strike_price": contract.strike_price,
            "expiry_date":  contract.expiry_date,
            "dte":          contract.dte,
            "cost_total":   cost_total,
            "is_simulated": contract.is_simulated,
            "source":       "voting_llm_option",  # P0 修复：使用规范命名，确保正确识别为 AUTO_MAIN
        }
        scheduler.position_store.add(symbol, position)
    except Exception as e:
        logger.warning(f"[期权] 登记期权持仓到内存失败：{e}")

    # P0 修复 (2026-05-21): 期权路径缺少 post_trade_hook 调用
    # 与 _submit_order LONG/SHORT 对齐，确保归因/mirror/push 副作用执行
    try:
        from app.pipeline.post_trade_hook import on_position_opened
        on_position_opened(position, signal, context or {}, scheduler)
    except Exception as _pte:
        logger.debug(f"[PostTradeHook·Option] 提交失败 (不影响下单): {_pte}")

    # 冷却
    scheduler._set_cooldown(symbol)

    # 事件记录
    scheduler.event_store.append(TradeEvent(
        event_type="option_order_submitted",
        signal_id=signal.signal_id,
        order_id=order_result.get("order_id", ""),
        data={
            "symbol":        symbol,
            "option_code":   contract.option_code,
            "direction":     "SHORT_PROXY",
            "qty":           qty,
            "price":         contract.last_price,
            "strike":        contract.strike_price,
            "expiry":        contract.expiry_date,
            "dte":           contract.dte,
            "cost_total":    cost_total,
        },
        source="scheduler",
        severity=AlertLevel.INFO,
    ))


# ═══════════════════════════════════════════════════════════════════
#  直接做空策略
# ═══════════════════════════════════════════════════════════════════

def _try_direct_short_only(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]):
    """直接卖空（不尝试期权）"""
    # 简化实现：复用普通下单逻辑
    _submit_order(scheduler, symbol, signal, context, None)


def _try_direct_short_with_fallback(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]):
    """优先直接卖空，失败降级买 Put"""
    try:
        # 尝试直接卖空
        _submit_order(scheduler, symbol, signal, context, None)
    except Exception as e:
        logger.warning(f"[SHORT] {symbol} 直接卖空失败，降级买 Put: {e}")
        _submit_put_option(scheduler, symbol, signal, context)


# ═══════════════════════════════════════════════════════════════════
#  核心下单入口 (风控门禁 → 持仓冲突 → 仓位计算 → 止盈止损 → 执行)
# ═══════════════════════════════════════════════════════════════════


def _check_gates_and_conflicts(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]) -> bool:
    """检查所有风控门禁与持仓冲突。返回 True 如果允许继续下单。"""
    # P0 审计修复 (2026-05-30): 信号质量门控 — 观测模式
    # 根因: 5/29 五笔交易 signal_strength=0, confidence=0.0 (voting_llm 链路未传递)
    # 策略: 先观测(日志), 不硬拦截. 待 voting_llm 信号传播修复后收紧为 hard block.
    try:
        _sig_str = int(getattr(signal, 'strength', 0) or 0)
        _sig_conf = float(getattr(signal, 'confidence', 0.0) or 0.0)
        _strategy = getattr(signal, 'strategy_id', '') or ''
        if _sig_str <= 0 and _sig_conf <= 0.0:
            if 'voting_llm' not in _strategy:
                # 非 voting_llm 策略: strength=0 & conf=0 → 拦截 (这些策略应有有效信号)
                logger.warning(
                    f"🚫 [SignalGate] 拦截 {symbol}: strength={_sig_str}, conf={_sig_conf:.2f}, "
                    f"strategy={_strategy} (非voting_llm, 信号不应为0)"
                )
                return False
            else:
                # voting_llm: 已知信号传播bug, 仅记录不拦截
                logger.warning(
                    f"⚠️ [SignalGate·观测] {symbol}: strength={_sig_str}, conf={_sig_conf:.2f} "
                    f"(voting_llm 信号传播bug, 放行但需修复)"
                )
    except Exception as _sg_err:
        logger.debug(f"[SignalGate] 检查异常 (放行): {_sg_err}")

    # 🔴 P0 关键修复: 全局风险状态机门禁检查
    # 在一切交易逻辑之前，检查系统是否处于 RISK_OFF 或 HALT 状态
    try:
        from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskAction
        from app.us_pipeline.risk_state_machine import RiskState  # For logging

        sm = RiskStateMachine()
        # Check if OPEN is allowed
        if not sm.is_trade_allowed(RiskAction.OPEN):
            current_state = sm.get_state().value
            logger.warning(f"🚫 [风控拦截] 拒绝交易 {symbol}: 当前系统处于 {current_state} 状态")
            return False
    except Exception as e:
        logger.error(f"⚠️ 风险状态机检查失败 (放行): {e}")

    # P3 优化 2026-04-29: 中概 ADR 集中度限制
    try:
        from app.risk.capital_safety import get_capital_safety_system, CHINA_ADRS
        adr_allowed, adr_reason = get_capital_safety_system().check_adr_limits(
            symbol, scheduler.position_store.all()
        )
        if not adr_allowed:
            logger.warning(f"[ADR限制] {symbol} 被拦截: {adr_reason}")
            from app.pipeline.position_monitor import _notify_risk_blocked
            _notify_risk_blocked(scheduler, symbol, f"ADR集中度: {adr_reason}")
            return False
    except Exception as e:
        logger.debug(f"[ADR限制] 检查异常 (放行): {e}")

    # ── VIX Regime 仓位系数 + 止损调整 ──
    vix_pos_mult = context.get("vix_position_multiplier", 1.0)
    vix_sl_adj = context.get("vix_stop_loss_adjustment", 1.0)
    vix_regime = context.get("vix_regime", "normal")

    if vix_pos_mult != 1.0 or vix_sl_adj != 1.0:
        # 调整信号 metadata 中的仓位建议
        if hasattr(signal, 'metadata') and signal.metadata:
            original_pct = signal.metadata.get("position_pct", 0.10)
            adjusted_pct = round(original_pct * vix_pos_mult, 4)
            signal.metadata["position_pct"] = adjusted_pct
            signal.metadata["vix_regime"] = vix_regime
            signal.metadata["vix_position_multiplier"] = vix_pos_mult
            signal.metadata["vix_stop_loss_adjustment"] = vix_sl_adj

            logger.info(
                "[VIX Regime] %s | regime=%s | 仓位系数=%.2f (%.0f%%→%.0f%%) | 止损调整=%.2f",
                symbol, vix_regime, vix_pos_mult,
                original_pct * 100, adjusted_pct * 100, vix_sl_adj
            )

            # 危机模式清仓拦截
            if vix_regime == "crisis" and vix_pos_mult == 0.0:
                logger.warning(f"🚫 [VIX 危机] {symbol} VIX 危机模式，拒绝开新仓")
                return False

    # Phase 3 新增：性能衰减检查 (Performance Decay)
    # 根据近期策略胜率动态调整信号强度，防止"连败"期间继续重仓
    try:
        from app.utils.performance_decay import get_decay_factor

        db_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ftseek_us.db")
        strategy = context.get("strategy", "unknown")
        decay = get_decay_factor(db_path, strategy, lookback_days=5)

        if decay < 1.0:
            original_strength = signal.strength
            signal.strength = max(10, original_strength * decay)
            logger.warning(f"📉 [性能衰减] 策略 {strategy} 近期表现不佳 -> 信号降权 (x{decay})")

            # 如果强度降得太低，直接拦截
            if signal.strength < 30:
                logger.warning(f"🚫 [性能衰减] {symbol} 信号降权后过低 ({signal.strength})，自动拦截")
                return False
    except Exception as e:
        logger.debug(f"[性能衰减] 检查失败 (不影响主流程): {e}")

    # ── Phase US-1: 美股盘前作战计划门禁 ──
    # ── P0 新增 (2026-04-25): 做空专用高门槛检查 ──
    direction_str = "BUY" if signal.direction == "LONG" else "SELL"
    is_short = (direction_str == "SELL")

    # ── P0 (2026-05-29): LONG 方向 strength 门禁 ──
    # 数据: strength 1-29 累计亏损 $18,757 (占全部亏损73%)
    # 之前 LONG 仅依赖 post-decay <30 拦截, 大量低信心信号漏过
    if not is_short:
        _long_min = 35  # 修复 (2026-06-02): 50→35, 与 unified_config.yaml long_min_strength 对齐
        try:
            from app.core.config_container import get_config as _gc_long
            _lt_cfg = _gc_long().get_dotted("risk.pre_trade", {}) or {}
            _long_min = int(_lt_cfg.get("long_min_strength", 35))
        except Exception:
            pass
        if signal.strength < _long_min:
            logger.warning(
                f"🚫 [做多门禁] {symbol} LONG 信号强度不足 "
                f"(strength={signal.strength} < {_long_min})"
            )
            get_rejection_logger().record(symbol,
                f"long_strength={signal.strength}<{_long_min}",
                "LONG_STRENGTH_WEAK", strength=signal.strength,
                confidence=None, direction="LONG")
            return False

    # ── P0 (2026-05-29): HIGH_VOL 硬拦截 ──
    # 数据: HIGH_VOL regime 133笔, 累计亏损 $39,537
    #   LONG+HIGH_VOL: 7.1% WR — 几乎必输
    #   SHORT+HIGH_VOL: 23.4% WR — 仍然大亏
    # pre_trade_check 已有 VIX>=25 拦截, 这里补充 regime 级拦截
    try:
        _hv_regime = str(getattr(signal, 'market_regime', '') or '').upper()
        if _hv_regime == "HIGH_VOL":
            logger.warning(
                f"🚫 [HIGH_VOL门禁] {symbol} 高波动市况禁止开新仓 "
                f"(regime={_hv_regime}, strength={signal.strength})"
            )
            get_rejection_logger().record(symbol,
                f"HIGH_VOL regime blocked",
                "HIGH_VOL_BLOCKED", strength=signal.strength,
                confidence=None, direction=direction_str)
            return False
    except Exception:
        pass

    # RFC-053 (2026-05-30): RANGE regime LONG 拦截
    # 数据: LONG+RANGE 22笔 9.1%WR -$3,058 — 无趋势市况做多几乎必输
    # SHORT+RANGE 54笔 25.9%WR -$2,651 — 仍允许但有风险
    try:
        _range_regime = str(getattr(signal, 'market_regime', '') or '').upper()
        if _range_regime == "RANGE" and not is_short:
            logger.warning(
                f"🚫 [RANGE门禁] {symbol} 无趋势市况禁止做多 "
                f"(regime={_range_regime}, strength={signal.strength})"
            )
            get_rejection_logger().record(symbol,
                f"RANGE regime blocks LONG",
                "RANGE_LONG_BLOCKED", strength=signal.strength,
                confidence=None, direction=direction_str)
            return False
    except Exception:
        pass

    if is_short:
        # 做空门禁 (P0-辩论修复 2026-05-26: direct语义, 无需反转)
        # strength 越高 = 信号越强 (LONG/SHORT 均同)
        # P1 (2026-05-28 任务#43): 门限可配置 + 计划标的 bonus
        # 原固定门限 (保留处理表述供回归测试识别): signal.strength < 50
        _short_threshold = 50
        _bp_bonus_enabled = True
        _bp_bonus = 0.25
        _bp_floor = 30
        try:
            from app.core.config_container import get_config as _gc_short
            _pt_cfg = _gc_short().get_dotted("risk.pre_trade", {}) or {}
            _short_threshold = int(_pt_cfg.get("short_min_strength", 50))
            _bp_bonus_enabled = bool(_pt_cfg.get("battle_plan_bonus_enabled", True))
            _bp_bonus = float(_pt_cfg.get("battle_plan_bonus", 0.25))
            _bp_floor = int(_pt_cfg.get("min_effective_strength_floor", 30))
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
        # battle_plan bonus: 计划标的降低门槛 (默认 25%, 低于 floor 则取 floor)
        _eff_short_threshold = _short_threshold
        _in_plan = False
        if _bp_bonus_enabled:
            try:
                from app.us_pipeline.battle_plan_manager import USBattlePlanManager
                _bp_mgr = USBattlePlanManager()
                _info = _bp_mgr.get_target_info(symbol) or {}
                if _info:
                    _status = str(_info.get("status", "")).upper()
                    _catalyst = str(_info.get("catalyst", ""))
                    if (
                        _status in ("ACTIVE", "VALID", "ANY", "")
                        and "Watchlist fallback" not in _catalyst
                    ):
                        _in_plan = True
                        _adj = int(round(_short_threshold * (1.0 - max(0.0, min(0.5, _bp_bonus)))))
                        _eff_short_threshold = max(_bp_floor, _adj)
            except Exception:
                _in_plan = False

        if signal.strength < _eff_short_threshold:
            _bp_tag = " [BP]" if _in_plan else ""
            logger.warning(
                f"🚫 [做空门禁] {symbol} 做空信号强度不足 "
                f"(strength={signal.strength} < {_eff_short_threshold}{_bp_tag})"
            )
            get_rejection_logger().record(symbol,
                f"short_strength={signal.strength}<{_eff_short_threshold}",
                "SHORT_EFFECTIVE_WEAK", strength=signal.strength,
                confidence=None, direction="SHORT")
            return False
        # 三重确认: RSI + MACD + 均线 (模拟盘放宽: 2→0)
        rsi = context.get("rsi", 50)
        macd_signal_val = context.get("macd_signal", 0)
        price = context.get("price", 0)
        ma20 = context.get("ma20", 0)

        triple_confirm = 0
        if rsi > 65: triple_confirm += 1
        if macd_signal_val < 0: triple_confirm += 1
        if ma20 > 0 and price > ma20: triple_confirm += 1

        if triple_confirm < 0:  # 模拟盘: 2→0, 禁用三重确认
            logger.warning(
                f"🚫 [做空门禁] {symbol} 三重确认不足 ({triple_confirm}/2): "
                f"RSI={rsi:.0f} MACD={macd_signal_val:.2f} price={price:.2f} MA20={ma20:.2f}"
            )
            get_rejection_logger().record(symbol,
                f"triple_confirm={triple_confirm}/2 RSI={rsi:.0f} MACD={macd_signal_val:.2f}",
                "SHORT_TRIPLE_CONFIRM", strength=signal.strength,
                confidence=None, direction="SHORT")
            return False
        logger.info(
            f"✅ [做空门禁] {symbol} 通过 (strength={signal.strength}, "
            f"三重确认={triple_confirm}/3)"
        )

        # 幻方增强门禁 (2026-04-29): 借券成本 + 轧空风险 + 市况适配
        try:
            from app.risk.short_specific_checks import (
                check_borrow_cost_with_conviction,
                check_short_squeeze,
                check_regime_for_short,
            )
            # Principle 1: 借券成本门控 (含高置信度豁免)
            # P0 修复 (2026-04-30): 传 signal.strength 而非 short_effective
            # check_borrow_cost_with_conviction 内部用 raw strength 判断豁免阈值
            ok, reason = check_borrow_cost_with_conviction(symbol, signal.strength)
            if not ok:
                logger.warning(f"🚫 [借券成本] {symbol} {reason}")
                return False
            # Principle 2: 轧空检测
            ok, reason = check_short_squeeze(symbol)
            if not ok:
                logger.warning(f"🚫 [轧空风险] {symbol} {reason}")
                return False
            # Principle 3: 市况适配 (牛市提高门槛)
            regime = context.get("market_regime", "")
            ok, reason = check_regime_for_short(regime, signal.strength)
            if not ok:
                logger.warning(f"🚫 [市况适配] {symbol} {reason}")
                return False
        except ImportError as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        allowed, reason = mgr.check_trade(symbol, direction_str, strength=signal.strength, confidence=signal.confidence)
        if not allowed:
            logger.warning(f"🚫 [US Plan Gate] 拦截交易 {symbol}: {reason}")
            return False
    except Exception as e:
        logger.debug(f"[US Plan Gate] 检查异常 (放行): {e}")

    # Generate a trace_id for this specific trade attempt
    trace_id = str(uuid.uuid4())[:8]
    logger.info(f"[TradeGuard] 开始评估订单 {symbol} | Trace ID: {trace_id}")

    # ── P0 新增: 价格动量门控 (防止逆势下单) ──
    try:
        from app.risk.price_momentum_gate import get_price_momentum_gate
        direction_str_check = signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction)
        gate = get_price_momentum_gate()
        passed, reason = gate.check(symbol, direction_str_check)
        if not passed:
            logger.warning(f"🚫 [价格动量门控] 拒绝交易 {symbol}: {reason}")
            return False
        else:
            logger.debug(f"[价格动量门控] {symbol} 通过: {reason}")
    except Exception as e:
        logger.debug(f"[价格动量门控] 检查异常 (放行): {e}")

    # ── P0: 交易保护网关检查 ──
    from app.core.trade_guard import TradeGuard, GuardDecision

    # 获取账户净值
    try:
        from app.risk.capital_safety import get_capital_safety_system
        equity = get_capital_safety_system()._state.current_equity
        if equity <= 0: equity = 100000.0
    except (AttributeError, ImportError):
        equity = 100000.0

    guard = TradeGuard({
        "equity": equity,
        "data_dir": os.path.join(os.path.dirname(__file__), "..", "data")
    })

    # 构建上下文 (包含 trace_id 以便全链路追踪)
    guard_context = {
        "trace_id": trace_id,
        "symbol": symbol,
        "direction": "OPEN_LONG" if signal.direction.value == "LONG" else "OPEN_SHORT",
        "signal_strength": signal.strength,
        "volatility": context.get("atr", 0) / max(0.001, context.get("price", 1)),
        "recent_news": [], 
        "is_reversal": False,
    }

    result = guard.evaluate(guard_context)
    decision = result["decision"]
    reason = result["reason"]
    result_trace = result.get("trace_id", trace_id)

    # 拦截逻辑
    if decision == GuardDecision.BLOCK_OPEN_LONG:
        logger.warning(f"[TradeGuard] 禁止开多 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.BLOCK_OPEN_SHORT:
        logger.warning(f"[TradeGuard] 禁止开空 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.DELAY_AND_WAIT:
        logger.info(f"[TradeGuard] 延迟交易 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.ALLOW_CLOSE_ONLY:
        logger.info(f"[TradeGuard] {symbol} 仅允许平仓逻辑 | Trace: {result_trace}")
        if not scheduler.position_store.has(symbol):
            return False

    # 检查 broker 持仓方向冲突
    if (scheduler.execution_engine
            and scheduler.execution_engine.broker_adapter
            and scheduler.execution_engine.broker_adapter.is_connected()):
        try:
            broker_positions = scheduler.execution_engine.broker_adapter.get_positions()
            for bp in broker_positions:
                if bp["symbol"] == symbol:
                    qty = bp.get("qty", 0)
                    if qty < 0 and signal.direction == Direction.LONG:
                        logger.warning(
                            f"[持仓冲突] {symbol} broker 有 {abs(qty):.0f} 股空头, "
                            f"新信号为 LONG, 自动平仓后再开仓"
                        )
                        success = _close_short_for_long(scheduler, symbol, abs(qty), bp.get("current_price", 0))
                        if not success:
                            logger.warning(f"[持仓冲突] {symbol} 空头平仓失败, 同步 broker 持仓状态")
                            scheduler.position_store.remove(symbol)
                        return False  # 平仓后等待下轮再开新仓
                    elif qty > 0 and signal.direction == Direction.SHORT:
                        logger.warning(
                            f"[持仓冲突] {symbol} broker 有 {qty:.0f} 股多头, "
                            f"新信号为 SHORT, 自动平仓后再做空"
                        )
                        success = _close_long_for_short(scheduler, symbol, qty, bp.get("current_price", 0))
                        if not success:
                            logger.warning(f"[持仓冲突] {symbol} 多头平仓失败, 同步 broker 持仓状态")
                            scheduler.position_store.remove(symbol)
                        return False
        except Exception as e:
            logger.warning(f"[持仓冲突] 检查 broker 持仓失败 (继续执行): {e}")

    # 检查 position_store 中是否存在反向持仓
    if scheduler.position_store.has(symbol):
        existing_pos = scheduler.position_store.get(symbol)
        existing_dir = existing_pos.direction
        if existing_dir != signal.direction:
            logger.warning(
                f"[持仓冲突] {symbol} 已有{existing_dir.value}持仓, "
                f"新信号为{signal.direction.value}, 需先平仓"
            )
            _close_position_for_conflict(scheduler, existing_pos)
            return False

    # ── P0 修复 (2026-05-02): PRE_CLOSE_EXIT 竞态防护 ──
    # 防止平仓后立即重入 (NVDA #11→#12 竞态: 7秒内同价重复入场)
    # 检查 trade_records 是否有近期的 PRE_CLOSE_EXIT 记录
    try:
        from app.storage.trade_store import TradeStore as _TS
        trade_store = getattr(scheduler, 'trade_store', None)
        if trade_store:
            cutoff = (now_et() - timedelta(minutes=5)).isoformat()
            conn = trade_store._get_conn()
            recent_close = conn.execute(
                "SELECT exit_time, exit_reason FROM trade_records "
                "WHERE symbol = ? AND exit_time > ? ORDER BY exit_time DESC LIMIT 1",
                (symbol, cutoff)
            ).fetchone()
            conn.close()
            if recent_close and recent_close[1] == 'PRE_CLOSE_EXIT':
                logger.warning(
                    f"[竞态防护] {symbol} 5 分钟内有 PRE_CLOSE_EXIT 记录，"
                    f"拒绝重复入场 (最近平仓: {recent_close[0][:19]})"
                )
                return False
    except Exception as e:
        logger.debug(f"[竞态防护] 检查失败 (继续): {e}")

    # Task #15 (2026-05-27): 风控全部通过，输出统一 [RISK] 日志前缀
    try:
        _trace_id = getattr(signal, "trace_id", "") or "NONE"
        logger.info(
            f"[RISK][trace:{_trace_id}] Pre-trade check passed for {symbol} "
            f"(dir={signal.direction.value if hasattr(signal.direction, 'value') else signal.direction}, "
            f"strength={signal.strength}, confidence={signal.confidence:.2f})"
        )
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    return True

def _handle_pyramid_add(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]) -> bool:
    """处理 Livermore 金字塔加仓。返回 True 如果执行了加仓。"""
    # ── Livermore 金字塔加仓处理 (P1-1) ──
    if scheduler.position_store.has(symbol) and signal.direction == Direction.LONG:
        try:
            from app.risk.pyramid_engine import get_pyramid_engine
            pyramid = get_pyramid_engine()
            position = scheduler.position_store.get(symbol)
            price = context.get("price", 0)

            # 检查是否满足加仓条件
            max_total_size = float(context.get("max_position_size", 50.0))
            can_add, add_reason, add_params = pyramid.can_add_position(
                position=position,
                current_price=price,
                max_total_size=max_total_size,
            )

            if can_add:
                # 执行加仓 (更新现有持仓)
                add_result = pyramid.execute_add_on(
                    position=position,
                    add_params=add_params,
                    order_func=lambda **kwargs: {"success": True, "order_id": f"ADD_{symbol}_{int(time.time())}"},
                )

                if add_result["success"]:
                    logger.info(f"[金字塔加仓] {symbol} 加仓完成: {add_reason}")

                    # P1 优化 (2026-04-18): 随机延迟 10-45 秒
                    import random
                    import time as _time
                    delay = random.uniform(10, 45)
                    logger.info(f"[金字塔加仓] 触发随机延迟 {delay:.0f}s 以分散系统性风险 (Beta 风险)")
                    _time.sleep(delay)

                    # 记录加仓事件
                    scheduler.event_store.append(TradeEvent(
                        event_type="pyramid_add_on",
                        signal_id=signal.signal_id,
                        trace_id=signal.trace_id,
                        position_id=position.position_id,
                        data={
                            "symbol": symbol,
                            "level": add_params['level'],
                            "add_size": add_params['add_size'],
                            "add_price": add_params['add_price'],
                            "new_avg_price": add_params['new_avg_price'],
                            "new_sl": add_params['new_sl'],
                        },
                        source="scheduler",
                        severity=AlertLevel.INFO,
                    ))
                    return  # 加仓完成, 不需要创建新持仓
                else:
                    logger.warning(f"[金字塔加仓] {symbol} 加仓执行失败: {add_result.get('reason')}")
            else:
                logger.debug(f"[金字塔加仓] {symbol} 跳过: {add_reason}")
                return  # 不满足加仓条件, 跳过
        except Exception as e:
            logger.warning(f"[金字塔加仓] {symbol} 异常 (继续常规下单): {e}")

    return False

def _load_short_position_ratio() -> float:
    """幻方 Principle 4: 做空非对称仓位比例 (从 ConfigContainer SSOT)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        ps = cfg.get_dotted("options.short_strategy.stock_short.position_sizing", {})
        return float(ps.get("short_to_long_ratio", 0.60))
    except Exception:
        return 0.60


def _load_short_exit_params() -> Tuple[float, float]:
    """读取做空止损/止盈比例 (从 ConfigContainer SSOT)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        sc = cfg.get_dotted("options.short_strategy.stock_short", {})
        sl = abs(float(sc.get("stop_loss_pct", -0.08)))
        tp = float(sc.get("take_profit_pct", 0.10))
        return sl, tp
    except Exception:
        return 0.08, 0.10


def _execute_short(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """做空路由: 正股做空 → Put 期权降级 (幻方增强版)

    策略:
    1. 配置 stock_short.enabled=true 且非模拟盘 → 尝试正股做空
    2. 正股做空失败/不支持 → 自动降级到 Put 期权
    3. 模拟盘 → 直接 Put 期权 (现有行为)
    """
    if signal.direction != Direction.SHORT:
        return

    # P0 加固 (2026-05-24): 做空路由修复 — 模拟盘也走正股做空
    # 根因: 旧逻辑 `stock_short_enabled and not is_sim` 导致模拟盘永远跳过正股做空，
    #        直接走 _submit_put_option → 被硬封锁 return → SHORT 信号静默丢弃
    # 修复: simulated_mode_force_options=false 时，模拟盘也尝试正股做空
    try_direct = False
    options_disabled = False
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        stock_short_enabled = cfg.get_dotted("options.short_strategy.stock_short.enabled", False)
        force_options = cfg.get_dotted("options.short_strategy.simulated_mode_force_options", False)
        options_enabled = cfg.get_dotted("options.enabled", True)
        options_disabled = not options_enabled
        is_sim = cfg.is_simulated() if hasattr(cfg, 'is_simulated') else True
        if stock_short_enabled:
            # 非模拟盘 → 直接做空
            # 模拟盘 + force_options=false → 也直接做空 (Futu 模拟盘已支持)
            # 模拟盘 + force_options=true → 走期权路径
            if not is_sim or not force_options:
                try_direct = True
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    if try_direct:
        try:
            _execute_stock_short(scheduler, symbol, signal, context, voting_result)
            return
        except Exception as e:
            logger.warning(f"[做空] {symbol} 正股做空失败，降级到 Put 期权: {e}")
            # P0-3 同步修复 (2026-05-03): 清理可能已添加的幽灵仓
            try:
                scheduler.position_store.remove(symbol)
            except (KeyError, Exception) as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    # 降级路径: Put 期权 (仅在期权启用时有效)
    if options_disabled:
        logger.warning(
            f"[做空·路由终止] {symbol} 正股做空不可用/失败，且期权已禁用(options.enabled=False)，"
            f"SHORT 信号放弃执行"
        )
        return
    try:
        if hasattr(scheduler, 'option_selector') and scheduler.option_selector is not None:
            if (scheduler.option_selector.quote_ctx is None
                    and scheduler.execution_engine
                    and hasattr(scheduler.execution_engine, "broker_adapter")):
                adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
                if adapter and adapter.is_connected() and adapter.quote_ctx:
                    scheduler.option_selector.quote_ctx = adapter.quote_ctx
            context["option_selector"] = scheduler.option_selector
        else:
            logger.debug(f"[做空] {symbol} option_selector 未初始化，将使用模拟模式")

        _submit_put_option(scheduler, symbol, signal, context)
    except Exception as e:
        logger.error(f"[做空] {symbol} Put 期权降级也失败: {e}", exc_info=True)


def _execute_stock_short(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """执行正股做空 — 幻方原则增强版

    功能:
    - 非对称仓位 (做空 = 做多 × short_to_long_ratio)
    - exit_policy.yaml short_overrides 止损止盈
    - 技术破位阻力位追踪
    - broker_adapter.try_direct_short() 调用
    - Position / Order 创建

    P2 2026-05-01 (G6+G7): Session-aware entry + entry_session in trail_state
    """
    price = round(float(context.get("price", 0)), 2)
    atr = float(context.get("atr", price * 0.02))

    if price <= 0:
        logger.warning(f"[正股做空] {symbol} 价格为 0，取消")
        return

    # ⭐ Pre-Entry Signal Gate (2026-05-19) — SHORT 入场前 LLM 拦截
    try:
        from app.llm.pre_entry_gate import check_gate
        sig_dict = {
            "strength": int(getattr(signal, "strength", 0) or 0),
            "confidence": float(getattr(signal, "confidence", 0) or 0),
            "factor_detail": (signal.metadata or {}).get("_factor_detail", {}) if hasattr(signal, "metadata") else {},
        }
        gate = check_gate(symbol, "SHORT", sig_dict, market_ctx=context)
        if not gate.go:
            logger.warning(
                f"🚫 [PreEntryGate·SHORT] {symbol} 被 LLM 拦截: {gate.reason} "
                f"(confidence={gate.confidence:.2f}, source={gate.source})"
            )
            try:
                get_rejection_logger().record(
                    symbol, gate.reason or "pre_entry_gate_no",
                    "PRE_ENTRY_GATE", strength=getattr(signal, "strength", 0),
                    confidence=getattr(signal, "confidence", 0), direction="SHORT",
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return
    except Exception as _ge:
        logger.debug(f"[PreEntryGate·SHORT·exception] {symbol} 检查失败 (放行): {_ge}")

    # P2 2026-05-01: 获取当前 session (G6 + G7)
    from app.core.shared_kernel import get_shared_kernel
    kernel = get_shared_kernel()
    entry_session = kernel.get_current_session()
    session_vol_ratio = kernel.get_session_vol_ratio(entry_session)

    # 非对称仓位 (幻方 Principle 4)
    long_qty = _get_order_qty(scheduler, symbol, atr, direction="SHORT", context=context)
    short_ratio = _load_short_position_ratio()
    quantity = max(1, int(long_qty * short_ratio))

    # 做空差异化止损止盈 (幻方 Principle 1-2) + Session-aware (G6)
    sl_pct, tp_pct = _load_short_exit_params()
    if session_vol_ratio != 1.0:
        sl_pct = sl_pct * session_vol_ratio
        tp_pct = tp_pct * session_vol_ratio
        logger.info(
            f"[Session止损·做空] {symbol} session={entry_session} "
            f"vol_ratio={session_vol_ratio:.2f} → "
            f"SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%"
        )
    stop_loss = round(price * (1 + sl_pct), 2)
    take_profit = round(price * (1 - tp_pct), 2)

    # 技术破位阻力位 (幻方 Principle 7)
    resistance = float(context.get("resistance_20d", price * 1.05))

    # 调用 Broker 直接做空
    adapter = None
    if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
        adapter = scheduler.execution_engine.broker_adapter

    if adapter is None or not adapter.is_connected():
        raise RuntimeError("broker_adapter 不可用")

    result = adapter.try_direct_short(symbol, quantity, price)
    if not result.get("success"):
        raise RuntimeError(f"try_direct_short 失败: {result.get('error')}")

    # 创建 Position
    position = Position(
        strategy_id=signal.strategy_id or "voting_llm",
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=Direction.SHORT,
        size=float(quantity),
        avg_entry_price=price,
        initial_sl=stop_loss,
        initial_tp=take_profit,
        current_sl=stop_loss,
        take_profit_levels=[
            {"ratio": 0.4, "price": take_profit},
            {"ratio": 0.6, "price": round(price * (1 - tp_pct * 1.5), 2)},
        ],
        time_stop_hours=int(_load_short_config_val("time_decay.max_holding_days", 5) * 24),
        market_regime=getattr(signal, "market_regime", "") or MarketRegime.RANGE,
    )
    position.entry_time = now_et()
    position.futu_order_id = result.get("futu_order_id", "")
    position.trail_state = {
        "resistance_20d": resistance,
        "lowest_price": price,
        "prev_scan_price": price,
        "triggered_tiers": [],
        "scale_out_original_size": float(quantity),
        "entry_session": entry_session,  # P2 G7: 开仓时写入
    }

    scheduler.position_store.add(symbol, position)
    # R1: 标记仓位创建成功, lock 归 _close_position → on_position_closed 释放
    if isinstance(context, dict):
        context["_r1_position_created"] = True

    # PostTradeHook (2026-05-16): SHORT 也走统一钩子 (替代散乱 mirror try/except)
    try:
        from app.pipeline.post_trade_hook import on_position_opened
        on_position_opened(position, signal, context or {}, scheduler)
    except Exception as _pte:
        logger.debug(f"[PostTradeHook·SHORT] 提交失败 (不影响下单): {_pte}")

    # 创建 Order
    order = Order(
        client_order_id=f"{symbol}_SHORT_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        symbol=symbol,
        direction=Direction.SHORT,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
    )
    order.futu_order_id = result.get("futu_order_id", "")

    # 事件 + 通知
    _notify_order(scheduler, symbol, signal, order, position, voting_result)
    scheduler.event_store.append(TradeEvent(
        event_type="order_submitted",
        signal_id=signal.signal_id,
        symbol=symbol,
        order_id=order.order_id,
        data={
            "symbol": symbol, "direction": "SHORT",
            "method": "stock_short", "quantity": quantity,
            "price": price, "stop_loss": stop_loss,
            "take_profit": take_profit, "resistance": resistance,
        },
        source="scheduler",
    ))

    logger.info(
        f"[正股做空] ✅ {symbol} 成功 qty={quantity} price=${price:.2f} "
        f"SL=${stop_loss:.2f} TP=${take_profit:.2f} "
        f"resistance=${resistance:.2f} futu_order_id={order.futu_order_id}"
    )


def _load_short_config_val(dotted_key: str, default: Any = None) -> Any:
    """从 ConfigContainer 读取做空配置"""
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(f"options.short_strategy.stock_short.{dotted_key}", default)
    except Exception:
        return default


def _execute_long(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """处理做多路由 (标准开仓)。

    P2 2026-05-01: Session-aware entry — 入场止损 = base_sl × session_vol_ratio,
    entry_session 在开仓时写入 trail_state 供退出监控器路径依赖使用。
    """
    price = round(context.get("price", 0), 2)
    atr = context.get("atr", price * 0.02)

    if price <= 0:
        logger.warning(f"[下单] {symbol} 价格为 0，取消下单")
        return

    # P4 观察 (2026-05-19): 信号强度反向 — 仅打日志, 不改决策.
    # 5/12-18 数据: strength 80+ 共 298 笔 avg -$1; strength 60-79 共 291 笔 avg +$11.
    # 如果今晚高 strength 信号继续亏损, 数据驱动决定是否反转评分.
    _strength_observed = int(getattr(signal, "strength", 0) or 0)
    if _strength_observed >= 80:
        logger.info(
            f"[P4·observation] {symbol} LONG strength={_strength_observed} ≥80, "
            f"5/12-18 同档位 avg=-$1/笔. 观察今晚表现. (此处仅记录, 不改行为)"
        )

    # ⭐ Pre-Entry Signal Gate (2026-05-19) — LLM 拦截历史失败模式
    # 默认 disabled (yaml.llm_features.pre_entry_gate.enabled=false), 启用后:
    # - 5/12-18 LCID LONG 14 笔历史 0 胜率 → LLM go=False 拒入场
    # - LLM 失败/超时 → 默认放行 (向后兼容)
    try:
        from app.llm.pre_entry_gate import check_gate
        sig_dict = {
            "strength": int(getattr(signal, "strength", 0) or 0),
            "confidence": float(getattr(signal, "confidence", 0) or 0),
            "factor_detail": (signal.metadata or {}).get("_factor_detail", {}) if hasattr(signal, "metadata") else {},
        }
        gate = check_gate(symbol, "LONG", sig_dict, market_ctx=context)
        if not gate.go:
            logger.warning(
                f"🚫 [PreEntryGate·LONG] {symbol} 被 LLM 拦截: {gate.reason} "
                f"(confidence={gate.confidence:.2f}, source={gate.source})"
            )
            try:
                get_rejection_logger().record(
                    symbol, gate.reason or "pre_entry_gate_no",
                    "PRE_ENTRY_GATE", strength=getattr(signal, "strength", 0),
                    confidence=getattr(signal, "confidence", 0), direction="LONG",
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return
    except Exception as _ge:
        logger.debug(f"[PreEntryGate·LONG·exception] {symbol} 检查失败 (放行): {_ge}")

    # P2 2026-05-01: 获取当前 session 和波动率比率 (G6 + G7)
    from app.core.shared_kernel import get_shared_kernel
    kernel = get_shared_kernel()
    entry_session = kernel.get_current_session()
    session_vol_ratio = kernel.get_session_vol_ratio(entry_session)

    # 仓位计算：固定 50 股（可在 watchlist_us.json 每只股票配置 order_qty 覆盖）
    quantity = _get_order_qty(scheduler, symbol, atr, direction="LONG", context=context)
    if quantity <= 0:
        logger.warning(f"[下单] {symbol} 数量为 0，跳过")
        return

    # ============================================================
    # Phase 3: Dynamic Stop-Loss & Take-Profit (Regime + Session Aware)
    # ============================================================

    # ── Livermore 止损纪律强化 (P0-2, 2026-04-16) ──
    # P3 优化 2026-04-29: ETF 宽基使用 MA 锚定止损，不用固定百分比
    CORE_ETFS = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM"}
    _is_core_etf = symbol in CORE_ETFS

    if _is_core_etf:
        LIVERMORE_HARD_STOP_PCT = 0.06  # ETF 最低 6% 止损
        logger.info(f"[ETF宽基] {symbol} 使用 ETF 专用止损参数 (min 6%%)")
    else:
        LIVERMORE_HARD_STOP_PCT = float(context.get("livermore_stop_pct", 0.10))
    LIVERMORE_TRAILING_ACTIVE = True

    # 1. Base Config — 统一配置访问 (P0-3 修复 2026-04-29: 使用 get_dotted 正确的键路径)
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
    except Exception as e:
        logger.debug(f"[动态仓位] 加载配置失败: {e}")
        _cfg = None

    sl_pct     = abs(float(_cfg.get_dotted("risk.stop_loss.default", -0.06))) if _cfg else 0.06
    tp1_pct    = float(_cfg.get_dotted("risk.take_profit.tier1", 0.08)) if _cfg else 0.08
    tp2_pct    = float(_cfg.get_dotted("risk.take_profit.tier2", 0.15)) if _cfg else 0.15
    tp3_pct    = float(_cfg.get_dotted("risk.take_profit.tier3", 0.25)) if _cfg else 0.25
    atr_mult   = float(_cfg.get_dotted("risk.stop_loss.trailing_atr_mult", 2.0)) if _cfg else 2.0

    # 1.5 IntradayScorer 波动率自适应止损 (2026-04-27)
    _scorer_sl = 0
    if voting_result:
        _scorer_sl = voting_result.get("position_suggestion", {}).get("stop_loss_pct", 0)
    if _scorer_sl > 0:
        logger.info(
            f"[Scorer止损] {symbol} scorer建议 {_scorer_sl*100:.1f}% "
            f"(ATR={context.get('atr',0):.2f} VIX={context.get('vix',20):.0f}), 原默认 {sl_pct*100:.0f}%"
        )
        sl_pct = max(sl_pct, _scorer_sl)

    # P2 2026-05-01 (G6): Session-aware 入场止损调整
    # opening_drive (1.50x) → 放宽止损, 容忍开盘噪音
    # midday (0.60x) → 收紧止损, 低波动不需要宽止损
    if session_vol_ratio != 1.0:
        sl_pct_before = sl_pct
        sl_pct = sl_pct * session_vol_ratio
        logger.info(
            f"[Session止损] {symbol} session={entry_session} "
            f"vol_ratio={session_vol_ratio:.2f} → "
            f"止损 {sl_pct_before*100:.1f}% → {sl_pct*100:.1f}%"
        )

    # Livermore 铁律: 止损不得低于硬底线
    if sl_pct < LIVERMORE_HARD_STOP_PCT:
        logger.info(f"[Livermore止损] 原止损 {sl_pct*100:.1f}% < {LIVERMORE_HARD_STOP_PCT*100:.0f}% 底线, 提升至 {LIVERMORE_HARD_STOP_PCT*100:.0f}%")
        sl_pct = LIVERMORE_HARD_STOP_PCT

    # 2. Dynamic Adjustment based on Market Regime
    _regime_vix = float(context.get("vix", 20.0))
    _regime = signal.market_regime

    if _regime == MarketRegime.HIGH_VOL or _regime_vix > 25:
        logger.info(f"[风控] {symbol} 检测到高波动 (VIX={_regime_vix}), 放宽风控参数")
        sl_pct = max(sl_pct, 0.12)
        tp1_pct = 0.15
        tp2_pct = 0.25
        tp3_pct = 0.40
        atr_mult = 3.0

    elif _regime == MarketRegime.TREND_UP:
        logger.info(f"[风控] {symbol} 检测到上涨趋势, 启用趋势跟随参数")
        sl_pct = max(sl_pct, 0.08)
        tp1_pct = 0.12
        tp2_pct = 0.20
        tp3_pct = 0.30
        atr_mult = 2.5

    elif _regime == MarketRegime.RANGE:
        logger.info(f"[风控] {symbol} 检测到震荡市场, 启用快进快出参数")
        sl_pct = max(LIVERMORE_HARD_STOP_PCT, 0.05)
        tp1_pct = 0.06
        tp2_pct = 0.10
        tp3_pct = 0.15
        atr_mult = 1.5

    # LLM 策略自定义止盈止损 (优先级高于 regime 默认值)
    if signal.metadata and signal.metadata.get("custom_take_profit_pct"):
        custom_tp = signal.metadata["custom_take_profit_pct"]
        custom_sl = signal.metadata.get("custom_stop_loss_pct", custom_tp / 2)
        logger.info(f"[LLM策略] 使用自定义风控: 止盈+{custom_tp*100:.0f}% 止损-{custom_sl*100:.0f}%")

        if signal.direction == Direction.LONG:
            stop_loss = price * (1 - custom_sl)
            take_profit_1 = price * (1 + custom_tp * 0.6)
            take_profit_2 = price * (1 + custom_tp * 0.8)
            take_profit_3 = price * (1 + custom_tp)
        else:
            stop_loss = price * (1 + custom_sl)
            take_profit_1 = price * (1 - custom_tp * 0.6)
            take_profit_2 = price * (1 - custom_tp * 0.8)
            take_profit_3 = price * (1 - custom_tp)
    else:
        # 默认 regime-based 止盈止损
        if signal.direction == Direction.LONG:
            stop_loss = price - max(atr * atr_mult, price * sl_pct)
            take_profit_1 = price * (1 + tp1_pct)
            take_profit_2 = price * (1 + tp2_pct)
            take_profit_3 = price * (1 + tp3_pct)
        else:
            stop_loss = price + max(atr * atr_mult, price * sl_pct)
            take_profit_1 = price * (1 - tp1_pct)
            take_profit_2 = price * (1 - tp2_pct)
            take_profit_3 = price * (1 - tp3_pct)

    # 创建持仓记录（本地）
    position = Position(
        strategy_id="voting_llm",
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=signal.direction,
        size=float(quantity),
        avg_entry_price=price,
        initial_sl=stop_loss,
        initial_tp=take_profit_1,
        current_sl=stop_loss,
        take_profit_levels=[
            {"ratio": 0.30, "price": take_profit_1},
            {"ratio": 0.30, "price": take_profit_2},
            {"ratio": 0.40, "price": take_profit_3},
        ],
        time_stop_hours=int(os.getenv("TIME_STOP_HOURS", "168")),
        market_regime=signal.market_regime,
        trail_state={
            "active": LIVERMORE_TRAILING_ACTIVE,
            "livermore_mode": True,
            "highest_price": price,
            "lowest_price": price,
            "peak_price": price,
            "prev_scan_price": price,
            "initial_atr": atr,
            "atr_mult": atr_mult,
            "entry_session": entry_session,  # P2 G7: 开仓时写入, 供退出监控器路径依赖
        },
    )

    # 构建订单
    order = Order(
        client_order_id=f"{symbol}_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=signal.direction,
        order_type=OrderType.LIMIT,
        quantity=float(quantity),
        price=price,
        stop_price=stop_loss,
    )

    # P1-5 修复 2026-04-26: 统一在下单前写入归因基线
    # P0 修复 (2026-05-07): trade_date 使用 ET 日期，date('now') 是 UTC 日期
    try:
        import sqlite3 as _sql3
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.core.paths import get_trade_records_db
        _et_now = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
        _db = str(get_trade_records_db())
        _conn = _sql3.connect(_db)
        _cur = _conn.cursor()
        # Phase A (2026-05-14): trace 入场快照 + owner_strategy + direction
        _direction_str = getattr(signal.direction, "value", str(getattr(signal, "direction", "LONG")))
        _owner = (signal.metadata or {}).get("owner_strategy") if hasattr(signal, "metadata") else None
        _owner = _owner or str(getattr(signal, 'strategy_id', '') or '') or "main"
        _cur.execute('''
            INSERT OR REPLACE INTO trade_attribution
            (trace_id, trade_date, symbol, direction, signal_strength, signal_confidence,
             owner_strategy, entry_time, entry_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            getattr(signal, 'trace_id', ''),
            _et_now,
            symbol,
            _direction_str,
            getattr(signal, 'strength', 0),
            getattr(signal, 'confidence', 0.0),
            _owner,
            _dt.now().isoformat(),
            float(price) if price else 0.0,
        ))
        _conn.commit()
        _conn.close()
    except Exception as _attr_err:
        logger.debug(f"[归因] trade_attribution 写入失败 (不影响下单): {_attr_err}")

    try:
        if scheduler.execution_engine:
            # Task #14 (2026-05-27): 金额 > 阈值 走 TWAP 拆单, 同时记录延迟/滑点
            _signal_time = float(getattr(signal, "created_at_ts", 0) or 0) or time.time()
            try:
                _ca = getattr(signal, "created_at", None)
                if _ca and hasattr(_ca, "timestamp"):
                    _signal_time = _ca.timestamp()
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            submitted_order, _exec_meta = _submit_order_optimized(
                scheduler, order, signal_price=price, signal_time=_signal_time,
                direction=signal.direction, signal_id=signal.signal_id, trace_id=signal.trace_id,
            )
            # 将执行元数据挂到 position.metadata, 供平仓时写入 trade_records.metadata_json
            try:
                if not hasattr(position, "metadata") or position.metadata is None:
                    position.metadata = {}
                position.metadata["execution"] = _exec_meta
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            # TWAP 下实际成交均价可能偏离信号价, 以成交均价作为持仓入场价
            try:
                _avg = float(getattr(submitted_order, "avg_fill_price", 0) or getattr(submitted_order, "fill_price", 0) or 0)
                if _avg > 0 and abs(_avg - price) / max(price, 1e-6) > 1e-6:
                    position.avg_entry_price = _avg
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            logger.info(
                f"[EXEC][trace:{getattr(signal, 'trace_id', '') or 'NONE'}] Order placed for {symbol} "
                f"({signal.direction.value} qty={quantity} price=${price:.2f} "
                f"SL=${stop_loss:.2f} TP1=${take_profit_1:.2f} "
                f"mode={_exec_meta.get('execution_mode')} "
                f"slip={_exec_meta.get('slippage_pct'):+.3f}% latency={_exec_meta.get('execution_latency_ms')}ms)"
            )
        else:
            # 无执行引擎：直接模拟成交
            order.state = OrderState.FILLED
            order.filled_quantity = order.quantity
            order.avg_fill_price = price
            submitted_order = order
            logger.info(
                f"[EXEC][trace:{getattr(signal, 'trace_id', '') or 'NONE'}] Simulated fill for {symbol} "
                f"({signal.direction.value} qty={quantity} price=${price:.2f})"
            )

        # 记录持仓
        submitted_futu_id = getattr(submitted_order, "futu_order_id", "")
        if submitted_futu_id:
            position.futu_order_id = submitted_futu_id
        # Phase A (2026-05-14): 标 owner_strategy + position_pct (供 coordinator 释放资金用)
        try:
            _meta = (signal.metadata or {}) if hasattr(signal, "metadata") else {}
            owner = _meta.get("owner_strategy")
            # ORB fix: fallback to strategy_id when metadata doesn't set owner_strategy
            # Prevents all pure strategies from defaulting to "main"
            _sid = str(getattr(signal, 'strategy_id', '') or '')
            position.owner_strategy = owner or getattr(position, "owner_strategy", None) or _sid or "main"
            # position_pct 优先级: signal.position_pct > signal.metadata.position_pct > 估算
            _pct = float(getattr(signal, "position_pct", 0) or 0)
            if _pct <= 0:
                _pct = float(_meta.get("position_pct", 0) or 0)
            if _pct <= 0 and quantity and price:
                # 兜底: cost / equity (如能拿到 equity)
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    _eq = get_capital_safety_system()._state.total_equity or 0
                    if _eq > 0:
                        _pct = (quantity * price) / _eq
                except Exception as _silent_e:
                    logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            position.position_pct = round(_pct, 4)
        except Exception:
            position.owner_strategy = "main"
            position.position_pct = 0.0

        # P0-1 (2026-05-14): 原子化 — 先 DB upsert (持久化), 再 in-memory add, 失败回滚.
        # 防 scheduler crash 在 add 与 upsert 之间 → broker 已有仓位但本地全丢.
        _db_persisted = False
        try:
            scheduler.trade_store.upsert_open_position(position, action="open")
            _db_persisted = True
        except Exception as e:
            logger.error(f"[下单·P0-1] DB upsert 失败, 撤销 in-memory add: {e}")
            # 不写 in-memory, 让 broker_reconcile 在下个周期发现 orphan 自行修
            # 关键告警 — 已下单但本地无记录
            try:
                from app.utils.critical_alerts import send_critical
                send_critical(
                    f"⚠️ 持仓 DB 持久化失败 (订单已成交但本地不知道)\n"
                    f"Symbol: {symbol} {direction}\n"
                    f"Qty: {quantity} Price: ${price:.2f}\n"
                    f"Error: {e}\n"
                    f"broker_reconcile 将在 5min 内尝试修复",
                    async_mode=True,
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return  # 中断后续 in-memory 操作

        # DB 持久化成功后, 写 in-memory PositionStore
        scheduler.position_store.add(symbol, position)
        # R1: 标记仓位创建成功
        if isinstance(context, dict):
            context["_r1_position_created"] = True

        # PostTradeHook (2026-05-16): 4 个副作用异步统一执行 (替代 ~75 行散乱 try/except)
        # 顺序: attribution → mirror → push → thesis_inferer
        # 主线 0 阻塞 (cooldown 30s 远盖过任何同步开销, 但代码结构更清晰)
        try:
            from app.pipeline.post_trade_hook import on_position_opened
            on_position_opened(position, signal, context or {}, scheduler)
        except Exception as _pte:
            logger.debug(f"[PostTradeHook] 提交失败 (不影响下单): {_pte}")

        # Phase A: 通知 coordinator (灰度, 仅当 enabled)
        try:
            _coord = getattr(scheduler, "coordinator", None)
            if _coord is not None and _coord.enabled:
                _coord.record_position_owner(symbol, position.owner_strategy,
                                             float(getattr(signal, "position_pct", 0.0) or 0.0))
        except Exception as _ce:
            logger.debug(f"[Coordinator] record_position_owner 失败: {_ce}")

        # LLM 异步审计 (P1-1 修复 2026-04-29: 盘中禁用)
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        current_phase = get_market_phase()

        llm_engine = getattr(scheduler, 'llm_engine', None)
        if llm_engine and hasattr(llm_engine, 'audit_position_async'):
            if current_phase == MarketPhase.TRADING:
                try:
                    audit_queue = getattr(scheduler, '_deferred_audit_queue', None)
                    if audit_queue is None:
                        scheduler._deferred_audit_queue = []
                        audit_queue = scheduler._deferred_audit_queue

                    audit_queue.append({
                        "symbol": symbol,
                        "context_keys": list(context.keys())[:10],
                        "price": context.get("price", 0),
                        "direction": signal.direction.value if hasattr(signal, 'direction') else "UNKNOWN",
                        "timestamp": time.time(),
                    })
                    logger.debug(
                        "[LLM 审计] 盘中阶段 → 排队到盘后执行 | %s %s @ $%.2f",
                        symbol, signal.direction.value if hasattr(signal, 'direction') else "?", context.get("price", 0)
                    )
                except Exception as e:
                    logger.debug(f"[LLM 审计] 排队失败 (已忽略): {e}")
            else:
                try:
                    rsshub = getattr(scheduler, 'llm_rsshub', None)
                    llm_engine.audit_position_async(symbol, context, rsshub=rsshub)
                except Exception as e:
                    logger.debug(f"[LLM 审计] 触发失败 (已忽略): {e}")

        # P1 新增: 关联交易结果到决策记录
        try:
            from app.storage.decision_recorder import get_decision_recorder
            recorder = get_decision_recorder()
            decision_id = signal.metadata.get("decision_id") if signal.metadata else None
            if decision_id:
                recorder.link_trade_outcome(
                    decision_id=decision_id,
                    symbol=symbol,
                    direction=signal.direction.value,
                    entry_price=price,
                    quantity=quantity,
                )
                logger.info(f"[DecisionRecorder] 关联交易: {decision_id} -> {symbol} entry=${price:.2f}")
        except Exception as e:
            logger.warning(f"[DecisionRecorder] 关联交易失败 (不影响主流程): {e}")

        # 记录冷却
        scheduler._set_cooldown(symbol)

        # 记录事件
        scheduler.event_store.append(TradeEvent(
            event_type="order_submitted",
            signal_id=signal.signal_id,
            order_id=order.order_id,
            data={
                "symbol": symbol,
                "direction": signal.direction.value,
                "quantity": quantity,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit_1": take_profit_1,
                "strength": signal.strength,
                "confidence": signal.confidence,
            },
            source="scheduler",
            severity=AlertLevel.INFO,
        ))

        # 推送通知
        _notify_order(scheduler, symbol, signal, order, position, voting_result)

        # 自适应轮询：开仓后升级为 P0 (持仓股)
        if scheduler.polling_controller:
            scheduler.polling_controller.update_tier(symbol, StockTier.POSITION)
            logger.info(f"[自适应轮询] {symbol} 开仓 → 升级为 P0 (持仓股)")

    except Exception as e:
        logger.error(f"[下单] {symbol} 失败：{e}", exc_info=True)
        # P0-3 修复 (2026-05-03): 下单失败时清理可能已添加的幽灵仓
        try:
            scheduler.position_store.remove(symbol)
        except KeyError as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
        except Exception as cleanup_err:
            logger.debug(f"[下单] 清理幽灵仓失败: {cleanup_err}")


# ═══════════════════════════════════════════════════════════════════
#  标的级冷却 (P0 2026-05-08)
# ═══════════════════════════════════════════════════════════════════

_symbol_cooldown_cache: Dict[str, float] = {}  # "{symbol}_{direction}" → cooldown_until_timestamp


def _check_symbol_cooldown(symbol: str, direction) -> bool:
    """P0 修复 (2026-05-08): 标的级冷却 — 连续亏损后禁止同方向开仓

    P1 升级 (2026-05-11): 覆盖 ALL 退出原因（不仅是 STRUCTURE_EXIT）
    QCOM 教训: 4 次 DAYTRADE_STOP 亏损 (-8.4% ~ -9.9%) 未被冷却计数
    """
    import time
    from datetime import datetime, timedelta
    try:
        from app.core.config_container import get_config as get_app_config
        cfg = get_app_config()
        sc_cfg = cfg.get_dotted("symbol_cooldown.structure_exit", {})
        consecutive_threshold = sc_cfg.get("consecutive_losses", 3)
        cooldown_hours = sc_cfg.get("cooldown_hours", 24)
    except Exception:
        consecutive_threshold = 3
        cooldown_hours = 24

    cache_key = f"{symbol}_{direction.name if hasattr(direction, 'name') else direction}"
    now = time.time()
    cooldown_until = _symbol_cooldown_cache.get(cache_key, 0)
    if now < cooldown_until:
        remaining = int((cooldown_until - now) / 3600)
        logger.warning(
            f"🚫 [标的冷却] {symbol} {direction} 冷却中，剩余 {remaining}h "
            f"(连续亏损 ≥{consecutive_threshold}次)"
        )
        return False

    # P1 升级 (2026-05-11): 查询最近 24h 同方向 ANY 亏损次数（不限于 STRUCTURE_EXIT）
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (now_et() - timedelta(hours=24)).strftime("%Y-%m-%d")
            direction_str = direction.name if hasattr(direction, "name") else str(direction)

            rows = conn.execute(
                "SELECT exit_price, pnl_pct, exit_reason FROM trade_records "
                "WHERE symbol = ? AND direction = ? "
                "AND exit_price > 0 AND pnl_pct < 0 "
                "AND trade_date >= ? "
                "ORDER BY id DESC LIMIT ?",
                (symbol, direction_str, cutoff, consecutive_threshold + 5)
            ).fetchall()

        if len(rows) >= consecutive_threshold:
            # 触发冷却
            cooldown_until = now + (cooldown_hours * 3600)
            _symbol_cooldown_cache[cache_key] = cooldown_until
            reasons = [r[2] for r in rows[:3]]
            logger.warning(
                f"🚫 [标的冷却·激活] {symbol} {direction_str} 24h 内亏损 {len(rows)}次 "
                f"(阈值={consecutive_threshold})，冷却 {cooldown_hours}h | 最近原因: {reasons}"
            )
            return False

        # 未达到阈值，放行
        return True

    except Exception as e:
        logger.debug(f"[标的冷却] 查询失败 (放行): {e}")
        return True


def _check_reversal_cooldown(scheduler, symbol: str, direction) -> bool:
    """F1 (2026-05-16): 平仓后反向开仓冷却 — 防 whipsaw

    实证 (5/14-5/15):
    - SNDK SHORT 平仓 90s 内 LONG 开仓 → -$22 亏损
    - CRM  LONG  平仓 173s 内 SHORT 开仓 → -$45 亏损
    - LCID SHORT 平仓 127s 内 LONG 开仓 → -$10 亏损

    机制: 查 trade_records 找最近一笔平仓 (任何 source_type), 若反向且时差 < cooldown 则拒.
    Returns: True = 允许 / False = 冷却中拒绝
    """
    import sqlite3
    from datetime import datetime, timedelta
    try:
        from app.core.config_container import get_config as _gc
        cfg = _gc()
        # 默认 180s (基于实证数据: 3 笔都在 200s 内反向). 沙盒 60s 加速实验.
        cooldown_sec = int(cfg.get_dotted("symbol_cooldown.reversal_seconds", 180))
        # F1: sandbox 可单独覆盖
        sb_override = cfg.get_dotted("mode.sandbox.overrides", {}) or {}
        if cfg.get_dotted("mode.sandbox.enabled", False):
            if "symbol_cooldown.reversal_seconds" in sb_override:
                cooldown_sec = int(sb_override["symbol_cooldown.reversal_seconds"])
    except Exception:
        cooldown_sec = 180

    if cooldown_sec <= 0:
        return True  # 完全禁用

    direction_str = direction.name if hasattr(direction, "name") else str(direction)
    opposite = "SHORT" if direction_str == "LONG" else "LONG"

    try:
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
        with sqlite3.connect(str(db_path)) as conn:
            # 最近一笔反向平仓 (跨 source_type 全量看，因为 whipsaw 可能跨策略)
            cutoff_iso = (now_et() - timedelta(seconds=cooldown_sec)).isoformat()
            row = conn.execute(
                "SELECT direction, exit_time, source_type FROM trade_records "
                "WHERE symbol = ? AND direction = ? AND exit_time IS NOT NULL "
                "AND exit_time > ? "
                "ORDER BY exit_time DESC LIMIT 1",
                (symbol, opposite, cutoff_iso)
            ).fetchone()

        if row:
            _, exit_time_str, src = row
            try:
                exit_dt = datetime.fromisoformat(exit_time_str)
                gap = (now_et() - exit_dt).total_seconds()
                logger.warning(
                    f"🚫 [F1·反向冷却] {symbol} {direction_str} 拒入场: "
                    f"{opposite} 在 {gap:.0f}s 前刚平仓 (源={src}, 冷却={cooldown_sec}s)"
                )
                return False
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    except Exception as e:
        logger.debug(f"[F1·反向冷却] 查询失败 (放行): {e}")
    return True


def _submit_order(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None):
    """构建订单并通过执行引擎下单 (Refactored 2026-04-29)"""
    
    # 0. PDT 保护门禁 (P0-fix 2026-05-02: 集成到执行路径)
    # 模拟盘不受 FINRA 约束，但为实盘做准备保留此检查
    try:
        trade_env = getattr(context, "get", lambda k, d=None: context.get(k, d)) if isinstance(context, dict) else lambda k, d=None: d
        env_val = trade_env("trade_env", None)
        if env_val is None:
            try:
                from app.core.config import get_config
                env_val = get_config().get("TRADE_ENV", 1)
            except Exception:
                env_val = 1  # 默认模拟盘
        # 仅实盘 (TRADE_ENV=0) 启用 PDT 拦截
        if int(env_val) == 0:
            from app.risk.pdt_guard import PDTGuard
            pdt = PDTGuard()
            is_overnight = signal.direction in (Direction.LONG,)  # SHORT 总是日内
            allowed, reason, _level = pdt.check_trade_permission(is_overnight=is_overnight)
            if not allowed:
                logger.warning(f"🚫 [PDT 拦截] {symbol}: {reason}")
                return
    except Exception as e:
        logger.debug(f"[PDT] 检查异常 (放行): {e}")
    
    # 0.5 P0 修复 (2026-05-08): 标的级冷却 — STRUCTURE_EXIT 连续亏损冷却
    if not _check_symbol_cooldown(symbol, signal.direction):
        return

    # F1 (2026-05-16): Whipsaw 反向冷却 — 平仓后 N 秒内禁止反向开仓
    # 实证: 5/14 SNDK SHORT→LONG 90s 亏损, 5/15 CRM LONG→SHORT 173s 亏损
    # 价格随机游走时, 反向追单大概率被吃损
    if not _check_reversal_cooldown(scheduler, symbol, signal.direction):
        return

    # Task #50 (2026-05-28): symbol+direction 级互斥锁
    # 在 coordinator 仲裁前先做 (symbol, direction) 级阻断:
    # - 避免 Main 与 Scalping 在同一标的同方向重复入场
    # - 不同方向 (对冲) 仍放行
    _sym_dir_owner = "main"
    try:
        _sig_meta = getattr(signal, "metadata", None) or {}
        _sym_dir_owner = str(_sig_meta.get("owner_strategy") or "main")
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    try:
        from app.execution.symbol_lock_manager import get_symbol_lock_manager
        _sym_dir_lock = get_symbol_lock_manager()
        _dir_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)
        if not _sym_dir_lock.try_acquire(symbol, _dir_str, _sym_dir_owner):
            logger.warning(
                f"[信号拒绝] {symbol} {_dir_str}: 被其他策略占有 "
                f"(holder={_sym_dir_lock.is_locked(symbol, _dir_str)}), {_sym_dir_owner} 跳过"
            )
            return
        # 标记本次持锁, 出口/平仓需释放
        context["_symdir_lock_acquired"] = True
        context["_symdir_lock_owner"] = _sym_dir_owner
        context["_symdir_lock_dir"] = _dir_str
    except Exception as _sde:
        logger.debug(f"[SymbolLock·symdir] acquire 异常 (放行): {_sde}")

    # 0.7 Phase A (2026-05-14): Coordinator 多源信号采集 + 仲裁审计 (无副作用)
    # T4 (2026-05-16): sandbox enforce — 真实阻断 SymbolLock 冲突
    # B.8: 让所有已注册 adapter 都生成候选信号 (Phase A: 大多数 adapter 返回 None)
    #      然后 arbitrate 审计写表.
    try:
        _coord = getattr(scheduler, "coordinator", None)
        if _coord is not None and _coord.enabled:
            _adapters = getattr(scheduler, "_strategy_adapters", []) or []
            # 主 adapter 必装 (主线信号已经生成, 直接挂)
            _main_adapter = next((a for a in _adapters if a.name == "main"), None)
            if _main_adapter is None:
                from app.strategy.adapters import MainPipelineAdapter
                _main_adapter = MainPipelineAdapter()
            _candidates = [(_main_adapter, signal)]
            # 其他 adapter (Phase A: 返回 None 不参与, Phase B: 真实生成)
            for _a in _adapters:
                if _a.name == "main":
                    continue
                try:
                    _other_sig = _a.generate_signal(symbol, context)
                    if _other_sig is not None:
                        _candidates.append((_a, _other_sig))
                except Exception as _adapter_err:
                    logger.debug(f"[Coordinator] adapter {_a.name} 信号生成失败 (跳过): {_adapter_err}")
            # 写审计 (即使 1 候选也写) — T8b: 传 ctx 让 effective_priority 走 regime-aware
            _coord.arb.arbitrate(symbol, _candidates, ctx=context)
            if len(_candidates) > 1:
                logger.info(
                    f"[Coordinator·multi] {symbol} 多源候选 n={len(_candidates)}: "
                    f"{[a.name for a, _ in _candidates]}"
                )

            # T4: enforce 模式下 SymbolLock 真实仲裁
            # 主 adapter 申请 lock, 已被其他 owner 持有 → DENIED (signal_arbitration 已留痕)
            if not _coord.is_observe_only():
                try:
                    _lock = _coord.lock
                    _ok = _lock.acquire(symbol, "main")
                    if not _ok:
                        _holder = _lock.owner(symbol) or "?"
                        logger.warning(
                            f"[T4·SymbolLock·ENFORCE] {symbol} held by {_holder}, "
                            f"main REJECTED — 跳过主线下单"
                        )
                        return  # 拒下单, 不进入 _check_gates_and_conflicts
                    # R1 (2026-05-16): 标记本次持锁, 出口必释放 (除非 _close_position 接管)
                    context["_r1_lock_acquired"] = True
                except Exception as _le:
                    logger.debug(f"[T4·SymbolLock] 异常 (放行): {_le}")
    except Exception as _ce:
        logger.debug(f"[Coordinator] multi-source/arbitrate 审计失败 (放行): {_ce}")

    # R1 (2026-05-16): 锁泄漏修复 — 4 个 return 路径必释放 lock
    # 仅 _execute_long/short 成功创建仓位后, lock 归 on_position_closed 释放;
    # 否则下面任意 return 都要主动 release.
    def _r1_release_if_held():
        try:
            if context.get("_r1_lock_acquired") and not context.get("_r1_position_created"):
                _coord = getattr(scheduler, "coordinator", None)
                if _coord is not None and _coord.enabled and not _coord.is_observe_only():
                    _coord.lock.release(symbol, "main")
                    logger.debug(f"[R1·lock·release] {symbol} 主线 (未成单) 主动释放")
        except Exception as _re:
            logger.debug(f"[R1·release] 异常: {_re}")
        # Task #50: symbol+direction 锁同步释放 (创仓失败路径)
        try:
            if context.get("_symdir_lock_acquired") and not context.get("_r1_position_created"):
                from app.execution.symbol_lock_manager import get_symbol_lock_manager
                get_symbol_lock_manager().release(
                    symbol,
                    context.get("_symdir_lock_dir", ""),
                    context.get("_symdir_lock_owner", "main"),
                )
                context["_symdir_lock_acquired"] = False
        except Exception as _sre:
            logger.debug(f"[SymbolLock·symdir·release] 异常: {_sre}")

    try:
        # 1. 风控门禁与冲突检查
        if not _check_gates_and_conflicts(scheduler, symbol, signal, context):
            _r1_release_if_held()
            return

        # 2. 金字塔加仓处理
        if _handle_pyramid_add(scheduler, symbol, signal, context):
            # 金字塔加仓不算新仓 (原仓还在), lock 仍归原仓平仓时释放
            return

        # 3. 按方向路由执行
        if signal.direction == Direction.SHORT:
            _execute_short(scheduler, symbol, signal, context, voting_result)
        else:
            _execute_long(scheduler, symbol, signal, context, voting_result)

        # 如果上面没标 _r1_position_created → 创仓失败, 主动释放
        if not context.get("_r1_position_created"):
            _r1_release_if_held()
    except Exception as _ee:
        _r1_release_if_held()
        raise


# ═══════════════════════════════════════════════════════════════════
#  平仓执行
# ═══════════════════════════════════════════════════════════════════



def _lookup_decision_id_by_trace(trace_id: str, symbol: str = "") -> str:
    """
    通过 trace_id 从 trade_attribution DB 反查 decision_id。
    用于平仓时无法从 position.metadata 获取 decision_id 的 fallback。
    Returns: decision_id or empty string.
    """


def _derive_entry_reason_for_close(meta: dict) -> str:
    """P0 修复 (2026-05-27): 平仓时推导 entry_reason

    优先级:
      1. position.metadata['entry_reason'] (开仓时已写入)
      2. 从 strategy_id + source_type 推导
      3. 兜底 'unknown'
    """
    from app.core.models import derive_entry_reason, EntryReason

    # 1. 直接读取开仓时写入的 entry_reason
    existing = meta.get("entry_reason", "")
    if existing and existing != "None":
        return existing

    # 2. 推导
    strategy_id = meta.get("strategy_id", "")
    source = meta.get("source", "") or meta.get("source_type", "")
    # source_type 可能是 MAIN_PIPELINE/SCALPING 等
    if source == "SCALPING":
        return EntryReason.SCALPING.value
    return derive_entry_reason(
        strategy_id=strategy_id,
        source=source,
        metadata=meta,
    )


def _fetch_attribution_data(trace_id: str, symbol: str = "", trade_date: str = "", direction: str = ""):
    """
    Fallback: query trade_attribution DB for signal data when position.metadata is empty.
    This handles the case where metadata is lost due to service restart or sync overwrite.
    Tries trace_id first, then falls back to symbol+date+direction matching.
    Returns: (signal_strength, signal_confidence) or (0, 0.0) if not found.
    """
    import sqlite3
    from app.core.paths import get_trade_records_db
    db_path = get_trade_records_db()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            # F19 (2026-05-12): schema 已由 TradeStore._ensure_trade_attribution_schema 集中管理
            # Primary: match by trace_id
            if trace_id:
                cursor.execute(
                    "SELECT signal_strength, signal_confidence FROM trade_attribution WHERE trace_id = ?",
                    (trace_id,)
                )
                row = cursor.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0]), float(row[1] or 0.0)
            # Fallback: match by symbol + trade_date + direction (when trace_id is empty)
            if symbol and trade_date:
                if direction:
                    cursor.execute(
                        "SELECT signal_strength, signal_confidence FROM trade_attribution "
                        "WHERE symbol = ? AND trade_date = ? ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    )
                else:
                    cursor.execute(
                        "SELECT signal_strength, signal_confidence FROM trade_attribution "
                        "WHERE symbol = ? AND trade_date = ? ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    )
                row = cursor.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0]), float(row[1] or 0.0)
    except Exception as e:
        logger.debug(f"[归因回填] 查询 trade_attribution 失败：{e}")
    return 0, 0.0


def _fetch_decision_data(symbol: str, trade_date: str = "", direction: str = ""):
    """
    Task #42 (P0, 2026-05-28): 投票→交易记录数据链路修复 — decisions.db 兜底查询.

    当 position.metadata 丢失且 trade_attribution 也查不到 (例如 trace_id 为空)
    时, 从 voting_coordinator 写入的 decisions.db 反查同一 (symbol, 交易日,
    方向) 组合的最近一条决策, 补出 final_strength / consensus_level / signal_strength.

    P1 修复 (2026-05-31): 新增 signal_snapshots.db 作为第 4 层兜底.
    当 decisions.db 也查不到时, 从 signal_snapshots 按 (symbol, trade_date) 匹配.

    Returns:
        dict {
            "signal_strength": int,    # = decisions.final_strength (以投票后强度为准)
            "signal_confidence": float, # = consensus_level
            "final_strength": int,
            "consensus_level": float,
            "decision_id": str,
        } 或空 dict (未命中).
    """
    import sqlite3
    try:
        from app.core.paths import get_decisions_db
        db_path = get_decisions_db()
    except Exception:
        return {}
    if not symbol:
        return {}
    # 规范化方向 (decisions.direction 存为 LONG/SHORT/NEUTRAL)
    _dir = (direction or "").upper().strip()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            # 优先: symbol + trade_date + direction
            params = [symbol]
            sql = ("SELECT decision_id, final_strength, consensus_level, direction "
                   "FROM decisions WHERE symbol = ?")
            if trade_date:
                sql += " AND date(created_at) = ?"
                params.append(trade_date)
            if _dir and _dir not in ("NEUTRAL", ""):
                sql += " AND direction = ?"
                params.append(_dir)
            sql += " ORDER BY created_at DESC LIMIT 1"
            row = cursor.execute(sql, tuple(params)).fetchone()
            if not row and (_dir or trade_date):
                # 退化: 仅匹配 symbol 取最近一条 (避免 trade_date 跨天/方向不一致漏掉)
                row = cursor.execute(
                    "SELECT decision_id, final_strength, consensus_level, direction "
                    "FROM decisions WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
            if row and row[1] is not None:
                return {
                    "decision_id":      row[0] or "",
                    "final_strength":   int(row[1] or 0),
                    "consensus_level":  float(row[2] or 0.0),
                    # decisions.db 中 final_strength 即为投票后强度, 以此作为 signal_strength 兜底
                    "signal_strength":  int(row[1] or 0),
                    "signal_confidence": float(row[2] or 0.0),
                    "direction":        row[3] or "",
                }
    except Exception as e:
        logger.debug(f"[Task#42][decisions 兜底] {symbol} 查询失败: {e}")

    # ── P1 第 4 层兜底 (2026-05-31): signal_snapshots.db ──
    # 当 decisions.db 也查不到时, 从 signal_snapshots 按 (symbol, trade_date) 匹配
    try:
        from app.core.paths import get_data_dir
        snap_db = get_data_dir() / "signal_snapshots.db"
        if snap_db.exists():
            with sqlite3.connect(str(snap_db)) as conn:
                cursor = conn.cursor()
                snap_params = [symbol]
                snap_sql = ("SELECT decision_id, strength, confidence, direction "
                           "FROM signal_snapshots WHERE symbol = ?")
                if trade_date:
                    snap_sql += " AND trade_date = ?"
                    snap_params.append(trade_date)
                if _dir and _dir not in ("NEUTRAL", ""):
                    snap_sql += " AND direction = ?"
                    snap_params.append(_dir)
                snap_sql += " ORDER BY created_at DESC LIMIT 1"
                snap_row = cursor.execute(snap_sql, tuple(snap_params)).fetchone()
                if not snap_row and trade_date:
                    snap_row = cursor.execute(
                        "SELECT decision_id, strength, confidence, direction "
                        "FROM signal_snapshots WHERE symbol = ? AND trade_date = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    ).fetchone()
                if snap_row and snap_row[1] is not None:
                    logger.info(
                        f"[P1·signal_snapshots 兜底] {symbol} {trade_date} "
                        f"strength={snap_row[1]} decision_id={snap_row[0]}"
                    )
                    return {
                        "decision_id":      snap_row[0] or "",
                        "final_strength":   int(snap_row[1] or 0),
                        "consensus_level":  float(snap_row[2] or 0.0),
                        "signal_strength":  int(snap_row[1] or 0),
                        "signal_confidence": float(snap_row[2] or 0.0),
                        "direction":        snap_row[3] or "",
                    }
    except Exception as e:
        logger.debug(f"[P1·signal_snapshots 兜底] {symbol} 查询失败: {e}")

    return {}


def _insert_trade_attribution(
    cursor, trace_id, symbol, strength=0, confidence=0.0,
    # T2 (2026-05-16): 写齐因子 5 列 + 元数据 — 入场时落, 平仓时 UPDATE 补 exit_*
    # 5/15 100% NULL → 复盘看不到哪个因子主导
    factor_detail: dict = None,
    owner_strategy: str = None,
    direction: str = None,
    entry_time: str = None,
    entry_price: float = 0.0,
    quantity: int = 0,
    regime: str = None,
    session: str = None,
    vix_at_entry: float = 0.0,
    signal_tags: str = None,
):
    """Insert a baseline attribution record for a new trade

    T2 扩列: 写入因子明细 + 入场元数据 (owner_strategy/regime/session/vix/signal_tags)
    """
    try:
        fd = factor_detail or {}
        ft = (fd.get("trend") or {}).get("raw")
        fm = (fd.get("momentum") or {}).get("raw")
        fv = (fd.get("volume") or {}).get("raw")
        fo = (fd.get("orderflow") or {}).get("raw")
        fl = (fd.get("lifecycle") or {}).get("raw")
        cursor.execute('''
            INSERT INTO trade_attribution
            (trace_id, trade_date, symbol, direction,
             signal_strength, signal_confidence,
             entry_time, entry_price, quantity,
             owner_strategy, regime, session, vix_at_entry, signal_tags,
             factor_trend, factor_momentum, factor_volume, factor_orderflow, factor_lifecycle,
             created_at)
            VALUES (?, date('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            trace_id, symbol, direction or "LONG",
            int(strength or 0), float(confidence or 0.0),
            entry_time, float(entry_price or 0.0), int(quantity or 0),
            owner_strategy, regime, session, float(vix_at_entry or 0.0), signal_tags,
            ft, fm, fv, fo, fl,
        ))
    except Exception as e:
        logger.warning(f"插入归因基准记录失败 (T2): {e}")


def _update_trade_attribution(
    trace_id: str,
    symbol: str,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
    holding_hours: float,
    signal_strength: int = 0,
    signal_confidence: float = 0.0,
    max_drawdown: float = 0.0,
):
    """
    交易平仓后，更新 trade_attribution 表的归因数据。
    与 HK 标准保持一致: 记录结果标签、盈亏类别、退出原因。
    """
    import sqlite3
    from pathlib import Path as _Path
    
    db_path = _Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
    
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            
            # P0 修复 (F19 2026-05-12): 确保 trade_attribution 表存在
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_attribution (
                    attribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT,
                    trade_date TEXT,
                    symbol TEXT,
                    direction TEXT DEFAULT 'LONG',
                    signal_strength INTEGER DEFAULT 0,
                    signal_confidence REAL DEFAULT 0.0,
                    actual_price REAL DEFAULT 0.0,
                    pnl REAL DEFAULT 0.0,
                    pnl_category TEXT,
                    exit_reason TEXT,
                    result_tags TEXT,
                    direction_correct INTEGER,
                    strategy_fit INTEGER DEFAULT 0,
                    overall_score INTEGER DEFAULT 60,
                    success_factors TEXT,
                    failure_factors TEXT,
                    max_drawdown REAL DEFAULT 0.0,
                    holding_hours REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Check if row exists
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if cursor.fetchone() is None:
                _insert_trade_attribution(cursor, trace_id, symbol, signal_strength, signal_confidence)

            # 1. 确定结果标签
            is_win = pnl > 0
            pnl_category = "win" if is_win else "loss"
            result_tags = json.dumps({
                "pnl_category": pnl_category,
                "exit_reason": exit_reason,
                "is_win": is_win,
            })
            
            # 2. 确定策略拟合度 (基于盈亏与置信度的关系)
            strategy_fit = 1 if (signal_confidence > 0.6 and is_win) else 0
            
            # 3. 计算综合评分 (0-100)
            base_score = 60
            pnl_bonus = min(30, abs(pnl_pct) * 100) if is_win else 0
            drawdown_penalty = max(0, max_drawdown * 10)
            final_score = max(0, min(100, int(base_score + pnl_bonus - drawdown_penalty)))
            
            # 4. 更新记录
            # RFC-050 (P1-1 fix): 移除不存在的 max_drawdown 列, holding_hours→holding_minutes
            _holding_min = int(holding_hours * 60) if holding_hours else 0
            cursor.execute('''
                UPDATE trade_attribution 
                SET actual_price = ?, 
                    pnl = ?,
                    pnl_category = ?,
                    exit_reason = ?,
                    result_tags = ?,
                    direction_correct = ?,
                    strategy_fit = ?,
                    overall_score = ?,
                    success_factors = ?,
                    failure_factors = ?,
                    signal_strength = COALESCE(?, signal_strength),
                    signal_confidence = COALESCE(?, signal_confidence),
                    holding_minutes = ?,
                    timestamp = CURRENT_TIMESTAMP
                WHERE trace_id = ?
            ''', (
                0.0, 
                pnl,
                pnl_category,
                exit_reason,
                result_tags,
                1 if is_win else 0,
                strategy_fit,
                final_score,
                json.dumps({"exit_reason": exit_reason}),
                json.dumps({"max_drawdown": max_drawdown}) if not is_win else "",
                signal_strength or None,
                signal_confidence or None,
                _holding_min,
                trace_id
            ))
            
            conn.commit()
            logger.debug(f"✅ 归因更新：{trace_id} | PnL={pnl:.2f} | Score={final_score}")

            # 2026-06-01: 修复 voting_predictions outcome 回填断裂
            # 原问题: VotingAccuracyTracker.update_outcome 从未被调用,
            # 导致 39 笔 voting_predictions 记录 outcome_match 全 NULL, 模型校准盲飞
            try:
                # 从 trade_records 查 direction (trade_attribution 没存 direction)
                row = cursor.execute(
                    "SELECT direction, trade_date FROM trade_records WHERE trace_id = ? LIMIT 1",
                    (trace_id,)
                ).fetchone()
                if row:
                    _direction, _trade_date = row[0], row[1]
                    from app.analysis.voting_accuracy_tracker import get_voting_accuracy_tracker
                    _tracker = get_voting_accuracy_tracker()
                    _tracker.update_outcome(
                        symbol=symbol,
                        actual_pnl=float(pnl),
                        actual_direction=str(_direction),
                        trade_date=str(_trade_date),
                    )
                    logger.debug(f"[VotingAccuracy] 回填 {symbol} direction={_direction} pnl={pnl:.2f}")
            except Exception as _e_voting:
                logger.debug(f"[VotingAccuracy] 回填失败 (不影响主流程): {_e_voting}")

    except Exception as e:
        logger.warning(f"更新归因记录失败：{e}")


def _is_option_contract(symbol: str) -> bool:
    """判断是否为美股期权合约 (OCC 格式: US.XXXXXXP/CCCCCCCC)"""
    # 期权格式示例: US.CRM260515P180000, US.AAPL260515C180000
    parts = symbol.split(".")
    if len(parts) < 2:
        return False
    ticker_part = parts[-1]  # e.g. "CRM260515P180000"
    # 期权 OCC 格式: 6位到期日(YYMMDD) + P/C + 8位行权价
    # 最小长度: "AAPL260515P180000" = 17 chars
    if len(ticker_part) < 15:
        return False
    return "P" in ticker_part[6:10] or "C" in ticker_part[6:10]


def _get_contract_multiplier(symbol: str) -> int:
    """获取合约乘数: 美股期权=100, 股票=1"""
    if _is_option_contract(symbol):
        return 100
    return 1


def _close_position(scheduler, position: Position, current_price: float, exit_reason: ExitReason):
    """平仓 — 触及即全平，简洁优先"""
    # Task #14 (2026-05-27): 如果该标的开仓 TWAP 还在进行中, 先取消剩余片, 避免平后被动加仓
    try:
        if cancel_active_twap(position.symbol, reason=f"close:{exit_reason.value}"):
            logger.warning(
                f"[平仓·TWAP取消] {position.symbol} 剩余 TWAP 片已取消 (reason={exit_reason.value})"
            )
    except Exception as _ce:
        logger.debug(f"[平仓·TWAP取消] 异常(忽略): {_ce}")

    # 合约乘数 (美股期权标准乘数=100, 股票=1)
    contract_multiplier = _get_contract_multiplier(position.symbol)

    # 计算盈亏 (期权需 ×100 合约乘数)
    if position.direction == Direction.LONG:
        pnl = (current_price - position.avg_entry_price) * position.size * contract_multiplier
        pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0
    else:
        pnl = (position.avg_entry_price - current_price) * position.size * contract_multiplier
        pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0

    logger.info(
        f"[平仓] {position.symbol} reason={exit_reason.value} "
        f"PnL=${pnl:.2f} ({pnl_pct:.2%})"
    )

    # 构建平仓订单
    # P1 修复 (2026-05-11): 止损单使用 Stop Order 而非 Market Order
    # 防止正常盘内波动误触发止损，减少滑点
    # 注意: gap 场景下 Stop 仍会滑点，但至少避免了 scan 间隔内的额外亏损
    close_order = Order(
        client_order_id=f"{position.symbol}_close_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        signal_id=position.signal_id,
        symbol=position.symbol,
        direction=Direction.SHORT if position.direction == Direction.LONG else Direction.LONG,
        order_type=OrderType.MARKET,  # P0 修复: STOP 不在枚举中导致 AttributeError
        quantity=position.size,
        price=current_price,
    )
    # P1-2 修复 2026-04-26: 平仓订单标记 is_exit=True，跳过幂等检查避免重复拦截
    if not hasattr(close_order, 'metadata') or close_order.metadata is None:
        close_order.metadata = {}
    close_order.metadata['is_exit'] = True

    try:
        if scheduler.execution_engine:
            close_result = scheduler.execution_engine.submit_order(close_order)
            # P0 Fix 2026-04-28: 记录平仓单的 futu_order_id 到 position.metadata
            close_futu_id = getattr(close_result, "futu_order_id", "")
            if close_futu_id:
                meta = getattr(position, "metadata", {}) or {}
                meta["close_futu_order_id"] = close_futu_id
                position.metadata = meta
                logger.info(f"[平仓] 平仓单 futu_order_id={close_futu_id}")
    except Exception as e:
        # P0 修复 2026-04-23: 平仓失败退避机制 — 防止无限循环
        meta = getattr(position, "metadata", None)
        if meta is None:
            meta = {}
            position.metadata = meta
        close_fail_count = meta.get("_close_fail_count", 0) + 1
        meta["_close_fail_count"] = close_fail_count

        max_retries = 3
        if close_fail_count >= max_retries:
            meta["_close_blocked"] = True
            meta["_close_blocked_reason"] = str(e)
            meta["_close_blocked_at"] = now_et().isoformat()
            logger.critical(
                f"[平仓·异常] {position.symbol} 连续 {close_fail_count} 次平仓失败，"
                f"已标记为异常持仓 (blocked=True)，需人工介入。"
                f"最后错误: {e}"
            )
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="mark_anomalous"
                    )
            except Exception as db_err:
                logger.debug(f"[平仓·异常] DB 同步失败: {db_err}")
            # 发送钉钉告警
            try:
                if hasattr(scheduler, 'dingtalk') and scheduler.dingtalk:
                    scheduler.dingtalk.send_text(
                        f"🚨 P0 异常持仓告警\n"
                        f"标的: {position.symbol}\n"
                        f"方向: {position.direction.value}\n"
                        f"数量: {position.size}\n"
                        f"入场价: ${position.avg_entry_price:.2f}\n"
                        f"当前价: ${current_price:.2f}\n"
                        f"PnL: ${pnl:.2f} ({pnl_pct:.2%})\n"
                        f"失败次数: {close_fail_count}\n"
                        f"最后错误: {e}\n"
                        f"操作: 请在富途牛牛 APP 中手动平仓"
                    )
            except Exception as notify_err:
                logger.debug(f"[平仓·告警] 钉钉通知失败: {notify_err}")
        else:
            logger.error(
                f"[平仓] {position.symbol} 下单失败 ({close_fail_count}/{max_retries})，"
                f"保留本地持仓状态: {e}"
            )
        return

    # P0 根因修复 (2026-05-14): 使用 OrderTracker 追踪订单全生命周期
    # 根因: 订单提交后未确认就标记 pending_close 并发送通知，导致虚假平仓
    # 修复: 注册订单到 OrderTracker，只在确认成交后才执行后续清理和通知
    if close_futu_id:
        from app.risk.order_tracker import get_order_tracker, OrderState
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        
        tracker = get_order_tracker()
        is_after_hours = get_market_phase() in (MarketPhase.AFTER_HOURS, MarketPhase.PRE_MARKET)
        
        # 注册订单到追踪器
        tracker.register_order(
            futu_order_id=close_futu_id,
            symbol=position.symbol,
            direction=position.direction.value,
            quantity=position.size,
            exit_reason=exit_reason.value,
            is_after_hours=is_after_hours,
        )
        
        # 如果未确认成交，不标记 pending_close，不发送通知
        # 持仓继续被监控，订单状态由 OrderTracker 在下次扫描时检查
        if is_after_hours:
            logger.warning(
                f"[平仓·盘后] {position.symbol} 订单已提交但盘后无法确认成交，"
                f"futu_id={close_futu_id}，持仓继续监控，等待开盘后确认"
            )
            # 标记订单为待追踪，但不清理持仓
            meta["_close_order_submitted"] = True
            meta["_close_futu_order_id"] = close_futu_id
            meta["_close_exit_reason"] = exit_reason.value
            meta["_close_pnl"] = pnl
            meta["_close_pnl_pct"] = pnl_pct
            meta["_close_current_price"] = current_price
            position.metadata = meta
            
            # 同步到 DB（保持持仓状态）
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="close_order_pending"
                    )
            except Exception as e:
                logger.warning(f"[平仓·盘后] DB 同步失败：{e}")
            return  # 不执行后续的持仓清理和通知
    
        # 交易时段：等待确认成交
        try:
            from futu import RET_OK as RET_OK_FUTU, TrdEnv
            import time as _time

            trd_env_f = TrdEnv.SIMULATE if getattr(scheduler, 'trade_env', 1) == 1 else TrdEnv.REAL
            order_confirmed = False

            # P1 修复 (2026-05-31): 确认窗口从 5s 缩短到 2s, 减少 FLAT_ALL 时的线程阻塞
            # 未确认的订单标记为 STALE, 下次扫描会重新确认
            for _attempt in range(2):
                _time.sleep(1)
                ret_q, data_q = scheduler.execution_engine.broker_adapter.trade_ctx.order_list_query(
                    order_id=close_futu_id,
                    trd_env=trd_env_f
                )
                if ret_q == RET_OK_FUTU and not data_q.empty:
                    status = str(data_q.iloc[0].get('order_status', ''))
                    if 'FILLED' in status or 'DEALT' in status:
                        order_confirmed = True
                        logger.info(f"[平仓·确认] {position.symbol} futu_order_id={close_futu_id} 状态={status} ✓")
                        tracker.update_order_state(close_futu_id, OrderState.FILLED)
                        break
                    elif 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
                        logger.warning(f"[平仓·失败] {position.symbol} futu_order_id={close_futu_id} 状态={status} — 订单被拒绝")
                        tracker.update_order_state(close_futu_id, OrderState.FAILED, {"futu_status": status})
                        meta['_close_order_failed'] = True
                        meta['_close_order_status'] = status
                        position.metadata = meta
                        return  # 订单失败，不继续
                    else:
                        logger.debug(f"[平仓·等待] {position.symbol} 订单状态={status}, 继续等待...")

            if not order_confirmed:
                logger.warning(
                    f"[平仓·未确认] {position.symbol} futu_order_id={close_futu_id} "
                    f"2s 内未确认成交，标记为 STALE，等待下次扫描确认"
                )
                tracker.update_order_state(close_futu_id, OrderState.STALE)
                meta['_close_order_unconfirmed'] = True
                meta['_close_order_submitted'] = True
                meta['_close_futu_order_id'] = close_futu_id
                meta['_close_exit_reason'] = exit_reason.value
                meta['_close_pnl'] = pnl
                meta['_close_pnl_pct'] = pnl_pct
                meta['_close_current_price'] = current_price
                position.metadata = meta
                
                # 同步到 DB（保持持仓状态）
                try:
                    if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                        scheduler.trade_store.upsert_open_position(
                            position, source="scheduler", action="close_order_unconfirmed"
                        )
                except Exception as e:
                    logger.warning(f"[平仓·未确认] DB 同步失败：{e}")
                return  # 未确认，不执行后续的持仓清理和通知
        except Exception as confirm_err:
            logger.warning(f"[平仓·确认异常] {position.symbol}: {confirm_err}")
            meta['_close_confirm_failed'] = str(confirm_err)
            position.metadata = meta
            # 确认异常，不继续执行清理和通知
            return

    # 只有确认成交后才执行后续的持仓清理和通知
    # 更新持仓 (标记为已平仓)
    # P0 修复 (2026-05-31): exit_time 统一用美东时间, 避免 CST 导致日期错位
    position.exit_time = now_et()
    position.exit_price = current_price
    position.exit_reason = exit_reason
    position.realized_pnl = pnl
    meta = getattr(position, "metadata", {}) or {}
    meta["_pending_close"] = True
    meta["_pending_close_at"] = now_et().isoformat()
    position.metadata = meta

    scheduler.position_store.remove(position.symbol)
    try:
        if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
            scheduler.trade_store.upsert_open_position(
                position, source="scheduler", action="pending_close"
            )
    except Exception as e:
        logger.warning(f"[平仓] DB 同步失败：{e}")

    # P0-2 修复 (2026-05-19): 标 tombstone, 防止 position_sync 反向恢复刚平仓
    # 配合 app/core/position_sync.py:USPositionSync.mark_closed_tombstone (60s ttl)
    try:
        from app.core.position_sync import get_us_position_sync
        _sync = get_us_position_sync()
        _sync.mark_closed_tombstone(position.symbol)
        # P0-3 加固 (2026-05-27): 立即确认 tombstone (broker 已确认成交)
        _sync.confirm_tombstone(position.symbol)
    except Exception as _te:
        logger.debug(f"[平仓] tombstone 标记失败 (不影响平仓): {_te}")

    # Phase A (2026-05-14): 通知 coordinator 释放资金 + 锁
    try:
        _coord = getattr(scheduler, "coordinator", None)
        if _coord is not None and _coord.enabled:
            _coord.on_position_closed(
                symbol=position.symbol,
                strategy=getattr(position, "owner_strategy", None),
                position_pct=float(getattr(position, "position_pct", 0.0) or 0.0),
            )
    except Exception as _ce:
        logger.debug(f"[Coordinator] on_position_closed 失败: {_ce}")

    # Task #50 (2026-05-28): 释放 symbol+direction 级互斥锁
    try:
        from app.execution.symbol_lock_manager import get_symbol_lock_manager
        _dir_str = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
        _owner = getattr(position, "owner_strategy", None) or "main"
        get_symbol_lock_manager().release(position.symbol, _dir_str, _owner)
    except Exception as _sre:
        logger.debug(f"[SymbolLock·symdir·close] 释放异常: {_sre}")

    # 写入持久化交易记录
    try:
        meta = getattr(position, "metadata", {}) or {}
        voting_result = meta.get("voting_result", {})
        model_scores = voting_result.get("model_scores", []) if voting_result else []

        # ── P0 Fix: Fallback to trade_attribution DB when metadata is empty ──
        # trade_attribution is written at signal creation time with correct values,
        # while position.metadata may be lost due to service restart or sync overwrite.
        signal_strength = meta.get("signal_strength", 0)
        signal_confidence = meta.get("signal_confidence", 0.0)
        # Task #42 (P0, 2026-05-28): 同步从 metadata 中读 voting/decision 快照,
        # 供后面 final_strength / consensus_level 优先使用
        _decision_data = {}
        _trade_date_for_lookup = _et_trade_date(position.entry_time)
        _direction_for_lookup = (
            position.direction.value if hasattr(position.direction, "value") else str(position.direction)
        )
        if signal_strength == 0:
            signal_strength, signal_confidence = _fetch_attribution_data(
                position.trace_id,
                symbol=position.symbol,
                trade_date=_trade_date_for_lookup,
            )
            if signal_strength > 0:
                logger.info(f"[平仓·归因回填] {position.symbol} trade_date={_trade_date_for_lookup} "
                            f"signal_strength={signal_strength} confidence={signal_confidence}")
        # Task #42 (P0, 2026-05-28): trade_attribution 仍为 0 时, 从 decisions.db 反查投票结果.
        # 场景: 代码重启后 position.metadata 丢失 + position.trace_id 为空
        # (例如 close-* 兜底 trace_id) 这种双丢失的古老仓位.
        if signal_strength == 0:
            _decision_data = _fetch_decision_data(
                symbol=position.symbol,
                trade_date=_trade_date_for_lookup,
                direction=_direction_for_lookup,
            )
            if _decision_data.get("signal_strength", 0) > 0:
                signal_strength = _decision_data["signal_strength"]
                signal_confidence = _decision_data["signal_confidence"]
                logger.info(
                    f"[平仓·decisions 兜底] {position.symbol} "
                    f"trade_date={_trade_date_for_lookup} dir={_direction_for_lookup} "
                    f"decision_id={_decision_data.get('decision_id', '')} "
                    f"final_strength={signal_strength} consensus={signal_confidence}"
                )

        # P1-5 (2026-05-14): UPDATE trade_attribution 的 exit_* 字段 (F19 完整 schema)
        try:
            import sqlite3 as _sql3
            from app.core.paths import get_trade_records_db
            _holding_min = int(position.holding_hours * 60) if position.holding_hours else 0
            _exit_reason_str = exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason)
            _conn = _sql3.connect(str(get_trade_records_db()))
            _conn.execute("""
                UPDATE trade_attribution SET
                    exit_time = ?, exit_price = ?, realized_pnl = ?,
                    exit_reason = ?, holding_minutes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE trace_id = ?
            """, (
                position.exit_time.isoformat() if position.exit_time else None,
                float(current_price),
                float(pnl),
                _exit_reason_str,
                _holding_min,
                position.trace_id or "",
            ))
            _conn.commit()
            _conn.close()
        except Exception as _e:
            logger.debug(f"[平仓·attribution UPDATE] {position.symbol} 失败 (不影响平仓): {_e}")

        # O4 (2026-05-16): voting 缺失时回退到 signal_strength/confidence
        # 5/15 4/12 主线 trades final_strength=0 (fast-path 传 None → voting_result={} → 0)
        # Task #42 (2026-05-28): 优先顺序 — voting_result -> decisions.db -> signal_strength
        _final_strength = (voting_result or {}).get("final_strength", 0) or 0
        _consensus_level = (voting_result or {}).get("consensus_level", 0.0) or 0.0
        if not _final_strength and _decision_data.get("final_strength", 0):
            _final_strength = int(_decision_data["final_strength"])
        if not _consensus_level and _decision_data.get("consensus_level", 0.0):
            _consensus_level = float(_decision_data["consensus_level"])
        if not _final_strength:
            _final_strength = int(signal_strength or 0)
        if not _consensus_level:
            _consensus_level = float(signal_confidence or 0.0)

        # O4: 写 market_regime + mode (此前两列 NULL/DEFAULT)
        # P1-1 修复 (2026-05-23): UNKNOWN → RANGE fallback, 消除93%UNKNOWN
        _regime = ""
        try:
            _raw = position.market_regime
            if hasattr(_raw, "value"):
                _regime = _raw.value
            elif _raw:
                _regime = str(_raw)
            # 规范化: 将 legacy 非标准值映射到 MarketRegime 枚举值
            _REGIME_NORMALIZE = {
                "unknown": "RANGE", "UNKNOWN": "RANGE", "": "RANGE",
                "normal": "RANGE", "NORMAL": "RANGE", "中性": "RANGE",
                "trending": "TREND_UP", "bullish": "TREND_UP",
                "bearish": "TREND_DOWN", "high_volatility": "HIGH_VOL",
            }
            _regime = _REGIME_NORMALIZE.get(_regime, _regime) or "RANGE"
        except Exception:
            _regime = "RANGE"
        _mode = "normal"
        try:
            from app.core.config_container import get_config as _gc
            if _gc().get_dotted("mode.sandbox.enabled", False):
                _mode = "sandbox"
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # P2 修复 (2026-05-19): legacy close trace_id 兜底
        # 5/18 ARM legacy 仓位被 FLAT_ALL 强平触发的 close 事件 (id=718/719) 无 trace_id —
        # legacy 持仓没有原始 entry signal trace_id, trade_records 写空 → 下游归因/对账断链.
        # 兜底: close-side 生成 close-{symbol}-{ts} 格式 trace_id.
        _trace_id = position.trace_id
        if not _trace_id:
            _trace_id = f"close-{position.symbol}-{int(time.time() * 1000)}"
            logger.info(
                f"[平仓·trace 兜底] {position.symbol} 无 entry trace_id (legacy 仓位?), "
                f"生成 close-side: {_trace_id}"
            )
        
        # Task #15 (2026-05-27): 退出 trace_id 传递 —
        # 优先从 position.metadata.exit_trace_id (position_exit_monitor 注入) 读取,
        # 未设置则 fallback 生成 (兼容 position_monitor / FLAT_ALL 等其他退出路径).
        _exit_trace_id = (meta.get("exit_trace_id") if isinstance(meta, dict) else "") or ""
        if not _exit_trace_id:
            _exit_trace_id = (
                f"EXIT-{position.symbol}-{now_et().strftime('%Y%m%d%H%M%S')}-"
                f"{uuid.uuid4().hex[:6].upper()}"
            )
        logger.info(
            f"[EXEC][trace:{_trace_id}\u2192{_exit_trace_id}] Close order placed for {position.symbol} "
            f"(reason={exit_reason.value if hasattr(exit_reason, 'value') else exit_reason}, "
            f"qty={position.size}, pnl=${pnl:.2f})"
        )
        
        scheduler.trade_store.save_trade({
            "trace_id":          _trace_id,
            # Task #15 (2026-05-27): 持久化 exit_trace_id，与 entry trace_id 配对, 支撑全链路复盘
            "exit_trace_id":     _exit_trace_id,
            "trade_date":        _et_trade_date(position.entry_time),
            "symbol":            position.symbol,
            "direction":         position.direction.value,
            "entry_time":        position.entry_time.isoformat(),
            "exit_time":         position.exit_time.isoformat(),
            "entry_price":       position.avg_entry_price,
            "exit_price":        current_price,
            "quantity":          position.size,
            "realized_pnl":      pnl,
            "pnl_pct":           pnl_pct,
            "exit_reason":       exit_reason.value,
            "holding_hours":     position.holding_hours,
            "signal_strength":   signal_strength,
            "signal_confidence": signal_confidence,
            "futu_order_id":     getattr(position, "futu_order_id", ""),
            "close_futu_order_id": meta.get("close_futu_order_id", ""),
            "voting_result":     voting_result,
            "final_strength":    _final_strength,
            "consensus_level":   _consensus_level,
            "model_scores":      model_scores,
            "strategy_id":       meta.get("strategy_id", "voting_llm"),
            "decision_reason":   meta.get("decision_reason", ""),
            "max_drawdown":      meta.get("max_drawdown", 0.0),
            # P0 修复 (2026-05-31): signal_id 归因链路 — 从 position 读取
            "signal_id":         getattr(position, "signal_id", "") or meta.get("signal_id", "") or _trace_id,
            # Phase A (2026-05-14): 持仓的 owner_strategy 必须落到 trade_records
            "owner_strategy":    getattr(position, "owner_strategy", None) or "main",
            # O4 (2026-05-16): regime + mode 落库 (此前两列 UNKNOWN/normal 都未传入)
            "market_regime":     _regime or "RANGE",
            "mode":              _mode,
            # P0 修复 (2026-05-27): entry_reason 归因链路修复
            # 从 position.metadata 读取开仓时写入的 entry_reason, 兜底从 strategy_id 推导
            "entry_reason":      _derive_entry_reason_for_close(meta),
            # Task #14 (2026-05-27): 执行优化元数据 (TWAP / 滑点 / 延迟)
            "metadata_json":     (meta.get("execution") if isinstance(meta.get("execution"), dict) else None),
            # P1 修复 (2026-05-31): decision_id 归因链路 — 关联 signal_snapshots + decisions
            # 优先从 position.metadata 读取 (开仓时写入), 兜底从 _decision_data 获取
            "decision_id":       meta.get("decision_id", "") or _decision_data.get("decision_id", ""),
        })

        # Task #26 (2026-05-27): 回填 voting_predictions 实际结果 (异步, 不阻平仓主线)
        try:
            from app.pipeline.post_trade_hook import on_position_closed as _vap_on_close
            _vap_on_close({
                "symbol": position.symbol,
                "pnl": float(pnl),
                "direction": (
                    position.direction.value
                    if hasattr(position.direction, "value")
                    else str(position.direction)
                ),
                "trade_date": _et_trade_date(position.entry_time),
            })
        except Exception as _vae:
            logger.debug(f"[投票准确率] 更新结果失败: {_vae}")

        # Y5 (2026-05-15): PnL 异常值监控 + 标的护栏 (主线路径)
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            get_symbol_daily_loss_guard().record_trade(position.symbol, float(pnl))
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # C4 fix (2026-05-31): 主线 P&L 计入共享日回撤池
        try:
            _risk_eng = getattr(scheduler, "risk_engine", None)
            _dd_ctrl = getattr(_risk_eng, "drawdown_controller", None) if _risk_eng else None
            if _dd_ctrl is not None:
                _dd_ctrl.record_trade_pnl(float(pnl), source="main")
        except Exception as _dd_e:
            logger.debug(f"[主线·C4] 回撤上报失败 (不影响平仓): {_dd_e}")

        # Task #29 (2026-05-27): 教训应用验证 — 学了→用了 链路
        try:
            from app.review.lesson_injector import LessonInjector
            _trade_dict = {
                "symbol": position.symbol,
                "trade_date": _et_trade_date(position.entry_time),
                "direction": str(position.direction.value if hasattr(position.direction, "value") else position.direction),
                "entry_time": position.entry_time.isoformat() if position.entry_time else "",
                "entry_price": float(position.avg_entry_price),
                "quantity": int(position.size),
                "stop_loss": float(getattr(position, "current_sl", 0) or 0),
                "signal_strength": int(signal_strength or 0),
                "signal_confidence": float(signal_confidence or 0.0),
                "realized_pnl": float(pnl),
            }
            _lesson_result = LessonInjector().verify_lesson_application(_trade_dict)
            if _lesson_result.get("applicable_lessons", 0) > 0:
                logger.info(
                    f"[教训验证] {position.symbol} "
                    f"教训应用率: {_lesson_result['application_rate']:.0%} "
                    f"({_lesson_result['applied_lessons']}/{_lesson_result['applicable_lessons']})"
                )
        except Exception as _le:
            logger.debug(f"[教训验证] 异常 (不影响平仓): {_le}")
        # Z1 (2026-05-15): Shadow 镜像同步平仓
        try:
            from app.evolution.shadow_mirror import get_shadow_mirror
            get_shadow_mirror().mirror_exit(
                real_trace_id=position.trace_id or position.position_id,
                symbol=position.symbol,
                exit_price=float(current_price),
                exit_reason=str(exit_reason.value if hasattr(exit_reason, "value") else exit_reason),
            )
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
        try:
            from app.risk.pnl_outlier_monitor import get_pnl_outlier_monitor
            get_pnl_outlier_monitor().check(position.symbol, float(pnl), {
                "direction": str(position.direction.value if hasattr(position.direction, "value") else position.direction),
                "source": "main",
                "exit_reason": str(exit_reason.value if hasattr(exit_reason, "value") else exit_reason),
            })
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # ── P0 Fix (2026-05-02): 关联决策结果到 decisions.db trade_outcomes ──
        # 这是 model_performance 更新和方向识别率评估的关键链路
        try:
            from app.storage.decision_recorder import get_decision_recorder
            recorder = get_decision_recorder()

            # 尝试从 metadata 获取 decision_id
            decision_id = meta.get("decision_id", "")
            if not decision_id:
                # Fallback: 通过 trace_id 从 trade_attribution 反查 decision_id
                if position.trace_id:
                    decision_id = _lookup_decision_id_by_trace(position.trace_id, position.symbol)

            if decision_id:
                recorder.link_trade_outcome(
                    decision_id=decision_id,
                    symbol=position.symbol,
                    direction=position.direction.value,
                    entry_price=position.avg_entry_price,
                    quantity=position.size,
                    exit_price=current_price,
                    realized_pnl=pnl,
                    exit_reason=exit_reason.value,
                    holding_hours=position.holding_hours,
                    entry_session=meta.get("entry_session"),
                    strategy_type=meta.get("strategy_type", meta.get("strategy_id", "voting_llm")),
                )
                logger.info(
                    f"[平仓·决策关联] {position.symbol} decision_id={decision_id} "
                    f"exit=${current_price:.2f} pnl=${pnl:.2f} reason={exit_reason.value}"
                )
            else:
                # P0 修复 (F20 2026-05-12): 不能丢数据, 写入 orphan_outcomes 用于后续手动归因
                logger.warning(
                    f"[平仓·决策关联] {position.symbol} 无法找到 decision_id "
                    f"(trace_id={position.trace_id})，写入 orphan_outcomes"
                )
                try:
                    from pathlib import Path as _P
                    import sqlite3 as _sq
                    orphan_db = _P(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
                    oconn = _sq.connect(str(orphan_db))
                    oconn.execute("""CREATE TABLE IF NOT EXISTS orphan_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT, direction TEXT, exit_price REAL, realized_pnl REAL,
                        exit_reason TEXT, exit_time TEXT, trace_id TEXT, size INTEGER,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )""")
                    oconn.execute(
                        "INSERT INTO orphan_outcomes (symbol, direction, exit_price, realized_pnl, exit_reason, exit_time, trace_id, size) VALUES (?,?,?,?,?,?,?,?)",
                        (position.symbol, position.direction.value, current_price, pnl, exit_reason.value, None, position.trace_id, position.size)
                    )
                    oconn.commit()
                    oconn.close()
                except Exception as _e:
                    logger.error(f"[orphan_outcomes] 写入失败: {_e}")
        except Exception as e:
            logger.warning(f"[平仓·决策关联] 失败 (不影响平仓主流程): {e}")

        # ── Phase 2: 更新归因记录 (正常流程) ──
        _update_trade_attribution(
            trace_id=position.trace_id,
            symbol=position.symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason.value,
            holding_hours=position.holding_hours,
            signal_strength=signal_strength,
            signal_confidence=signal_confidence,
            max_drawdown=meta.get("max_drawdown", 0.0),
        )

        # ═══════════════════════════════════════════════════════════
        # P0 修复 (2026-05-08): 写入 signal_outcomes 表
        # 打通主管线 (MAIN_PIPELINE) 学习闭环 — 552 笔交易中 96 笔主管线缺失信号结果
        # ═══════════════════════════════════════════════════════════
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            db_path = str(get_trade_records_db())

            # 从 meta 提取策略上下文
            strategy_id = meta.get("strategy_id", "voting_llm")
            market_regime = meta.get("market_regime", "RANGE")
            max_dd = meta.get("max_drawdown", 0.0)

            # 映射 exit_reason 到 regime (fallback)
            # P1-1 (2026-05-23): 使用标准 MarketRegime 枚举值
            if market_regime in ("UNKNOWN", "unknown", ""):
                er = exit_reason.value.lower()
                if "trail" in er:
                    market_regime = "TREND_UP"
                elif "stop" in er or "flash" in er:
                    market_regime = "HIGH_VOL"
                else:
                    market_regime = "RANGE"

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO signal_outcomes
                       (code, action, strategy, entry_price, exit_price, pnl_pct,
                        holding_periods, market_regime, scenario, adx_value,
                        mtf_alignment, sentiment_score, consensus_score,
                        quality_score, max_drawdown, max_profit, confidence, signal_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        position.symbol,
                        position.direction.value,
                        strategy_id,
                        position.avg_entry_price,
                        current_price,
                        pnl_pct * 100,  # 存储百分比
                        max(1, int(position.holding_hours * 4)) if position.holding_hours else 1,
                        market_regime,
                        meta.get("lifecycle", meta.get("entry_session", "unknown")),
                        meta.get("adx", 0.0) or 0.0,
                        meta.get("mtf_alignment", 0.0) or 0.0,
                        meta.get("sentiment_score", 0.0) or 0.0,
                        signal_confidence if signal_confidence > 0 else 0.5,
                        1.0,
                        abs(max_dd) if max_dd and max_dd < 0 else 0.0,
                        abs(pnl_pct) if pnl_pct > 0 else 0.0,
                        signal_confidence if signal_confidence > 0 else 0.5,
                        position.signal_id,  # P0-2 (2026-05-20): 信号-交易关联
                    )
                )
                conn.commit()

            logger.info(
                f"✅ [信号结果] {position.symbol} MAIN_PIPELINE 已写入 signal_outcomes | "
                f"strategy={strategy_id} | pnl={pnl_pct*100:+.2f}% | "
                f"regime={market_regime} | exit={exit_reason.value}"
            )
        except Exception as outcome_e:
            logger.warning(f"[信号结果] MAIN_PIPELINE 写入 signal_outcomes 失败 (不影响主流程): {outcome_e}")
        # ═══════════════════════════════════════════════════════════

        # ── Phase 3: 记录退出事件 (WAL) ──
        try:
            from app.core.event_log import log_event
            log_event(
                trace_id=position.trace_id,
                event_type="EXIT",
                symbol=position.symbol,
                data={
                    "exit_price": current_price,
                    "pnl": pnl,
                    "exit_reason": exit_reason.value,
                    "holding_hours": position.holding_hours,
                }
            )
        except Exception as e:
            logger.warning(f"[平仓] 写入交易记录失败: {e}")

    except Exception as e:
        logger.warning(f"[平仓] 写入交易记录失败（不影响平仓）: {e}")

    # 记录事件
    scheduler.event_store.append(TradeEvent(
        event_type="position_closed",
        position_id=position.position_id,
        signal_id=position.signal_id,
        data={
            "symbol": position.symbol,
            "exit_reason": exit_reason.value,
            "exit_price": current_price,
            "entry_price": position.avg_entry_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "holding_hours": position.holding_hours,
        },
        source="scheduler",
        severity=AlertLevel.INFO,
    ))

    # 推送平仓通知
    try:
        _notify_exit(scheduler, position, exit_reason, pnl, pnl_pct)
    except Exception as e:
        logger.debug(f"[平仓] 通知失败: {e}")

    # Phase 1: Obsidian 双写 — 美股平仓交易记录
    try:
        from app.core.obsidian_writer import get_obsidian_writer

        writer = get_obsidian_writer()
        direction_str = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
        writer.write_trade({
            'id': f"US-{position.symbol.replace('.', '_')}-{int(time.time())}",
            'code': f"US.{position.symbol}",
            'market': 'US',
            'entry_time': position.entry_time.isoformat() if hasattr(position.entry_time, 'isoformat') else str(position.entry_time),
            'exit_time': position.exit_time.isoformat() if hasattr(position.exit_time, 'isoformat') else str(position.exit_time),
            'entry_price': position.avg_entry_price,
            'exit_price': current_price,
            'quantity': position.size,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'strategy': meta.get('strategy_id', 'voting_llm'),
            'action': direction_str.upper(),
            'exit_reason': exit_reason.value if hasattr(exit_reason, 'value') else str(exit_reason),
            'tags': ['us_market', exit_reason.value if hasattr(exit_reason, 'value') else 'exit'],
            'stop_loss': getattr(position, 'stop_loss', None),
            'take_profit': getattr(position, 'take_profit', None),
            'highest_price': meta.get('highest_price', None),
            'lowest_price': meta.get('lowest_price', None),
        })
        logger.debug(f"[Obsidian] 美股平仓已写入: {position.symbol}")
    except Exception as _e:
        logger.debug(f"[Obsidian] 美股平仓写入失败: {_e}")

    # 风控：连续亏损记录
    if pnl < 0 and scheduler.risk_engine and hasattr(scheduler.risk_engine, "strategy_breaker"):
        try:
            scheduler.risk_engine.strategy_breaker.record_loss("voting_llm")
        except Exception as e:
            logger.debug(f"[平仓] 策略熔断记录失败: {e}")