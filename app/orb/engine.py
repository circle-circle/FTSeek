"""
ORB 辅线引擎 — RFC-056

独立 Opening Range Breakout 管线:
  - 5 秒轮询 daemon 线程 (突破窗口需要快速响应)
  - ETF 专属标的池 (与主线零重叠)
  - 3 层防御入场 + 独立退出栈
  - 与主线共享: PositionStore, ExecutionEngine, _open_position_lock

生命周期:
  scheduler_core.__init__() → get_orb_engine() → 构造
  scheduler_core.run_forever() → orb_engine.start() → 启动 daemon
  scheduler_core.stop() → orb_engine.stop() → 停止
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, time as dt_time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================
#  ORB 持仓状态
# ============================================================

class ORBPosition:
    """ORB 单仓位跟踪状态"""

    __slots__ = (
        "symbol", "direction", "entry_price", "quantity",
        "entry_time", "orb_high", "orb_low", "orb_mid",
        "atr_at_entry", "peak_price", "trough_price",
        "trail_stop", "order_id",
    )

    def __init__(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        orb_high: float,
        orb_low: float,
        atr_at_entry: float,
        order_id: str = "",
    ):
        self.symbol = symbol
        self.direction = direction  # "LONG" / "SHORT"
        self.entry_price = entry_price
        self.quantity = quantity
        self.entry_time = datetime.now()  # 用于日志, 非关键
        self.orb_high = orb_high
        self.orb_low = orb_low
        self.orb_mid = (orb_high + orb_low) / 2.0 if orb_high > 0 and orb_low > 0 else 0.0
        self.atr_at_entry = atr_at_entry

        # 追踪状态 (每轮更新)
        self.peak_price = entry_price     # LONG: 持仓最高
        self.trough_price = entry_price   # SHORT: 持仓最低
        self.trail_stop = 0.0             # 追踪止损线 (0 = 未激活)
        self.order_id = order_id


# ============================================================
#  ORB 引擎
# ============================================================

class ORBEngine:
    """ORB 独立引擎 — daemon 线程, 5 秒轮询"""

    def __init__(
        self,
        config: "ORBConfig",
        execution_engine: Any = None,
        data_router: Any = None,
        position_store: Any = None,
        notifier: Any = None,
        open_position_lock: Optional[threading.RLock] = None,
    ):
        from app.orb.config import ORBConfig
        from app.orb.exits import ORBExitStack

        self._cfg: ORBConfig = config
        self._exec = execution_engine
        self._data = data_router
        self._store = position_store
        self._notifier = notifier
        self._lock = open_position_lock or threading.RLock()

        # 退出栈
        self._exit_stack = ORBExitStack(
            sl_atr_mult=config.stop_loss_atr_mult,
            tp_atr_mult=config.take_profit_atr_mult,
            trail_activation_atr=config.trail_activation_atr,
            trail_multiplier=config.trail_multiplier,
            eod_time=config.eod_exit_time,
        )

        # 运行状态
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 每轮刷新的开盘区间 {symbol: {"high": float, "low": float, "ready": bool}}
        self._ranges: Dict[str, Dict[str, Any]] = {}

        # 活跃 ORB 仓位 {symbol: ORBPosition}
        self._positions: Dict[str, ORBPosition] = {}

        # 每日统计
        self._daily_trades = 0
        self._daily_date = ""

        logger.info(
            "[ORB] 引擎构造 | mode=%s | universe=%d只 | "
            "SL=%.1fATR TP=%.1fATR trail=%.1f×@%.1fATR",
            config.mode,
            len(config.universe),
            config.stop_loss_atr_mult,
            config.take_profit_atr_mult,
            config.trail_multiplier,
            config.trail_activation_atr,
        )

    # ── 生命周期 ──

    def start(self):
        """启动 daemon 线程"""
        if self._running:
            logger.warning("[ORB] 已在运行")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_forever,
            name="ORBEngine",
            daemon=True,
        )
        self._thread.start()
        logger.info("[ORB] daemon 线程已启动 | mode=%s", self._cfg.mode)

    def stop(self):
        """停止引擎"""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("[ORB] 引擎已停止")

    # ── 主循环 ──

    def _run_forever(self):
        """daemon 主循环"""
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.error("[ORB] tick 异常: %s", e, exc_info=True)

            time.sleep(self._cfg.polling_interval_seconds)

    def _tick(self):
        """单轮 tick — 根据时段分派"""
        from app.utils.timezone import now_et

        now = now_et()
        current_time = now.time()
        today_str = now.strftime("%Y-%m-%d")

        # 跨日重置
        if today_str != self._daily_date:
            self._daily_date = today_str
            self._daily_trades = 0
            self._ranges.clear()

        # 交易时段检查 (仅 ET 常规交易时段)
        market_open = dt_time(9, 30)
        range_end = dt_time(9, 30 + self._cfg.range_minutes)  # 09:45
        entry_end = _parse_time(self._cfg.entry_end_et)       # 14:00
        eod_exit = _parse_time(self._cfg.eod_exit_time)        # 15:55
        market_close = dt_time(16, 0)

        if current_time < market_open or current_time >= market_close:
            return  # 盘前/盘后, 跳过

        # Phase 1: 构建开盘区间 (09:30 - 09:45)
        if market_open <= current_time < range_end:
            ready_count = sum(1 for r in self._ranges.values() if r.get("ready"))
            if ready_count < len(self._cfg.universe):
                logger.info("[ORB] Phase 1: 构建区间 | %d/%d 就绪 | time=%s",
                            ready_count, len(self._cfg.universe), current_time.strftime("%H:%M:%S"))
            self._build_ranges()
            return

        # Phase 2: 入场窗口 (09:45 - entry_end) — 检测突破 + 监控已有仓位
        if range_end <= current_time < entry_end:
            # 容错: 如果区间还没构建 (晚启动), 立即补建
            ready_count = sum(1 for r in self._ranges.values() if r.get("ready"))
            if ready_count == 0:
                logger.info("[ORB] Phase 2: 区间未就绪, 延迟补建 (late start)")
                self._build_ranges()
            self._evaluate_breakouts(now, current_time)
            self._monitor_exits(now, current_time)
            return

        # Phase 3: 持仓监控 (11:00 - 15:55) — 只监控退出, 不开新仓
        if entry_end <= current_time < eod_exit:
            self._monitor_exits(now, current_time)
            return

        # Phase 4: 盘末退出 (15:55 - 16:00)
        if current_time >= eod_exit:
            self._force_close_all(now, current_time)

    # ── Phase 1: 构建开盘区间 ──

    def _build_ranges(self):
        """从 1-min kline 构建每个 ETF 的开盘区间"""
        if not self._data:
            logger.warning("[ORB] _build_ranges: data_router 不可用")
            return

        # 诊断: 检查数据路径 (仅首次)
        if not getattr(self, '_diag_logged', False):
            futu = getattr(self._data, 'futu', None)
            has_futu_kline = futu is not None and hasattr(futu, 'get_kline')
            logger.info("[ORB] 数据路径: data_router=%s, has_futu=%s, futu.get_kline=%s",
                         type(self._data).__name__, futu is not None, has_futu_kline)
            self._diag_logged = True

        for symbol in self._cfg.universe:
            if symbol in self._ranges and self._ranges[symbol].get("ready"):
                continue  # 已构建, 跳过
            try:
                # 获取 1-min klines (至少 range_minutes 根)
                klines = self._get_1min_klines(symbol, count=self._cfg.range_minutes + 5)
                if not klines or len(klines) < self._cfg.range_minutes:
                    logger.info("[ORB] %s kline 不足: got %d, need %d",
                                 symbol, len(klines) if klines else 0, self._cfg.range_minutes)
                    continue

                # 取开盘前 N 根作为区间 (klines 按时间排序, 取最早的 15 根)
                # 如果总数 <= range_minutes, 全用; 否则取前 N 根
                orb_bars = klines[:self._cfg.range_minutes] if len(klines) > self._cfg.range_minutes else klines
                orb_high = max(float(b.get("high", 0)) for b in orb_bars)
                orb_low = min(float(b.get("low", 0)) for b in orb_bars)

                if orb_high > 0 and orb_low > 0 and orb_high > orb_low:
                    width_pct = (orb_high - orb_low) / orb_low * 100
                    self._ranges[symbol] = {
                        "high": orb_high,
                        "low": orb_low,
                        "ready": True,
                        "width_pct": width_pct,
                    }
                    logger.debug(
                        "[ORB] %s 区间构建: H=%.2f L=%.2f W=%.2f%%",
                        symbol, orb_high, orb_low, width_pct,
                    )
            except Exception as e:
                logger.debug("[ORB] %s 区间构建失败: %s", symbol, e)

    # ── Phase 2: 突破检测 ──

    def _evaluate_breakouts(self, now: datetime, current_time: dt_time):
        """评估突破信号 — 3 层防御"""
        for symbol, rng in list(self._ranges.items()):
            if not rng.get("ready"):
                continue
            # 已有仓位 → 跳过 (本引擎内部)
            if symbol in self._positions:
                continue
            # 2026-06-01 互斥: 主线Pipeline已有该标的持仓 → 跳过
            # 防止 orb_pipeline 和 orb_engine 同时开仓同一ETF
            if self._store and self._store.has(symbol):
                logger.debug("[ORB] %s 跳过: PositionStore已有持仓 (主线或引擎)", symbol)
                continue
            # 每日交易上限
            if self._daily_trades >= self._cfg.max_daily_trades:
                break
            # 最大仓位数 (含引擎内部 + PositionStore中所有orb_*仓位)
            orb_count = len(self._positions)
            if self._store:
                for pos in self._store.all():
                    _owner = getattr(pos, 'owner_strategy', '') or ''
                    if _owner.startswith('orb_') and _owner != 'orb_engine':
                        orb_count += 1  # 计入 orb_pipeline 等其他ORB仓位
            if orb_count >= self._cfg.max_positions:
                break

            try:
                signal = self._check_breakout(symbol, rng)
                if signal:
                    self._execute_entry(symbol, signal, rng)
            except Exception as e:
                logger.warning("[ORB] %s 突破检测异常: %s", symbol, e)

    def _check_breakout(
        self, symbol: str, rng: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """3 层防御检测突破

        Returns:
            {"direction": "LONG"/"SHORT", "price": float, "atr": float} 或 None
        """
        orb_high = rng["high"]
        orb_low = rng["low"]
        width_pct = rng["width_pct"]

        # Layer 1: 区间宽度过滤
        if width_pct < self._cfg.min_range_width_pct:
            return None
        if width_pct > self._cfg.max_range_width_pct:
            return None

        # 获取当前价格
        price = self._get_current_price(symbol)
        if not price or price <= 0:
            return None

        # 获取 ATR
        atr = self._get_atr(symbol)
        if not atr or atr <= 0:
            atr = price * 0.01  # fallback: 1% 估算

        # 获取量能
        vol_ratio = self._get_volume_ratio(symbol)

        # Layer 2: 量能确认 (≥ 1.1x)
        if vol_ratio < self._cfg.min_volume_ratio:
            return None

        # Layer 3: 突破方向检测
        buffer = price * self._cfg.breakout_buffer_pct / 100.0

        if price > orb_high + buffer:
            # LONG 突破
            # 不追高: 价格已远离区间 > max_range_width_pct 则放弃
            chase_pct = (price - orb_high) / orb_high * 100
            if chase_pct > self._cfg.max_range_width_pct:
                logger.debug("[ORB] %s LONG 追高放弃: chase=%.2f%%", symbol, chase_pct)
                return None
            return {"direction": "LONG", "price": price, "atr": atr}

        if price < orb_low - buffer:
            # SHORT 突破
            chase_pct = (orb_low - price) / orb_low * 100
            if chase_pct > self._cfg.max_range_width_pct:
                logger.debug("[ORB] %s SHORT 追高放弃: chase=%.2f%%", symbol, chase_pct)
                return None
            return {"direction": "SHORT", "price": price, "atr": atr}

        return None

    def _execute_entry(
        self, symbol: str, signal: Dict[str, Any], rng: Dict[str, Any]
    ):
        """执行入场 (observe/shadow 记录, active 下单)"""
        direction = signal["direction"]
        price = signal["price"]
        atr = signal["atr"]

        # 计算仓位大小: 风险金额 / 止损距离
        sl_distance = atr * self._cfg.stop_loss_atr_mult
        if sl_distance <= 0:
            return

        equity = self._get_equity()
        risk_amount = equity * self._cfg.position_risk_pct
        quantity = int(risk_amount / sl_distance)
        if quantity <= 0:
            quantity = 1

        orb_mid = (rng["high"] + rng["low"]) / 2.0

        logger.info(
            "[ORB] 🎯 %s %s 突破信号 | price=%.2f | orb=[%.2f, %.2f] | "
            "atr=%.4f | qty=%d | SL=%.2f | TP=%.2f | mode=%s",
            symbol, direction, price, rng["low"], rng["high"],
            atr, quantity,
            price - sl_distance if direction == "LONG" else price + sl_distance,
            price + atr * self._cfg.take_profit_atr_mult if direction == "LONG"
                  else price - atr * self._cfg.take_profit_atr_mult,
            self._cfg.mode,
        )

        if not self._cfg.is_active:
            # observe / shadow 模式: 记录但不执行
            if self._cfg.is_shadow:
                logger.info("[ORB] shadow 模式: 记录虚拟仓位 %s %s", symbol, direction)
                # shadow 也创建 ORBPosition 跟踪虚拟表现
                self._positions[symbol] = ORBPosition(
                    symbol=symbol,
                    direction=direction,
                    entry_price=price,
                    quantity=quantity,
                    orb_high=rng["high"],
                    orb_low=rng["low"],
                    atr_at_entry=atr,
                    order_id=f"shadow_{uuid.uuid4().hex[:8]}",
                )
                self._daily_trades += 1
            return

        # active 模式: 实际下单
        result = self._submit_order(symbol, direction, price, quantity)
        if not result or not result.get("success"):
            logger.warning("[ORB] %s 下单失败: %s", symbol, result)
            return

        order_id = result.get("order_id", "")

        # 等待成交
        filled = self._wait_for_fill(result, timeout=15)
        if not filled:
            logger.warning("[ORB] %s 订单未成交, 放弃", symbol)
            return

        # 创建仓位跟踪
        pos = ORBPosition(
            symbol=symbol,
            direction=direction,
            entry_price=price,
            quantity=quantity,
            orb_high=rng["high"],
            orb_low=rng["low"],
            atr_at_entry=atr,
            order_id=order_id,
        )
        self._positions[symbol] = pos
        self._daily_trades += 1

        # 同步到 PositionStore (供退出监控使用)
        if self._store:
            from app.core.models import Position, Direction as ModelDirection
            store_pos = Position(
                symbol=symbol,
                direction=ModelDirection.SHORT if direction == "SHORT" else ModelDirection.LONG,
                size=quantity,
                entry_price=price,
                current_price=price,
            )
            store_pos.owner_strategy = "orb_engine"
            store_pos.metadata = {
                "orb_high": rng["high"],
                "orb_low": rng["low"],
                "orb_mid": orb_mid,
                "atr_at_entry": atr,
            }
            self._store.add(symbol, store_pos)

        # 通知
        self._notify(
            f"🟢 ORB 入场 | {symbol} {direction}\n"
            f"价格: ${price:.2f} | 数量: {quantity}\n"
            f"区间: [{rng['low']:.2f}, {rng['high']:.2f}]\n"
            f"SL: ${price - sl_distance:.2f} | TP: ${price + atr * self._cfg.take_profit_atr_mult:.2f}"
            if direction == "LONG" else
            f"🟢 ORB 入场 | {symbol} {direction}\n"
            f"价格: ${price:.2f} | 数量: {quantity}\n"
            f"区间: [{rng['low']:.2f}, {rng['high']:.2f}]\n"
            f"SL: ${price + sl_distance:.2f} | TP: ${price - atr * self._cfg.take_profit_atr_mult:.2f}"
        )

    # ── 退出监控 ──

    def _monitor_exits(self, now: datetime, current_time: dt_time):
        """监控所有活跃 ORB 仓位的退出条件"""
        for symbol, pos in list(self._positions.items()):
            try:
                price = self._get_current_price(symbol)
                if not price or price <= 0:
                    continue

                atr = self._get_atr(symbol) or pos.atr_at_entry

                # 更新 peak / trough
                if pos.direction == "LONG":
                    pos.peak_price = max(pos.peak_price, price)
                else:
                    pos.trough_price = min(pos.trough_price, price)

                peak = pos.peak_price if pos.direction == "LONG" else pos.trough_price

                # 更新追踪止损线 (ratchet)
                pos.trail_stop = self._exit_stack.compute_trail_stop(
                    pos.direction, atr, peak, pos.trail_stop, pos.entry_price,
                )

                # 评估退出
                decision = self._exit_stack.evaluate(
                    direction=pos.direction,
                    entry_price=pos.entry_price,
                    current_price=price,
                    atr=atr,
                    orb_mid=pos.orb_mid,
                    peak_price=peak,
                    trail_stop=pos.trail_stop,
                    current_time=current_time,
                )

                if decision and decision.should_exit:
                    self._execute_exit(symbol, pos, price, decision)

            except Exception as e:
                logger.warning("[ORB] %s 退出监控异常: %s", symbol, e)

    def _execute_exit(
        self,
        symbol: str,
        pos: ORBPosition,
        price: float,
        decision: "ORBExitDecision",
    ):
        """执行退出"""
        # 计算 PnL
        if pos.direction == "LONG":
            pnl = (price - pos.entry_price) * pos.quantity
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl = (pos.entry_price - price) * pos.quantity
            pnl_pct = (pos.entry_price - price) / pos.entry_price * 100

        logger.info(
            "[ORB] %s %s 退出 | %s | PnL: $%.2f (%.1f%%) | "
            f"entry=%.2f exit=%.2f qty=%d | %s",
            symbol, pos.direction, decision.reason,
            pnl, pnl_pct, pos.entry_price, price, pos.quantity, decision.detail,
        )

        if self._cfg.is_active:
            # 实际平仓
            close_dir = "SHORT" if pos.direction == "LONG" else "LONG"
            self._submit_order(symbol, close_dir, price, pos.quantity)

            # 从 PositionStore 移除
            if self._store:
                self._store.remove(symbol)

        # 移除仓位跟踪
        self._positions.pop(symbol, None)

        # 通知
        emoji = "💰" if pnl > 0 else "💸"
        self._notify(
            f"{emoji} ORB 退出 | {symbol} {pos.direction}\n"
            f"原因: {decision.reason}\n"
            f"PnL: ${pnl:.2f} ({pnl_pct:+.1f}%)\n"
            f"entry=${pos.entry_price:.2f} exit=${price:.2f}"
        )

    def _force_close_all(self, now: datetime, current_time: dt_time):
        """盘末强制全平"""
        for symbol, pos in list(self._positions.items()):
            price = self._get_current_price(symbol)
            if not price:
                price = pos.entry_price  # 取不到价用入场价

            from app.orb.exits import ORBExitDecision
            eod_decision = ORBExitDecision(
                reason="ORB_EOD_EXIT",
                close_pct=100.0,
                detail=f"盘末强制退出: {current_time.strftime('%H:%M')}",
            )
            self._execute_exit(symbol, pos, price, eod_decision)

    # ── 市场数据接口 ──

    def _get_current_price(self, symbol: str) -> Optional[float]:
        """获取当前价格"""
        if not self._data:
            return None
        try:
            quote = self._data.get_quote(symbol)
            if quote and isinstance(quote, dict):
                return float(quote.get("last", 0) or quote.get("price", 0) or 0)
            if hasattr(quote, "last"):
                return float(quote.last)
        except Exception:
            pass
        return None

    def _get_atr(self, symbol: str) -> Optional[float]:
        """获取 ATR (从 data_router 或 kline 计算)"""
        if not self._data:
            return None
        try:
            # 优先从 data_router 获取
            if hasattr(self._data, "get_atr"):
                return self._data.get_atr(symbol)
            # fallback: 从 kline 简单计算
            klines = self._get_1min_klines(symbol, count=20)
            if klines and len(klines) >= 14:
                trs = []
                for i in range(1, len(klines)):
                    h = float(klines[i].get("high", 0))
                    l = float(klines[i].get("low", 0))
                    pc = float(klines[i - 1].get("close", 0))
                    if h > 0 and l > 0 and pc > 0:
                        tr = max(h - l, abs(h - pc), abs(l - pc))
                        trs.append(tr)
                if trs:
                    return sum(trs[-14:]) / 14
        except Exception:
            pass
        return None

    def _get_volume_ratio(self, symbol: str) -> float:
        """获取当前成交量 / 5 日均量"""
        if not self._data:
            return 1.0  # 无数据时不阻拦
        try:
            if hasattr(self._data, "get_volume_ratio"):
                ratio = self._data.get_volume_ratio(symbol)
                if ratio and ratio > 0:
                    return float(ratio)
        except Exception:
            pass
        return 1.0  # fallback: 不过滤

    def _get_1min_klines(self, symbol: str, count: int = 20) -> Optional[List[Dict]]:
        """获取 1-min klines"""
        if not self._data:
            return None
        try:
            # 路径 1: data_router.get_klines (如果存在)
            if hasattr(self._data, "get_klines"):
                result = self._data.get_klines(symbol, ktype="1min", count=count)
                if result:
                    return result
            # 路径 2: data_router.futu.get_kline (Futu LV3 provider)
            futu = getattr(self._data, "futu", None)
            if futu and hasattr(futu, "get_kline"):
                result = futu.get_kline(symbol, ktype="K_1M", count=count)
                if result:
                    return result
                else:
                    logger.info("[ORB] %s futu.get_kline 返回空", symbol)
            # 路径 3: execution_engine.broker_adapter.get_kline
            if self._exec:
                adapter = getattr(self._exec, "broker_adapter", None)
                if adapter and hasattr(adapter, "get_kline"):
                    result = adapter.get_kline(symbol, ktype="K_1M", count=count)
                    if result:
                        return result
        except Exception as e:
            logger.warning("[ORB] %s kline 获取异常: %s", symbol, e)
        return None

    def _get_equity(self) -> float:
        """获取账户权益"""
        try:
            if hasattr(self._exec, "get_equity"):
                return float(self._exec.get_equity())
        except Exception:
            pass
        return 100000.0  # fallback: 模拟盘默认 $100K

    # ── 订单执行 ──

    def _submit_order(
        self, symbol: str, direction: str, price: float, quantity: float
    ) -> Optional[Dict[str, Any]]:
        """提交订单"""
        if not self._exec:
            logger.warning("[ORB] execution_engine 不可用")
            return None

        try:
            from app.core.models import Order, OrderType, Direction as ModelDirection

            price = round(price, 2)
            order = Order(
                client_order_id=f"orb_{symbol}_{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                direction=ModelDirection.SHORT if direction == "SHORT" else ModelDirection.LONG,
                order_type=OrderType.LIMIT,
                quantity=float(quantity),
                price=price,
            )

            if hasattr(self._exec, "submit_order"):
                result = self._exec.submit_order(order)
                order_id = getattr(result, "order_id", str(id(result)))
                return {"success": True, "order_id": str(order_id), "result": result}

        except Exception as e:
            logger.error("[ORB] %s 下单异常: %s", symbol, e, exc_info=True)

        return {"success": False, "error": "submit_order not available"}

    def _wait_for_fill(self, order_result: Dict, timeout: int = 15) -> bool:
        """等待订单成交确认 (与 scalping 同模式)"""
        result = order_result.get("result")
        if result is None:
            return False

        # 模拟盘: 直接 FILLED
        state = getattr(result, "state", None)
        if state is not None:
            if str(getattr(state, "value", state)) == "FILLED":
                return True

        return False  # fallback: 不阻塞

    # ── 通知 ──

    def _notify(self, message: str):
        """发送通知 (best-effort)"""
        if not self._notifier:
            return
        try:
            if hasattr(self._notifier, "send"):
                self._notifier.send(message)
            elif callable(self._notifier):
                self._notifier(message)
        except Exception:
            pass


# ============================================================
#  辅助函数
# ============================================================

def _parse_time(time_str: str) -> dt_time:
    """解析 HH:MM 字符串"""
    parts = time_str.split(":")
    return dt_time(int(parts[0]), int(parts[1]))


# ============================================================
#  单例
# ============================================================

_orb_engine_instance: Optional[ORBEngine] = None


def get_orb_engine(**kwargs) -> ORBEngine:
    """获取 ORB 引擎单例"""
    global _orb_engine_instance
    if _orb_engine_instance is None:
        from app.orb.config import ORBConfig

        raw_cfg = kwargs.pop("config", {})
        cfg = ORBConfig.from_dict(raw_cfg) if isinstance(raw_cfg, dict) else raw_cfg
        _orb_engine_instance = ORBEngine(config=cfg, **kwargs)

    return _orb_engine_instance
