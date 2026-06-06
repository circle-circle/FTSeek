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
    """ORB 单仓位跟踪状态 (增强版 — RFC-056-DESIGN)"""

    __slots__ = (
        "symbol", "direction", "entry_price", "quantity", "initial_quantity",
        "entry_time", "orb_high", "orb_low", "orb_mid",
        "atr_at_entry", "sl_distance", "peak_price", "trough_price",
        "trail_stop", "order_id",
        # 分层止盈追踪
        "layer1_done", "layer2_done",  # 是否已平仓 1/3, 2/3
        "breakeven_activated",        # 是否已移到 breakeven
        "trail_activated",            # 是否已激活追踪止损
        "r_multiple",                 # 风险单位 (sl_distance)
        # Task #11 (2026-06-02): 全链路归因 ID, 贯穿信号→入场→持仓→退出→trade_records
        "trace_id",
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
        sl_distance: float = 0.0,  # 止损距离 (价格单位)
        order_id: str = "",
        trace_id: str = "",
    ):
        self.symbol = symbol
        self.direction = direction  # "LONG" / "SHORT"
        self.entry_price = entry_price
        self.quantity = quantity
        self.initial_quantity = quantity  # 初始仓位 (用于分层止盈)
        self.entry_time = datetime.now()
        self.orb_high = orb_high
        self.orb_low = orb_low
        self.orb_mid = (orb_high + orb_low) / 2.0 if orb_high > 0 and orb_low > 0 else 0.0
        self.atr_at_entry = atr_at_entry
        self.sl_distance = sl_distance if sl_distance > 0 else atr_at_entry  # 兜底

        # 追踪状态 (每轮更新)
        self.peak_price = entry_price     # LONG: 持仓最高
        self.trough_price = entry_price   # SHORT: 持仓最低
        self.trail_stop = 0.0             # 追踪止损线 (0 = 未激活)
        self.order_id = order_id

        # 分层止盈状态
        self.layer1_done = False          # 1R 平 1/3
        self.layer2_done = False          # 2R 平 1/3
        self.breakeven_activated = False  # 止损移到 breakeven
        self.trail_activated = False      # 追踪止损激活
        self.r_multiple = sl_distance     # 风险单位

        # Task #11 (2026-06-02): trace_id 上传 — 若未传入则生成, 保证全链路不裸奔
        if not trace_id:
            try:
                from app.core.trace import generate_trace_id
                trace_id = generate_trace_id(strategy="orb_engine")
            except Exception:
                trace_id = ""
        self.trace_id = trace_id


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

        # 当日亏损退出的标的集合 (2026-06-03): 禁止同日重新入场
        self._closed_at_loss_today: set = set()

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

    # ── Futu 订阅 ──

    def _subscribe_symbols(self):
        """提前订阅 ORB 标的到 Futu (确保 K 线数据就绪)

        应在 start() 之前调用，给 Futu 数据管道预热时间。
        """
        if getattr(self, '_subscribed', False):
            return
        if not self._data:
            return
        try:
            futu = getattr(self._data, 'futu', None)
            if futu and hasattr(futu, 'subscribe'):
                orb_futu_symbols = [s.replace('US.', '') for s in self._cfg.universe]
                futu.subscribe(orb_futu_symbols, sub_types=['QUOTE', 'K_1M', 'K_15M'])
                logger.info("[ORB] ✅ Futu 订阅完成: %d 只 ETF (%s)",
                           len(orb_futu_symbols), orb_futu_symbols)
                self._subscribed = True
        except Exception as e:
            logger.warning("[ORB] ⚠️ Futu 订阅失败: %s", e)

    # ── Kline 工具 ──

    @staticmethod
    def _extract_today_opening_bars(klines: List[Dict], range_minutes: int = 15) -> List[Dict]:
        """从 kline 列表中提取当日开盘区间的 bar

        优先匹配 09:30 起的 bar；若 09:30 不可用则用当日最早 bar。
        返回最多 range_minutes 根。
        """
        if not klines:
            return []
        try:
            from datetime import datetime as _dt
            import pytz
            now_et = _dt.now(pytz.timezone('US/Eastern'))
            today_str = now_et.strftime("%Y-%m-%d")
            market_open_key = f"{today_str} 09:30"

            today_bars = []
            for k in klines:
                tk = str(k.get("time_key", k.get("time", "")))
                if tk >= market_open_key:
                    today_bars.append(k)

            # 如果 09:30 起无数据，尝试用当日全部 bar (晚启动场景)
            if not today_bars:
                for k in klines:
                    tk = str(k.get("time_key", k.get("time", "")))
                    if tk.startswith(today_str):
                        today_bars.append(k)

            return today_bars[:range_minutes]
        except Exception:
            return []

    # ── 生命周期 ──

    def start(self):
        """启动 daemon 线程"""
        if self._running:
            logger.warning("[ORB] 已在运行")
            return
        # 提前订阅: 给 Futu 数据管道预热时间 (必须在 daemon 启动前)
        self._subscribe_symbols()
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
        """单轮 tick — 连续突破模式 (移除时间限制)"""
        from app.utils.timezone import now_et

        now = now_et()
        current_time = now.time()
        today_str = now.strftime("%Y-%m-%d")

        # 状态日志：每 5 分钟输出一次
        tick_count = getattr(self, '_tick_count', 0) + 1
        self._tick_count = tick_count
        if tick_count % 60 == 1:
            logger.info("[ORB] tick #%d | time=%s | positions=%d | ranges=%d",
                       tick_count, current_time.strftime("%H:%M:%S"),
                       len(self._positions), len(self._ranges))

        # 跨日重置
        if today_str != self._daily_date:
            self._daily_date = today_str
            self._daily_trades = 0
            self._closed_at_loss_today = set()  # 重置亏损标的冷却 (2026-06-03)
            # 不再清除区间，允许隔夜持仓继续使用
            logger.info("[ORB] 跨日重置 | date=%s", today_str)

        # 交易时段检查 (仅 ET 常规交易时段)
        market_open = dt_time(9, 30)
        market_close = dt_time(16, 0)

        if current_time < market_open or current_time >= market_close:
            self._monitor_exits(now, current_time)
            return

        # Phase 1: 开盘区间构建 (09:30 - 09:45)
        range_end = dt_time(9, 45)
        if market_open <= current_time < range_end:
            ready_count = sum(1 for r in self._ranges.values()
                            if r.get("ready") and not r.get("is_rolling"))
            if ready_count < len(self._cfg.universe):
                logger.info("[ORB] Phase 1: 构建开盘区间 | %d/%d 就绪 | time=%s",
                            ready_count, len(self._cfg.universe), current_time.strftime("%H:%M:%S"))
                self._build_opening_ranges()
            # 同时监控持仓
            self._monitor_exits(now, current_time)
            return

        # Phase 2: 连续突破模式 (09:45 - 16:00)
        # 构建滚动区间 + 检测突破 + 监控持仓
        _t0 = time.time()
        self._build_rolling_ranges()
        _elapsed_build = time.time() - _t0

        _t1 = time.time()
        self._evaluate_breakouts(now, current_time)
        _elapsed_eval = time.time() - _t1

        _t2 = time.time()
        self._monitor_exits(now, current_time)
        _elapsed_monitor = time.time() - _t2

        # 诊断日志: 每 5 分钟输出一次 (tick_count % 60 == 1 已在上方处理)
        if tick_count % 60 == 1:
            logger.info(
                "[ORB] ⏱ build=%.1fs eval=%.1fs monitor=%.1fs ranges=%d positions=%d",
                _elapsed_build, _elapsed_eval, _elapsed_monitor,
                len(self._ranges), len(self._positions)
            )

        # 15:30 后调整隔夜持仓止损
        self._manage_overnight_positions(now, current_time)

    # ── Phase 1: 构建开盘区间 ──

    def _build_opening_ranges(self):
        """从 1-min kline 构建每个 ETF 的开盘区间 (增强版: 质量评分 + ATR + VWAP)"""
        if not self._data:
            logger.warning("[ORB] _build_ranges: data_router 不可用")
            return

        # 诊断: 检查数据路径 (仅首次)
        if not getattr(self, '_diag_logged', False):
            futu = getattr(self._data, 'futu', None)
            has_futu_kline = futu is not None and hasattr(futu, 'get_kline')
            subscribed = getattr(self, '_subscribed', False)
            logger.info("[ORB] 数据路径: data_router=%s, has_futu=%s, subscribed=%s",
                         type(self._data).__name__, has_futu_kline, subscribed)
            self._diag_logged = True

        for symbol in self._cfg.universe:
            if symbol in self._ranges and self._ranges[symbol].get("ready"):
                continue  # 已构建, 跳过
            try:
                # 获取 1-min klines (至少 range_minutes 根)
                klines = self._get_1min_klines(symbol, count=self._cfg.range_minutes + 5)
                if not klines or len(klines) < self._cfg.range_minutes:
                    # Fallback: 使用当前价格构建伪区间 (避免 daemon 卡死)
                    current_price = self._get_current_price(symbol)
                    if current_price and current_price > 0:
                        # 假设区间宽度为 0.3% (最小宽度)
                        width_pct = 0.3
                        orb_high = current_price * (1 + width_pct / 200)
                        orb_low = current_price * (1 - width_pct / 200)
                        # Fallback 区间质量较低
                        self._ranges[symbol] = {
                            "high": orb_high,
                            "low": orb_low,
                            "ready": True,
                            "width_pct": width_pct,
                            "atr": current_price * 0.003,  # 假设 ATR = 0.3%
                            "vwap": current_price,
                            "volume_ratio": 1.0,
                            "quality_score": 0.3,  # 低质量
                            "fallback": True,
                        }
                        logger.warning(
                            "[ORB] %s kline 不足 (%d/%d), 使用 fallback 区间: H=%.2f L=%.2f (质量=%.2f)",
                            symbol, len(klines) if klines else 0, self._cfg.range_minutes,
                            orb_high, orb_low, 0.3
                        )
                        continue
                    else:
                        logger.info("[ORB] %s kline 不足且无法获取当前价格: got %d, need %d",
                                     symbol, len(klines) if klines else 0, self._cfg.range_minutes)
                        continue

                # 提取当日开盘区间 bar (P0-fix: 按日期过滤, 防止使用前日数据)
                orb_bars = self._extract_today_opening_bars(klines, self._cfg.range_minutes)
                if not orb_bars:
                    # 无当日数据: 使用全部 kline (晚启动 / kline 无时间戳场景)
                    orb_bars = klines[:self._cfg.range_minutes]
                if not orb_bars or len(orb_bars) < 5:
                    logger.info("[ORB] %s 当日 bar 不足: %d 根 (总 kline=%d)",
                                symbol, len(orb_bars) if orb_bars else 0, len(klines))
                    continue
                orb_high = max(float(b.get("high", 0)) for b in orb_bars)
                orb_low = min(float(b.get("low", 0)) for b in orb_bars)

                if orb_high > 0 and orb_low > 0 and orb_high > orb_low:
                    width_pct = (orb_high - orb_low) / orb_low * 100

                    # 计算 ATR (从区间 K 线)
                    trs = []
                    for i in range(1, len(orb_bars)):
                        h = float(orb_bars[i].get("high", 0))
                        l = float(orb_bars[i].get("low", 0))
                        pc = float(orb_bars[i - 1].get("close", 0))
                        if h > 0 and l > 0 and pc > 0:
                            tr = max(h - l, abs(h - pc), abs(l - pc))
                            trs.append(tr)
                    atr = sum(trs) / len(trs) if trs else (orb_high - orb_low) / 2

                    # 计算 VWAP (从区间 K 线)
                    cum_vol = 0.0
                    cum_pv = 0.0
                    for b in orb_bars:
                        h = float(b.get("high", 0))
                        l = float(b.get("low", 0))
                        c = float(b.get("close", 0))
                        v = float(b.get("volume", 0))
                        if h > 0 and l > 0 and c > 0 and v > 0:
                            typical = (h + l + c) / 3.0
                            cum_pv += typical * v
                            cum_vol += v
                    vwap = cum_pv / cum_vol if cum_vol > 0 else (orb_high + orb_low) / 2

                    # 获取成交量比率
                    volume_ratio = self._get_volume_ratio(symbol)

                    # 计算质量评分 (假设 LONG 方向, 实际会在突破时重新计算)
                    current_price = self._get_current_price(symbol) or (orb_high + orb_low) / 2
                    quality_score = self._calculate_quality_score(
                        orb_width_pct=width_pct,
                        volume_ratio=volume_ratio,
                        price=current_price,
                        vwap=vwap,
                        direction="LONG",  # 初始假设
                    )

                    self._ranges[symbol] = {
                        "high": orb_high,
                        "low": orb_low,
                        "ready": True,
                        "width_pct": width_pct,
                        "atr": atr,
                        "vwap": vwap,
                        "volume_ratio": volume_ratio,
                        "quality_score": quality_score,
                        "fallback": False,
                    }
                    logger.info(
                        "[ORB] %s 区间构建成功: H=%.2f L=%.2f W=%.2f%% ATR=%.3f VWAP=%.2f VolR=%.2f Q=%.2f",
                        symbol, orb_high, orb_low, width_pct, atr, vwap, volume_ratio, quality_score,
                    )
            except Exception as e:
                logger.warning("[ORB] %s 区间构建异常: %s", symbol, e, exc_info=True)

    def _build_rolling_ranges(self):
        """构建 30 分钟滚动区间 (连续突破模式)

        30 分钟窗口比 15 分钟更稳定，减少区间紧贴价格导致的"零突破"问题。
        """
        if not self._data:
            logger.warning("[ORB] _build_rolling_ranges: data_router 不可用")
            return

        _ROLLING_WINDOW = 30  # 滚动区间长度 (分钟)
        _ROLLING_FETCH = _ROLLING_WINDOW + 5  # 多取 5 根冗余

        built_count = 0
        for symbol in self._cfg.universe:
            # 跳过已有持仓的标的
            if symbol in self._positions:
                continue

            # 跳过已有滚动区间的标的 (每 5 分钟更新一次)
            if symbol in self._ranges and self._ranges[symbol].get("is_rolling"):
                last_update = self._ranges[symbol].get("last_update", 0)
                if time.time() - last_update < 300:  # 5 分钟内不重复构建
                    continue

            try:
                # 获取最近 35 根 1-min klines (使用最近 30 根构建区间)
                klines = self._get_1min_klines(symbol, count=_ROLLING_FETCH)
                if not klines or len(klines) < _ROLLING_WINDOW:
                    logger.debug("[ORB] %s klines 不足: got %d, need %d",
                                symbol, len(klines) if klines else 0, _ROLLING_WINDOW)
                    continue

                # 取最近 30 根作为滚动区间
                rolling_bars = klines[-_ROLLING_WINDOW:]
                rolling_high = max(float(b.get("high", 0)) for b in rolling_bars)
                rolling_low = min(float(b.get("low", 0)) for b in rolling_bars)

                if rolling_high > 0 and rolling_low > 0 and rolling_high > rolling_low:
                    width_pct = (rolling_high - rolling_low) / rolling_low * 100

                    # 计算 ATR (从滚动 K 线)
                    trs = []
                    for i in range(1, len(rolling_bars)):
                        h = float(rolling_bars[i].get("high", 0))
                        l = float(rolling_bars[i].get("low", 0))
                        pc = float(rolling_bars[i - 1].get("close", 0))
                        if h > 0 and l > 0 and pc > 0:
                            tr = max(h - l, abs(h - pc), abs(l - pc))
                            trs.append(tr)
                    atr = sum(trs) / len(trs) if trs else (rolling_high - rolling_low) / 2

                    # 计算 VWAP (从滚动 K 线)
                    cum_vol = 0.0
                    cum_pv = 0.0
                    for b in rolling_bars:
                        h = float(b.get("high", 0))
                        l = float(b.get("low", 0))
                        c = float(b.get("close", 0))
                        v = float(b.get("volume", 0))
                        if h > 0 and l > 0 and c > 0 and v > 0:
                            typical = (h + l + c) / 3.0
                            cum_pv += typical * v
                            cum_vol += v
                    vwap = cum_pv / cum_vol if cum_vol > 0 else (rolling_high + rolling_low) / 2

                    # 获取成交量比率
                    volume_ratio = self._get_volume_ratio(symbol)

                    # 计算质量评分
                    current_price = self._get_current_price(symbol) or (rolling_high + rolling_low) / 2
                    quality_score = self._calculate_quality_score(
                        orb_width_pct=width_pct,
                        volume_ratio=volume_ratio,
                        price=current_price,
                        vwap=vwap,
                        direction="LONG",  # 初始假设
                    )

                    # 更新滚动区间
                    self._ranges[symbol] = {
                        "high": rolling_high,
                        "low": rolling_low,
                        "ready": True,
                        "width_pct": width_pct,
                        "atr": atr,
                        "vwap": vwap,
                        "volume_ratio": volume_ratio,
                        "quality_score": quality_score,
                        "fallback": False,
                        "is_rolling": True,  # 标记为滚动区间
                        "last_update": time.time(),  # 记录更新时间
                    }
                    built_count += 1
                    logger.info(
                        "[ORB] %s 滚动区间更新: H=%.2f L=%.2f W=%.2f%% ATR=%.3f Q=%.2f",
                        symbol, rolling_high, rolling_low, width_pct, atr, quality_score,
                    )
            except Exception as e:
                logger.warning("[ORB] %s 滚动区间构建异常: %s", symbol, e, exc_info=True)

        if built_count > 0:
            logger.info("[ORB] 滚动区间构建完成: %d/%d 个标的", built_count, len(self._cfg.universe))

    def _is_cross_engine_cooled(self, symbol: str) -> bool:
        """检查主线引擎是否在最近 30 分钟内退出了该标的.

        防止场景: MAIN 止损 XLK LONG → ORB 立刻开 XLK SHORT → 如果 XLK 反弹则两边亏.
        返回 True = 仍在冷却中, 应跳过.
        """
        _COOLDOWN_MINUTES = 30
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            conn = sqlite3.connect(str(get_trade_records_db()))
            row = conn.execute(
                "SELECT exit_time FROM trade_records "
                "WHERE symbol=? AND source_type='MAIN_PIPELINE' "
                "ORDER BY exit_time DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            conn.close()
            if not row:
                return False
            from datetime import datetime, timezone, timedelta
            exit_time_str = row[0]
            if not exit_time_str:
                return False
            # 解析 ISO 格式 (含时区)
            exit_dt = datetime.fromisoformat(exit_time_str)
            if exit_dt.tzinfo is None:
                exit_dt = exit_dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            elapsed_minutes = (now_utc - exit_dt).total_seconds() / 60.0
            if elapsed_minutes < _COOLDOWN_MINUTES:
                logger.info(
                    "[ORB·跨引擎冷却] %s 主线 %d 分钟前退出, 还需等 %d 分钟",
                    symbol, int(elapsed_minutes), _COOLDOWN_MINUTES - int(elapsed_minutes),
                )
                return True
        except Exception as e:
            logger.debug(f"[ORB·跨引擎冷却] {symbol} 查询失败: {e}")
        return False

    def _calculate_max_positions(self) -> int:
        """
        动态计算最大持仓数 (基于账户资金)

        计算逻辑:
        - 基础持仓数 = 账户资金 / 10000 (每 1 万美元允许 1 个持仓)
        - 最小持仓数 = 2
        - 最大持仓数 = 配置上限 (默认 10)
        - 隔夜持仓限制 = 最大持仓数 * 0.5 (隔夜风险更严)

        Returns:
            int: 最大允许持仓数
        """
        # 获取账户资金
        equity = self._get_account_equity()

        # 基础计算: 每 1 万美元允许 1 个持仓
        base_positions = int(equity / 10000)

        # 限制范围
        min_positions = 2
        max_positions = self._cfg.max_positions if self._cfg.max_positions > 0 else 10

        # 计算当前持仓数
        current_positions = len(self._positions)

        # 判断是否接近盘末 (15:30 后)
        from app.utils.timezone import now_et
        current_time = now_et().time()
        is_near_close = current_time >= dt_time(15, 30)

        if is_near_close:
            # 接近盘末: 限制新持仓 (允许隔夜但更保守)
            allowed = min(max(base_positions, min_positions), max_positions) // 2
            logger.debug("[ORB] 接近盘末, 持仓限制收紧: %d -> %d", max_positions, allowed)
            return allowed
        else:
            # 正常时段: 使用完整限制
            allowed = min(max(base_positions, min_positions), max_positions)
            return allowed

    def _get_account_equity(self) -> float:
        """获取账户资金 (从 PositionStore 或配置)"""
        # 尝试从 PositionStore 获取
        if self._store and hasattr(self._store, 'get_account_equity'):
            try:
                equity = self._store.get_account_equity()
                if equity and equity > 0:
                    return float(equity)
            except Exception as e:
                logger.debug("[ORB] 获取账户资金失败: %s", e)

        # Fallback: 从配置获取
        if hasattr(self._cfg, 'initial_capital') and self._cfg.initial_capital > 0:
            return float(self._cfg.initial_capital)

        # Default: 10 万美元
        return 100000.0

    def _manage_overnight_positions(self, now: datetime, current_time: dt_time):
        """
        管理隔夜持仓 (15:30 后触发)

        策略:
        1. 放宽止损: 止损距离 * 1.5 (隔夜波动更大)
        2. 追踪止损: 如果已激活, 放宽追踪距离 * 1.3
        3. 记录日志: 标记为隔夜持仓
        """
        # 只在 15:30 后执行一次
        if current_time < dt_time(15, 30):
            return

        # 检查今天是否已经调整过
        today = now.date()
        if hasattr(self, '_overnight_adjusted_date') and self._overnight_adjusted_date == today:
            return

        logger.info("[ORB] 15:30 后调整隔夜持仓止损")

        for symbol, pos in self._positions.items():
            # 放宽止损距离
            original_sl_distance = pos.sl_distance
            pos.sl_distance *= 1.5

            # 放宽追踪止损距离
            if pos.trail_stop > 0:
                original_trail_distance = abs(pos.entry_price - pos.trail_stop)
                new_trail_distance = original_trail_distance * 1.3

                if pos.direction == "LONG":
                    pos.trail_stop = pos.peak_price - new_trail_distance
                else:  # SHORT
                    pos.trail_stop = pos.trough_price + new_trail_distance

            logger.info(
                "[ORB] %s 隔夜止损调整: SL距离 %.2f -> %.2f, Trail %.2f -> %.2f",
                symbol,
                original_sl_distance,
                pos.sl_distance,
                abs(pos.entry_price - (pos.trail_stop if pos.trail_stop > 0 else pos.entry_price - original_sl_distance)),
                abs(pos.entry_price - pos.trail_stop) if pos.trail_stop > 0 else 0
            )

        # 标记今天已调整
        self._overnight_adjusted_date = today

    # ── Phase 2: 突破检测 ──

    def _evaluate_breakouts(self, now: datetime, current_time: dt_time):
        """评估突破信号 — 连续突破模式"""
        # 入场窗口截止检查 (entry_end_et 配置, 默认 14:00)
        entry_end_str = getattr(self._cfg, 'entry_end_et', '14:00')
        _h, _m = (int(x) for x in entry_end_str.split(':'))
        entry_end = dt_time(_h, _m)
        if current_time >= entry_end:
            return

        # 动态计算最大持仓数 (基于账户资金)
        max_positions = self._calculate_max_positions()

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
            # 2026-06-05 跨引擎冷却: MAIN 刚平仓的标的, ORB 30分钟内不开新仓
            # 防止 MAIN 止损后 ORB 立刻反向开仓 (如 XLK LONG 止损 → ORB SHORT)
            if self._is_cross_engine_cooled(symbol):
                logger.debug("[ORB] %s 跳过: 主线最近退出, 跨引擎冷却中", symbol)
                continue
            # 2026-06-03: 当日亏损退出的标的冷却 — 禁止重新入场
            if (getattr(self._cfg, 'same_symbol_loss_cooldown', False)
                    and symbol in getattr(self, '_closed_at_loss_today', set())):
                logger.debug("[ORB] %s 跳过: 当日亏损冷却 (禁止重新入场)", symbol)
                continue
            # 每日交易上限
            if self._daily_trades >= self._cfg.max_daily_trades:
                break
            # 动态持仓数限制 (含引擎内部 + PositionStore中所有orb_*仓位)
            orb_count = len(self._positions)
            if self._store:
                for pos in self._store.all():
                    _owner = getattr(pos, 'owner_strategy', '') or ''
                    if _owner.startswith('orb_') and _owner != 'orb_engine':
                        orb_count += 1  # 计入 orb_pipeline 等其他ORB仓位
            if orb_count >= max_positions:
                logger.debug("[ORB] 达到动态持仓上限 %d/%d", orb_count, max_positions)
                break

            try:
                signal = self._check_breakout(symbol, rng)
                if signal:
                    self._execute_entry(symbol, signal, rng)
            except Exception as e:
                logger.warning("[ORB] %s 突破检测异常: %s", symbol, e, exc_info=True)

    def _check_breakout(
        self, symbol: str, rng: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """4 层防御检测突破 (增强版: 质量评分)

        Returns:
            {"direction": "LONG"/"SHORT", "price": float, "atr": float, "regime": str} 或 None
        """
        orb_high = rng["high"]
        orb_low = rng["low"]
        width_pct = rng["width_pct"]

        # Layer 1: 区间宽度过滤
        if width_pct < self._cfg.min_range_width_pct:
            return None
        if width_pct > self._cfg.max_range_width_pct:
            return None

        # 获取当前价格 (唯一必须实时获取的数据)
        price = self._get_current_price(symbol)
        if not price or price <= 0:
            return None

        # ── 优先使用 rng 缓存的 ATR/volume_ratio (避免阻塞 Futu API) ──
        atr = rng.get("atr", 0)
        if not atr or atr <= 0:
            atr = self._get_atr(symbol)
        if not atr or atr <= 0:
            atr = price * 0.01  # fallback: 1% 估算

        vol_ratio = rng.get("volume_ratio", 1.0)

        # Layer 2: 量能确认 (≥ 1.1x)
        if vol_ratio < self._cfg.min_volume_ratio:
            return None

        # Layer 3: 突破方向检测
        # 混合 buffer: 取 max(pct-based, 0.1×ATR), 避免在窄幅区间过于敏感
        pct_buffer = price * self._cfg.breakout_buffer_pct / 100.0
        atr_buffer = atr * 0.1
        buffer = max(pct_buffer, atr_buffer)

        direction = None
        if price > orb_high + buffer:
            # LONG 突破
            # 不追高: 价格已远离区间 > max_range_width_pct 则放弃
            chase_pct = (price - orb_high) / orb_high * 100
            if chase_pct > self._cfg.max_range_width_pct:
                logger.debug("[ORB] %s LONG 追高放弃: chase=%.2f%%", symbol, chase_pct)
                return None
            direction = "LONG"

        elif price < orb_low - buffer:
            # SHORT 突破
            chase_pct = (orb_low - price) / orb_low * 100
            if chase_pct > self._cfg.max_range_width_pct:
                logger.debug("[ORB] %s SHORT 追高放弃: chase=%.2f%%", symbol, chase_pct)
                return None
            direction = "SHORT"

        if not direction:
            return None

        # Layer 4: 区间质量评分 (RFC-056-DESIGN)
        vwap = rng.get("vwap", 0.0)
        if vwap <= 0:
            vwap = self._get_vwap(symbol)

        quality_score = self._calculate_quality_score(
            orb_width_pct=width_pct,
            volume_ratio=vol_ratio,
            price=price,
            vwap=vwap,
            direction=direction,
        )

        # 质量门槛: < 0.5 不交易
        if quality_score < 0.5:
            logger.info(
                "[ORB] %s %s 质量不足: Q=%.2f < 0.5 (W=%.2f%% VolR=%.2f VWAP=%.2f)",
                symbol, direction, quality_score, width_pct, vol_ratio, vwap
            )
            return None

        # 判断市场状态 (简化版: 基于 ATR 和宽度)
        regime = "RANGE"
        if width_pct > 1.5 and vol_ratio > 1.3:
            regime = "TREND_UP" if direction == "LONG" else "TREND_DOWN"
        elif atr / price > 0.02:  # ATR > 2% 价格
            regime = "HIGH_VOL"
        elif atr / price < 0.005:  # ATR < 0.5% 价格
            regime = "LOW_VOL"

        # ── 方向偏见 (2026-06-03): regime-aware direction filter ──
        # 数据驱动: 昨晚真实数据 SHORT 盈利 10:1 胜过 LONG (下跌市)
        # TREND_DOWN / LOW_VOL+下行 → 提高 LONG 突破门槛, 降低假突破损失
        # TREND_UP → 提高 SHORT 突破门槛, 避免逆势做空
        bias_threshold = None
        bias_reason = ""

        if direction == "LONG":
            if regime == "TREND_DOWN":
                bias_threshold = getattr(self._cfg, 'regime_short_bias_quality', 0.85)
                bias_reason = f"TREND_DOWN → LONG bias Q≥{bias_threshold:.2f}"
            elif regime == "LOW_VOL" and vwap > 0 and price < vwap:
                # LOW_VOL+下行: 价格低于VWAP = 有下行压力, LONG 胜率低
                bias_threshold = getattr(self._cfg, 'low_vol_short_bias_quality', 0.85)
                bias_reason = f"LOW_VOL+下行 → LONG bias Q≥{bias_threshold:.2f}"
        elif direction == "SHORT":
            if regime == "TREND_UP":
                bias_threshold = getattr(self._cfg, 'regime_long_bias_quality', 0.85)
                bias_reason = f"TREND_UP → SHORT bias Q≥{bias_threshold:.2f}"

        if bias_threshold and quality_score < bias_threshold:
            logger.info(
                "[ORB] %s %s 方向偏见拒绝: %s | Q=%.2f (需要≥%.2f)",
                symbol, direction, bias_reason, quality_score, bias_threshold,
            )
            return None

        logger.info(
            "[ORB] %s %s 突破确认 | Q=%.2f regime=%s W=%.2f%% VolR=%.2f",
            symbol, direction, quality_score, regime, width_pct, vol_ratio
        )

        # Task #11 (2026-06-02): 信号生成时创建 trace_id, 供后续 ORBPosition / PositionStore /
        # TradeStore 一路携带, 实现全链路追溯
        try:
            from app.core.trace import generate_trace_id
            _orb_trace_id = generate_trace_id(strategy="orb_engine")
        except Exception:
            _orb_trace_id = ""

        return {
            "direction": direction,
            "price": price,
            "atr": atr,
            "regime": regime,
            "quality_score": quality_score,
            "trace_id": _orb_trace_id,
        }

    def _execute_entry(
        self, symbol: str, signal: Dict[str, Any], rng: Dict[str, Any]
    ):
        """执行入场 (增强版: 自适应止损 + 分层止盈)"""
        direction = signal["direction"]
        price = signal["price"]
        atr = signal["atr"]
        regime = signal.get("regime", "RANGE")
        quality_score = signal.get("quality_score", 0.5)
        # Task #11 (2026-06-02): 取出信号携带的 trace_id, 下传到 ORBPosition / PositionStore /
        # TradeStore, 未携带时在入口补一个作为兜底
        trace_id = signal.get("trace_id", "") or ""
        if not trace_id:
            try:
                from app.core.trace import generate_trace_id
                trace_id = generate_trace_id(strategy="orb_engine")
            except Exception:
                trace_id = ""

        # 自适应止损计算 (RFC-056-DESIGN)
        width_pct = rng.get("width_pct", 0.5)
        sl_distance = self._calculate_adaptive_stop(
            orb_width_pct=width_pct,
            atr=atr,
            price=price,
            regime=regime,
        )

        if sl_distance <= 0:
            logger.warning("[ORB] %s 止损距离无效: %.4f", symbol, sl_distance)
            return

        # 仓位大小: 风险金额 / 止损距离
        # P1 (2026-06-06): capital_pct 生效 — ORB 只用分配到的资金比例算风险
        # 根因: capital_pct 在 unified_config.yaml 定义为 0.20 但从未应用
        #        ORB 用全账户 $1M 算 risk_amount, 实际应为 $200K
        equity = self._get_equity()
        orb_capital = equity * self._cfg.capital_pct
        risk_amount = orb_capital * self._cfg.position_risk_pct
        logger.debug(
            "[ORB] 仓位计算: equity=$%.0f × capital_pct=%.2f = orb_capital=$%.0f | "
            "× risk_pct=%.3f = risk_amount=$%.0f",
            equity, self._cfg.capital_pct, orb_capital,
            self._cfg.position_risk_pct, risk_amount,
        )

        # 质量评分调整仓位 (高质量加仓, 低质量减仓)
        if quality_score >= 0.8:
            risk_amount *= 1.2  # 高质量加 20%
        elif quality_score < 0.6:
            risk_amount *= 0.7  # 低质量减 30%

        quantity = int(risk_amount / sl_distance)
        if quantity <= 0:
            quantity = 1

        orb_mid = (rng["high"] + rng["low"]) / 2.0

        logger.info(
            "[ORB] 🎯 %s %s 突破入场 | price=%.2f | orb=[%.2f, %.2f] | "
            "ATR=%.3f regime=%s Q=%.2f | qty=%d SL=%.2f (%.1f%%) | mode=%s",
            symbol, direction, price, rng["low"], rng["high"],
            atr, regime, quality_score, quantity,
            price - sl_distance if direction == "LONG" else price + sl_distance,
            sl_distance / price * 100,
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
                    sl_distance=sl_distance,
                    order_id=f"shadow_{uuid.uuid4().hex[:8]}",
                    trace_id=trace_id,
                )
                self._daily_trades += 1
                logger.info(
                    "[%s] ORB shadow 入场: %s %s @ %.2f qty=%d",
                    trace_id or "NO_TRACE", symbol, direction, price, quantity,
                )
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

        # 创建仓位跟踪 (包含自适应止损和分层止盈状态)
        pos = ORBPosition(
            symbol=symbol,
            direction=direction,
            entry_price=price,
            quantity=quantity,
            orb_high=rng["high"],
            orb_low=rng["low"],
            atr_at_entry=atr,
            sl_distance=sl_distance,
            order_id=order_id,
            trace_id=trace_id,
        )
        self._positions[symbol] = pos
        self._daily_trades += 1
        logger.info(
            "[%s] ORB 入场: %s %s @ %.2f qty=%d order_id=%s",
            pos.trace_id or "NO_TRACE", symbol, direction, price, quantity, order_id,
        )

        # 同步到 PositionStore (供退出监控使用)
        if self._store:
            from app.core.models import Position, Direction as ModelDirection
            store_pos = Position(
                symbol=symbol,
                direction=ModelDirection.SHORT if direction == "SHORT" else ModelDirection.LONG,
                size=quantity,
                avg_entry_price=price,
                initial_sl=price - sl_distance if direction == "LONG" else price + sl_distance,
            )
            store_pos.owner_strategy = "orb_engine"
            # Task #11 (2026-06-02): trace_id 同步到 PositionStore, 供退出 / 日报 / 归因使用
            try:
                store_pos.trace_id = trace_id
            except Exception:
                pass
            store_pos.metadata = {
                "orb_high": rng["high"],
                "orb_low": rng["low"],
                "orb_mid": orb_mid,
                "atr_at_entry": atr,
                "sl_distance": sl_distance,
                "regime": regime,
                "quality_score": quality_score,
                "trace_id": trace_id,
            }
            self._store.add(symbol, store_pos)

        # 通知
        self._notify(
            f"🟢 ORB 入场 | {symbol} {direction}\n"
            f"价格: ${price:.2f} | 数量: {quantity}\n"
            f"区间: [{rng['low']:.2f}, {rng['high']:.2f}]\n"
            f"ATR: {atr:.3f} | Regime: {regime} | Q: {quality_score:.2f}\n"
            f"SL: ${price - sl_distance:.2f} ({sl_distance/price*100:.1f}%)"
            if direction == "LONG" else
            f"🟢 ORB 入场 | {symbol} {direction}\n"
            f"价格: ${price:.2f} | 数量: {quantity}\n"
            f"区间: [{rng['low']:.2f}, {rng['high']:.2f}]\n"
            f"ATR: {atr:.3f} | Regime: {regime} | Q: {quality_score:.2f}\n"
            f"SL: ${price + sl_distance:.2f} ({sl_distance/price*100:.1f}%)"
        )

    # ── 退出监控 ──

    def _monitor_exits(self, now: datetime, current_time: dt_time):
        """监控所有活跃 ORB 仓位的退出条件 (增强版: 分层止盈)"""
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

                # Layer 1: 分层止盈评估 (RFC-056-DESIGN)
                layer_action = self._evaluate_layered_exits(pos, price)

                if layer_action == "LAYER1":
                    # 1R: 平 1/3, 止损移到 breakeven
                    close_qty = pos.initial_quantity // 3
                    if close_qty > 0:
                        pos.quantity -= close_qty
                        pos.layer1_done = True
                        pos.breakeven_activated = True
                        logger.info(
                            "[ORB] %s Layer 1 (1R): 平仓 %d 股, SL→breakeven (%.2f), 剩余 %d 股",
                            symbol, close_qty, pos.entry_price, pos.quantity
                        )
                        if self._cfg.is_active:
                            close_dir = "SHORT" if pos.direction == "LONG" else "LONG"
                            self._submit_order(symbol, close_dir, price, close_qty)
                        self._notify(f"📊 {symbol} Layer 1 (1R): 平仓 {close_qty} 股, SL→breakeven")
                    continue

                elif layer_action == "LAYER2":
                    # 2R: 平 1/3, 激活追踪止损
                    close_qty = pos.initial_quantity // 3
                    if close_qty > 0:
                        pos.quantity -= close_qty
                        pos.layer2_done = True
                        pos.trail_activated = True
                        # 初始化追踪止损
                        if pos.direction == "LONG":
                            pos.trail_stop = peak - atr * self._cfg.trail_multiplier
                        else:
                            pos.trail_stop = peak + atr * self._cfg.trail_multiplier
                        logger.info(
                            "[ORB] %s Layer 2 (2R): 平仓 %d 股, trail激活 (%.2f), 剩余 %d 股",
                            symbol, close_qty, pos.trail_stop, pos.quantity
                        )
                        if self._cfg.is_active:
                            close_dir = "SHORT" if pos.direction == "LONG" else "LONG"
                            self._submit_order(symbol, close_dir, price, close_qty)
                        self._notify(f"📊 {symbol} Layer 2 (2R): 平仓 {close_qty} 股, trail激活")
                    continue

                elif layer_action == "TRAIL_HIT":
                    # 追踪止损触发: 平剩余仓位
                    from app.orb.exits import ORBExitDecision
                    trail_decision = ORBExitDecision(
                        reason="ORB_TRAIL_STOP",
                        close_pct=100.0,
                        detail=f"追踪止损触发: trail={pos.trail_stop:.2f} price={price:.2f}",
                    )
                    self._execute_exit(symbol, pos, price, trail_decision)
                    continue

                # Layer 2: 传统退出栈 (止损, 盘末等)
                # 更新追踪止损线 (ratchet)
                if pos.trail_activated:
                    pos.trail_stop = self._exit_stack.compute_trail_stop(
                        pos.direction, atr, peak, pos.trail_stop, pos.entry_price,
                    )

                # 评估传统退出 (止损, 盘末, 固定止盈)
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
                    # 2026-06-03: 噪音过滤 — 持仓 < min_hold_seconds 跳过 FLOOR_STOP
                    # 只允许 trailing/take_profit/EOD 在初期触发, 过滤假突破噪音止损
                    if (decision.reason == "ORB_FLOOR_STOP"
                            and getattr(self._cfg, 'min_hold_seconds', 0) > 0):
                        hold_seconds = (datetime.now() - pos.entry_time).total_seconds()
                        if hold_seconds < self._cfg.min_hold_seconds:
                            logger.info(
                                "[ORB] %s FLOOR_STOP 跳过: 持仓%.0fs < min_hold=%ds (噪音过滤)",
                                symbol, hold_seconds, self._cfg.min_hold_seconds,
                            )
                            continue
                    self._execute_exit(symbol, pos, price, decision)

            except Exception as e:
                logger.warning("[ORB] %s 退出监控异常: %s", symbol, e, exc_info=True)

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

        # 2026-06-03: 亏损退出 → 当日冷却, 禁止该标的重新入场
        if pnl < 0 and getattr(self._cfg, 'same_symbol_loss_cooldown', False):
            self._closed_at_loss_today.add(symbol)
            logger.info(
                "[ORB] %s 亏损退出 ($%.2f), 加入当日冷却列表 (%d 个)",
                symbol, pnl, len(self._closed_at_loss_today),
            )

        # ── 写入 trade_records (2026-06-02 修复: ORB 交易记录缺失) ──
        try:
            from app.storage.trade_store import TradeStore
            from app.core.paths import get_trade_records_db
            from app.utils.timezone import now_et as _orb_now_et
            import json as _json
            _ts = TradeStore(str(get_trade_records_db()))
            _entry_time = pos.entry_time.isoformat() if hasattr(pos.entry_time, 'isoformat') else str(pos.entry_time)
            _exit_time = _orb_now_et().isoformat()  # 2026-06-05 修复: datetime.now()→now_et()
            _trade_date = pos.entry_time.strftime("%Y-%m-%d") if hasattr(pos.entry_time, 'strftime') else _orb_now_et().strftime("%Y-%m-%d")
            _holding_hours = (_orb_now_et() - pos.entry_time).total_seconds() / 3600 if pos.entry_time else 0
            # 2026-06-05 修复: ORB 不再硬编码 strength=65/confidence=0.65
            # breakout_quality: ORB range / ATR ratio, 反映突破质量
            _orb_range = (pos.orb_high - pos.orb_low) if (pos.orb_high and pos.orb_low) else 0
            _orb_atr = pos.atr_at_entry if pos.atr_at_entry else 1.0
            _breakout_quality = int(min(99, max(1, (_orb_range / _orb_atr) * 30))) if _orb_atr > 0 else None
            _ts.save_trade({
                "trace_id": pos.trace_id or f"orb-{symbol}-{int(time.time())}",
                "trade_date": _trade_date,
                "symbol": symbol,
                "direction": pos.direction,
                "entry_time": _entry_time,
                "exit_time": _exit_time,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "quantity": int(pos.quantity),
                "realized_pnl": pnl,
                "pnl_pct": pnl_pct / 100,
                "exit_reason": decision.reason,
                "holding_hours": _holding_hours,
                "signal_strength": _breakout_quality,  # 2026-06-05: 真实 breakout quality, 不再硬编码65
                "signal_confidence": 0.0,              # 2026-06-05: ORB 无投票, 标0而非假数据0.65
                "futu_order_id": pos.order_id or "",
                "source_type": "ORB_ENGINE",
                "entry_reason": "orb_breakout",
                "owner_strategy": "orb_engine",
                "market_regime": "RANGE",
                "mode": "normal",
                "signal_id": pos.trace_id or "",
                "decision_id": "",
                "metadata_json": _json.dumps({
                    "orb_high": pos.orb_high,
                    "orb_low": pos.orb_low,
                    "atr_at_entry": pos.atr_at_entry,
                    "sl_distance": pos.sl_distance,
                }),
                # 2026-06-05 数据质量修复: ORB 也写 signal_details
                "signal_details": _json.dumps({
                    "signal_strength": _breakout_quality,
                    "signal_confidence": 0.0,
                    "orb_range": _orb_range,
                    "atr_at_entry": pos.atr_at_entry,
                    "breakout_direction": pos.direction,
                    "entry_reason": "orb_breakout",
                    "exit_reason": decision.reason,
                }, ensure_ascii=False),
            })
            logger.info("[ORB] %s 交易记录已写入 trade_records | pnl=$%.2f | strength=%s", symbol, pnl, _breakout_quality)

            # 2026-06-05: ORB 也写 signal_lifecycle
            import sqlite3 as _orb_sl
            from app.core.paths import get_trade_records_db as _orb_sl_db
            _orb_sl_conn = _orb_sl.connect(str(_orb_sl_db()))
            _orb_sl_conn.execute("""
                INSERT INTO signal_lifecycle (
                    trade_id, symbol, signal_strength, signal_confidence,
                    signal_direction, trade_direction, entry_price, exit_price,
                    realized_pnl, pnl_pct, exit_reason, holding_hours,
                    owner_strategy, source_type, trade_date, trade_mode,
                    created_at
                ) VALUES (
                    (SELECT MAX(id) FROM trade_records),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                symbol, _breakout_quality, 0.0,
                pos.direction, pos.direction,
                pos.entry_price, price,
                pnl, pnl_pct / 100, decision.reason,
                _holding_hours, "orb_engine", "ORB_ENGINE",
                _trade_date, "normal",
                _orb_now_et().isoformat(),
            ))
            _orb_sl_conn.commit()
            _orb_sl_conn.close()

        except Exception as _tre:
            logger.warning("[ORB] %s trade_records 写入失败 (不影响退出): %s", symbol, _tre)

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
        """获取当前价格 (优先读 Push 缓存, 避免阻塞 Futu API)"""
        if not self._data:
            return None

        # 路径 0: 直接读 Push quote 缓存 (零延迟, 无 API 调用)
        try:
            futu = getattr(self._data, 'futu', None)
            if futu and hasattr(futu, '_push_quote_cache'):
                with futu._push_cache_lock:
                    cached = futu._push_quote_cache.get(symbol)
                    if cached:
                        _p = float(cached.get("price", 0) or cached.get("last", 0) or 0)
                        if _p > 0:
                            return _p
        except Exception:
            pass

        # 路径 1: data_router.get_quote (可能触发 API 调用)
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
        """获取当前成交量 / 均量 (从 kline 自行计算)

        计算方法: 最近 5 根 K 线均量 / 全部可用 K 线均量
        data_router 无 get_volume_ratio 方法，故从 K 线直接推算。
        """
        if not self._data:
            return 1.0  # 无数据时不阻拦

        # 路径 1: data_router 原生支持 (未来扩展)
        try:
            if hasattr(self._data, "get_volume_ratio"):
                ratio = self._data.get_volume_ratio(symbol)
                if ratio and ratio > 0:
                    return float(ratio)
        except Exception:
            pass

        # 路径 2: 从 kline 数据计算 (近期均量 / 整体均量)
        try:
            klines = self._get_1min_klines(symbol, count=60)
            if klines and len(klines) >= 10:
                volumes = []
                for k in klines:
                    v = float(k.get("volume", 0))
                    if v > 0:
                        volumes.append(v)

                if len(volumes) >= 10:
                    recent_n = min(5, len(volumes) // 3)
                    recent_avg = sum(volumes[-recent_n:]) / recent_n
                    session_avg = sum(volumes) / len(volumes)
                    if session_avg > 0:
                        ratio = recent_avg / session_avg
                        return max(0.1, min(ratio, 10.0))  # clamp [0.1, 10.0]
        except Exception:
            pass

        return 1.0  # fallback: 不过滤

    def _get_vwap(self, symbol: str) -> float:
        """获取 VWAP (Volume Weighted Average Price)"""
        if not self._data:
            return 0.0
        try:
            # 尝试从 data_router 获取
            if hasattr(self._data, "get_vwap"):
                vwap = self._data.get_vwap(symbol)
                if vwap and vwap > 0:
                    return float(vwap)

            # Fallback: 从 K 线计算
            klines = self._get_1min_klines(symbol, count=60)
            if klines and len(klines) > 0:
                cum_vol = 0.0
                cum_pv = 0.0
                for k in klines:
                    h = float(k.get("high", 0))
                    l = float(k.get("low", 0))
                    c = float(k.get("close", 0))
                    v = float(k.get("volume", 0))
                    if h > 0 and l > 0 and c > 0 and v > 0:
                        typical_price = (h + l + c) / 3.0
                        cum_pv += typical_price * v
                        cum_vol += v

                if cum_vol > 0:
                    return cum_pv / cum_vol
        except Exception:
            pass
        return 0.0

    def _get_1min_klines(self, symbol: str, count: int = 20) -> Optional[List[Dict]]:
        """获取 1-min klines (带超时保护)"""
        if not self._data:
            return None
        try:
            # 路径 1: data_router.get_klines (如果存在)
            if hasattr(self._data, "get_klines"):
                result = self._data.get_klines(symbol, ktype="1min", count=count)
                if result:
                    return result

            # 路径 2: data_router.futu.get_kline (Futu LV3 provider) — 带超时
            futu = getattr(self._data, "futu", None)
            if futu and hasattr(futu, "get_kline"):
                # 先检查 push cache 是否有数据 (非阻塞)
                push_cache = getattr(futu, '_push_kline_cache', {})
                futu_symbol = symbol.replace('US.', '')  # US.IWM -> IWM
                if futu_symbol in push_cache and push_cache[futu_symbol]:
                    cached_data = push_cache[futu_symbol]
                    logger.debug("[ORB] %s 使用 push cache: %d 根 K 线", symbol, len(cached_data))
                    return cached_data[-count:] if len(cached_data) > count else cached_data

                # 超时保护: 3 秒内获取 K 线，否则返回 None
                import concurrent.futures
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(futu.get_kline, symbol, "K_1M", count)
                        result = future.result(timeout=3.0)
                        if result:
                            return result
                        else:
                            logger.info("[ORB] %s futu.get_kline 返回空", symbol)
                except concurrent.futures.TimeoutError:
                    logger.warning("[ORB] %s futu.get_kline 超时 (3s)，跳过", symbol)
                    return None

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

    # ── 区间质量评估 (RFC-056-DESIGN) ──

    def _calculate_quality_score(
        self,
        orb_width_pct: float,
        volume_ratio: float,
        price: float,
        vwap: float,
        direction: str,
    ) -> float:
        """
        计算区间质量评分 (0.0 - 1.0)

        评分模型:
          quality = 0.4 * width_score + 0.3 * volume_score + 0.3 * vwap_score

        width_score:
          - 0.3% <= width <= 2.0%: 1.0 (理想)
          - 0.2% <= width < 0.3%: 0.5 (偏窄)
          - 2.0% < width <= 3.0%: 0.5 (偏宽)
          - 其他: 0.0 (不可交易)

        volume_score:
          - ratio >= 1.5: 1.0 (放量)
          - 1.2 <= ratio < 1.5: 0.7 (适度)
          - 0.8 <= ratio < 1.2: 0.5 (平量)
          - ratio < 0.8: 0.3 (缩量)

        vwap_score:
          - LONG 且 price > vwap: 1.0
          - SHORT 且 price < vwap: 1.0
          - 其他: 0.5
        """
        # Width score
        if 0.3 <= orb_width_pct <= 2.0:
            width_score = 1.0
        elif 0.2 <= orb_width_pct < 0.3 or 2.0 < orb_width_pct <= 3.0:
            width_score = 0.5
        else:
            width_score = 0.0

        # Volume score
        if volume_ratio >= 1.5:
            volume_score = 1.0
        elif volume_ratio >= 1.2:
            volume_score = 0.7
        elif volume_ratio >= 0.8:
            volume_score = 0.5
        else:
            volume_score = 0.3

        # VWAP score
        if vwap > 0 and price > 0:
            if direction == "LONG" and price > vwap:
                vwap_score = 1.0
            elif direction == "SHORT" and price < vwap:
                vwap_score = 1.0
            else:
                vwap_score = 0.5
        else:
            vwap_score = 0.5  # 无 VWAP 数据时中性

        quality = 0.4 * width_score + 0.3 * volume_score + 0.3 * vwap_score
        return quality

    def _calculate_adaptive_stop(
        self,
        orb_width_pct: float,
        atr: float,
        price: float,
        regime: str = "RANGE",
    ) -> float:
        """
        计算自适应止损距离 (价格单位)

        公式:
          sl_distance = max(
            orb_width * sl_width_mult,
            atr * sl_atr_mult,
            min_sl_distance
          )

        Regime 自适应:
          TREND_UP/DOWN: 宽松 (sl_width_mult=0.5, sl_atr_mult=1.2)
          RANGE:         适中 (sl_width_mult=0.8, sl_atr_mult=1.5)
          HIGH_VOL:      收紧 (sl_width_mult=0.3, sl_atr_mult=0.8)
          LOW_VOL:       放宽 (sl_width_mult=1.0, sl_atr_mult=2.0)

        兜底:
          min_sl_distance = price * 0.5%
          max_sl_distance = price * 3.0%
        """
        # Regime 参数映射
        regime_params = {
            "TREND_UP":   (0.5, 1.2),
            "TREND_DOWN": (0.5, 1.2),
            "RANGE":      (0.8, 1.5),
            "HIGH_VOL":   (0.3, 0.8),
            "LOW_VOL":    (1.0, 2.0),
        }
        sl_width_mult, sl_atr_mult = regime_params.get(regime, (0.8, 1.5))

        # 计算止损距离
        orb_width = price * orb_width_pct / 100.0
        sl_from_width = orb_width * sl_width_mult
        sl_from_atr = atr * sl_atr_mult

        sl_distance = max(sl_from_width, sl_from_atr)

        # 兜底限制
        min_sl = price * 0.005  # 0.5%
        max_sl = price * 0.03   # 3.0%

        sl_distance = max(min_sl, min(max_sl, sl_distance))

        return sl_distance

    def _evaluate_layered_exits(
        self,
        pos: ORBPosition,
        current_price: float,
    ) -> Optional[str]:
        """
        评估分层止盈

        三层机制:
          Layer 1 (1R): 平 1/3, 止损移到 breakeven
          Layer 2 (2R): 平 1/3, 激活追踪止损
          Layer 3 (>2R): 剩余仓位追踪止损

        Returns:
          - "LAYER1": 需要执行 Layer 1
          - "LAYER2": 需要执行 Layer 2
          - "TRAIL_HIT": 追踪止损触发
          - None: 无需操作
        """
        if pos.r_multiple <= 0:
            return None

        # 计算当前 R-multiple
        if pos.direction == "LONG":
            current_r = (current_price - pos.entry_price) / pos.r_multiple
        else:
            current_r = (pos.entry_price - current_price) / pos.r_multiple

        # Layer 1: 1R 平 1/3
        if not pos.layer1_done and current_r >= 1.0:
            return "LAYER1"

        # Layer 2: 2R 平 1/3
        if not pos.layer2_done and current_r >= 2.0:
            return "LAYER2"

        # Layer 3: 追踪止损 (2R 后激活)
        if pos.layer2_done and pos.trail_activated and pos.trail_stop > 0:
            if pos.direction == "LONG" and current_price <= pos.trail_stop:
                return "TRAIL_HIT"
            elif pos.direction == "SHORT" and current_price >= pos.trail_stop:
                return "TRAIL_HIT"

        return None

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

            # SSOT (2026-06-06): 统一安全门禁 — 替代散落 inline 检查
            # 三层检查: 单品种25% + 方向65% + 总敞口80%
            try:
                from app.risk.pre_trade_gate import pre_trade_safety_check
                _ps = getattr(self, '_pos_store', None) or getattr(self, 'position_store', None)
                _ok, _mq, _rsn = pre_trade_safety_check(
                    symbol=symbol, direction=direction, quantity=int(quantity),
                    price=price, position_store=_ps,
                )
                if not _ok and _mq <= 0:
                    logger.warning(f"🚫 [ORB·安全门禁] {symbol} {direction} 拒绝: {_rsn}")
                    return {"success": False, "error": "pre_trade_gate_rejected", "reason": _rsn}
            except Exception as _gate_err:
                # fail-close: 门禁异常时拒绝下单
                logger.error(f"🚫 [ORB·安全门禁] {symbol} 异常 (拒绝): {_gate_err}")
                return {"success": False, "error": "pre_trade_gate_error", "reason": str(_gate_err)}

            # ── P1 (2026-06-04): 最小门禁注入 — 防 ORB 绕过风控 ──
            # 根因: ORB 直连 execution_engine，不受 RiskStateMachine/PDT/cooldown 保护
            # 修复: 在此注入三项最小门禁，拦截条件与主路径对齐

            # 门禁 1: RiskStateMachine (RISK_OFF / HALT → 拒绝)
            try:
                from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskAction
                _rsm = RiskStateMachine()
                if not _rsm.is_trade_allowed(RiskAction.OPEN):
                    _state = _rsm.get_state().value
                    logger.warning(f"🚫 [ORB·风控·P1] {symbol} 拒绝: 系统 {_state} 状态禁止开仓")
                    return {"success": False, "error": f"risk_state_{_state}"}
            except Exception:
                pass

            # 门禁 2: 冷却期 (同标的近期止损后禁入)
            try:
                _scheduler = getattr(self, '_scheduler', None) or getattr(self, 'scheduler', None)
                if _scheduler and hasattr(_scheduler, '_is_in_cooldown') and _scheduler._is_in_cooldown(symbol):
                    logger.warning(f"🚫 [ORB·冷却·P1] {symbol} 拒绝: 信号冷却中")
                    return {"success": False, "error": "cooldown_active"}
            except Exception:
                pass

            # 门禁 3: PDT 日交易限制
            try:
                from app.risk.pdt_guard import get_pdt_guard
                _pdt = get_pdt_guard()
                if _pdt:
                    _allowed, _reason, _risk = _pdt.check_trade_permission()
                    if not _allowed:
                        logger.warning(f"🚫 [ORB·PDT·P1] {symbol} 拒绝: {_reason}")
                        return {"success": False, "error": f"pdt_{_reason}"}
            except Exception:
                pass

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
