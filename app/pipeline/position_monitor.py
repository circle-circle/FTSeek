"""
持仓监控管线 — FTSeek-US 持仓监控、止损止盈、对账

从 scheduler_tasks.py 拆分而来。本模块包含完整的持仓监控循环、
期权管理、资本安全水位追踪、Broker 对账。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.core.models import (
    Position, Direction, MarketRegime,
    Order, OrderType, ExitReason, TradeEvent, AlertLevel,
)
from app.utils.timezone import _et_trade_date
from app.execution.options_selector import OptionSelector
from app.risk.position_exit_monitor import get_exit_monitor

logger = logging.getLogger(__name__)

# 期权价格获取失败计数器 {option_code: fail_count}
_opt_price_fail_count: Dict[str, int] = {}


def _notify_risk_blocked(scheduler, symbol: str, reason: str):
    """风控拦截通知"""
    logger.debug(f"[风控] {symbol} 被拦截：{reason}")


def _get_option_price(scheduler, option_code: str, entry_price: float) -> float:
    """获取期权当前价格（双源容错：持仓查询 → 行情快照）
    
    旧逻辑仅依赖 get_option_positions()（遍历持仓列表匹配 option_code）。
    问题：Broker 断连或期权不在持仓列表时返回 0.0 → 监控静默跳过，止损止盈失效。
    修复：失败时 fallback 到 get_market_snapshot([option_code]) 直接查行情。
    """
    # 调试日志：检查 scheduler.execution_engine 状态
    if not scheduler.execution_engine:
        logger.warning(f"[期权监控] scheduler.execution_engine 为 None")
        return 0.0
    if not hasattr(scheduler.execution_engine, "broker_adapter"):
        logger.warning(f"[期权监控] scheduler.execution_engine.broker_adapter 属性缺失")
        return 0.0
    adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
    if not adapter:
        logger.warning(f"[期权监控] broker_adapter 为 None")
        return 0.0
    if not adapter.is_connected():
        logger.warning(f"[期权监控] broker_adapter.is_connected() = False")
        return 0.0

    # 源 1：从持仓列表获取 current_price
    try:
        opt_positions = adapter.get_option_positions()
        for op in opt_positions:
            if op.get("option_code") == option_code:
                price = float(op.get("current_price", 0))
                if price > 0:
                    return price
    except Exception as e:
        logger.debug(f"[期权监控] 持仓查询失败 {option_code}: {e}")

    # 源 2：get_market_snapshot 直接查期权行情（备用）
    try:
        if adapter.quote_ctx:
            from futu import RET_OK
            ret, data = adapter.quote_ctx.get_market_snapshot([option_code])
            if ret == RET_OK and data is not None and len(data) > 0:
                price = float(data["last_price"].values[0])
                if price > 0:
                    logger.info(f"[期权监控] 行情快照获取价格 {option_code}: ${price:.2f}")
                    return price
            else:
                logger.warning(f"[期权监控] 行情快照返回失败 {option_code}: ret={ret}")
        else:
            logger.warning(f"[期权监控] adapter.quote_ctx 为 None")
    except ImportError:
        logger.warning(f"[期权监控] futu 模块导入失败")
        pass
    except Exception as e:
        logger.warning(f"[期权监控] 行情快照失败 {option_code}: {e}")

    return 0.0


def _partial_close_position(scheduler, position: Position, current_price: float, close_qty: int, exit_reason: ExitReason):
    """部分平仓 — 分批止盈专用，减少仓位但不全平"""
    from app.pipeline.order_executor import _close_position

    if close_qty <= 0 or close_qty >= position.size:
        _close_position(scheduler, position, current_price, exit_reason)
        return

    if position.direction == Direction.LONG:
        pnl = (current_price - position.avg_entry_price) * close_qty
        pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0
    else:
        pnl = (position.avg_entry_price - current_price) * close_qty
        pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0

    logger.info(
        f"[分批止盈] {position.symbol} Tier触发 | "
        f"平仓 {close_qty}/{int(position.size)} 股 | "
        f"预估PnL=${pnl:.2f} ({pnl_pct:.2%}) | "
        f"剩余 {int(position.size - close_qty)} 股"
    )

    close_order = Order(
        client_order_id=f"{position.symbol}_scaleout_{int(time.time())}",
        signal_id=position.signal_id,
        symbol=position.symbol,
        direction=Direction.SHORT if position.direction == Direction.LONG else Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=close_qty,
        price=current_price,
    )
    if not hasattr(close_order, 'metadata') or close_order.metadata is None:
        close_order.metadata = {}
    close_order.metadata['is_exit'] = True

    try:
        if scheduler.execution_engine:
            scheduler.execution_engine.submit_order(close_order)
            position.size -= close_qty
            logger.info(
                f"[分批止盈·完成] {position.symbol} 仓位更新: "
                f"{int(position.size + close_qty)} → {int(position.size)} 股"
            )
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="partial_close"
                    )
            except Exception as db_err:
                logger.debug(f"[分批止盈] DB 同步失败: {db_err}")
            # P0 修复 (2026-05-11): 亏损的部分平仓也需要更新策略熔断计数器
            if pnl < 0 and scheduler.risk_engine and hasattr(scheduler.risk_engine, "strategy_breaker"):
                try:
                    scheduler.risk_engine.strategy_breaker.record_loss("voting_llm")
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[分批止盈·失败] {position.symbol}: {e}")


def _close_option_position(scheduler, position: Position, reason: str, current_option_price: float):
    """平仓期权持仓"""
    meta        = getattr(position, "metadata", {}) or {}
    option_code = meta.get("option_code", "")
    qty         = int(position.size)
    entry_price = position.avg_entry_price
    cost_total  = meta.get("cost_total", entry_price * qty * 100)
    now_str     = datetime.now().isoformat()
    exit_price  = current_option_price if current_option_price > 0 else 0.0
    value_now   = exit_price * qty * 100
    pnl         = value_now - cost_total
    pnl_pct     = pnl / cost_total if cost_total > 0 else 0.0
    holding_hours = 0.0
    if position.entry_time:
        # P0 修复 (2026-05-11): 复用 get_market_hours_us 跳过周末
        from app.risk.position_exit_monitor import get_market_hours_us
        holding_hours = get_market_hours_us(position.entry_time, datetime.now())

    logger.info(f"[期权平仓] {position.symbol} {option_code} reason={reason} exit=${exit_price:.2f} PnL=${pnl:.0f} ({pnl_pct:.2%})")

    # P0 fix (2026-05-22): Check order result BEFORE DB cleanup to prevent orphan options
    # Pattern 34: _close_option_position was fire-and-forget — DB marked exited but Futu still holds
    order_submitted = False
    try:
        adapter = None
        if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
            adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
        if adapter and option_code:
            result = adapter.submit_option_order(option_code=option_code, qty=qty, price=exit_price, direction="SELL")
            if result and result.get("status") in ("rejected", "failed"):
                error_msg = result.get("message", "").lower()
                # P1 修复：检测到"持仓不足"说明 Broker 端已平仓，应清理 DB
                if "持仓不足" in error_msg or "insufficient position" in error_msg or "no position" in error_msg:
                    logger.warning(f"[期权平仓] {position.symbol} 持仓不足 (Broker 端已平仓)，执行 DB 清理")
                    # 执行 DB 清理，避免幽灵仓
                    try:
                        if option_code:
                            scheduler.trade_store.remove_option_position(option_code)
                        scheduler.position_store.remove(position.symbol)
                        _remove_from_open_positions_db(position.symbol)
                    except Exception as e:
                        logger.warning(f"[期权平仓] DB 清理失败：{e}")
                    return
                else:
                    logger.warning(f"[期权平仓] {position.symbol} 订单被拒/失败 status={result.get('status')}, 跳过 DB 清理防止幽灵仓")
                    return  # Don't mark DB exited — Futu still holds position
            order_submitted = True
    except Exception as e:
        logger.warning(f"[期权平仓] {position.symbol} 卖出期权失败: {e}, 跳过DB清理防止幽灵仓")
        return  # Exception = order not confirmed, don't clean up DB

    try:
        # P0 修复 (2026-05-22): 检测 sandbox 模式
        _option_mode = "normal"
        try:
            from app.core.config_container import get_config as _gc
            if _gc().get_dotted("mode.sandbox.enabled", False):
                _option_mode = "sandbox"
        except Exception:
            pass
        scheduler.trade_store.save_trade({
            "trace_id": position.trace_id, "trade_date": _et_trade_date(position.entry_time or datetime.now()),
            "symbol": position.symbol, "direction": "SHORT_PROXY_OPTION",
            "entry_time": position.entry_time.isoformat() if position.entry_time else "",
            "exit_time": now_str, "entry_price": entry_price, "exit_price": exit_price,
            "quantity": qty, "realized_pnl": pnl, "pnl_pct": pnl_pct,
            "exit_reason": reason, "holding_hours": holding_hours,
            "signal_strength": meta.get("signal_strength", 0),
            "signal_confidence": meta.get("signal_confidence", 0.0), "futu_order_id": option_code,
            # P0 修复 (2026-05-22): 归因字段透传 — 期权交易也需要完整归因
            "strategy_id": meta.get("strategy_id", "voting_llm"),
            "owner_strategy": getattr(position, "owner_strategy", None) or "main",
            "source_type": "SHORT_PROXY_OPTION",
            "market_regime": getattr(position, "market_regime", "UNKNOWN") if hasattr(getattr(position, "market_regime", "UNKNOWN"), "value") else str(getattr(position, "market_regime", "UNKNOWN") or "UNKNOWN"),
            "mode": _option_mode,
        })
    except Exception as e:
        logger.warning(f"[期权平仓] 写入 trade_records 失败：{e}")

    try:
        if option_code:
            scheduler.trade_store.remove_option_position(option_code)
    except Exception as e:
        logger.warning(f"[期权平仓] 删除 option_positions 失败：{e}")

    scheduler.position_store.remove(position.symbol)
    _remove_from_open_positions_db(position.symbol)  # P0 修复: 同步清理 open_positions 表

    if scheduler.notifier:
        try:
            pnl_sign = "+" if pnl >= 0 else ""
            scheduler.notifier.send_text(
                f"期权平仓 [{reason}]\n标的：{position.symbol}\n期权：{option_code}\n"
                f"买入：${entry_price:.2f}  卖出：${exit_price:.2f}\n"
                f"手数：{qty}手  总盈亏：{pnl_sign}${pnl:.0f}（{pnl_sign}{pnl_pct:.2%}）"
            )
        except Exception as e:
            logger.warning(f"[期权平仓] 钉钉通知失败：{e}")

    # P0 修复 (2026-05-11): 亏损的期权平仓也需要更新策略熔断计数器
    if pnl < 0 and scheduler.risk_engine and hasattr(scheduler.risk_engine, "strategy_breaker"):
        try:
            scheduler.risk_engine.strategy_breaker.record_loss("voting_llm")
        except Exception:
            pass

    # P0 fix (2026-05-22): Update trade attribution on option close
    # Pattern 35: _close_option_position was missing _update_trade_attribution call
    # Root cause: attribution written on open but never updated on close → 0% coverage
    try:
        from app.pipeline.order_executor import _update_trade_attribution
        _update_trade_attribution(
            trace_id=position.trace_id,
            symbol=position.symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            holding_hours=holding_hours,
            signal_strength=meta.get("signal_strength", 0),
            signal_confidence=meta.get("signal_confidence", 0.0),
        )
        logger.debug(f"[期权平仓] 归因更新完成: {position.symbol} {position.trace_id}")
    except Exception as e:
        logger.warning(f"[期权平仓] 归因更新失败 (不影响平仓): {e}")


def _execute_option_close_order(scheduler, position, option_code, qty, reason, current_option_price=0.0):
    """执行期权平仓订单并记录交易"""
    try:
        adapter = None
        if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
            adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
        order_result = None
        if adapter and option_code:
            try:
                order_result = adapter.submit_option_order(option_code=option_code, qty=qty, price=0, direction="SELL")
                # 检查订单提交结果
                if order_result and order_result.get("status") == "rejected":
                    logger.error(f"[期权平仓] {position.symbol} 订单被拒绝：{order_result.get('error')}")
                    # 订单被拒绝，清除计数器避免无限循环，但不清理 DB
                    _opt_price_fail_count.pop(option_code, None)
                    return
                logger.info(f"[期权平仓] {position.symbol} 提交卖出订单 {option_code} qty={qty} result={order_result}")
            except Exception as e:
                logger.warning(f"[期权平仓] 提交订单失败：{e}")
                # 提交失败，清除计数器避免无限循环
                _opt_price_fail_count.pop(option_code, None)
                return
        if current_option_price <= 0:
            current_option_price = _get_option_price(scheduler, option_code, position.avg_entry_price)
        _close_option_position(scheduler, position, reason=reason, current_option_price=current_option_price)
        # 成功后清除失败计数器
        _opt_price_fail_count.pop(option_code, None)
    except Exception as e:
        logger.error(f"[期权平仓] {position.symbol} 执行异常：{e}", exc_info=True)


def _load_price_fail_closed_set() -> set:
    """加载 PRICE_FAIL 已关闭的期权代码集合，防止同步循环"""
    try:
        import json as _json
        _pf_p = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
        if os.path.exists(_pf_p):
            return set(_json.load(open(_pf_p)))
    except Exception:
        pass
    return set()


def restore_positions_from_db(scheduler):
    """服务重启后从 DB 快照恢复未平仓持仓到内存"""
    try:
        # P0 修复 (2026-05-21): 加载 PRICE_FAIL 已关闭集合，跳过这些期权
        _pf_closed = _load_price_fail_closed_set()
        if _pf_closed:
            logger.info(f"[持仓恢复] PRICE_FAIL 已关闭集合: {_pf_closed}")

        positions = scheduler.trade_store.load_open_positions()
        if not positions:
            logger.info("[持仓恢复] DB 无未平仓记录")
            return 0
        restored = 0
        for row in positions:
            try:
                symbol = row.get("symbol", "")
                if not symbol:
                    continue

                # P0 修复 (2026-05-21): 跳过 PRICE_FAIL 已关闭的期权，阻断同步死循环
                if symbol in _pf_closed:
                    logger.warning(f"[持仓恢复] 跳过 PRICE_FAIL 已关闭: {symbol}")
                    continue
                direction_str = row.get("direction", "LONG")
                direction = Direction.SHORT if direction_str == "SHORT" else Direction.LONG
                
                # P0 修复 (2026-05-20): 期权仓位归主线策略管理
                is_option = any(x in symbol for x in ['26', '27', '28']) and any(x in symbol for x in ['P', 'C'])
                strategy_id = "voting_llm_option" if is_option else row.get("strategy_id", "recovered")
                
                position = Position(
                    strategy_id=strategy_id,
                    signal_id=row.get("signal_id", ""), trace_id=row.get("trace_id", ""),
                    symbol=symbol, direction=direction,
                    size=float(row.get("size", row.get("quantity", 0))),
                    avg_entry_price=float(row.get("avg_entry_price", 0)),
                    initial_sl=float(row.get("initial_sl", 0)),
                    initial_tp=float(row.get("initial_tp", 0)),
                    current_sl=float(row.get("current_sl", 0)),
                    take_profit_levels=row.get("take_profit_levels", []) or [],
                    time_stop_hours=int(row.get("time_stop_hours", 168)),
                    market_regime=MarketRegime.RANGE,
                )
                entry_time_raw = row.get("entry_time")
                if entry_time_raw and isinstance(entry_time_raw, str):
                    position.entry_time = datetime.fromisoformat(entry_time_raw)
                elif isinstance(entry_time_raw, datetime):
                    position.entry_time = entry_time_raw
                else:
                    position.entry_time = datetime.now()
                position.trail_state = row.get("trail_state", {}) or {}
                position.metadata = {}
                # P0 修复 (2026-05-20): 期权仓位设置正确的 metadata.source
                is_option = any(x in symbol for x in ['26', '27', '28']) and any(x in symbol for x in ['P', 'C'])
                if is_option:
                    position.metadata = {"source": "voting_llm_option", "is_option": True}
                # P0 修复 (2026-05-12): 恢复异常标记，防止重启后无限循环平仓
                # P0 修复 (2026-05-13): 自动恢复 — 当 Broker 确认持仓存在且 DB 一致时,
                #   清除 is_anomalous 并恢复正常监控 (之前是一旦标记就永久阻塞退出逻辑)
                if row.get("is_anomalous"):
                    broker_qty = None
                    if broker_positions_map and symbol in broker_positions_map:
                        broker_qty = broker_positions_map[symbol].get("qty")
                    if broker_qty is not None:
                        db_size = float(row.get("size", row.get("quantity", 0)))
                        if abs(broker_qty - db_size) < 1:  # 允许 1 股舍入误差
                            # Broker 确认 + 数量一致 → 自动恢复
                            try:
                                scheduler.trade_store.unmark_anomalous(
                                    symbol,
                                    reason=f"broker_confirmed qty={broker_qty}"
                                )
                                logger.info(
                                    f"[持仓恢复·自动修复] {symbol} is_anomalous 已清除, "
                                    f"Broker 确认 {broker_qty} 股 = DB {db_size} 股, 恢复正常监控"
                                )
                            except Exception as e:
                                logger.warning(f"[持仓恢复·自动修复] {symbol} unmark_anomalous 失败: {e}")
                                # 降级: 仍然标记为阻塞
                                position.metadata["_close_blocked"] = True
                                position.metadata["_close_blocked_reason"] = f"DB is_anomalous=1, unmark failed: {e}"
                        else:
                            # Broker 有但数量不一致 → 仍然阻塞
                            position.metadata["_close_blocked"] = True
                            position.metadata["_close_blocked_reason"] = (
                                f"DB is_anomalous=1, Broker qty={broker_qty} ≠ DB {db_size}"
                            )
                            logger.warning(
                                f"[持仓恢复] {symbol} Broker/DB 数量不一致, 仍标记为异常"
                            )
                    else:
                        # Broker 无此持仓 → 仍标记为异常
                        position.metadata["_close_blocked"] = True
                        position.metadata["_close_blocked_reason"] = "DB is_anomalous=1 (历史平仓失败)"
                        position.metadata["_close_blocked_at"] = row.get("last_sync_at", "")
                        logger.warning(f"[持仓恢复] {symbol} 已标记为异常持仓, 跳过监控")
                scheduler.position_store.add(symbol, position)
                restored += 1
                logger.info(f"[持仓恢复] {symbol} {direction_str} {position.size}股 @${position.avg_entry_price:.2f}")
            except Exception as e:
                logger.warning(f"[持仓恢复] 恢复单条记录失败: {e}")
        logger.info(f"[持仓恢复] 完成：{restored}/{len(positions)} 条记录恢复")

        # P0-2 修复 (2026-05-22): 从 option_positions 表恢复期权持仓
        try:
            import sqlite3
            db_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ftseek_us.db'))
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT option_code, qty, entry_price, strike_price, expiry_date, entry_time FROM option_positions")
            option_rows = cursor.fetchall()
            conn.close()

            for row in option_rows:
                option_code, qty, entry_price, strike_price, expiry_date, entry_time = row
                if not option_code:
                    continue
                # 跳过 PRICE_FAIL 已关闭的期权
                if option_code in _pf_closed:
                    logger.warning(f"[持仓恢复] 跳过 PRICE_FAIL 已关闭期权：{option_code}")
                    continue
                # 检查内存中是否已存在
                if scheduler.position_store.has(option_code):
                    continue
                # 创建 Position 对象
                position = Position(
                    strategy_id="voting_llm_option",
                    signal_id="",
                    trace_id="",
                    symbol=option_code,
                    direction=Direction.LONG,  # 期权买入方向默认为 LONG
                    size=float(qty),
                    avg_entry_price=float(entry_price),
                    initial_sl=0.0,
                    initial_tp=0.0,
                    current_sl=0.0,
                    take_profit_levels=[],
                    time_stop_hours=72,
                    market_regime=MarketRegime.RANGE,
                )
                # 解析 entry_time
                if entry_time and isinstance(entry_time, str):
                    try:
                        position.entry_time = datetime.fromisoformat(entry_time)
                    except Exception:
                        position.entry_time = datetime.now()
                else:
                    position.entry_time = datetime.now()
                # 设置 metadata
                position.metadata = {
                    "source": "voting_llm_option",
                    "protection": "managed",  # P1 修复：期权仓位归主线策略管理
                    "is_option": True,
                    "option_code": option_code,
                    "strike_price": float(strike_price) if strike_price else 0.0,
                    "expiry_date": expiry_date or "",
                    "entry_time": entry_time or "",
                }
                scheduler.position_store.add(option_code, position)
                restored += 1
                logger.info(f"[持仓恢复] 期权 {option_code} LONG {qty}股 @${entry_price:.2f}")
        except Exception as e:
            logger.warning(f"[持仓恢复] 期权持仓恢复失败：{e}")

        return restored
    except Exception as e:
        logger.warning(f"[持仓恢复] 查询 DB 失败: {e}")
        return 0


def _monitor_option_positions(scheduler):
    """定期检查所有期权持仓，执行自动平仓和风险警报"""
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    # P0 修复 (2026-05-21): 增加 AFTER_HOURS 检查
    # 根因：期权在盘后时段 (16:00-20:00 ET) 流动性极低，价格数据不可靠
    #       且所有数据源（Yahoo/AlphaVantage/TwelveData）均不支持期权符号
    #       导致每 3 分钟触发一次"所有数据源均失败"ERROR
    #       修复：仅交易时段 (TRADING) 执行期权价格轮询和风控检查
    if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT, MarketPhase.PRE_MARKET, MarketPhase.AFTER_HOURS):
        return
    try:
        positions = scheduler.position_store.all()
        if not positions:
            return

        # P0 修复 (2026-05-21): 加载 PRICE_FAIL 已关闭集合，跳过这些期权的监控
        _pf_closed = _load_price_fail_closed_set()

        try:
            from app.core.config_container import get_config
            _oc = get_config().get_section("options", {})
        except Exception:
            _oc = {}
        tp_ratio = float(_oc.get("TAKE_PROFIT_RATIO", 2.0))
        sl_ratio = float(_oc.get("STOP_LOSS_RATIO", 0.3))
        for position in positions:
            try:
                meta = getattr(position, "metadata", {}) or {}
                if not meta.get("is_option") and not meta.get("option_code"):
                    continue
                option_code = meta.get("option_code", "")
                expiry_date = meta.get("expiry_date", "")

                # P0 修复 (2026-05-21): 跳过 PRICE_FAIL 已关闭的期权，阻断死循环
                if option_code in _pf_closed or position.symbol in _pf_closed:
                    continue
                entry_price = position.avg_entry_price
                qty = int(position.size)
                dte = OptionSelector.days_to_expiry(expiry_date) if expiry_date else 999

                if dte <= 1:
                    logger.info(f"[期权监控] {position.symbol} {option_code} DTE={dte}<=1，触发到期自动平仓")
                    _execute_option_close_order(scheduler, position, option_code, qty, reason="EXPIRE_DTE_LE_1")
                    continue

                entry_time_str = meta.get("entry_time", "")
                if entry_time_str:
                    try:
                        from datetime import datetime as _dt
                        from app.execution.short_strategy import load_short_config
                        entry_time = _dt.fromisoformat(entry_time_str)
                        # P0 修复 (2026-05-11): 复用 get_market_hours_us 跳过周末/节假日
                        # 旧代码用 wall-clock，期权周五持仓到周一会误触发
                        from app.risk.position_exit_monitor import get_market_hours_us
                        holding_hours = get_market_hours_us(entry_time, datetime.now())
                        _opt_cfg = load_short_config()
                        max_hours = _opt_cfg.get("options", {}).get("max_holding_hours", 72)
                        if holding_hours > max_hours:
                            logger.warning(f"[期权·时间止损] {position.symbol} {option_code} 持仓时间 {holding_hours:.1f}h > {max_hours}h")
                            _execute_option_close_order(scheduler, position, option_code, qty, reason="TIME_STOP_MAX_HOURS")
                            continue
                    except Exception as e:
                        logger.debug(f"[期权·时间止损] 解析 entry_time 失败: {e}")

                current_price = _get_option_price(scheduler, option_code, entry_price)
                if current_price <= 0:
                    _opt_price_fail_count[option_code] = _opt_price_fail_count.get(option_code, 0) + 1
                    _fail_n = _opt_price_fail_count[option_code]
                    if _fail_n % 10 == 1:
                        logger.warning(
                            f"[期权监控] 价格获取失败 {_fail_n}次 "
                            f"{position.symbol} {option_code} — 止损止盈暂不生效"
                        )
                    # P1 修复 (2026-05-22): PRICE_FAIL 改为告警 + 延迟处理
                    # P1b 修复 (2026-05-22): 100+ 次失败 → 强制平仓 + 写入 blocklist，防止无限循环
                    # P1c 修复 (2026-05-22 04:25): 50 次失败 → 立即写入 blocklist，防止重启后重导入
                    #     数据：PRICE_FAIL_50~53 已造成 -$13K+ 损失，blocklist 在 100 才写入太晚
                    if _fail_n >= 50 and _fail_n % 50 == 0:
                        logger.error(
                            f"[期权·风控告警] {position.symbol} {option_code} "
                            f"价格获取连续失败 {_fail_n}次 — 止盈止损暂不生效，等待行情恢复"
                        )
                        # P1c: 50 次失败即写入 blocklist（与 force-close 阈值同步）
                        #     防止 service restart 后 position_sync 重新导入已爆仓的期权
                        try:
                            _pf_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
                            import json as _pf_json
                            _existing = set()
                            if os.path.exists(_pf_path):
                                _existing = set(_pf_json.load(open(_pf_path)))
                            _existing.add(position.symbol)
                            _existing.add(option_code)
                            with open(_pf_path, 'w') as _pf_f:
                                _pf_json.dump(sorted(_existing), _pf_f)
                            logger.info(f"[期权·BLOCKLIST@50] 已写入 {position.symbol}, {option_code} → price_fail_closed.json (fail_n={_fail_n})")
                        except Exception as bl_err:
                            logger.error(f"[期权·BLOCKLIST@50] 写入失败: {bl_err}")
                    if _fail_n >= 100:
                        logger.critical(
                            f"[期权·PRICE_FAIL_FORCE_CLOSE] {position.symbol} {option_code} "
                            f"价格获取连续失败 {_fail_n}次，强制平仓并加入 blocklist"
                        )
                        try:
                            _execute_option_close_order(scheduler, position, option_code, qty, reason=f"PRICE_FAIL_{_fail_n}")
                        except Exception as pf_err:
                            logger.error(f"[期权·PRICE_FAIL_FORCE_CLOSE] 平仓执行失败: {pf_err}")
                        # 写入 blocklist 防止 position_sync 重新导入（50 次已写，此处幂等追加）
                        try:
                            _pf_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
                            import json as _pf_json
                            _existing = set()
                            if os.path.exists(_pf_path):
                                _existing = set(_pf_json.load(open(_pf_path)))
                            _existing.add(position.symbol)
                            _existing.add(option_code)
                            with open(_pf_path, 'w') as _pf_f:
                                _pf_json.dump(sorted(_existing), _pf_f)
                            logger.info(f"[期权·BLOCKLIST] 已写入 {position.symbol}, {option_code} → price_fail_closed.json")
                        except Exception as bl_err:
                            logger.error(f"[期权·BLOCKLIST] 写入失败: {bl_err}")
                        # P3 修复 (2026-05-26): 清理峰值跟踪防止僵尸记录
                        try:
                            from app.exit.option_quick_exit import get_quick_exit_engine
                            qe = get_quick_exit_engine()
                            qe.reset_peak(position.symbol)
                            logger.debug(f"[期权·清理峰值] {position.symbol} peak_profit 已清除")
                        except Exception as peak_err:
                            logger.debug(f"[期权·清理峰值] 失败 (可忽略): {peak_err}")
                        _opt_price_fail_count.pop(option_code, None)
                    continue
                _opt_price_fail_count.pop(option_code, None)  # 成功后清零

                pnl_ratio = current_price / entry_price if entry_price > 0 else 1.0
                pnl_pct = pnl_ratio - 1.0

                try:
                    from app.exit.option_quick_exit import get_quick_exit_engine
                    from app.core.config_container import get_config
                    _opt_cfg = get_config().get_section("options", {})
                    qe = get_quick_exit_engine(_opt_cfg.get("quick_exit", {}))
                    qe.profit_target = _opt_cfg.get("profit_target_pct", 0.15)
                    qe.stop_loss = abs(_opt_cfg.get("stop_loss_pct", -0.25))  # P3: 40% → 25% 补偿滑点
                    should_exit, exit_reason = qe.check_exit({
                        "symbol": position.symbol, "entry_price": entry_price,
                        "entry_time": meta.get("entry_time", ""), "expiry": expiry_date,
                        "pnl_pct": pnl_pct, "option_code": option_code,
                    }, current_price, pnl_pct)
                    if should_exit:
                        logger.info(f"[期权·快速退出] {position.symbol} {exit_reason}")
                        _execute_option_close_order(scheduler, position, option_code, qty, reason=f"QUICK_EXIT_{exit_reason[:20]}")
                        continue
                except Exception:
                    pass

                if pnl_ratio >= tp_ratio:
                    _execute_option_close_order(scheduler, position, option_code, qty, reason="TAKE_PROFIT", current_option_price=current_price)
                    continue
                if pnl_ratio <= sl_ratio:
                    _execute_option_close_order(scheduler, position, option_code, qty, reason="STOP_LOSS", current_option_price=current_price)
                    continue
            except Exception as e:
                logger.error(f"[期权监控] {position.symbol} 检查异常：{e}", exc_info=True)
    except Exception as e:
        logger.error(f"[期权监控] 全局异常：{e}", exc_info=True)

# ═══════════════════════════════════════════════════════════════════
#  持仓检查 (ExitMonitor 驱动)
# ═══════════════════════════════════════════════════════════════════

def _check_positions(scheduler):
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
        return
    if phase == MarketPhase.PRE_MARKET:
        return
    # P0 修复 (2026-05-14): 盘后时段 (AFTER_HOURS 16:00-20:00 ET) 禁止退出检查
    # 根因: 盘后流动性极低，价格数据不可靠，trailing stop 误触发风险高
    # 且 Futu 盘后下单可能无法成交或产生严重滑点
    if phase == MarketPhase.AFTER_HOURS:
        return
    positions = scheduler.position_store.all()
    if scheduler.polling_controller and positions:
        pos_info = {}
        for p in positions:
            meta = getattr(p, "metadata", {}) or {}
            if meta.get("is_option"): continue
            pos_info[p.symbol] = {"stop_loss": p.current_sl, "entry_price": p.avg_entry_price}
        at_risk = scheduler.polling_controller.check_stop_loss_risk_for_positions(pos_info)
        for risk in at_risk:
            logger.warning(f"[预估风控] {risk['symbol']} 预估触及止损 (上次价=${risk['last_price']:.2f} SL=${risk['stop_loss']:.2f} 概率={risk['risk_probability']:.0%})")
    monitored_positions = []
    # P0 Karpathy fix (2026-05-13): broker-synced 仓位不执行 exit_monitor 逻辑
    # 根因: QQQ 是 futu_sync 导入的 broker 仓位，被 exit_monitor 当作系统仓位管理，
    #       某次平仓失败后 is_anomalous=1 永久卡死。
    #       修复: 只让 system 仓位 (signal/scalping) 进入 exit_monitor。
    trail_only_positions = []  # broker-synced: 只更新 trail，不执行 exit
    for position in positions:
        meta = getattr(position, "metadata", {}) or {}
        if meta.get("is_option"): continue
        # broker-synced 仓位: 跳过 exit_monitor，但加入 trail-only 列表
        strategy = getattr(position, 'strategy_id', '') or ''
        if strategy in ('futu_sync',):
            trail_only_positions.append(position)
            continue
        if meta.get("_close_blocked"):
            # P0 修复 (2026-05-13): 运行时自动恢复 — 每小时尝试一次
            last_attempt = meta.get("_close_recovery_attempt", "")
            now_key = datetime.now().strftime("%Y-%m-%d %H:")
            hour_bucket = datetime.now().strftime("%Y-%m-%d %H")
            if last_attempt != hour_bucket:
                meta["_close_recovery_attempt"] = hour_bucket
                logger.info(
                    f"[监控·自动恢复尝试] {position.symbol} 尝试清除 is_anomalous..."
                )
                try:
                    result = scheduler.trade_store.unmark_anomalous(
                        position.symbol, reason="runtime_self_heal"
                    )
                    # result=True: 成功清除; result=False: 可能已清除或无记录 (也视为可恢复)
                    meta["_close_blocked"] = False
                    meta["_close_blocked_reason"] = ""
                    monitored_positions.append(position)
                    logger.info(
                        f"[监控·自动恢复成功] {position.symbol} 已恢复监控 "
                        f"(unmark_result={result})"
                    )
                    continue
                except Exception as e:
                    logger.warning(f"[监控·自动恢复] {position.symbol} 异常: {e}")
            last_warn = meta.get("_close_warned_at", "")
            if last_warn != now_key:
                logger.warning(f"[监控] {position.symbol} 已标记为异常持仓, 跳过监控")
                meta["_close_warned_at"] = now_key
            continue
        monitored_positions.append(position)
    if not monitored_positions: return
    from app.pipeline.order_executor import _get_current_price
    price_cache = {}
    for position in monitored_positions:
        current_price = _get_current_price(scheduler, position.symbol)
        # P2 fix (2026-05-01): Validate numeric type before caching (prevents dict from Push cache)
        if isinstance(current_price, (int, float)) and current_price > 0:
            price_cache[position.symbol] = current_price
            if scheduler.polling_controller:
                change_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
                scheduler.polling_controller.update_result(position.symbol, current_price, change_pct)
    if not price_cache:
        metadata = getattr(scheduler, "_price_fail_metadata", {"count": 0, "last_alert": ""})
        metadata["count"] = metadata.get("count", 0) + 1
        setattr(scheduler, "_price_fail_metadata", metadata)
        if metadata["count"] >= 6 and metadata.get("last_alert") != datetime.now().strftime("%Y-%m-%d %H:"):
            logger.warning(f"[退出监控] 连续 {metadata['count']} 轮价格获取失败，所有持仓失去止损保护！")
            metadata["last_alert"] = datetime.now().strftime("%Y-%m-%d %H:")
            setattr(scheduler, "_price_fail_metadata", metadata)
        return
    if hasattr(scheduler, "_price_fail_metadata"):
        scheduler._price_fail_metadata = {"count": 0, "last_alert": ""}
    exit_monitor = get_exit_monitor()
    if not exit_monitor._initialized:
        try:
            if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
                adapter = scheduler.execution_engine.broker_adapter
                if adapter and adapter.is_connected() and adapter.quote_ctx:
                    exit_monitor.initialize(quote_ctx=adapter.quote_ctx)
        except Exception as e:
            logger.debug(f"[退出监控] ExitMonitor 初始化跳过: {e}")
    def _get_price(sym):
        return price_cache.get(sym)
    def _execute_close(pos, reason):
        from app.pipeline.order_executor import _close_position
        try:
            reason_map = {"FLASH_EXIT": ExitReason.FLASH_EXIT, "HARD_STOP": ExitReason.HARD_STOP,
                "ATR_STOP": ExitReason.TRAILING_STOP, "CHANDELIER": ExitReason.TRAILING_STOP,
                "BREAKEVEN": ExitReason.BREAKEVEN_STOP, "TIME_STOP": ExitReason.TIME_STOP,
                "SCALE_OUT": ExitReason.SCALE_OUT, "PRE_CLOSE_EXIT": ExitReason.PRE_CLOSE_EXIT,
                "TECHNICAL_BREACH": ExitReason.STRUCTURE_EXIT}
            er = reason_map.get(reason.get("reason", ""), ExitReason.TRAILING_STOP)
            cp = reason.get("exit_price", pos.avg_entry_price)
            close_qty = reason.get("close_qty", 0)
            if close_qty > 0 and close_qty < pos.size:
                _partial_close_position(scheduler, pos, cp, close_qty, er)
            else:
                _close_position(scheduler, pos, cp, er)
            # P0 修复 (2026-05-12): _close_position 内部吞异常，不抛给调用方。
            # 改为检查 metadata 中的 _close_blocked 标记判断是否真正成功。
            meta = getattr(pos, "metadata", {}) or {}
            if meta.get("_close_blocked"):
                logger.error(f"[退出监控] {pos.symbol} 平仓被阻断: {meta.get('_close_blocked_reason', 'unknown')}")
                return False
            # P2 修复 (2026-05-08): 退出后设置信号冷却，防止同一标的立即被重新开仓
            # 根因: QCOM 05-07 单日 15 笔 SHORT，累计 -5.08% — 退出后无冷却导致反复进出
            try:
                scheduler._set_cooldown(pos.symbol)
                logger.info(f"[退出冷却] {pos.symbol} 设置信号冷却 ({getattr(scheduler, 'SIGNAL_COOLDOWN_MINUTES', 60)}min)")
            except Exception as ce:
                logger.debug(f"[退出冷却] 设置失败 {pos.symbol}: {ce}")
            return True
        except Exception as e:
            logger.error(f"[退出监控] {pos.symbol} 平仓执行失败: {e}")
            return False
    policy_guardian = None
    try:
        from app.risk.exit_policy_guardian import get_guardian
        policy_guardian = get_guardian()
    except Exception: pass
    earnings_guard = None
    try:
        from app.risk.earnings_risk_guard import EarningsRiskGuard
        earnings_guard = EarningsRiskGuard()
    except Exception: pass
    try:
        closed = exit_monitor.scan_positions(positions=monitored_positions, get_price_fn=_get_price,
            execute_close_fn=_execute_close, policy_guardian=policy_guardian, earnings_guard=earnings_guard)
        if closed:
            for c in closed:
                _pnl = c.get('pnl_pct', 0.0)
                logger.warning(f"[退出执行] {c['symbol']} | {c['reason']} | PnL={_pnl*100:.1f}%")
    except Exception as e:
        logger.error(f"[退出监控] 扫描异常: {e}", exc_info=True)
    try:
        if hasattr(scheduler, "trade_store") and scheduler.trade_store:
            import hashlib as _hl
            # 系统仓位: 已有 trail 更新
            _all_trail = list(monitored_positions) + trail_only_positions
            for pos in _all_trail:
                if not pos.trail_state: continue
                _stable = {k: v for k, v in pos.trail_state.items() if k != "prev_scan_price"}
                _stable["current_sl"] = getattr(pos, "current_sl", 0)
                current_hash = _hl.md5(json.dumps(_stable, sort_keys=True).encode()).hexdigest()
                last_hash = getattr(pos, "_last_trail_hash", None)
                if current_hash != last_hash:
                    # Fix: preserve original source instead of overwriting with "exit_monitor"
                    _pos_src = (getattr(pos, 'metadata', None) or {}).get('source', 'auto_main')
                    scheduler.trade_store.upsert_open_position(pos, source=_pos_src, action="update_trail")
                    pos._last_trail_hash = current_hash
    except Exception as e:
        logger.debug(f"[退出监控] trail_state DB 同步失败: {e}")

def _reconcile_position_store(scheduler):
    """P0: 强制 Broker 对账并同步到 PositionStore
    
    P0 修复 (2026-05-01): 复用 USPositionSync 的 Futu 连接，不再每次创建/关闭 OpenSecTradeContext。
    之前每次调用都新建连接 → query → close()，导致 CallClose 日志刷屏 + 浪费 OpenD 槽位。
    
    P0 增强 (2026-05-09): 
      - 跨 open_positions DB (Scalping) 交叉对账
      - 发现 discrepancies 时推送钉钉/飞书告警
      - 检测 "账面已平但券商仍在" (Scalping平仓未实际提交订单的Bug)
    """
    from app.core.models import Position as Pos, Direction as Dir, MarketRegime as MR
    
    # 1. 查询 Broker 实际持仓
    try:
        from app.core.position_sync import get_us_position_sync
        syncer = get_us_position_sync()
        broker_positions = syncer.query_broker_positions()
    except Exception as e:
        logger.debug(f"[持仓对账] Broker 查询失败: {e}")
        return
    
    # 2. 获取内存 PositionStore
    mem_positions = list(scheduler.position_store.all())
    mem_symbols = set(p.symbol for p in mem_positions)
    
    # 3. 获取 open_positions DB 中的 Scalping 记录
    db_scalping_symbols = _get_db_scalping_symbols()
    
    broker_symbols = set(broker_positions.keys()) if broker_positions else set()
    
    # ── 对账分析 ──
    discrepancies = []
    
    # 3a. Broker 有但内存无 → 可能是 Scalping 仓位 (内存已清但 open_positions 有)
    # P0 修复 (2026-05-23): 加载 PRICE_FAIL 已关闭集合，防止期权重导入循环
    # 根因: position_sync 正确跳过 PRICE_FAIL 期权，但对账函数未过滤，
    #       导致期权被重新加入 position_store → signal pipeline 扫描 → ERROR 循环
    _pf_closed = _load_price_fail_closed_set()

    new_symbols = broker_symbols - mem_symbols
    for symbol in new_symbols:
        # P0 修复 (2026-05-23): 跳过 PRICE_FAIL 已关闭的期权，与 position_sync 保持一致
        if symbol in _pf_closed:
            logger.warning(f"[持仓对账] 跳过 PRICE_FAIL 已关闭期权 {symbol} (防止重导入循环)")
            continue
        bp = broker_positions[symbol]
        is_scalping_in_db = symbol in db_scalping_symbols
        
        if is_scalping_in_db:
            # Scalping 在 open_positions 有记录但 PositionStore 已清 → 恢复
            direction = Dir.SHORT if bp['direction'] == 'SHORT' else Dir.LONG
            cost, size = bp['cost'], bp['qty']
            sl_mult = 1.08 if bp['direction'] == 'SHORT' else 0.92
            position = Pos(strategy_id="scalping", symbol=symbol, direction=direction, size=size,
                avg_entry_price=cost, initial_sl=cost*sl_mult, current_sl=cost*sl_mult,
                initial_tp=cost*(0.85 if bp['direction']=='SHORT' else 1.15),
                take_profit_levels=[], time_stop_hours=72, market_regime=MR.RANGE)
            position.trail_state = {"highest_price": cost, "lowest_price": cost, "prev_scan_price": cost}
            position.metadata = {"source": "scalping", "source_type": "SCALPING", "reconciled": True}
            scheduler.position_store.add(symbol, position)
            logger.warning(f"[持仓对账] 恢复 Scalping 持仓 {symbol} ({bp['direction']} {size}股 @${cost:.2f})")
            discrepancies.append({
                "type": "recovered_scalping",
                "symbol": symbol,
                "direction": bp['direction'],
                "size": size,
                "cost": cost,
            })
        else:
            # 完全未知的新持仓 → 可能是漏记，也可能是 Futu 结算延迟
            # P0 修复 (2026-05-11): 检查是否刚刚被系统平仓（Futu 结算延迟窗口）
            from datetime import datetime, timedelta
            RECENT_CLOSE_WINDOW_MINUTES = 10
            try:
                from app.storage.trade_store import TradeStore
                import os
                db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'ftseek_us.db')
                ts = TradeStore(db_path)
                conn = ts._get_conn()
                cutoff = (datetime.now() - timedelta(minutes=RECENT_CLOSE_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                recent_exits = conn.execute(
                    "SELECT COUNT(*) FROM trade_records WHERE symbol=? AND exit_time>=? AND exit_reason IS NOT NULL",
                    (symbol, cutoff)
                ).fetchone()[0]
                # 也检查 open_positions 表是否有同标的 recent close 标记
                recent_pos_closes = conn.execute(
                    "SELECT COUNT(*) FROM open_positions WHERE symbol=? AND sync_status='closed' AND updated_at>=?",
                    (symbol, cutoff)
                ).fetchone()[0]
                if recent_exits > 0 or recent_pos_closes > 0:
                    logger.info(
                        f"[持仓对账] {symbol}: Broker 有持仓但本地无记录，但 {RECENT_CLOSE_WINDOW_MINUTES}min 内"
                        f"有退出记录（{recent_exits}+{recent_pos_closes}），判定为 Futu 结算延迟，跳过创建"
                    )
                    continue
            except Exception as check_err:
                logger.debug(f"[持仓对账] {symbol}: 冷却期检查失败，继续按新持仓处理: {check_err}")

            direction = Dir.SHORT if bp['direction'] == 'SHORT' else Dir.LONG
            cost, size = bp['cost'], bp['qty']
            sl_mult = 1.08 if bp['direction'] == 'SHORT' else 0.92
            
            # P0 修复 (2026-05-11): 仓位集中度门禁 — 防止单只持仓超限
            from app.core.config_container import get_config
            try:
                cfg = get_config()
                max_pos_pct = cfg.get_dotted("risk.position.max_position_pct", 0.15)
                account_equity = 0.0
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    cs = get_capital_safety_system()
                    account_equity = getattr(cs._state, 'current_equity', 1000000)
                except Exception:
                    account_equity = float(cfg.get_dotted("capital.fixed_amount", 1000000))
                position_value = size * cost
                position_pct = position_value / account_equity if account_equity > 0 else 999
                if position_pct > max_pos_pct:
                    logger.critical(
                        f"[持仓对账·超限] {symbol} ({bp['direction']}) 仓位 {position_pct:.0%} "
                        f"超过上限 {max_pos_pct:.0%} (${position_value:,.0f} / ${account_equity:,.0f})。"
                        f"恢复真实持仓但标记告警，建议立即减仓。"
                    )
            except Exception as e:
                logger.warning(f"[持仓对账·集中度检查] 失败 (放行): {e}")
            
            position = Pos(strategy_id="broker_reconcile", symbol=symbol, direction=direction, size=size,
                avg_entry_price=cost, initial_sl=cost*sl_mult, current_sl=cost*sl_mult,
                initial_tp=cost*(0.85 if bp['direction']=='SHORT' else 1.15),
                take_profit_levels=[], time_stop_hours=72, market_regime=MR.RANGE)
            position.trail_state = {"highest_price": cost, "lowest_price": cost, "prev_scan_price": cost}
            scheduler.position_store.add(symbol, position)
            logger.warning(f"[持仓对账] 发现 Broker 新持仓 {symbol} ({bp['direction']} {size}股 @${cost:.2f})")
            discrepancies.append({
                "type": "unknown_position",
                "symbol": symbol,
                "direction": bp['direction'],
                "size": size,
                "cost": cost,
            })
    
    # 3b. 内存/DB 有但 Broker 无 → 幽灵仓
    all_local_symbols = mem_symbols | db_scalping_symbols
    ghost_symbols = all_local_symbols - broker_symbols
    try:
        from app.core.position_sync import get_us_position_sync
        option_symbols = get_us_position_sync()._get_option_position_symbols()
    except Exception:
        option_symbols = set()
    cleaned = 0
    for symbol in ghost_symbols:
        if symbol in option_symbols: continue
        
        # 判断是主管线还是Scalping
        is_scalping = symbol in db_scalping_symbols and symbol not in mem_symbols
        
        if symbol in mem_symbols:
            scheduler.position_store.remove(symbol)
        _remove_from_open_positions_db(symbol)
        cleaned += 1
        logger.info(f"[持仓对账] 清理幽灵仓 {symbol} ({'Scalping' if is_scalping else 'Main'})")
        discrepancies.append({
            "type": "ghost_cleaned",
            "symbol": symbol,
            "source": "scalping" if is_scalping else "main_pipeline",
        })
    
    # 3c. 共同持仓 → 检查数量/成本价差异
    common_symbols = mem_symbols & broker_symbols
    for symbol in common_symbols:
        bp = broker_positions[symbol]
        existing = scheduler.position_store.get(symbol)
        if existing is None: continue
        if abs(existing.size - bp['qty']) > 0.1 or abs(existing.avg_entry_price - bp['cost']) > 0.01:
            existing.size = bp['qty']
            existing.avg_entry_price = bp['cost']
            sl_mult = 1.08 if bp['direction'] == 'SHORT' else 0.92
            existing.initial_sl = bp['cost'] * sl_mult
            if not existing.current_sl or existing.current_sl == 0: existing.current_sl = bp['cost'] * sl_mult
            existing.trail_state = existing.trail_state or {}
            existing.trail_state["prev_scan_price"] = bp['cost']
            logger.info(f"[持仓对账] 更新 {symbol}: size={bp['qty']}, cost=${bp['cost']:.2f}")
    
    # ── 告警推送 ──
    if discrepancies:
        _send_reconcile_alert(scheduler, discrepancies, broker_positions, mem_positions, db_scalping_symbols)
    
    if new_symbols or cleaned:
        logger.info(f"[持仓对账] 完成: +{len(new_symbols)} 新增, -{cleaned} 清理, 内存共 {scheduler.position_store.count()} 个")


def _get_db_scalping_symbols() -> set:
    """从 open_positions DB 获取 Scalping 持仓标的"""
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT symbol FROM open_positions WHERE strategy_id='scalping'")
            symbols = set(row[0] for row in cursor.fetchall())
            return symbols
        finally:
            conn.close()
    except Exception:
        return set()


def _remove_from_open_positions_db(symbol: str):
    """从 open_positions DB 移除指定标的"""
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _send_reconcile_alert(scheduler, discrepancies: list, broker_positions: dict,
                          mem_positions: list, db_scalping_symbols: set):
    """对账发现差异时推送钉钉/飞书告警"""
    try:
        # 统计
        recovered = [d for d in discrepancies if d['type'] == 'recovered_scalping']
        unknown = [d for d in discrepancies if d['type'] == 'unknown_position']
        ghosts = [d for d in discrepancies if d['type'] == 'ghost_cleaned']
        
        broker_count = len(broker_positions)
        mem_count = len(mem_positions)
        db_count = len(db_scalping_symbols)
        
        lines = [
            f"### [FTSeek-US] 🔍 持仓对账告警\n\n",
            f"**📊 对账摘要**:\n\n",
            f"- 券商实际持仓: {broker_count} 只\n\n",
            f"- 内存 PositionStore: {mem_count} 只\n\n",
            f"- DB open_positions (Scalping): {db_count} 只\n\n",
        ]
        
        if recovered:
            lines.append(f"**🟡 已恢复 Scalping 持仓**:\n\n")
            for d in recovered:
                lines.append(f"- {d['symbol']} {d['direction']} {d['size']}股 @${d['cost']:.2f}\n\n")
        
        if unknown:
            lines.append(f"**🔴 未知新持仓 (需人工确认)**:\n\n")
            for d in unknown:
                lines.append(f"- {d['symbol']} {d['direction']} {d['size']}股 @${d['cost']:.2f}\n\n")
        
        if ghosts:
            lines.append(f"**⚪ 已清理幽灵仓**:\n\n")
            for d in ghosts:
                src = "Scalping" if d.get('source') == 'scalping' else 'Main'
                lines.append(f"- {d['symbol']} ({src})\n\n")
        
        lines.append(f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        if scheduler.notifier:
            scheduler.notifier.send_text("\n".join(lines))
            logger.info(f"[持仓对账] 告警已推送: {len(recovered)}恢复 + {len(unknown)}未知 + {len(ghosts)}幽灵")
    except Exception as e:
        logger.warning(f"[持仓对账] 告警推送失败: {e}")


def _update_capital_safety(scheduler):
    """更新资本安全水位状态"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        account_info = {}
        if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
            adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
            if adapter and adapter.is_connected():
                try: account_info = adapter.get_account_info()
                except Exception as e: logger.debug(f"[资本安全] 富途账户查询失败: {e}")
        total_assets = account_info.get("total_assets", 0)
        cash = account_info.get("cash", 0)
        market_val = account_info.get("market_val", 0)
        buying_power = account_info.get("buying_power", 0)
        if total_assets <= 0:
            # Broker API 不可用时，使用上次已知现金 + 实际持仓市值
            positions = scheduler.position_store.all()
            market_val = sum(p.size * p.avg_entry_price for p in positions if p.avg_entry_price > 0)
            # P2 修复 (2026-04-29): 用上次已知现金 + 实际市值，而非错误的 initial + mv*0.1
            last_cash = capital._state.cash if capital._state.cash > 0 else capital._state.initial_capital
            cash = last_cash
            total_assets = cash + market_val
            logger.debug(f"[资本安全] Broker 数据不可用，估算: cash=${cash:,.0f} mv=${market_val:,.0f} equity=${total_assets:,.0f}")
        state = capital.update_account(current_equity=total_assets, cash=cash, market_value=market_val, buying_power=buying_power)
        report = capital.get_status_report()
        is_first = not hasattr(scheduler, '_last_water_level')
        level_changed = is_first or state.water_level.value != getattr(scheduler, '_last_water_level', '')
        if level_changed:
            logger.info(f"[资本安全] {report['water_emoji']} {report['water_label']} | 净值=${report['current_equity']:,.0f} | 回撤 {report['drawdown_pct']:.1f}% | 风险系数 {report['risk_multiplier']}")
        if not hasattr(scheduler, '_last_water_level'): scheduler._last_water_level = state.water_level.value
        if state.water_level.value != scheduler._last_water_level:
            old_level = scheduler._last_water_level
            scheduler._last_water_level = state.water_level.value
            if scheduler.notifier and state.water_level.value in ('yellow','orange','red','blackout'):
                emojis = {"yellow":"🟡","orange":"🟠","red":"🔴","blackout":"⚫"}
                labels = {"yellow":"警戒","orange":"警告","red":"危险","blackout":"熔断"}
                scheduler.notifier.send_text(f"{emojis.get(state.water_level.value,'')} FTSeek-US 资本水位变化\n旧水位: {old_level} → 新水位: {labels.get(state.water_level.value,'')}\n当前净值: ${report['current_equity']:,.0f}\n历史高点: ${report['peak_equity']:,.0f}\n当前回撤: {report['drawdown_pct']:.1f}%")
                if state.water_level.value == 'blackout': logger.critical(f"[资本安全] 熔断触发: {state.lock_reason}")
    except Exception as e:
        logger.warning(f"[资本安全] 更新失败: {e}")


# ═══════════════════════════════════════════════════════════════════
#  待确认订单检查 (P0 根因修复 2026-05-14)
# ═══════════════════════════════════════════════════════════════════

def _check_pending_orders(scheduler):
    """
    检查所有待确认的订单状态
    
    职责:
    1. 检查未确认订单的成交状态
    2. 盘后订单在开盘时重新评估
    3. 超时订单标记为 STALE
    4. 确认成交后执行持仓清理和通知
    """
    try:
        from app.risk.order_tracker import get_order_tracker, OrderState
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        
        tracker = get_order_tracker()
        phase = get_market_phase()
        
        # 获取所有待处理的订单
        pending = tracker.get_pending_orders()
        if not pending:
            return
        
        logger.info(f"[订单追踪] 检查 {len(pending)} 个待确认订单 (phase={phase.value})")
        
        for order in pending:
            futu_id = order["futu_order_id"]
            symbol = order["symbol"]
            state = order["state"]
            
            # 盘后订单，等待开盘后处理
            if state == OrderState.AFTER_HOURS_PENDING.value:
                if phase == MarketPhase.TRADING:
                    logger.info(f"[订单追踪] {symbol} 盘后订单现在开盘，重新检查状态")
                    # 重新检查订单状态
                    _process_pending_order(scheduler, tracker, order)
                continue
            
            # 交易时段检查订单状态
            if phase == MarketPhase.TRADING:
                _process_pending_order(scheduler, tracker, order)
            elif state in (OrderState.PENDING_CONFIRM.value, OrderState.SUBMITTED.value):
                # 非交易时段检查超时
                tracker.check_stale_orders()
                
    except Exception as e:
        logger.warning(f"[订单追踪] 检查待确认订单失败: {e}")


def _process_pending_order(scheduler, tracker, order: dict):
    """处理单个待确认订单"""
    try:
        from app.risk.order_tracker import OrderState
        from app.pipeline.order_executor import _notify_exit, _close_position
        from app.core.models import ExitReason
        from futu import RET_OK as RET_OK_FUTU, TrdEnv
        
        futu_id = order["futu_order_id"]
        symbol = order["symbol"]
        direction = order["direction"]
        quantity = order["quantity"]
        exit_reason_str = order.get("exit_reason", "UNKNOWN")
        meta = order.get("metadata", {})
        
        # 查询订单状态
        if not scheduler.execution_engine or not scheduler.execution_engine.broker_adapter:
            logger.warning(f"[订单追踪] {symbol} 无 broker_adapter，跳过")
            return
        
        trd_env = TrdEnv.SIMULATE if getattr(scheduler, 'trade_env', 1) == 1 else TrdEnv.REAL
        ret, data = scheduler.execution_engine.broker_adapter.trade_ctx.order_list_query(
            order_id=futu_id,
            trd_env=trd_env
        )
        
        if ret != RET_OK_FUTU or data.empty:
            logger.warning(f"[订单追踪] {symbol} 查询订单 {futu_id} 失败: ret={ret}")
            return
        
        status = str(data.iloc[0].get('order_status', ''))
        logger.info(f"[订单追踪] {symbol} futu_id={futu_id} 状态={status}")
        
        if 'FILLED' in status or 'DEALT' in status:
            # 订单已成交，执行持仓清理和通知
            logger.info(f"[订单追踪] {symbol} 订单已成交，执行平仓清理")
            tracker.update_order_state(futu_id, OrderState.FILLED, {"futu_status": status})
            
            # 获取持仓信息
            position = scheduler.position_store.get(symbol)
            if position:
                # 获取当前价格
                from app.pipeline.order_executor import _get_current_price
                current_price = meta.get("close_current_price", _get_current_price(scheduler, symbol))
                if current_price <= 0:
                    current_price = position.avg_entry_price  # fallback
                
                # 计算盈亏
                if position.direction.value == "LONG":
                    pnl = (current_price - position.avg_entry_price) * position.size
                    pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
                else:
                    pnl = (position.avg_entry_price - current_price) * position.size
                    pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price
                
                # 执行平仓清理
                position.exit_time = __import__('datetime', fromlist=['datetime']).datetime.now()
                position.exit_price = current_price
                position.exit_reason = ExitReason.__members__.get(exit_reason_str, ExitReason.STOP_LOSS)
                position.realized_pnl = pnl
                
                # 从持仓中移除
                scheduler.position_store.remove(symbol)
                
                # 写入交易记录
                try:
                    if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                        # P0 修复 (2026-05-22): 从 position 和 trade_attribution 回填 signal_strength
                        _sig_strength = 0
                        _sig_confidence = 0.0
                        _owner = "main"
                        _strategy = "voting_llm"
                        try:
                            _pos_meta = getattr(position, "metadata", {}) or {}
                            _sig_strength = _pos_meta.get("signal_strength", 0)
                            _sig_confidence = _pos_meta.get("signal_confidence", 0.0)
                            _owner = getattr(position, "owner_strategy", None) or "main"
                            _strategy = _pos_meta.get("strategy_id", "voting_llm")
                            # Fallback: 从 trade_attribution DB 回填
                            if _sig_strength == 0 and position.trace_id:
                                from app.pipeline.order_executor import _fetch_attribution_data
                                _sig_strength, _sig_confidence = _fetch_attribution_data(
                                    position.trace_id,
                                    symbol=symbol,
                                    trade_date=_et_trade_date(position.entry_time),
                                )
                        except Exception:
                            pass
                        scheduler.trade_store.save_trade({
                            "trace_id": position.trace_id,
                            "trade_date": _et_trade_date(position.entry_time),
                            "symbol": symbol,
                            "direction": position.direction.value,
                            "entry_time": position.entry_time.isoformat(),
                            "exit_time": position.exit_time.isoformat(),
                            "entry_price": position.avg_entry_price,
                            "exit_price": current_price,
                            "quantity": position.size,
                            "realized_pnl": pnl,
                            "pnl_pct": pnl_pct,
                            "exit_reason": exit_reason_str,
                            "holding_hours": position.holding_hours,
                            "signal_strength": _sig_strength,
                            "signal_confidence": _sig_confidence,
                            "futu_order_id": getattr(position, "futu_order_id", ""),
                            "close_futu_order_id": futu_id,
                            "strategy_id": _strategy,
                            "owner_strategy": _owner,
                        })
                except Exception as e:
                    logger.warning(f"[订单追踪] {symbol} 交易记录写入失败: {e}")
                
                # 发送通知（只在确认成交后发送）
                try:
                    _notify_exit(scheduler, position, position.exit_reason or ExitReason.STOP_LOSS, pnl, pnl_pct)
                    logger.info(f"[订单追踪] {symbol} 平仓通知已发送")
                except Exception as e:
                    logger.warning(f"[订单追踪] {symbol} 通知发送失败: {e}")
            else:
                logger.warning(f"[订单追踪] {symbol} 持仓已不存在，跳过清理")
                
        elif 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
            # 订单失败/被拒绝
            logger.warning(f"[订单追踪] {symbol} 订单被拒绝，状态={status}")
            tracker.update_order_state(futu_id, OrderState.FAILED, {"futu_status": status})
            
            # 清除 pending 标记，让持仓继续被监控
            position = scheduler.position_store.get(symbol)
            if position:
                meta = getattr(position, "metadata", {}) or {}
                meta.pop("_close_order_submitted", None)
                meta.pop("_close_futu_order_id", None)
                meta.pop("_close_order_unconfirmed", None)
                position.metadata = meta
                logger.info(f"[订单追踪] {symbol} 订单失败，持仓继续监控")
                
        elif 'SUBMITTING' in status or 'SUBMITTED' in status or 'WAITING' in status:
            # 仍在排队
            logger.info(f"[订单追踪] {symbol} 订单仍在排队，状态={status}")
            tracker.update_order_state(futu_id, OrderState.PENDING_CONFIRM, {"futu_status": status})
            
    except Exception as e:
        logger.warning(f"[订单追踪] {symbol} 处理订单失败: {e}")


def monitor_loop(scheduler):
    """持仓监控主循环 (集成资本安全水位追踪)"""
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    last_capital_update = 0
    last_subscription_check = 0
    last_broker_reconcile = 0
    # last_coordinator_reconcile removed (2026-05-18): duplicate of T6 reconcile
    last_order_tracker_gc = 0  # P0-5 (2026-05-14)

    # P2-8 (2026-05-14): watchdog 注册 + 启动
    # P0-X (2026-05-15): 加 auto_ping (基于昨日 scan_loop 1802s 误报告警)
    try:
        from app.observability.watchdog import get_watchdog
        wd = get_watchdog()
        _interval_sec = scheduler.MONITOR_INTERVAL_MINUTES * 60
        wd.register("monitor_loop", stale_threshold_sec=_interval_sec * 5)
        wd.register("scan_loop", stale_threshold_sec=scheduler.SCAN_INTERVAL_MINUTES * 60 * 3)
        wd.start(check_interval_sec=60.0)
        # auto_ping: 只要 scheduler.running, 每 30s 自动 ping (不依赖循环顶部)
        wd.auto_ping_when_alive(
            "scan_loop", lambda: getattr(scheduler, "running", False), interval_sec=30
        )
        wd.auto_ping_when_alive(
            "monitor_loop", lambda: getattr(scheduler, "running", False), interval_sec=30
        )
    except Exception as _wde:
        logger.debug(f"[Watchdog] 启动失败 (降级): {_wde}")
        wd = None
    # _coord_reconcile_interval removed (2026-05-18): duplicate of T6 reconcile
    while scheduler.running:
        try:
            # P2-8: 心跳
            try:
                if wd is not None:
                    wd.ping("monitor_loop")
            except Exception:
                pass
            phase = get_market_phase()
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                for _ in range(int(scheduler.MONITOR_INTERVAL_MINUTES * 60)):
                    if not scheduler.running: return
                    time.sleep(1)
                continue
            now = time.time()
            if now - last_broker_reconcile >= 300:
                last_broker_reconcile = now
                try: _reconcile_position_store(scheduler)
                except Exception as e: logger.debug(f"[持仓对账] 异常: {e}")
            # P0-R2 (2026-05-14): 推送 SystemCircuitBreaker 状态到 PreTradeChecker
            try:
                _eng = getattr(scheduler, "risk_engine", None)
                _sb = getattr(_eng, "system_breaker", None) if _eng else None
                if _sb:
                    _state = "TRIGGERED" if _sb.is_triggered() else (
                        "DEGRADED" if (hasattr(_sb, "is_degraded") and _sb.is_degraded()) else "NORMAL"
                    )
                    _ck = getattr(_eng, "pre_trade_checker", None) if _eng else None
                    if _ck and hasattr(_ck, "update_breaker_state"):
                        _ck.update_breaker_state(_state)
            except Exception as _bre:
                logger.debug(f"[SystemBreaker push] 异常: {_bre}")
            # P1-8 (2026-05-14): Eastmoney 新闻源 symbols 与 watchlist 同步
            try:
                wl = getattr(scheduler, "watchlist", None)
                if wl:
                    # 提取 ticker (支持 dict / str / Stock 对象)
                    symbols = []
                    for item in wl:
                        if isinstance(item, str):
                            symbols.append(item.replace("US.", "").upper())
                        elif isinstance(item, dict):
                            sym = item.get("symbol") or item.get("code") or item.get("ticker")
                            if sym:
                                symbols.append(str(sym).replace("US.", "").upper())
                        elif hasattr(item, "symbol"):
                            symbols.append(str(item.symbol).replace("US.", "").upper())
                    # news_pipeline 内部 source list, 找 EastmoneyUSSource
                    if hasattr(scheduler, "news_pipeline_v2") and scheduler.news_pipeline_v2:
                        for src in getattr(scheduler.news_pipeline_v2, "_sources", []) or []:
                            if hasattr(src, "update_symbols") and getattr(src, "source_id", "") == "eastmoney_us":
                                src.update_symbols(symbols)
                                break
            except Exception as _ee:
                logger.debug(f"[Eastmoney·sync] 异常: {_ee}")

            # P0-5 (2026-05-14): OrderTracker 每日 GC
            if now - last_order_tracker_gc >= 86400:
                last_order_tracker_gc = now
                try:
                    from app.risk.order_tracker import get_order_tracker
                    get_order_tracker().gc_stale_orders(max_age_hours=24)
                except Exception as e:
                    logger.debug(f"[OrderTracker·GC] 异常: {e}")
            # Phase A (2026-05-14): coordinator 账本/锁与 PositionStore 对账
            # DISABLED (2026-05-18): Duplicate of T6 reconcile (scheduler_core.py:778-816).
            # Both ran on offset 150s intervals, causing capital oscillation:
            # T6 reads positions correctly (by_strategy={'main': 0.451}),
            # but this path reads EMPTY positions (by_strat_pct={}) → resets allocations to 0.0.
            # T6 alone is sufficient for periodic reconcile.
            # if now - last_coordinator_reconcile >= _coord_reconcile_interval:
            #     last_coordinator_reconcile = now
            #     try:
            #         _coord = getattr(scheduler, "coordinator", None)
            #         if _coord is not None and _coord.enabled:
            #             _coord.reconcile(scheduler.position_store)
            #             snap = _coord.capital.snapshot()
            #             logger.info(
            #                 f"[Coordinator·reconcile] capital={snap.by_strategy} "
            #                 f"total={snap.total_reserved:.3f}/{snap.max_total:.2f} "
            #                 f"locks={_coord.lock.all_locked()}"
            #             )
            #     except Exception as e:
            #         logger.debug(f"[Coordinator·reconcile] 异常: {e}")
            if now - last_subscription_check >= 600:
                last_subscription_check = now
                try: scheduler._ensure_futu_disconnected_during_hk()
                except Exception as e: logger.debug(f"[动态订阅] 检查异常: {e}")
            if scheduler._is_market_open():
                now = time.time()
                if now - last_capital_update >= 300:
                    try:
                        _update_capital_safety(scheduler)
                        last_capital_update = now
                    except Exception as e: logger.warning(f"[资本安全] 更新异常: {e}")
            # P2 修复 (2026-04-29): 非交易时段也更新资金状态 (盘前/盘后持仓仍存在)
            else:
                now = time.time()
                if now - last_capital_update >= 600:
                    try:
                        _update_capital_safety(scheduler)
                        last_capital_update = now
                    except Exception as e: logger.debug(f"[资本安全] 非交易时段更新异常: {e}")
            if scheduler.position_store.count() > 0:
                _check_positions(scheduler)
                _monitor_option_positions(scheduler)
            # P0 根因修复 (2026-05-14): 检查待确认订单状态
            _check_pending_orders(scheduler)
        except Exception as e:
            logger.error(f"[监控] 异常：{e}", exc_info=True)
        _wait = scheduler.MONITOR_INTERVAL_MINUTES * 60
        _start = time.time()
        while scheduler.running and (time.time() - _start) < _wait:
            time.sleep(1)
