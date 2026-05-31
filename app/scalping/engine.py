"""
Scalping 引擎 — 主循环

架构:
  独立 daemon 线程，5 秒 polling 循环
  不共享主事件循环，不依赖 LLM
  
  流程:
    1. 时段检查 → 是否允许交易
    2. 趋势过滤 → SPY/VIX 是否太强
    3. 熔断检查 → 连续亏损是否触发
    4. 资金检查 → 预算是否足够
    5. 信号检测 → RSI/VWAP/BB 投票
    6. 下单 → 通过 ExecutionEngine
    7. 监控 → 止盈止损/时间止损
    8. 平仓 → 更新统计和预算
"""

import threading
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
import sys
from app.utils.timezone import now_et

logger = logging.getLogger(__name__)

# P0 Karpathy fix (2026-05-13): 强制标的策略验证器
class _ForcedTickerValidator:
    """跟踪强制交易标的的每笔交易，验证 scalping 策略精准度。
    
    核心思想: 无论信号质量如何都交易指定标的，积累足够样本后
    精确评估策略真实胜率 (而非只看被信号过滤后的表现)。
    """
    def __init__(self, forced_tickers: list, notifier=None):
        self._tickers = {t["code"]: t for t in forced_tickers}
        self._notifier = notifier
        self._trades: Dict[str, List[dict]] = {code: [] for code in self._tickers}
        self._daily_pnl: Dict[str, float] = {code: 0.0 for code in self._tickers}
        self._date = now_et().strftime("%Y-%m-%d")
    
    def record_trade(self, symbol: str, pnl_pct: float):
        if symbol not in self._tickers:
            return
        # 跨日重置
        today = now_et().strftime("%Y-%m-%d")
        if today != self._date:
            self._emit_daily_report()
            self._trades = {code: [] for code in self._tickers}
            self._daily_pnl = {code: 0.0 for code in self._tickers}
            self._date = today
        
        self._trades[symbol].append({"pnl_pct": pnl_pct, "ts": now_et().isoformat()})
        self._daily_pnl[symbol] += pnl_pct
        
        cfg = self._tickers[symbol]
        max_loss = cfg.get("max_daily_loss_usd", 100)
        # 估算每日亏损 (假设每笔仓位 ~$10K)
        est_daily_loss = abs(self._daily_pnl[symbol]) * 10000
        if est_daily_loss > max_loss:
            logger.warning(
                f"[Scalping·强制标的熔断] {symbol} 日亏损 ${est_daily_loss:.0f} > ${max_loss}, "
                f"今日不再交易"
            )
    
    def _emit_daily_report(self):
        if not self._trades:
            return
        lines = ["📊 Scalping 强制标的策略验证报告"]
        for symbol, trades in self._trades.items():
            if not trades:
                continue
            n = len(trades)
            wins = sum(1 for t in trades if t["pnl_pct"] > 0)
            avg_pnl = sum(t["pnl_pct"] for t in trades) / n if n else 0
            total_pnl = sum(t["pnl_pct"] for t in trades)
            lines.append(
                f"  {symbol}: {n}笔 | 胜率 {wins/n:.0%} | "
                f"平均PnL {avg_pnl:+.2%} | 累计 {total_pnl:+.2%}"
            )
        if len(lines) > 1:
            report = "\n".join(lines)
            logger.info(report)
            if self._notifier:
                try:
                    self._notifier.send_text(report)
                except Exception:
                    pass

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))



# 期权合约乘数检测 (与 order_executor 逻辑一致)
def _is_option_contract(symbol: str) -> bool:
    """判断是否为美股期权合约 (OCC 格式: US.XXXXXXP/CCCCCCCC)"""
    parts = symbol.split(".")
    if len(parts) < 2:
        return False
    ticker_part = parts[-1]
    if len(ticker_part) < 15:
        return False
    return "P" in ticker_part[6:10] or "C" in ticker_part[6:10]


def _get_contract_multiplier(symbol: str) -> int:
    """获取合约乘数: 美股期权=100, 股票=1"""
    if _is_option_contract(symbol):
        return 100
    return 1


def _migrate_old_schedule(old_schedule: dict) -> dict:
    """向后兼容: 迁移旧 3 段 schedule → 新 5 段 phases 格式"""
    from app.scalping.time_gate import DEFAULT_PHASES_CONFIG
    new_cfg = DEFAULT_PHASES_CONFIG.copy()

    for phase_name in ("morning", "midday"):
        if phase_name in old_schedule:
            old = old_schedule[phase_name]
            if phase_name in new_cfg:
                new_cfg[phase_name]["start_minutes"] = old.get("start_minutes", new_cfg[phase_name]["start_minutes"])
                new_cfg[phase_name]["end_minutes"] = old.get("end_minutes", new_cfg[phase_name]["end_minutes"])
                new_cfg[phase_name]["directions"] = old.get("directions", new_cfg[phase_name]["directions"])
                new_cfg[phase_name]["signals"] = old.get("signals", new_cfg[phase_name]["signals"])
                # 从旧 TP/SL 反推 scaler
                base = new_cfg["base_params"]
                new_cfg[phase_name]["tp_scale"] = round(old.get("take_profit", 0.015) / base["tp_pct"], 2)
                new_cfg[phase_name]["sl_scale"] = round(old.get("stop_loss", 0.008) / base["sl_pct"], 2)
                new_cfg[phase_name]["pos_scale"] = round(old.get("position_pct", 0.10) / base["position_pct"], 2)
                new_cfg[phase_name]["hold_scale"] = round(old.get("max_hold_minutes", 5) / base["max_hold_minutes"], 2)

    # late_afternoon → afternoon
    if "late_afternoon" in old_schedule:
        old = old_schedule["late_afternoon"]
        if "afternoon" in new_cfg:
            new_cfg["afternoon"]["directions"] = old.get("directions", ["LONG"])
            new_cfg["afternoon"]["signals"] = old.get("signals", ["rsi"])
            base = new_cfg["base_params"]
            new_cfg["afternoon"]["tp_scale"] = round(old.get("take_profit", 0.012) / base["tp_pct"], 2)
            new_cfg["afternoon"]["sl_scale"] = round(old.get("stop_loss", 0.007) / base["sl_pct"], 2)
            new_cfg["afternoon"]["pos_scale"] = round(old.get("position_pct", 0.08) / base["position_pct"], 2)
            new_cfg["afternoon"]["hold_scale"] = round(old.get("max_hold_minutes", 4) / base["max_hold_minutes"], 2)

    return new_cfg


class ScalpingEngine:
    """
    Scalping 均值回归引擎
    
    核心特征:
      - 独立线程 (daemon=True)
      - 5 秒 polling 循环
      - 纯技术指标 (RSI/VWAP/BB)
      - 固定止盈止损 + 时间止损
      - observe / active 两种模式
    
    集成点:
      - DailyActivePool: 标的隔离
      - RiskStateMachine: 全局风控
      - ExecutionEngine: 下单
      - ScalpingBudget: 资金隔离
      - ScalpingCircuitBreaker: 熔断
      - TrendFilter: 趋势过滤
      - TimeGate: 时段路由
      - SignalDetector: 信号检测
      - ScalpingStatsTracker: 统计
    """

    def __init__(
        self,
        execution_engine=None,
        data_router=None,
        risk_engine=None,
        notifier=None,
        # 配置参数
        mode: str = "observe",           # observe / active
        polling_interval: int = 5,        # polling 间隔 (秒)
        # 子模块
        time_gate=None,
        signal_detector=None,
        fixed_exit=None,
        time_exit=None,
        budget=None,
        circuit_breaker=None,
        trend_filter=None,
        stats_tracker=None,
        daily_active_pool=None,
        open_position_lock=None,          # P0: 跨引擎开仓互斥锁
        stock_selector=None,              # P0: 每日自动选股器
        debouncer=None,                   # 2026-05-09: 告警防抖器 (AlertDebouncer)
    ):
        self.mode = mode
        self.polling_interval = polling_interval

        # 外部依赖
        self.execution_engine = execution_engine
        self.data_router = data_router
        self.risk_engine = risk_engine
        self.notifier = notifier
        self.daily_active_pool = daily_active_pool
        self._open_position_lock = open_position_lock  # P0: 跨引擎互斥锁
        self.stock_selector = stock_selector            # P0: 选股器
        self.debouncer = debouncer                      # 2026-05-09: 告警防抖器
        # P1-A (2026-05-15): 注入 scheduler 引用 — 接 coordinator (默认 None, scheduler 启动后挂)
        self.scheduler = None

        # 子模块 (延迟初始化或使用传入的)
        self.time_gate = time_gate
        self.signal_detector = signal_detector
        self.fixed_exit = fixed_exit
        self.time_exit = time_exit
        self.budget = budget
        self.circuit_breaker = circuit_breaker
        self.trend_filter = trend_filter
        self.stats_tracker = stats_tracker

        # 统一交易记录 (Karpathy: 所有来源统一落库)
        self.trade_store = None

        # 内部状态
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._waiter_active = False
        self._capital_sync_active = False
        self._capital_sync_thread: Optional[threading.Thread] = None
        self._active_trades: Dict[str, Dict] = {}  # symbol → trade info
        self._lock = threading.Lock()
        self._moc_fired_this_session = False  # P0 修复: MOC session 守卫，防止重复触发
        self._last_moc_date = None  # P0 修复 (2026-05-09): 追踪 MOC 日期，跨交易日自动重置

        # P1 修复 (2026-05-06): 信号冷却 — 防止同一标的每tick重复开仓
        self._signal_cooldown: Dict[str, float] = {}  # symbol → last_signal_timestamp
        self._signal_cooldown_sec = 300  # P0 修复 (2026-05-08): 从 180s 延长至 300s，减少同标的反复触发
        # RFC-053 (2026-05-30): 信号检测去重 — 防止 SignalDetector 每6s spam 同一信号
        # 即使信号被下游拒绝(pre_trade_check/budget), 检测层也需冷却
        self._detection_cooldown: Dict[str, float] = {}  # "symbol:direction" → timestamp
        self._detection_cooldown_sec = 60  # 同symbol+direction 60s内不重复检测

        # P0 修复 (2026-05-13): 下单失败冷却 — 防止 broker 拒绝后无限重试 (如方向冲突)
        self._order_fail_cooldown: Dict[str, float] = {}  # symbol → fail_timestamp
        self._order_fail_cooldown_sec = 600  # 10分钟冷却，避免反复触发同一错误

        # P2 修复 (2026-05-19): sl_hit 后二级冷却 — 防"刚损刚再入"
        # 5/18 TME 案例: 22:33:39 sl_hit, 22:33:41 (2s 后) 再开仓.
        # signal_cooldown=300s 是 entry-to-entry, 在 hold 5min 后 sl_hit 时已"过期".
        # 这里追加 last_loss → 90s 强制冷却, 给市场反应时间.
        self._loss_cooldown: Dict[str, float] = {}  # symbol → last_sl_hit_timestamp
        self._loss_cooldown_sec = 90

        # P0 修复 (2026-05-08): 标的级冷却 — 追踪每标的 sl_hit 次数
        self._symbol_sl_hit_count: Dict[str, Dict] = {}  # symbol → {"count": int, "date": str}

        # P0 修复 (2026-05-08): STRUCTURE_EXIT 连续亏损冷却
        self._symbol_structure_exit_losses: Dict[str, Dict] = {}  # symbol → {"consecutive": int, "last_exit_ts": float, "direction": str}
        self._structure_exit_cooldown_cfg: Dict = {}  # {"consecutive_losses": int, "cooldown_hours": int, "direction": str}

        # 动量生命周期融合 (symbol → {closes, momentum, prev_momentum})
        self._lifecycle_state: Dict[str, Dict] = {}
        self._lifecycle_ema_n: int = 5
        self._lifecycle_max_closes: int = 200

        logger.info(f"[ScalpingEngine] 初始化 | 模式: {mode} | polling: {polling_interval}s")

    def _init_submodules(self):
        """初始化子模块 (如果未传入)"""
        from app.scalping.time_gate import TimeGate
        from app.scalping.signals import SignalDetector
        from app.scalping.exits import FixedExit, TimeExit, ATRExit
        from app.scalping.budget import ScalpingBudget
        from app.scalping.circuit_breaker import ScalpingCircuitBreaker
        from app.scalping.trend_filter import TrendFilter
        from app.scalping.stats import ScalpingStatsTracker

        # 从配置加载参数
        scalping_cfg = self._load_scalping_config()
        self.scalping_cfg = scalping_cfg  # P0 fix (2026-05-13): save for _get_scalping_codes

        # ── 共享内核 (VIX/风控 SSOT, 两个引擎共用) ──
        from app.core.shared_kernel import get_shared_kernel
        shared_kernel = get_shared_kernel()

        # ── TimeGate v2: 5段细分 + base_params + scalers ──
        if self.time_gate is None:
            phases_cfg = scalping_cfg.get("phases")
            if phases_cfg:
                self.time_gate = TimeGate(phases_config=phases_cfg, shared_kernel=shared_kernel)
            else:
                # 向后兼容: 旧 schedule 格式
                old_schedule = scalping_cfg.get("schedule")
                if old_schedule:
                    self.time_gate = TimeGate(phases_config=_migrate_old_schedule(old_schedule),
                                              shared_kernel=shared_kernel)
                else:
                    self.time_gate = TimeGate(shared_kernel=shared_kernel)

        if self.signal_detector is None:
            self.signal_detector = SignalDetector(
                rsi_period=scalping_cfg.get("rsi_period", 14),
                rsi_oversold=scalping_cfg.get("rsi_oversold", 25),
                rsi_overbought=scalping_cfg.get("rsi_overbought", 75),
                # P1 修复 (2026-05-19): vwap 偏离从 1.2% → 2.0% (更严).
                # 5/12-18 数据: <6min 461 笔仅 2.8% 胜率, 大部分是 1.2% 偏离的日内噪音.
                # 收紧到 2.0% 减少噪音入场 (实际生产 yaml 可覆盖).
                vwap_deviation_pct=scalping_cfg.get("vwap_deviation_pct", 0.020),
                min_signals=scalping_cfg.get("min_signals", 2),
                # P0 新增 (2026-05-20): LONG 接飞刀防护
                # 归因: vwap+bollinger 无 RSI 31笔亏$850, VWAP>2% below 39笔仅3胜
                long_guard_cfg=scalping_cfg.get("long_guard", None),
            )

        if self.fixed_exit is None:
            params = self.time_gate.get_params()
            tp = params["take_profit"] if params else 0.015
            sl = params["stop_loss"] if params else 0.008
            self.fixed_exit = FixedExit(take_profit_pct=tp, stop_loss_pct=sl)

        # ═══════════════════════════════════════════════════════════
        # P2 修复 (2026-05-08): ATR 波动率自适应退出
        # 配置驱动开关，默认启用。ATR 不可用时自动回退到 FixedExit
        # Task #9 (2026-05-30): TP/SL 倍数优先从 exit_policy.yaml::scalping_overrides.atr_exit 读取,
        #   unified_config.scalping.atr_tp_multiplier/atr_sl_multiplier 仅作为兑底覆写
        # ═══════════════════════════════════════════════════════════
        self.use_atr_exit = scalping_cfg.get("use_atr_exit", False)
        if self.use_atr_exit:
            # 优先使用 unified_config 显式参数; 未配置时传 None 让 ATRExit 从 exit_policy.yaml 读取
            tp_mult = scalping_cfg.get("atr_tp_multiplier")
            sl_mult = scalping_cfg.get("atr_sl_multiplier")
            self.atr_exit = ATRExit(tp_multiplier=tp_mult, sl_multiplier=sl_mult)
            logger.info(
                f"[ScalpingEngine] ATR 自适应退出已启用 | "
                f"TP 倍数={self.atr_exit.tp_mult}x | SL 倍数={self.atr_exit.sl_mult}x | "
                f"范围 TP=[{self.atr_exit.min_tp_pct:.2%},{self.atr_exit.max_tp_pct:.2%}] "
                f"SL=[{self.atr_exit.min_sl_pct:.2%},{self.atr_exit.max_sl_pct:.2%}]"
            )
        else:
            self.atr_exit = None
        
        # P0 新增 (2026-05-09): 轻量追踪止损配置
        trail_cfg = scalping_cfg.get("trail_stop", {})
        self.trail_enabled = trail_cfg.get("enabled", True)
        self.trail_mult = trail_cfg.get("trail_mult", 1.5)
        self.min_profit_to_activate = trail_cfg.get("min_profit_to_activate", 0.01)
        if self.trail_enabled:
            logger.info(
                f"[ScalpingEngine] 轻量追踪止损已启用 | "
                f"trail_mult={self.trail_mult}x | min_profit={self.min_profit_to_activate:.1%}"
            )
        
        # ═══════════════════════════════════════════════════════════
        # P0 新增 (2026-05-09): 订单流过滤配置
        # ═══════════════════════════════════════════════════════════
        self._order_book_cfg = scalping_cfg.get("order_book_filter", {})
        if self._order_book_cfg.get("enabled", True):
            logger.info(
                f"[ScalpingEngine] 订单流过滤已启用 | "
                f"min_bid_ask={self._order_book_cfg.get('min_bid_ask_ratio', 1.5)}"
            )
        
        # ═══════════════════════════════════════════════════════════
        # P1 新增 (2026-05-09): 多时间框架确认配置
        # ═══════════════════════════════════════════════════════════
        self._multi_tf_cfg = scalping_cfg.get("multi_timeframe", {})
        if self._multi_tf_cfg.get("enabled", True):
            logger.info(
                f"[ScalpingEngine] 多时间框架确认已启用 | "
                f"higher_tf={self._multi_tf_cfg.get('higher_tf', 'K_5M')}"
            )
        
        # ═══════════════════════════════════════════════════════════
        # P1 新增 (2026-05-09): 逐笔成交异动检测配置
        # ═══════════════════════════════════════════════════════════
        self._ticker_cfg = scalping_cfg.get("ticker_flow", {})
        if self._ticker_cfg.get("enabled", True):
            logger.info(
                f"[ScalpingEngine] 逐笔成交检测已启用 | "
                f"window={self._ticker_cfg.get('window_size', 50)}, "
                f"imbalance={self._ticker_cfg.get('imbalance_ratio', 2.0)}x"
            )
        # ═══════════════════════════════════════════════════════════

        if self.time_exit is None:
            params = self.time_gate.get_params()
            max_hold = params["max_hold_minutes"] if params else 5
            self.time_exit = TimeExit(max_hold_minutes=max_hold)

        if self.budget is None:
            # SSOT: Capital Adaptivity - Read from capital.fixed_amount
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                base_equity = float(cfg.get_dotted("capital.fixed_amount", 500_000))
            except Exception:
                base_equity = 500_000
            
            total_equity = self._get_account_equity(base_equity)
            # 策略级风险预算 (SSOT: 基于百分比动态计算)
            budgets_cfg = self._load_strategy_budgets()
            scalping_budget_cfg = budgets_cfg.get("scalping", {})
            
            # P0 修复 (2026-05-03): 资金自适应 — 优先使用 % 配置，自动适配资金规模
            # 1. 尝试读取百分比 (推荐)
            loss_pct = scalping_budget_cfg.get("max_daily_loss_pct")
            if loss_pct:
                self._strategy_max_daily_loss = total_equity * loss_pct
                logger.info(
                    f"[ScalpingEngine] 策略日亏损限额动态计算: "
                    f"${self._strategy_max_daily_loss:,.0f} ({loss_pct:.0%} of equity)"
                )
            else:
                # 2. 降级：读取绝对金额 (向下兼容)
                self._strategy_max_daily_loss = scalping_budget_cfg.get("max_daily_loss", total_equity * 0.02)
                logger.warning(
                    f"[ScalpingEngine] 策略日亏损限额使用绝对值 (建议改用 %): "
                    f"${self._strategy_max_daily_loss:,.0f}"
                )
            
            self._strategy_realized_pnl_today = 0.0

            self.budget = ScalpingBudget(
                total_equity=total_equity,
                scalping_budget_pct=scalping_cfg.get("budget_pct", 0.30),
                max_position_pct=scalping_cfg.get("max_position_pct", 0.10),
                max_daily_loss_pct=scalping_cfg.get("max_daily_loss_pct", 0.02),
                max_concurrent=scalping_cfg.get("max_concurrent", 5),
            )

        if self.circuit_breaker is None:
            cb_cfg = scalping_cfg.get("circuit_breaker", {})
            self.circuit_breaker = ScalpingCircuitBreaker(
                max_consecutive_losses=cb_cfg.get("max_consecutive_losses", 3),
                cooldown_minutes=cb_cfg.get("cooldown_minutes", 60),
            )

        # P1 修复 (2026-05-07): 从配置读取信号冷却时间
        self._signal_cooldown_sec = scalping_cfg.get("signal_cooldown_sec", 300)
        logger.info(f"[ScalpingEngine] 信号冷却时间: {self._signal_cooldown_sec}s")

        # C3+S2 修复 (2026-05-13): 每标的每日最大交易次数
        self._daily_max_trades_per_symbol = scalping_cfg.get("daily_max_trades_per_symbol", 5)
        self._per_symbol_daily_count: Dict[str, Dict] = {}  # symbol → {"count": int, "date": str}

        # P0 修复 (2026-05-08): 加载 symbol_cooldown 配置 (STRUCTURE_EXIT 连续亏损冷却)
        self._load_symbol_cooldown_config()

        if self.trend_filter is None:
            tf_cfg = scalping_cfg.get("trend_filter", {})
            self.trend_filter = TrendFilter(
                spy_threshold=tf_cfg.get("spy_threshold", 0.03),
                vix_threshold=tf_cfg.get("vix_threshold", 35),
            )

        # ── 预测型动量检测器 (Momentum) ──
        from app.scalping.signals import MomentumDetector
        mom_cfg = scalping_cfg.get("momentum", {})
        self.momentum_detector = MomentumDetector(
            breakout_threshold=mom_cfg.get("breakout_threshold", 0.005),
            breakdown_threshold=mom_cfg.get("breakdown_threshold", 0.005),
            volume_surge=mom_cfg.get("volume_surge", 1.2),
            rsi_overbought=mom_cfg.get("rsi_overbought", 70),
        )

        if self.stats_tracker is None:
            self.stats_tracker = ScalpingStatsTracker()

        # ── P1 修复 (2026-05-09): 美股保证金风控初始化 ──
        try:
            from app.risk.us_margin_guard import get_margin_guard
            margin_guard = get_margin_guard()
            # 注入 broker 并调用 initialize (关键修复: 之前只 set_broker 未 initialize)
            if self.execution_engine:
                adapter = getattr(self.execution_engine, "broker_adapter", None)
                if adapter and adapter.is_connected():
                    margin_guard.initialize(broker_adapter=adapter)
                    logger.info("[ScalpingEngine] 美股保证金风控 (MarginGuard) 已初始化并连接")
                else:
                    # broker 尚未连接，先 set_broker 等待连接
                    margin_guard.set_broker(adapter)
                    logger.info("[ScalpingEngine] MarginGuard broker 已注入 (等待连接后初始化)")
            else:
                logger.warning("[ScalpingEngine] MarginGuard 跳过初始化: execution_engine 不可用")
        except Exception as e:
            logger.warning(f"[ScalpingEngine] MarginGuard 初始化失败 (不影响运行): {e}")

        # ── P0 每日自动选股器 (2026-04-30) ──
        if self.stock_selector is None:
            try:
                from app.scalping.stock_selector import create_stock_selector
                # 尝试获取 EarningsRiskGuard (主管线初始化时已创建)
                earnings_guard = None
                try:
                    from app.risk.earnings_risk_guard import get_earnings_risk_guard
                    earnings_guard = get_earnings_risk_guard()
                except Exception:
                    pass
                self.stock_selector = create_stock_selector(
                    data_router=self.data_router,
                    earnings_guard=earnings_guard,
                    scalping_config=scalping_cfg,
                )
            except Exception as e:
                logger.warning(f"[ScalpingEngine] 选股器初始化失败 (降级): {e}")
                self.stock_selector = None

        # 统一交易记录 (Karpathy: 所有成交/建议/预测统一落库)
        from app.storage.unified_trade_store import get_unified_trade_store
        self.trade_store = get_unified_trade_store()
        logger.info("[ScalpingEngine] 统一交易记录已连接")

        # P0 Karpathy fix (2026-05-13): 强制标的策略验证器
        forced_tickers = scalping_cfg.get("forced_tickers", [])
        if forced_tickers:
            self._forced_ticker_validation = _ForcedTickerValidator(forced_tickers, self.notifier)
            logger.info(f"[ScalpingEngine] 强制标的验证器已初始化: {[t['code'] for t in forced_tickers]}")

    def _load_scalping_config(self) -> Dict:
        """从 unified_config.yaml 加载 scalping 配置"""
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()
            return cfg.get_dotted("scalping", {}) or {}
        except Exception as e:
            logger.debug(f"[ScalpingEngine] 配置加载失败: {e}")
            return {}

    def _get_main_pipeline_direction(self, symbol: str) -> Optional[str]:
        """
        P0 方向对齐锁 (2026-05-31): 查询主管线最近决策方向
        
        从 decisions.db 查询同标的最近 30 分钟内的决策,
        返回 "LONG" / "SHORT" / None (无数据或中性).
        
        设计: 仅查询, 不修改任何状态. 失败返回 None (不阻塞 scalping).
        """
        try:
            import sqlite3
            from datetime import timedelta as _td
            from app.core.paths import get_decisions_db
            from app.utils.timezone import now_et
            db_path = get_decisions_db()
            
            # P0 修复 (2026-05-31): 用 Python ET 时间替代 SQLite datetime('now')
            # SQLite datetime('now') 返回 UTC, 与 DB 中 ET 时间不匹配
            cutoff = (now_et() - _td(minutes=30)).isoformat()
            
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute("""
                    SELECT direction, final_strength, created_at
                    FROM decisions
                    WHERE symbol = ?
                      AND created_at > ?
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (symbol, cutoff)).fetchone()
                
                if row:
                    direction, strength, created_at = row
                    # 只有 strength 足够明确时才锁定方向
                    # strength > 50 = LONG, strength < 50 = SHORT
                    if strength and strength > 55:
                        return "LONG"
                    elif strength and strength < 45:
                        return "SHORT"
                    # 45-55 区间为中性, 不锁定
                return None
        except Exception as e:
            logger.debug(f"[Scalping·方向锁] {symbol} decisions.db 查询失败: {e}")
            return None

    def _get_account_equity(self, fallback: float) -> float:
        """从 broker 动态获取账户净值 (SSOT 修复 2026-04-30)
        
        模拟盘/实盘切换时自动适配真实资金规模。
        获取失败时 fallback 到配置值。
        """
        try:
            if self.execution_engine and hasattr(self.execution_engine, 'broker_adapter'):
                adapter = self.execution_engine.broker_adapter
                if adapter and adapter.is_connected():
                    equity = adapter.get_account_equity()
                    if equity and equity > 0:
                        logger.info(
                            f"[ScalpingEngine] 动态获取账户净值: ${equity:,.0f} "
                            f"(trade_env={'模拟' if adapter.trade_env == 1 else '实盘'})"
                        )
                        return equity
        except Exception as e:
            logger.debug(f"[ScalpingEngine] 动态获取账户净值失败: {e}")
        logger.info(f"[ScalpingEngine] 使用配置总资金: ${fallback:,.0f}")
        return fallback

    def _load_strategy_budgets(self) -> Dict:
        """从 unified_config.yaml 加载策略级风险预算"""
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()
            return cfg.get_dotted("strategy_budgets", {}) or {}
        except Exception:
            return {}

    def _load_symbol_cooldown_config(self):
        """P0 修复 (2026-05-08): 加载 symbol_cooldown 配置"""
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()
            self._structure_exit_cooldown_cfg = cfg.get_dotted("symbol_cooldown.structure_exit", {})
            if self._structure_exit_cooldown_cfg:
                logger.info(
                    f"[ScalpingEngine] STRUCTURE_EXIT 冷却已配置: "
                    f"连续亏损={self._structure_exit_cooldown_cfg.get('consecutive_losses', 3)}次, "
                    f"冷却={self._structure_exit_cooldown_cfg.get('cooldown_hours', 24)}小时, "
                    f"方向={self._structure_exit_cooldown_cfg.get('direction', 'same_only')}"
                )
            else:
                logger.info("[ScalpingEngine] STRUCTURE_EXIT 冷却未配置，将使用默认值 (3次/24小时)")
                self._structure_exit_cooldown_cfg = {
                    "consecutive_losses": 3,
                    "cooldown_hours": 24,
                    "direction": "same_only"
                }
        except Exception as e:
            logger.debug(f"[ScalpingEngine] symbol_cooldown 配置加载失败: {e}")
            self._structure_exit_cooldown_cfg = {
                "consecutive_losses": 3,
                "cooldown_hours": 24,
                "direction": "same_only"
            }

    def _is_symbol_on_cooldown(self, symbol: str) -> bool:
        """P0 修复 (2026-05-08): 检查标的是否在 STRUCTURE_EXIT 连续亏损冷却中"""
        if not self._structure_exit_cooldown_cfg:
            return False

        state = self._symbol_structure_exit_losses.get(symbol)
        if not state:
            return False

        consecutive = state.get("consecutive", 0)
        last_exit_ts = state.get("last_exit_ts", 0)
        direction = state.get("direction", "")

        threshold = self._structure_exit_cooldown_cfg.get("consecutive_losses", 3)
        cooldown_hours = self._structure_exit_cooldown_cfg.get("cooldown_hours", 24)
        direction_mode = self._structure_exit_cooldown_cfg.get("direction", "same_only")

        # 未达到阈值，不冷却
        if consecutive < threshold:
            return False

        # 检查是否仍在冷却期内
        now = time.time()
        elapsed_hours = (now - last_exit_ts) / 3600.0
        if elapsed_hours < cooldown_hours:
            logger.warning(
                f"[Scalping冷却] {symbol} STRUCTURE_EXIT 连续{consecutive}次亏损, "
                f"冷却剩余 {cooldown_hours - elapsed_hours:.1f} 小时"
            )
            return True

        # 冷却期已过，清理状态
        logger.info(f"[Scalping冷却] {symbol} STRUCTURE_EXIT 冷却期已过，恢复交易")
        del self._symbol_structure_exit_losses[symbol]
        return False

    def _record_structure_exit(self, symbol: str, direction: str, is_loss: bool):
        """P0 修复 (2026-05-08): 记录 STRUCTURE_EXIT 结果，更新连续亏损计数"""
        if not self._structure_exit_cooldown_cfg:
            return

        state = self._symbol_structure_exit_losses.get(symbol)
        if not state:
            state = {"consecutive": 0, "last_exit_ts": 0, "direction": ""}

        now = time.time()

        if is_loss:
            # 如果是同方向连续亏损（或 direction_mode=any），累加计数
            direction_mode = self._structure_exit_cooldown_cfg.get("direction", "same_only")
            if direction_mode == "same_only":
                if state["direction"] == direction or state["consecutive"] == 0:
                    state["consecutive"] += 1
                    state["direction"] = direction
                    state["last_exit_ts"] = now
                    logger.warning(
                        f"[Scalping冷却] {symbol} {direction} STRUCTURE_EXIT 亏损, "
                        f"连续亏损次数: {state['consecutive']}"
                    )
                else:
                    # 不同方向，重置计数
                    state["consecutive"] = 1
                    state["direction"] = direction
                    state["last_exit_ts"] = now
            else:  # any
                state["consecutive"] += 1
                state["last_exit_ts"] = now
        else:
            # 盈利，重置计数
            if state["consecutive"] > 0:
                logger.info(f"[Scalping冷却] {symbol} STRUCTURE_EXIT 盈利，重置连续亏损计数")
            state["consecutive"] = 0
            state["direction"] = ""

        self._symbol_structure_exit_losses[symbol] = state

    def _check_strategy_daily_loss(self) -> bool:
        """检查 Scalping 策略是否已耗尽当日风险预算"""
        if self._strategy_max_daily_loss <= 0:
            return True  # 未配置预算, 放行
        if self._strategy_realized_pnl_today <= -self._strategy_max_daily_loss:
            logger.warning(
                f"[Scalping 风控] 策略日亏损预算耗尽! "
                f"亏损: ${-self._strategy_realized_pnl_today:,.0f} >= "
                f"预算: ${self._strategy_max_daily_loss:,.0f}"
            )
            return False
        return True

    def start(self):
        """启动 Scalping 引擎 (daemon 线程)

        P1 修复 (2026-05-07): 非交易时段不再直接 return，而是启动等待线程，
        市场开盘后自动激活主循环。解决"服务在盘前启动，开盘后 scalping 从不工作"的 bug。
        """
        if self.running:
            logger.warning("[ScalpingEngine] 已在运行中")
            return

        # P1 修复: 不在主线程中阻塞，改为后台等待/激活
        self._waiter_thread = threading.Thread(
            target=self._wait_and_activate,
            name="ScalpingWaiter",
            daemon=True,
        )
        self._waiter_thread.start()

        # P0 修复 (2026-05-09): 启动定时资金同步线程 (与港股 data_subscriber cleanup_loop 对齐)
        self._capital_sync_active = True
        self._capital_sync_thread = threading.Thread(
            target=self._capital_sync_loop,
            name="ScalpingCapitalSync",
            daemon=True,
        )
        self._capital_sync_thread.start()

        phase = "unknown"
        if self.time_gate is not None:
            phase = self.time_gate.get_current_phase()
        logger.info(f"[ScalpingEngine] 等待线程已启动 (当前 phase={phase})，开盘后自动激活")

    def _wait_and_activate(self):
        """等待交易时段到来，然后激活主循环

        P1 修复 (2026-05-07): 非交易时段自适应轮询 + 日志静默。
        - WEEKEND:  15min 轮询，不输出任何日志
        - OVERNIGHT: 5min 轮询，不输出任何日志
        - PRE_MARKET: 60s 轮询，阶段切换时只输出一次日志
        - 进入交易时段后自动激活主循环
        """
        # 1. 等待子模块初始化 (在等待线程中做，避免阻塞服务启动)
        self._init_submodules()

        # P0 修复 (2026-05-07): 从 open_positions 恢复 scalping 活跃交易
        self._restore_scalping_positions()

        # 2. 等待到交易时段 — 自适应轮询，非交易时段静默
        from app.core.scheduler_triggers import get_market_phase, MarketPhase

        last_phase_str = None
        while not self.time_gate.is_trading_allowed():
            if not getattr(self, '_waiter_active', True):
                return

            phase = get_market_phase()

            # 根据市场阶段选择轮询间隔
            if phase == MarketPhase.WEEKEND:
                poll_interval = 900   # 15min — 周末低频
            elif phase == MarketPhase.OVERNIGHT:
                poll_interval = 300   # 5min — 深夜中频
            else:
                poll_interval = 60    # 1min — 盘前准备期高频

            # 仅在阶段切换时输出一次日志（避免每 60s 刷屏）
            phase_str = phase.value
            if phase_str != last_phase_str:
                logger.info(f"[ScalpingEngine] 当前 {phase_str}，静默等待开盘 (轮询间隔={poll_interval}s)")
                last_phase_str = phase_str

            time.sleep(poll_interval)

        # 3. 交易时段已到，激活主循环
        logger.info("[ScalpingEngine] 交易时段已到，激活 Scalping 引擎")

        self._subscribe_scalping_codes()

        # P0 (2026-04-30): 每日自动选股
        if self.stock_selector and self.daily_active_pool:
            try:
                selected = self.stock_selector.populate_pool(self.daily_active_pool)
                logger.info(f"[ScalpingEngine] 自动选股完成: {selected} 只入选")
            except Exception as e:
                logger.warning(f"[ScalpingEngine] 自动选股失败 (降级到活跃池): {e}")

        self.running = True

        self._thread = threading.Thread(
            target=self._run_loop,
            name="ScalpingEngine",
            daemon=True,
        )
        self._thread.start()

        mode_label = "👁️ OBSERVE (干跑)" if self.mode == "observe" else "🚀 ACTIVE"
        logger.info(f"[ScalpingEngine] {mode_label} 模式已启动 | 线程: {self._thread.name}")

        if self.mode == "observe":
            logger.info(
                "[ScalpingEngine] ⚠️ 当前为 observe 模式，"
                "所有交易仅记录不执行。"
                "验证 24 小时后切换到 active 模式。"
            )

    def _capital_sync_loop(self):
        """P0 修复 (2026-05-09): 定时资金同步 (对标港股 data_subscriber cleanup_loop)

        每 60s 从 Futu 拉取账户资产并同步到 CapitalSafetySystem + ScalpingBudget。
        仅交易时段执行，非交易时段静默跳过。
        等待 execution_engine 就绪后再开始同步。
        """
        broker_ready_logged = False
        while self._capital_sync_active:
            try:
                # 非交易时段跳过
                if not self.time_gate.is_trading_allowed():
                    broker_ready_logged = False  # 重置状态
                    last_sync_phase = None
                    time.sleep(60)
                    continue

                # 等待 broker 就绪
                if self.execution_engine is None or getattr(self.execution_engine, "broker_adapter", None) is None:
                    if not broker_ready_logged:
                        logger.debug("[Scalping 定时同步] 等待 execution_engine 就绪...")
                        broker_ready_logged = True
                    time.sleep(30)
                    continue

                # 执行同步
                from app.risk.account_refresher import get_account_refresher
                refresher = get_account_refresher()

                adapter = getattr(self.execution_engine, "broker_adapter", None)
                if refresher._broker is None and adapter and adapter.is_connected():
                    refresher._broker = adapter

                if refresher._broker and refresher._broker.is_connected():
                    account_info = refresher.refresh()
                    if account_info and account_info.get("buying_power", 0) > 0:
                        from app.risk.capital_safety import get_capital_safety_system
                        capital = get_capital_safety_system()
                        capital.update_account(
                            current_equity=account_info.get("total_assets", 0),
                            cash=account_info.get("cash", 0),
                            market_value=account_info.get("market_val", 0),
                            buying_power=account_info.get("buying_power", 0),
                        )

                        total = account_info.get("total_assets", 0)
                        bp = account_info.get("buying_power", 0)
                        level = capital.get_state().get("level", "UNKNOWN") if hasattr(capital, "get_state") else "OK"

                        # 仅状态变化时输出 (避免日志刷屏)
                        current_phase_label = f"{total:,.0f}_{level}"
                        if current_phase_label != last_sync_phase:
                            logger.info(
                                f"[Scalping 定时同步] 水位=${total:,.0f} | "
                                f"buying_power=${bp:,.0f} | level={level}"
                            )
                            last_sync_phase = current_phase_label

                        # 同步刷新 scalping budget
                        if self.budget:
                            self.budget.update_total_equity(total)
            except Exception as e:
                logger.debug(f"[Scalping 定时同步] 同步失败: {e}")

            time.sleep(60)

    def stop(self):
        """停止 Scalping 引擎"""
        self.running = False
        self._waiter_active = False
        self._capital_sync_active = False

        # C5 归因 (2026-05-13): 停引擎时刷新归因数据
        try:
            from app.analysis.strategy_attribution import get_attribution
            get_attribution()._flush_daily_attribution()
            logger.info("[ScalpingEngine] 归因数据已刷新")
        except Exception:
            pass

        if self._thread:
            self._thread.join(timeout=10)
            logger.info("[ScalpingEngine] 主循环线程已停止")
        if getattr(self, '_waiter_thread', None):
            self._waiter_thread.join(timeout=5)
            logger.info("[ScalpingEngine] 等待线程已停止")
        if getattr(self, '_capital_sync_thread', None):
            self._capital_sync_thread.join(timeout=5)
            logger.info("[ScalpingEngine] 资金同步线程已停止")

    def _run_loop(self):
        """主循环"""
        logger.info("[ScalpingEngine] 主循环启动")

        while self.running:
            try:
                self._tick()
                time.sleep(self.polling_interval)
            except Exception as e:
                logger.error(f"[ScalpingEngine] 循环异常: {e}", exc_info=True)
                time.sleep(self.polling_interval * 2)  # 异常后加倍等待


    def _is_active_time(self) -> bool:
        """
        轻量级时间门控 (P0 优化): 根据配置检查当前是否为 Scalping 活跃时段。
        """
        try:
            from datetime import datetime, time as dtime
            import pytz
            
            # 1. 获取当前 ET 时间
            ET = pytz.timezone("America/New_York")
            now_et = datetime.now(ET)
            current_time = now_et.time()
            
            # 2. 从配置获取活跃时段
            try:
                from app.core.config_container import get_config as get_app_config
                cfg = get_app_config()
                active_hours_str = cfg.get_dotted("scalping.active_hours", [])
            except Exception:
                # 配置不可用时，回退到默认 (全天候，除了 TimeGate 拦截的)
                return True
            
            if not active_hours_str:
                return True # 未配置限制，默认允许
                
            # 3. 检查当前时间是否在任意一个区间内
            for period in active_hours_str:
                if "-" in period:
                    start_str, end_str = period.split("-")
                    try:
                        # 解析 HH:MM
                        h_s, m_s = map(int, start_str.split(":"))
                        h_e, m_e = map(int, end_str.split(":"))
                        
                        start_time = dtime(h_s, m_s)
                        end_time = dtime(h_e, m_e)
                        
                        if start_time <= current_time <= end_time:
                            return True
                    except Exception:
                        continue
            
            return False # 不在任何配置区间内
            
        except Exception as e:
            # 发生异常时放行，避免阻断交易
            logger.debug(f"[ScalpingEngine] 时间门控检查异常 (放行): {e}")
            return True


    def _tick(self):
        """单次 tick — 完整流程"""
        # 0a. P0 防御纵深 (2026-05-26): 节假日/周末门控 — 必须在一切交易逻辑之前
        # 防止 MOC 等逻辑在 is_trading_allowed 检查之前执行
        if not self.time_gate.is_trading_allowed():
            return

        # 0b. MOC 保护: 强制平仓检查 (15:55+)
        if self.time_gate.should_force_close_all():
            # P0 修复 (2026-05-09): 跨交易日自动重置 MOC 守卫
            from datetime import date as _date
            today = _date.today()
            if today != self._last_moc_date:
                self._moc_fired_this_session = False
                self._last_moc_date = today
            self._force_close_all_positions("MOC 收盘强制平仓")
            return

        # 1. (已移至 0a: 节假日/周末门控在 _tick 顶部)
        
        # 1b. 轻量级时间门控 (Scalping 活跃时段检查)
        if not self._is_active_time():
            return

        # 1c. P0 新增 (2026-05-12): 趋势日过滤 — 均值回归策略在趋势日失效
        if self._is_trending_day():
            logger.debug("[ScalpingEngine] 趋势日检测: SPY/VIX 显示强趋势，暂停 Scalping 交易")
            return

        # 1d. P0 新增 (2026-05-12): 每日交易次数限制 — 防止过度交易
        if not self._can_trade_today():
            logger.debug("[ScalpingEngine] 今日交易次数已达上限，暂停交易")
            return

        # 1e. MOC 保护: 15:50+ 只平仓不开新仓
        new_entry_blocked = self.time_gate.is_new_entry_blocked()

        # 2. 全局风控检查
        if not self._check_global_risk():
            return

        # 3. 熔断检查
        if self.circuit_breaker.is_blocked():
            return

        # 3b. 策略级日亏损预算检查
        if not self._check_strategy_daily_loss():
            return

        # 4. 日亏损检查
        if self.budget.is_daily_loss_exceeded():
            return

        # 5. 获取 scalping 专属标的
        scalping_codes = self._get_scalping_codes()
        logger.info(f"[ScalpingEngine] _tick 检查: 标的数={len(scalping_codes)} | 样本={scalping_codes[:5]}")
        if not scalping_codes:
            return

        # 6. 检查活跃交易 (监控止盈止损)
        self._monitor_active_trades(scalping_codes)

        # 7. 扫描新信号 (MOC 保护: 15:50+ 跳过)
        if not new_entry_blocked:
            for code in scalping_codes:
                try:
                    self._scan_symbol(code)
                except Exception as e:
                    logger.debug(f"[ScalpingEngine] {code} 扫描失败: {e}")

        # P3 修复 (2026-05-06): K线配额告警 — 如果大部分标的都无法获取K线，发送告警
        self._check_kline_quota_health(scalping_codes)

    def _check_kline_quota_health(self, scalping_codes: list):
        """检查K线配额健康度，超阈值时发送告警通知"""
        if not hasattr(self, '_quota_alert_count'):
            self._quota_alert_count = 0
            self._last_quota_alert_time = 0
        
        # 非交易时段跳过检查 (避免周末/盘后误报)
        if self.time_gate and not self.time_gate.is_trading_allowed():
            return
        
        # 每5分钟最多告警一次
        import time
        now = time.time()
        if now - self._last_quota_alert_time < 300:
            return
        
        # 使用已有的 self.data_router 检查
        if not self.data_router:
            return
        
        degraded_count = 0
        checked = 0
        for code in scalping_codes[:10]:
            try:
                ctx = self.data_router.get_full_market_context(code)
                checked += 1
                if ctx and ctx.get("data_source") == "futu_lv3_quote_only":
                    degraded_count += 1
            except Exception:
                pass
        
        if checked == 0:
            return
        
        # 超过60%标的降级 → 发送告警
        ratio = degraded_count / checked
        if ratio > 0.6:
            self._quota_alert_count += 1
            self._last_quota_alert_time = now
            logger.warning(
                f"[ScalpingEngine] K线配额告警 | {degraded_count}/{checked} "
                f"标的降级为quote_only (比率={ratio:.0%})"
            )

            # 2026-05-09: 使用防抖器 (观察期 + 升级推送)
            if self.debouncer:
                self.debouncer.record(
                    alert_type="kline_quota",
                    title="K线配额告警",
                    content=f"{degraded_count}/{checked} 标的降级为quote_only (比率={ratio:.0%})",
                    level="WARN",
                    alerter=self.notifier,
                )
            elif self.notifier:
                # 降级: 直接推送 (原有行为)
                try:
                    self.notifier.send_text(
                        f"⚠️ [FTSeek-US] K线配额告警\n"
                        f"{degraded_count}/{checked} 标的降级为quote_only\n"
                        f"技术指标(RSI/MACD/ATR)不可用，信号质量下降\n"
                        f"已触发 {self._quota_alert_count} 次告警"
                    )
                except Exception:
                    pass

    def _check_global_risk(self) -> bool:
        """检查全局风控状态"""
        try:
            # 尝试获取 RiskStateMachine 状态
            from app.us_pipeline.risk_state_machine import get_risk_state_machine
            rsm = get_risk_state_machine()
            state = rsm.get_current_state()

            if state in ("RISK_OFF", "HALT", "HALTED"):
                logger.debug(f"[ScalpingEngine] 全局风控 {state}，暂停 scalping")
                return False
        except Exception:
            pass  # RiskStateMachine 不可用，继续

        return True

    # ═══════════════════════════════════════════════════════════
    # P0 新增 (2026-05-12): 趋势日过滤 — 均值回归策略在趋势日失效
    # ═══════════════════════════════════════════════════════════

    def _is_trending_day(self) -> bool:
        """
        判断是否为趋势日
        
        趋势日特征:
        - SPY 涨跌超过 0.8% (强单边走势)
        - VIX > 25 (高波动，趋势延续概率高)
        - 两者满足其一即视为趋势日
        
        均值回归策略在趋势日表现极差 (胜率仅 0-5%)
        """
        try:
            # 获取 SPY 日变化
            spy_change = 0.0
            vix = None
            
            if self.data_router:
                try:
                    spy_ctx = self.data_router.get_full_market_context("US.SPY")
                    if spy_ctx:
                        spy_change = spy_ctx.get("spy_change", 0.0)
                        vix = spy_ctx.get("vix")
                except Exception:
                    pass
            
            # SPY 涨跌超过 0.8% 视为趋势日
            if abs(spy_change) > 0.008:
                logger.info(
                    f"[ScalpingEngine] 趋势日检测: SPY {spy_change:+.1%} > 0.8% → 暂停交易"
                )
                return True
            
            # VIX > 25 视为高波动趋势日
            if vix and vix > 25:
                logger.info(
                    f"[ScalpingEngine] 趋势日检测: VIX {vix:.1f} > 25 → 暂停交易"
                )
                return True
            
            return False
            
        except Exception as e:
            logger.warning(f"[ScalpingEngine] 趋势日检测失败: {e}，继续交易")
            return False  # 检测失败时默认允许交易

    # ═══════════════════════════════════════════════════════════
    # P0 新增 (2026-05-12): 每日交易次数限制 — 防止过度交易
    # ═══════════════════════════════════════════════════════════

    def _can_trade_today(self) -> bool:
        """
        检查今日是否还可以交易
        
        限制:
        - 每日最多 10 笔交易
        - 连续亏损 3 笔后暂停当日交易
        """
        try:
            from datetime import date
            today = date.today().isoformat()
            
            # 初始化或跨日重置
            if not hasattr(self, '_daily_trade_count'):
                self._daily_trade_count = {}
                self._daily_consecutive_losses = {}
            
            # 跨日重置
            if self._daily_trade_count.get("date") != today:
                self._daily_trade_count = {"date": today, "count": 0}
                self._daily_consecutive_losses = {"date": today, "count": 0}
            
            # 检查每日最大交易次数
            max_trades = 10  # 每日最多 10 笔
            if self._daily_trade_count.get("count", 0) >= max_trades:
                logger.info(
                    f"[ScalpingEngine] 交易次数限制: 今日已交易 {self._daily_trade_count['count']} 笔 >= {max_trades} → 暂停"
                )
                return False
            
            # 检查连续亏损
            max_consecutive_losses = 3
            if self._daily_consecutive_losses.get("count", 0) >= max_consecutive_losses:
                # 2026-05-12: 只记录一次，避免每 tick 日志刷屏
                if not getattr(self, '_loss_limit_logged', False):
                    self._loss_limit_logged = True
                    logger.info(
                        f"[ScalpingEngine] 连续亏损限制: 今日连续亏损 {self._daily_consecutive_losses['count']} 笔 >= {max_consecutive_losses} → 暂停当日 Scalping"
                    )
                return False
            else:
                self._loss_limit_logged = False  # 新的一天或重置后清除
            
            return True
            
        except Exception as e:
            logger.warning(f"[ScalpingEngine] 交易次数检查失败: {e}，继续交易")
            return True  # 检查失败时默认允许交易

    def _record_trade_result(self, pnl_pct: float, symbol: str = ""):
        """
        记录交易结果 (用于每日交易次数限制)

        Args:
            pnl_pct: 盈亏百分比
            symbol: 标的代码 (用于每标每日交易次数统计)
        """
        try:
            from datetime import date
            today = date.today().isoformat()

            if not hasattr(self, '_daily_trade_count'):
                self._daily_trade_count = {"date": today, "count": 0}
                self._daily_consecutive_losses = {"date": today, "count": 0}

            # 跨日重置
            if self._daily_trade_count.get("date") != today:
                self._daily_trade_count = {"date": today, "count": 0}
                self._daily_consecutive_losses = {"date": today, "count": 0}

            # 更新交易次数
            self._daily_trade_count["count"] = self._daily_trade_count.get("count", 0) + 1

            # C3+S2 修复 (2026-05-13): 每标每日交易次数统计
            if symbol and hasattr(self, '_per_symbol_daily_count'):
                sym_stats = self._per_symbol_daily_count.get(symbol, {"count": 0, "date": ""})
                if sym_stats.get("date") != today:
                    sym_stats = {"count": 0, "date": today}
                sym_stats["count"] = sym_stats.get("count", 0) + 1
                self._per_symbol_daily_count[symbol] = sym_stats

            # 更新连续亏损
            if pnl_pct <= 0:
                self._daily_consecutive_losses["count"] = self._daily_consecutive_losses.get("count", 0) + 1
            else:
                self._daily_consecutive_losses["count"] = 0  # 盈利重置连续亏损
            
            logger.debug(
                f"[ScalpingEngine] 交易记录: 今日第 {self._daily_trade_count['count']} 笔 | "
                f"连续亏损 {self._daily_consecutive_losses['count']} 笔 | PnL={pnl_pct:+.2%}"
            )

            # P0 Karpathy fix (2026-05-13): 强制标的策略验证
            if symbol and hasattr(self, '_forced_ticker_validation'):
                self._forced_ticker_validation.record_trade(symbol, pnl_pct)
            
        except Exception as e:
            logger.warning(f"[ScalpingEngine] 记录交易结果失败: {e}")

    def _get_scalping_codes(self) -> List[str]:
        """获取 scalping 专属标的"""
        if self.daily_active_pool:
            codes = self.daily_active_pool.get_scalping_codes()
            if codes:
                result = list(codes)
            else:
                # 降级: 如果无专属标的，扫描通用活跃池 (供测试验证)
                result = list(self.daily_active_pool.get_active_codes())
        else:
            result = []
        # P0 Karpathy fix (2026-05-13): 合并强制交易标的
        forced = self.scalping_cfg.get("forced_tickers", [])
        for ft in forced:
            code = ft.get("code", "")
            if code and code not in result:
                result.append(code)
                logger.info(f"[Scalping·强制标的] 加入 {code} (min_trades={ft.get('min_trades_per_day', 1)})")

        # P0 新增 (2026-05-20): 黑名单过滤 — 数据驱动剔除持续亏损标的
        blacklist_cfg = self.scalping_cfg.get("blacklist", {})
        blacklist_symbols = set(blacklist_cfg.get("symbols", []))
        if blacklist_symbols:
            before = len(result)
            result = [c for c in result if c.split(".")[-1] not in blacklist_symbols]
            removed = before - len(result)
            if removed:
                logger.info(f"[Scalping·黑名单] 过滤 {removed} 只标的: {blacklist_symbols}")

        return result

    def _subscribe_scalping_codes(self):
        """
        P0 修复 (2026-04-30): 对 Scalping 专属标的订阅 Futu 实时行情

        确保 Scalping 标的通过 Futu Push 订阅获取实时数据，
        避免走免费 HTTP API (限流严重, 数据陈旧)。
        槽位预算: Scalping 标的数 × 2 (QUOTE + K_1M), 通常 < 20 槽位。
        """
        codes = self._get_scalping_codes()
        if not codes:
            logger.info("[Scalping Futu] 无专属标的，跳过订阅")
            return

        if not self.data_router or not hasattr(self.data_router, 'futu') or not self.data_router.futu:
            logger.info("[Scalping Futu] Futu 未启用，使用 data_router HTTP 降级模式")
            return

        # 去重: 排除已在主管线 watchlist 中订阅的标的
        try:
            from app.utils.watchlist_manager import get_stock_codes
            watchlist_codes = set(get_stock_codes())
        except Exception:
            watchlist_codes = set()

        new_codes = [c for c in codes if c not in watchlist_codes]
        if not new_codes:
            logger.info(f"[Scalping Futu] 全部 {len(codes)} 只标的已由主管线订阅")
            return

        try:
            self.data_router.futu.subscribe(new_codes, sub_types=['QUOTE', 'K_1M'])
            slots = len(new_codes) * 2
            logger.info(
                f"[Scalping Futu] 已订阅 {len(new_codes)} 只标的 | "
                f"槽位: +{slots} | 标的: {new_codes[:5]}{'...' if len(new_codes) > 5 else ''}"
            )
        except Exception as e:
            logger.warning(f"[Scalping Futu] 订阅失败 (降级到 HTTP): {e}")

    def _scan_symbol(self, symbol: str):
        """扫描单个标的的信号"""
        # 如果已有活跃交易，跳过
        with self._lock:
            if symbol in self._active_trades:
                return

            # P1 修复 (2026-05-06): 信号冷却检查 — 防止同一标的重复开仓
            now = time.time()
            last_signal_time = self._signal_cooldown.get(symbol, 0)
            if now - last_signal_time < self._signal_cooldown_sec:  # 使用配置值 (默认 300s)
                return

            # P2 修复 (2026-05-19): sl_hit 二级冷却检查 — 防"刚损刚再入"
            # 5/18 TME: sl_hit 后 2s 即可再开仓 (因为 signal_cooldown 是 entry-to-entry, hold 5min 后已耗尽).
            last_loss_time = self._loss_cooldown.get(symbol, 0)
            if now - last_loss_time < self._loss_cooldown_sec:
                remaining = self._loss_cooldown_sec - (now - last_loss_time)
                logger.debug(
                    f"[Scalping冷却·loss] {symbol} 上次 sl_hit 后冷却中, 剩余 {remaining:.0f}s"
                )
                return

            # P0 修复 (2026-05-13): 下单失败冷却检查 — 防止 broker 拒绝后无限重试
            last_fail_time = self._order_fail_cooldown.get(symbol, 0)
            if now - last_fail_time < self._order_fail_cooldown_sec:
                remaining = self._order_fail_cooldown_sec - (now - last_fail_time)
                logger.debug(f"[Scalping冷却] {symbol} 下单失败冷却中, 剩余 {remaining:.0f}s")
                return

            # RFC-053 (2026-05-30): 信号检测级冷却 — 防止 detect() spam 日志
            # 检查所有可能的方向(LONG/SHORT), 任一在冷却中就跳过
            _now_det = time.time()
            _in_det_cooldown = False
            for _d in (LONG, SHORT):
                _dk = f"{symbol}:{_d}"
                _last_det = self._detection_cooldown.get(_dk, 0)
                if _now_det - _last_det < self._detection_cooldown_sec:
                    _in_det_cooldown = True
                    break
            if _in_det_cooldown:
                return

            # P0 修复 (2026-05-08): 标的级冷却 — sl_hit 当日超过3次冷却到次日
            if hasattr(self, '_symbol_sl_hit_count'):
                today = now_et().strftime("%Y-%m-%d")
                sl_count = self._symbol_sl_hit_count.get(symbol, {}).get("count", 0)
                sl_date = self._symbol_sl_hit_count.get(symbol, {}).get("date", "")
                if sl_date == today and sl_count >= 3:
                    logger.debug(f"[Scalping冷却] {symbol} 今日 sl_hit={sl_count}≥3, 冷却到次日")
                    return
                # 跨日重置
                if sl_date != today:
                    self._symbol_sl_hit_count[symbol] = {"count": 0, "date": today}

            # C3+S2 修复 (2026-05-13): 每标的每日最大交易次数
            if hasattr(self, '_per_symbol_daily_count'):
                today = now_et().strftime("%Y-%m-%d")
                sym_stats = self._per_symbol_daily_count.get(symbol, {"count": 0, "date": ""})
                if sym_stats.get("date") != today:
                    sym_stats = {"count": 0, "date": today}
                    self._per_symbol_daily_count[symbol] = sym_stats
                if sym_stats.get("count", 0) >= self._daily_max_trades_per_symbol:
                    logger.debug(
                        f"[Scalping] {symbol} 今日已达上限 "
                        f"({sym_stats['count']}/{self._daily_max_trades_per_symbol})"
                    )
                    return

            # P0 修复 (2026-05-08): STRUCTURE_EXIT 连续亏损冷却检查
            if self._is_symbol_on_cooldown(symbol):
                logger.debug(f"[Scalping冷却] {symbol} STRUCTURE_EXIT 连续亏损冷却中，跳过开仓")
                return

            # RFC-051: Coordinator 锁前置过滤 — 跳过被其他策略锁定的标的
            # 根因: scalping 对 QCOM/UNH/SNDK (main 锁定) 每 6s 生成信号，
            # 虽然 hedge_guard 阻止执行，但浪费计算/配额且制造日志噪音
            try:
                sched = getattr(self, "scheduler", None)
                coord = getattr(sched, "coordinator", None) if sched else None
                if coord is not None and getattr(coord, "enabled", False):
                    _lock_owner = coord.lock.owner(symbol) if hasattr(coord, 'lock') and hasattr(coord.lock, 'owner') else None
                    if _lock_owner and _lock_owner != "scalping":
                        return  # 被 main/llm_plan 等锁定，静默跳过
            except Exception:
                pass  # 锁查询异常不阻断

            # RFC-051: PositionStore 反向持仓前置过滤
            # 根因: 即使 coordinator 未锁定，position_store 中已有反向仓位时
            # 不应生成信号（双重保险，与 hedge_guard 形成纵深防御）
            try:
                sched = getattr(self, "scheduler", None)
                ps = getattr(sched, "position_store", None) if sched else None
                if ps is not None:
                    _existing = ps.get(symbol)
                    if _existing is not None:
                        _ex_dir = _existing.direction.value if hasattr(_existing.direction, 'value') else str(_existing.direction)
                        _ex_owner = getattr(_existing, 'owner_strategy', None) or (_existing.metadata or {}).get('source', '')
                        if _ex_owner and _ex_owner != 'scalping':
                            return  # 其他策略的持仓，scalping 不介入
            except Exception:
                pass

        # 获取行情数据
        try:
            data = self._fetch_market_data(symbol)
            if not data:
                return
        except Exception as e:
            logger.debug(f"[ScalpingEngine] {symbol} 行情获取失败: {e}")
            return

        # 获取当前时段参数
        params = self.time_gate.get_params()
        if not params:
            return

        # 趋势过滤
        if self.trend_filter:
            spy_change = data.get("spy_change", 0)
            vix = data.get("vix")
            block_reason = self.trend_filter.should_block(spy_change, vix)
            if block_reason:
                return

        # ── 动量生命周期融合 ──
        lifecycle = self._update_lifecycle(symbol, data["price"])
        lp = self._lifecycle_signal_priority(lifecycle)
        # P0 修复 (2026-05-08): 注入 lifecycle 到 data dict 供 _execute_trade 使用
        data["lifecycle"] = lifecycle or "unknown"

        # 根据生命周期调整时段允许的方向
        effective_directions = list(params["directions"])
        if lp["direction_bias"]:
            if lp["direction_bias"] not in effective_directions:
                effective_directions = [lp["direction_bias"]]
        if lp["suppress_mr_long"] and LONG in effective_directions:
            effective_directions.remove(LONG)
        if lp["suppress_mr_short"] and SHORT in effective_directions:
            effective_directions.remove(SHORT)
        if not effective_directions:
            return  # All directions suppressed

        # ── P0 修复 (2026-05-31): 方向对齐锁 — 防止 scalping 与主管线反向开仓 ──
        # 数据证据: 方向一致 avg_pnl=+0.59%, 方向不一致 avg_pnl=-25.3%
        # 原理: 查询 decisions.db 最近 30 分钟内同标的的决策方向,
        # 如果主管线明确看多(strength>50), scalping 不应做空, 反之亦然
        try:
            _main_dir = self._get_main_pipeline_direction(symbol)
            if _main_dir in ("LONG", "SHORT"):
                _opposite = "SHORT" if _main_dir == "LONG" else "LONG"
                if _opposite in effective_directions:
                    effective_directions.remove(_opposite)
                    logger.info(
                        f"[Scalping·方向锁] {symbol} 主管线={_main_dir}, "
                        f"移除反向 {_opposite}, 允许={effective_directions}"
                    )
        except Exception as _dir_lock_e:
            logger.debug(f"[Scalping·方向锁] {symbol} 查询失败 (不影响): {_dir_lock_e}")
        
        if not effective_directions:
            return  # All directions suppressed after alignment

        # P6 优化 (2026-05-22): 数据驱动方向偏好
        # 数据：SHORT 胜率 37.0% vs LONG 20.9%，优先选择 SHORT
        direction_preference = self._load_scalping_config().get("direction_preference")
        # 正确路径：strategies.scalping.direction_preference
        if not direction_preference:
            try:
                from app.core.config_container import get_config as get_app_config
                cfg = get_app_config()
                direction_preference = cfg.get_dotted("strategies.scalping.direction_preference")
            except Exception:
                pass
        if direction_preference and direction_preference in effective_directions:
            # 如果偏好方向在允许方向中，优先只交易该方向
            effective_directions = [direction_preference]
            logger.debug(f"[Scalping] 方向偏好 {direction_preference}，过滤其他方向")

        # 信号检测
        # ── 生命周期感知的优先级策略 ──
        vol = data.get("volume", 0)
        avg_vol = data.get("avg_volume", data.get("volume", 0) * 1.5)
        resistance = data.get("bb_upper", data["price"] * 1.02)
        support = data.get("bb_lower", data["price"] * 0.98)

        # ═══════════════════════════════════════════════════════════
        # P1 修复 (2026-05-08): 计算 3 分钟价格动量，用于早期信号
        # 用近 3 根 1 分钟 K 线或价格快照计算短期动量
        # ═══════════════════════════════════════════════════════════
        price_momentum_3m = self._compute_short_momentum(symbol, data.get("price"))

        mom_signal = None
        mr_signal = None

        if lp["momentum_first"]:
            # accelerating: 动量优先
            mom_signal = self.momentum_detector.detect(
                price=data["price"], volume=vol, avg_volume=avg_vol,
                resistance=resistance, support=support,
                rsi_14=data.get("rsi_14", 50),
                allowed_directions=effective_directions,
            )
            if not mom_signal:
                mr_signal = self.signal_detector.detect(
                    price=data["price"], rsi_14=data.get("rsi_14", 50),
                    vwap=data.get("vwap", data["price"]),
                    bb_upper=data.get("bb_upper", data["price"] * 1.02),
                    bb_lower=data.get("bb_lower", data["price"] * 0.98),
                    allowed_directions=effective_directions, vix=data.get("vix"),
                    price_momentum_3m=price_momentum_3m,
                )
        else:
            # converging/turning/fading: 均值回归优先
            mr_signal = self.signal_detector.detect(
                price=data["price"], rsi_14=data.get("rsi_14", 50),
                vwap=data.get("vwap", data["price"]),
                bb_upper=data.get("bb_upper", data["price"] * 1.02),
                bb_lower=data.get("bb_lower", data["price"] * 0.98),
                allowed_directions=effective_directions, vix=data.get("vix"),
                price_momentum_3m=price_momentum_3m,
                # P0/P1 新增 (2026-05-09): 订单流/多时间框架/逐笔成交
                order_book=data.get("order_book"),
                order_book_cfg=getattr(self, '_order_book_cfg', {}),
                k5m_trend=data.get("k5m_trend"),
                multi_tf_cfg=getattr(self, '_multi_tf_cfg', {}),
                ticker_flow=data.get("ticker_flow"),
                ticker_cfg=getattr(self, '_ticker_cfg', {}),
            )
            if not mr_signal:
                mom_signal = self.momentum_detector.detect(
                    price=data["price"], volume=vol, avg_volume=avg_vol,
                    resistance=resistance, support=support,
                    rsi_14=data.get("rsi_14", 50),
                    allowed_directions=effective_directions,
                )

        if mom_signal:
            signal = {
                "direction": mom_signal["direction"],
                "signals": [mom_signal["type"]],
                "votes": 3 if lifecycle == "accelerating" else 2,
            }
            logger.info(
                f"[ScalpingEngine] 🚀 动量触发 | {symbol} {mom_signal['direction']} "
                f"| {mom_signal['type']} | lifecycle={lifecycle} | Vol: {vol/max(avg_vol,1):.1f}x"
            )
        elif mr_signal:
            signal = mr_signal
        else:
            return

        direction = signal["direction"]

        # RFC-053 (2026-05-30): 设置信号检测冷却 — 防止同symbol+direction 60s内重复触发
        _det_key = f"{symbol}:{direction}"
        self._detection_cooldown[_det_key] = time.time()

        # RFC-051: RSI 极端值 + 个股趋势对齐过滤
        # 根因: SNDK RSI=100 + trend=UP + STRONG_BUY → 生成 SHORT 信号
        # 均值回归策略做空 RSI=100 的强上升趋势股票，历史上胜率极低
        # 规则: RSI≥95 + trend=UP → 禁 SHORT; RSI≤5 + trend=DOWN → 禁 LONG
        _rsi = data.get("rsi_14", data.get("rsi", 50))
        _trend = str(data.get("trend", "")).upper()
        if direction == SHORT and _rsi >= 95 and _trend in ("UP", "STRONG_UP"):
            logger.debug(
                f"[Scalping·RSI趋势对齐] {symbol} RSI={_rsi:.0f} trend={_trend} "
                f"拒绝逆势 SHORT (做空RSI极端+强上升趋势)"
            )
            return
        if direction == LONG and _rsi <= 5 and _trend in ("DOWN", "STRONG_DOWN"):
            logger.debug(
                f"[Scalping·RSI趋势对齐] {symbol} RSI={_rsi:.0f} trend={_trend} "
                f"拒绝逆势 LONG (做多RSI极端+强下降趋势)"
            )
            return

        # ═══════════════════════════════════════════════════════════
        # P1 修复 (2026-05-09): SPY 方向对齐 — 逆趋势交易降仓
        # 原则: 不硬拦截逆趋势交易，但降低仓位控制风险
        # 幻方经验: 均值回归策略在趋势日逆方向交易胜率显著下降
        # ═══════════════════════════════════════════════════════════
        spy_change = data.get("spy_change", 0)
        spy_trend_threshold = 0.005  # SPY 涨跌超过 0.5% 视为有趋势
        if abs(spy_change) > spy_trend_threshold:
            # 判断是否逆 SPY 趋势
            is_counter_trend = False
            if spy_change > 0 and direction == "SHORT":
                is_counter_trend = True  # SPY 上涨但做空
            elif spy_change < 0 and direction == "LONG":
                is_counter_trend = True  # SPY 下跌但做多

            if is_counter_trend:
                # 逆趋势交易: 仓位减半，止损收紧 20%
                params["position_pct"] = params.get("position_pct", 0.08) * 0.5
                # P1 修复 (2026-05-22): 默认值从 0.007 对齐到 YAML base_params 0.020
                params["stop_loss"] = params.get("stop_loss", 0.020) * 0.8
                logger.info(
                    f"[方向对齐] {symbol} {direction} 逆 SPY 趋势 ({spy_change:+.1%}) → "
                    f"仓位减半 + 止损收紧 20%"
                )

        # ── 跨引擎方向冲突检查 (幻方原则: 禁止与主管线反向) ──
        existing_pos = self._get_existing_main_position(symbol)
        if existing_pos:
            main_direction = existing_pos.get("direction", "")
            if main_direction and main_direction != direction:
                logger.info(
                    f"[跨引擎冲突] {symbol} Scalping={direction} vs Main={main_direction} → 拒绝"
                )
                # C5 归因 (2026-05-13): 记录信号冲突供盘后分析
                try:
                    from app.analysis.strategy_attribution import get_attribution
                    get_attribution().record_signal_disagreement(
                        symbol=symbol,
                        main_direction=main_direction,
                        main_strength=None,
                        scalping_direction=direction,
                        scalping_strength=signal.get("votes"),
                        context={"phase": params.get("phase", ""), "price": data.get("price")},
                    )
                except Exception:
                    pass
                return
            # 同向: 允许但减半仓位 (不叠加两个引擎的敞口)
            params["position_pct"] = params.get("position_pct", 0.08) * 0.5
            logger.info(
                f"[跨引擎协同] {symbol} Scalping={direction} 与主管线同向 → 仓位减半"
            )

        # 资金分配检查
        position_size = self._calculate_position_size(
            symbol=symbol,
            price=data["price"],
            position_pct=params.get("position_pct", 0.10),
        )

        if not self.budget.can_allocate(position_size * data["price"]):
            logger.debug(
                f"[ScalpingEngine] {symbol} 资金不足，跳过"
            )
            return

        # VIX 自适应止损已在 TimeGate/SharedKernel 中完成。
        # 此处 sl_pct 直接使用 params["stop_loss"], 不再重复计算。

        # 执行交易 (或记录 observe)
        self._execute_trade(
            symbol=symbol,
            direction=direction,
            price=data["price"],
            position_size=position_size,
            tp_pct=params["take_profit"],
            sl_pct=params["stop_loss"],
            max_hold_minutes=params["max_hold_minutes"],
            signals="/".join(signal["signals"]),
            signal_context=data,  # P0 修复 (2026-05-08): 传递完整信号上下文
        )

    # ============================================================
    # 动量生命周期融合
    # ============================================================

    def _compute_short_momentum(self, symbol: str, current_price: float) -> Optional[float]:
        """
        计算 3 分钟价格动量 (用于 P1 早期信号)

        维护每个标的近 3 个价格快照，计算 (current - price_3m_ago) / price_3m_ago
        返回动量比率 (如 0.002 = +0.2%)

        Args:
            symbol: 标的代码
            current_price: 当前价格
        Returns:
            3分钟价格变化率，或 None (数据不足时)
        """
        if not hasattr(self, '_price_history'):
            self._price_history: Dict[str, list] = {}

        if symbol not in self._price_history:
            self._price_history[symbol] = []

        history = self._price_history[symbol]
        history.append(current_price)

        # 只保留最近 4 个快照 (当前 + 过去 3 个)
        if len(history) > 4:
            history.pop(0)

        # 需要至少 3 个历史快照才能计算 3 分钟动量
        if len(history) < 4:
            return None

        price_3m_ago = history[0]  # 3 个快照前的价格
        if price_3m_ago <= 0:
            return None

        momentum = (current_price - price_3m_ago) / price_3m_ago
        return momentum

    def _update_lifecycle(self, symbol: str, price: float) -> Optional[str]:
        """
        更新动量生命周期状态并返回当前阶段

        Returns: "accelerating" | "coasting" | "fading" |
                 "declining" | "converging" | "turning_up" |
                 "turning_down" | "idle" | None (insufficient data)
        """
        if symbol not in self._lifecycle_state:
            self._lifecycle_state[symbol] = {
                "closes": [], "momentum": [], "prev_momentum": 0.0,
            }
        state = self._lifecycle_state[symbol]

        state["closes"].append(price)
        if len(state["closes"]) > self._lifecycle_max_closes:
            state["closes"] = state["closes"][-self._lifecycle_max_closes:]

        # Need enough data for double EMA
        min_len = self._lifecycle_ema_n * 2 + 1
        if len(state["closes"]) < min_len:
            return None

        closes = state["closes"]
        n = self._lifecycle_ema_n
        k = 2.0 / (n + 1)

        # Layer 1 EMA
        ema1 = [closes[0]]
        for c in closes[1:]:
            ema1.append(c * k + ema1[-1] * (1 - k))

        # Layer 2 EMA
        ema2 = [ema1[0]]
        for v in ema1[1:]:
            ema2.append(v * k + ema2[-1] * (1 - k))

        if len(ema2) < 2:
            return None

        prev = ema2[-2]
        curr = ema2[-1]
        if prev <= 0:
            return None

        momentum = (curr / prev - 1.0) * 1000.0
        prev_momentum = state["prev_momentum"]
        state["prev_momentum"] = momentum
        state["momentum"].append(momentum)
        if len(state["momentum"]) > 50:
            state["momentum"] = state["momentum"][-50:]

        # Classify
        if prev_momentum <= 0 and momentum > 0:
            return "turning_up"
        if prev_momentum >= 0 and momentum < 0:
            return "turning_down"
        if momentum > 0:
            return "accelerating" if momentum > prev_momentum else "coasting"
        if momentum < 0:
            return "declining" if momentum < prev_momentum else "converging"
        return "idle"

    def _lifecycle_signal_priority(self, lifecycle: Optional[str]) -> Dict[str, Any]:
        """
        根据生命周期返回信号优先级和方向偏好

        原则:
          accelerating → 趋势跟随优先 (MomentumDetector)
          converging   → 均值回归优先 (SignalDetector), 偏多
          turning      → 均值回归优先, 方向锁
          declining    → 只做空
          fading       → 均值回归优先, 偏空
        """
        result = {
            "momentum_first": False,    # True = 优先动量检测
            "direction_bias": None,     # "LONG" / "SHORT" / None
            "suppress_mr_long": False,  # 抑制均值回归 LONG
            "suppress_mr_short": False, # 抑制均值回归 SHORT
        }
        if lifecycle is None:
            return result

        if lifecycle == "accelerating":
            result["momentum_first"] = True
            result["direction_bias"] = "LONG"
        elif lifecycle == "coasting":
            result["momentum_first"] = False
        elif lifecycle == "fading":
            result["suppress_mr_long"] = True
            result["direction_bias"] = "SHORT"
        elif lifecycle == "declining":
            result["suppress_mr_long"] = True
            result["direction_bias"] = "SHORT"
        elif lifecycle == "converging":
            result["momentum_first"] = False
            result["direction_bias"] = "LONG"
            result["suppress_mr_short"] = True
        elif lifecycle in ("turning_up",):
            result["momentum_first"] = False
            result["direction_bias"] = "LONG"
        elif lifecycle in ("turning_down",):
            result["momentum_first"] = False
            result["direction_bias"] = "SHORT"

        return result

    def _fetch_market_data(self, symbol: str) -> Optional[Dict]:
        """
        获取标的行情数据
        
        需要返回:
          price, rsi_14, vwap, bb_upper, bb_lower
        """
        if not self.data_router:
            return None

        try:
            # P0 修复 (2026-05-05): get_realtime_price 不存在, 用 get_full_market_context 统一获取
            ctx = self.data_router.get_full_market_context(symbol)
            if not ctx:
                # 降级: 尝试 get_quote
                quote = self.data_router.get_quote(symbol) if hasattr(self.data_router, 'get_quote') else None
                if not quote or quote.get("price", 0) <= 0:
                    return None
                price = quote["price"]
                indicators = {}
                volume = quote.get("volume", 0)
                avg_volume = quote.get("avg_volume", 0)
            else:
                price = ctx.get("price", ctx.get("last_price", 0))
                if not price or price <= 0:
                    return None

                # P1 修复 (2026-05-06): 直接从 ctx 提取指标, 不再调用 get_technical_indicators
                # 该函数内部会再次调用 get_full_market_context → get_kline, 造成双倍 API 请求
                # ctx 已包含 LV3 计算的全部指标: rsi/macd/bb_upper/bb_lower/vwap/atr
                indicators = {
                    "rsi_14": ctx.get("rsi", 50),
                    "vwap": ctx.get("vwap", price),
                    "bb_upper": ctx.get("bb_upper", ctx.get("boll_upper", price * 1.02)),
                    "bb_lower": ctx.get("bb_lower", ctx.get("boll_lower", price * 0.98)),
                    "atr": ctx.get("atr", ctx.get("atr_14", 0)),  # P2 新增: ATR 波动率
                }
                volume = ctx.get("volume", 0)
                avg_volume = ctx.get("avg_volume", 0)

            # 获取大盘数据
            spy_change = 0
            vix = None
            try:
                # P1 修复 (2026-04-30): 实际计算 SPY 涨跌幅
                spy_ctx = self.data_router.get_spy_context() if hasattr(self.data_router, 'get_spy_context') else {}
                spy_price = spy_ctx.get("spy_price", 0)
                spy_prev_close = spy_ctx.get("spy_prev_close", 0)
                if spy_price > 0 and spy_prev_close > 0:
                    spy_change = (spy_price - spy_prev_close) / spy_prev_close
                elif spy_ctx.get("spx_change_percent"):
                    spy_change = spy_ctx["spx_change_percent"] / 100.0

                # P1 修复 (2026-04-30): 实际获取 VIX
                if hasattr(self.data_router, 'get_vix'):
                    vix = self.data_router.get_vix()
            except Exception:
                pass

            # ═══════════════════════════════════════════════════════════
            # P0/P1 新增 (2026-05-09): 订单流/多时间框架/逐笔成交数据
            # ═══════════════════════════════════════════════════════════
            order_book = None
            k5m_trend = None
            ticker_flow = None

            if hasattr(self.data_router, 'futu') and self.data_router.futu:
                try:
                    # 订单流深度 (异步缓存，延迟 <100ms)
                    order_book = self.data_router.futu.get_order_book(symbol)
                except Exception:
                    pass

                try:
                    # 5 分钟趋势判断
                    k5m_data = self.data_router.futu.get_kline(symbol, ktype="K_5M", count=20)
                    if k5m_data and len(k5m_data) >= 5:
                        closes = [k['close'] for k in k5m_data if 'close' in k]
                        if len(closes) >= 5:
                            # 简单趋势判断: 最近 5 根 K 线的高低点
                            recent_5 = closes[-5:]
                            if recent_5[-1] > recent_5[0] * 1.001:
                                k5m_trend = "UP"
                            elif recent_5[-1] < recent_5[0] * 0.999:
                                k5m_trend = "DOWN"
                            else:
                                k5m_trend = "NEUTRAL"
                except Exception:
                    pass

                try:
                    # 逐笔成交分析
                    tickers = self.data_router.futu.get_recent_tickers(symbol, count=50)
                    if tickers:
                        threshold = self._ticker_cfg.get("large_order_threshold", 10000)
                        large_buy = sum(1 for t in tickers if t['volume'] >= threshold and t['direction'] == 'BUY')
                        large_sell = sum(1 for t in tickers if t['volume'] >= threshold and t['direction'] == 'SELL')
                        ticker_flow = {
                            "large_buy_count": large_buy,
                            "large_sell_count": large_sell,
                            "total_count": len(tickers),
                        }
                except Exception:
                    pass

            return {
                "price": price,
                "rsi_14": indicators.get("rsi_14", 50),
                "vwap": indicators.get("vwap", price),
                "bb_upper": indicators.get("bb_upper", price * 1.02),
                "bb_lower": indicators.get("bb_lower", price * 0.98),
                "atr": indicators.get("atr", 0),  # P2 新增: ATR 波动率
                "volume": volume,
                "avg_volume": avg_volume,
                "spy_change": spy_change,
                "vix": vix,
                # P0/P1 新增 (2026-05-09): 订单流/多时间框架/逐笔成交
                "order_book": order_book,
                "k5m_trend": k5m_trend,
                "ticker_flow": ticker_flow,
            }
        except Exception as e:
            logger.debug(f"[ScalpingEngine] {symbol} 行情获取异常: {e}")
            return None

    def _calculate_position_size(
        self, symbol: str, price: float, position_pct: float
    ) -> int:
        """
        计算仓位 (P0 修复: 严格限制单笔上限 + 水位感知缩放)
        
        公式: min(
            available_budget × position_pct × risk_mult × pos_mult / price,
            max_shares_by_limit
        )
        
        P1 修复 (2026-05-08): 注入水位乘法，对齐主管线 calc_position_size 逻辑
          GREEN  → 100% 仓位
          YELLOW → 50% 仓位
          ORANGE → 25% 仓位
          RED    → 0% (禁止开仓)
        """
        available = self.budget.get_available_budget()
        
        # ── 水位感知缩放 (P1 2026-05-08) ──
        risk_mult = 1.0
        pos_mult = 1.0
        water_level = "UNKNOWN"
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            risk_mult = capital.get_risk_multiplier()
            pos_mult = capital.get_position_multiplier()
            water_level = capital._state.water_level.value if hasattr(capital._state, 'water_level') else "UNKNOWN"
        except Exception as e:
            logger.debug(f"[PositionSizer] 水位获取失败 (使用默认 1.0): {e}")
        
        # 水位缩放后的有效仓位比例
        effective_pct = position_pct * risk_mult * pos_mult
        
        size_by_budget = int((available * effective_pct) / max(price, 0.01))
        
        # 严格硬性上限: 绝不超过有效预算的 effective_pct
        max_allowed_cost = available * effective_pct
        max_shares_by_limit = int(max_allowed_cost / max(price, 0.01))
        
        final_size = min(size_by_budget, max_shares_by_limit)
        
        # 记录计算明细，便于排查仓位异常
        logger.info(
            f"[PositionSizer] {symbol} | price=${price:.2f} | "
            f"available=${available:,.0f} | pct={position_pct:.0%} | "
            f"wl={water_level} | risk_mult={risk_mult:.2f} | pos_mult={pos_mult:.2f} | "
            f"effective_pct={effective_pct:.0%} | "
            f"calc={size_by_budget} | hard_cap={max_shares_by_limit} → {final_size}"
        )

        # 确保最小 1 股 (水位 RED 时 final_size=0 保持 0)
        return max(1, final_size) if final_size > 0 else 0

    def _execute_trade(
        self,
        symbol: str,
        direction: str,
        price: float,
        position_size: int,
        tp_pct: float,
        sl_pct: float,
        max_hold_minutes: int,
        signals: str,
        signal_context: Dict = None,  # P0 修复 (2026-05-08): 信号上下文
    ):
        """
        执行交易 (或 observe 记录)
        """
        # B.2 修复 (2026-05-19): 排除低价股 (< $20) — 摩擦吞利灾难.
        # 5/12-5/18 数据: LCID($5.75) 21 笔 4.8% 胜率 -$656; TME($9) 15 笔 0% 胜率 -$442.
        # 低价股 round-trip 摩擦 0.06-0.09%, TP 距离仅 0.4% — 利润空间不够覆盖摩擦.
        if price < 20.0:
            logger.warning(
                f"🚫 [Scalping·低价股门] {symbol} ${price:.2f} < $20 拒绝入场 "
                f"(scalping 摩擦灾难, 历史 LCID/TME 0-5% 胜率)"
            )
            return {"success": False, "error": "price_too_low"}

        # C.1 修复 (2026-05-19): regime=TREND_DOWN 时禁 LONG.
        # 5/12-5/18 LONG 73 笔 2.7% 胜率 -$3196 vs SHORT 44 笔 15.9% +$1444.
        # 系统在熊市被强迫做多 (scalping 默认 LONG 偏向). regime gate 强制方向匹配.
        if direction == "LONG" and signal_context:
            _regime = str(signal_context.get("regime") or signal_context.get("market_regime") or "").upper()
            if _regime in ("TREND_DOWN", "DOWNTREND", "BEAR"):
                logger.warning(
                    f"🚫 [Scalping·regime门] {symbol} LONG 被禁 (regime={_regime}, "
                    f"5/12-18 数据: 熊市做多胜率 2.7%)"
                )
                return {"success": False, "error": "long_in_downtrend"}
            # RFC-053 (2026-05-30): RANGE regime 也禁 LONG
            # 数据: LONG+RANGE 22笔 9.1%WR -$3,058
            if _regime in ("RANGE", "SIDEWAYS", "NEUTRAL"):
                logger.warning(
                    f"🚫 [Scalping·regime门] {symbol} LONG 被禁 (regime={_regime}, "
                    f"无趋势做多 9.1% WR)"
                )
                return {"success": False, "error": "long_in_range"}

        # F1 (2026-05-16): 反向开仓冷却 — 防 whipsaw
        # 短线尤其严重: 5/14 SNDK SHORT→LONG 90s, 5/15 CRM 173s, LCID 127s
        try:
            from app.pipeline.order_executor import _check_reversal_cooldown
            from app.core.models import Direction as _DirEnum
            _dir_enum = _DirEnum.LONG if direction == "LONG" else _DirEnum.SHORT
            sched = getattr(self, "scheduler", None)
            if sched is not None and not _check_reversal_cooldown(sched, symbol, _dir_enum):
                return {"success": False, "error": "reversal_cooldown"}
        except Exception as _f1e:
            logger.debug(f"[F1·反向冷却·scalping] 异常 (放行): {_f1e}")

        phase = self.time_gate.get_current_phase()

        # 获取当前阶段的 scaler（用于 ATR 阶段自适应）
        # Task #9 (2026-05-30): SSOT 依然是 time_gate.cfg (unified_config.scalping.phases),
        #   仅在 time_gate 未提供时从 exit_policy.yaml::scalping_overrides.phase_scaling 读取兜底
        phase_tp_scale = 1.0
        phase_sl_scale = 1.0
        if self.time_gate and phase and phase != "closed":
            p = self.time_gate.cfg.get(phase, {})
            phase_tp_scale = p.get("tp_scale", None)
            phase_sl_scale = p.get("sl_scale", None)
            if phase_tp_scale is None or phase_sl_scale is None:
                from app.scalping.exits import get_phase_scaling
                fb_tp, fb_sl = get_phase_scaling(phase)
                phase_tp_scale = phase_tp_scale if phase_tp_scale is not None else fb_tp
                phase_sl_scale = phase_sl_scale if phase_sl_scale is not None else fb_sl

        # 计算止盈止损价格 (使用当前时段参数, 非初始化默认值)
        # ═══════════════════════════════════════════════════════════
        # P2 修复 (2026-05-08): ATR 自适应退出 — 优先使用 ATRExit
        # 从 signal_context 提取 ATR 值，不可用时回退到 FixedExit
        # ═══════════════════════════════════════════════════════════
        ctx = signal_context or {}
        atr_value = ctx.get("atr", ctx.get("atr_14", 0))
        exit_mode = "fixed"  # 默认模式

        if self.use_atr_exit and self.atr_exit and atr_value and atr_value > 0:
            # 使用 ATR 自适应退出（含 TimeGate 阶段 scaler）
            levels = self.atr_exit.calculate_levels(
                price, direction, atr_value,
                phase_tp_scale=phase_tp_scale,
                phase_sl_scale=phase_sl_scale,
            )
            exit_mode = "atr"
            logger.info(
                f"[ATR退出] {symbol} | ATR={atr_value:.3f} | "
                f"phase={phase} (tp_scale={phase_tp_scale}x, sl_scale={phase_sl_scale}x) | "
                f"TP={levels['take_profit']:.2f} ({levels['tp_pct']:.2%}) | "
                f"SL={levels['stop_loss']:.2f} ({levels['sl_pct']:.2%}) | "
                f"盈亏比={levels['risk_reward_ratio']}:1"
            )
        else:
            # 回退到 FixedExit (时段参数)
            self.fixed_exit.update_params(tp_pct, sl_pct)
            levels = self.fixed_exit.calculate_levels(price, direction)
            if self.use_atr_exit:
                logger.debug(
                    f"[ATR回退] {symbol} ATR={atr_value}，使用 FixedExit "
                    f"TP={tp_pct:.2%} SL={sl_pct:.2%}"
                )

        # 在 levels 中记录退出模式，供 _monitor_exits 使用
        levels["exit_mode"] = exit_mode
        # ═══════════════════════════════════════════════════════════

        # 记录统计
        from app.scalping.stats import TradeRecord
        trade = TradeRecord(
            symbol=symbol,
            direction=direction,
            entry_price=price,
            take_profit=levels["take_profit"],
            stop_loss=levels["stop_loss"],
            signals=signals,
            phase=phase,
            quantity=position_size,
        )
        self.stats_tracker.record_entry(trade)

        # P1 修复 (2026-05-06): 设置信号冷却时间戳
        with self._lock:
            self._signal_cooldown[symbol] = time.time()

        # DEBUG (2026-05-07): 检查mode值
        logger.info(f"[ScalpingEngine] [DEBUG] _execute_trade 到达mode检查点 | mode={self.mode} | symbol={symbol}")

        # P0 融合修复 (2026-05-27): 反向持仓冲突检查
        # 根因: position_monitor 从 broker 导入 QCOM LONG → scalping 引擎不知道 →
        # 尝试 SHORT → broker 拒绝 "持仓不足" (模拟盘不支持裸卖空已有 LONG 的标的).
        # 修复: 下单前检查 position_store 是否已有反向持仓 → 跳过.
        try:
            _ps = getattr(self, "scheduler", None)
            if _ps and hasattr(_ps, "position_store") and _ps.position_store:
                _existing = _ps.position_store.get(symbol)
                if _existing:
                    _exist_dir = getattr(_existing, "direction", None)
                    _exist_dir_str = _exist_dir.value if hasattr(_exist_dir, "value") else str(_exist_dir) if _exist_dir else ""
                    if _exist_dir_str and _exist_dir_str.upper() != direction.upper():
                        logger.warning(
                            f"🚫 [Scalping·反向冲突] {symbol} 已有 {_exist_dir_str} 持仓 "
                            f"(position_monitor 导入), 拒绝 scalping {direction}"
                        )
                        return {"success": False, "error": "opposing_position_exists"}
        except Exception as _opp_err:
            logger.debug(f"[Scalping·反向检查] 异常 (放行): {_opp_err}")

        # 统一交易记录 (Karpathy: 所有来源统一落库)
        # P0 修复 (2026-05-07): 仅在下单成功后写入 trade_store，防止被风控拦截/下单失败时产生幽灵记录
        record_id = None
        signal_record = None
        try:
            from app.storage.unified_trade_store import (
                UnifiedTradeRecord, SOURCE_SCALPING,
                ENTRY_RSI, ENTRY_VWAP, ENTRY_BOLLINGER,
            )
            entry_reasons = signals.split("/")
            # P0 修复 (2026-05-08): 从 signal_context 提取真实指标值，不再硬编码 0
            ctx = signal_context or {}
            rsi_val = ctx.get("rsi_14", 0)
            vwap_val = ctx.get("vwap", 0)
            bb_upper_val = ctx.get("bb_upper", 0)
            bb_lower_val = ctx.get("bb_lower", 0)
            vix_val = ctx.get("vix", 0)
            lifecycle = ctx.get("lifecycle", "unknown")

            if self.trade_store:
                # O4/O5 (2026-05-16): 从 sandbox / signal_context 读 mode + final_strength
                _mode_val = "sandbox" if (signal_context or {}).get("sandbox_mode") else "normal"
                try:
                    from app.core.config_container import get_config as _gc
                    if _gc().get_dotted("mode.sandbox.enabled", False):
                        _mode_val = "sandbox"
                except Exception:
                    pass
                _final_strength = int(len(entry_reasons) * 33)
                # T1 (2026-05-16): scalping 入场生成 trace_id, 写入 UnifiedTradeRecord
                # 5/15 22/26 trades trace_id="" — 复盘无法追溯到信号/因子.
                # 格式与主线一致: US-YYYYMMDD-XXXXXX (6 字符 hex)
                # 若 signal_context 已有 trace_id (上游传入) 则复用, 否则新生成.
                import uuid as _uuid
                _trace_id = (signal_context or {}).get("trace_id") or (
                    f"US-{now_et().strftime('%Y%m%d')}-"
                    f"{_uuid.uuid4().hex[:6].upper()}"
                )
                signal_record = UnifiedTradeRecord(
                    symbol=symbol,
                    direction=direction,
                    source_type=SOURCE_SCALPING,
                    entry_price=price,
                    quantity=position_size,
                    entry_reason=entry_reasons,
                    signal_strength=_final_strength,
                    signal_confidence=len(entry_reasons) / 3.0,
                    signal_details={
                        "rsi_14": rsi_val,
                        "vwap": vwap_val,
                        "bb_upper": bb_upper_val,
                        "bb_lower": bb_lower_val,
                        "vix": vix_val,
                        "phase": phase,
                        "lifecycle": lifecycle,
                    },
                    metadata={
                        "scalping_mode": self.mode,
                        "take_profit": levels["take_profit"],
                        "stop_loss": levels["stop_loss"],
                        "tp_pct": tp_pct,
                        "sl_pct": sl_pct,
                        "max_hold_minutes": max_hold_minutes,
                    },
                    # O4/O5: 补齐 owner_strategy + final_strength + market_regime + mode
                    owner_strategy="scalping",
                    final_strength=_final_strength,
                    consensus_level=len(entry_reasons) / 3.0,
                    market_regime=str(ctx.get("regime") or ctx.get("market_regime") or "unknown"),
                    mode=_mode_val,
                    strategy_id="scalping",
                    # T1 (2026-05-16): trace_id 串起全链路 (mirror_entry/exit, attribution, ...)
                    trace_id=_trace_id,
                )
                # T1: 把 trace_id 回写 signal_context, 供后续 mirror_entry/exit + close_trade 复用
                if signal_context is not None:
                    signal_context["trace_id"] = _trace_id
        except Exception as e:
            logger.warning(f"[ScalpingEngine] 交易记录构造失败 (不影响下单): {e}")

        # Observe 模式: 仅记录 (写入 trade_store 用于跟踪)
        if self.mode == "observe":
            # P0 修复 (2026-05-07): observe 模式下插入 trade_store 记录
            if self.trade_store and signal_record:
                try:
                    record_id = self.trade_store.insert(signal_record)
                except Exception as e:
                    logger.warning(f"[ScalpingEngine] observe 模式记录失败: {e}")

            logger.info(
                f"[ScalpingEngine] 👁️ OBSERVE | "
                f"{symbol} {direction} @ ${price:.2f} | "
                f"TP: ${levels['take_profit']:.2f} | "
                f"SL: ${levels['stop_loss']:.2f} | "
                f"数量: {position_size} | "
                f"信号: {signals}"
            )
            # Observe 模式下模拟跟踪
            with self._lock:
                self._active_trades[symbol] = {
                    "trade": trade,
                    "levels": levels,
                    "entry_time": now_et(),
                    "max_hold_minutes": max_hold_minutes,
                    "direction": direction,
                    "entry_price": price,
                    "position_size": position_size,
                    "tp_pct": tp_pct,
                    "sl_pct": sl_pct,
                    "is_observe": True,
                    "record_id": record_id,
                    "signal_record": signal_record,
                    "signal_context": signal_context or {},  # P0 修复 (2026-05-08): 保存信号上下文供 _close_trade 回写 signal_outcomes
                    # P0 新增 (2026-05-09): 轻量追踪止损
                    "peak_price": price,
                }
            return

        # Active 模式: 实际下单
        logger.info(f"[ScalpingEngine] [DEBUG] ACTIVE模式路径 | symbol={symbol} | lock={'yes' if self._open_position_lock else 'no'}")
        # P0 修复 (2026-04-30): 跨引擎互斥锁 + position_store 预占位
        lock = self._open_position_lock
        if lock:
            lock.acquire()
            logger.info(f"[ScalpingEngine] [DEBUG] 锁获取成功 | symbol={symbol}")
        try:
            # P0 二次风控门禁 — 严格校验实际成本是否超出单笔限制
            actual_cost = price * position_size
            max_allowed = self.budget.get_total_budget() * 0.10  # 硬编码 10% 单笔上限
            if actual_cost > max_allowed:
                logger.critical(
                    f"🚫 [Scalping 风控] {symbol} 单笔成本超限拒绝! "
                    f"实际: ${actual_cost:,.0f} > 上限: ${max_allowed:,.0f}"
                )
                return

            if not self._verify_global_risk_gate(symbol, direction, price, position_size):
                logger.warning(f"🚫 [Scalping 风控] {symbol} 未通过全局风控，拒绝下单")
                return

            self._register_in_position_store(
                symbol, direction, price, position_size,
                tp_price=levels["take_profit"], sl_price=levels["stop_loss"],
                max_hold_minutes=max_hold_minutes,
            )
        finally:
            if lock:
                lock.release()

        try:
            # 下单在锁外进行 (网络调用可能耗时数秒，不宜持锁)
            order_result = self._place_order(
                symbol=symbol,
                direction=direction,
                price=price,
                quantity=position_size,
                signals=signals,  # RFC-053: 传递信号投票
            )

            if order_result and order_result.get("success"):
                # P0 修复 (2026-05-07): 等待订单成交确认后再写入 trade_store
                # 模拟盘 execution_engine 已修复：submit_order 直接返回 FILLED 状态
                # 实盘 SENT 状态需要轮询 Futu 确认成交
                filled = self._wait_for_fill(order_result, timeout=15)
                if not filled:
                    logger.warning(
                        f"[ScalpingEngine] {symbol} 订单未成交 (超时)，不写入 trade_store"
                    )
                    self._unregister_from_position_store(symbol)
                    return

                # 成交确认后才写入 trade_store，防止产生幽灵记录
                if self.trade_store and signal_record:
                    try:
                        record_id = self.trade_store.insert(signal_record)
                        logger.info(f"[ScalpingEngine] trade_store 记录 | ID={record_id} | {symbol} {direction}")
                    except Exception as e:
                        logger.warning(f"[ScalpingEngine] trade_store 写入失败 (不影响交易): {e}")

                # 分配资金
                cost = price * position_size
                self.budget.allocate(cost)

                # P0 修复 (2026-05-09): 成交后同步资本安全水位 + 刷新 budget
                # 与港股修复逻辑对齐: executor.py 中的 _sync_water_level_capital()
                try:
                    from app.risk.account_refresher import get_account_refresher
                    refresher = get_account_refresher()

                    # 注入 broker_adapter (仅首次)
                    if refresher._broker is None and self.execution_engine:
                        adapter = getattr(self.execution_engine, "broker_adapter", None)
                        if adapter and adapter.is_connected():
                            refresher._broker = adapter

                    # 刷新账户并同步水位
                    if refresher._broker and refresher._broker.is_connected():
                        account_info = refresher.refresh()
                        if account_info and account_info.get("buying_power", 0) > 0:
                            from app.risk.capital_safety import get_capital_safety_system
                            capital = get_capital_safety_system()
                            capital.update_account(
                                current_equity=account_info.get("total_assets", 0),
                                cash=account_info.get("cash", 0),
                                market_value=account_info.get("market_val", 0),
                                buying_power=account_info.get("buying_power", 0),
                            )
                            total = account_info.get("total_assets", 0)
                            bp = account_info.get("buying_power", 0)
                            level = capital.get_state().get("level", "UNKNOWN") if hasattr(capital, "get_state") else "OK"
                            logger.info(
                                f"[Scalping 资金同步] 成交后水位刷新: "
                                f"equity=${total:,.0f} | buying_power=${bp:,.0f} | level={level}"
                            )

                            # 同时刷新 scalping budget 以反映真实可用资金
                            if self.budget:
                                self.budget.update_total_equity(total)
                                logger.debug(
                                    f"[Scalping 资金同步] Budget 刷新: "
                                    f"总资金=${total:,.0f} | scalping_budget=${self.budget._budget:,.0f}"
                                )
                except Exception as e:
                    logger.debug(f"[Scalping 资金同步] 刷新失败 (不影响交易): {e}")

                # 记录活跃交易
                with self._lock:
                    self._active_trades[symbol] = {
                        "trade": trade,
                        "levels": levels,
                        "entry_time": now_et(),
                        "max_hold_minutes": max_hold_minutes,
                        "direction": direction,
                        "entry_price": price,
                        "position_size": position_size,
                        "order_id": order_result.get("order_id"),
                        "record_id": record_id,
                        "signal_record": signal_record,
                        "is_observe": False,
                        "signal_context": signal_context or {},  # P0 修复 (2026-05-08): 保存信号上下文供 _close_trade 回写 signal_outcomes
                        # P0 新增 (2026-05-09): 轻量追踪止损
                        "peak_price": price,  # 初始 peak = entry
                    }

                logger.info(
                    f"[ScalpingEngine] 🚀 ACTIVE | "
                    f"{symbol} {direction} @ ${price:.2f} | "
                    f"TP: ${levels['take_profit']:.2f} | "
                    f"SL: ${levels['stop_loss']:.2f} | "
                    f"数量: {position_size} | "
                    f"信号: {signals}"
                )

                # O2 (2026-05-16): Scalping 入场也镜像 (5/15 ARM SHORT +$1219 / SNDK SHORT +$195 都缺 shadow)
                try:
                    from app.evolution.shadow_mirror import get_shadow_mirror
                    _trace_id = (signal_context or {}).get("trace_id") or order_result.get("order_id") or f"scalp_{symbol}_{int(time.time())}"
                    get_shadow_mirror().mirror_entry(
                        real_trace_id=str(_trace_id),
                        symbol=symbol,
                        direction=direction,
                        entry_price=float(price),
                        quantity=int(position_size),
                        signal_strength=int((signal_context or {}).get("signal_strength", 0) or 0),
                        signal_confidence=float((signal_context or {}).get("signal_confidence", 0) or 0),
                        owner_strategy="scalping",
                    )
                except Exception as _sme:
                    logger.debug(f"[ShadowMirror·Scalping] entry 失败 (不影响实盘): {_sme}")

                # T3 (2026-05-16): Scalping 入场也写 trade_attribution (5/15 仅主线 4 行, 22 笔短线缺)
                # 含 signal_tags=entry_reasons (rsi/vwap/bollinger), 复盘可分解短线因子贡献
                try:
                    import sqlite3 as _sql3_t3
                    from app.core.paths import get_trade_records_db as _get_t3db
                    from app.pipeline.order_executor import _insert_trade_attribution as _ins_attr_t3
                    _ctx_t3 = signal_context or {}
                    _factor_t3 = {
                        "trend": {"raw": (_ctx_t3.get("trend_strength", 0) or 0), "conf": 1.0},
                        "momentum": {"raw": (_ctx_t3.get("momentum_score", 0) or 0), "conf": 1.0},
                        "volume": {"raw": (_ctx_t3.get("volume_ratio", 0) or 0), "conf": 1.0},
                    }
                    _regime_t3 = str(_ctx_t3.get("regime") or _ctx_t3.get("market_regime") or "unknown")
                    _session_t3 = str(_ctx_t3.get("session") or phase or "")
                    _vix_t3 = float(_ctx_t3.get("vix", 0) or 0)
                    _sigtags_t3 = "/".join(entry_reasons) if entry_reasons else "scalping"
                    _conn_t3 = _sql3_t3.connect(str(_get_t3db()))
                    try:
                        _cur_t3 = _conn_t3.cursor()
                        _cur_t3.execute(
                            "SELECT trace_id FROM trade_attribution WHERE trace_id = ?",
                            (_trace_id,)
                        )
                        if not _cur_t3.fetchone():
                            _ins_attr_t3(
                                _cur_t3, _trace_id, symbol,
                                _final_strength, len(entry_reasons) / 3.0,
                                factor_detail=_factor_t3,
                                owner_strategy="scalping",
                                direction=direction,
                                entry_time=now_et().isoformat(),
                                entry_price=float(price),
                                quantity=int(position_size),
                                regime=_regime_t3,
                                session=_session_t3,
                                vix_at_entry=_vix_t3,
                                signal_tags=_sigtags_t3,
                            )
                            _conn_t3.commit()
                    finally:
                        _conn_t3.close()
                except Exception as _t3e:
                    logger.debug(f"[T3·attribution·scalping] 写入失败 (不影响实盘): {_t3e}")

                # L3 修复 (2026-05-19): scalping 触发 thesis_inferer (LLM 异步推断入场理由)
                # 5/12-5/18 主线仅 6 笔, scalping ~110 笔/天, thesis_inferer 0 触发数据 — LLM 链路空跑.
                # scalping 已自管 ShadowMirror+attribution (T3 上方), 这里仅追加 thesis 异步调用.
                # 失败不阻塞 scalping 主流程 (本身已 daemon Thread 异步).
                try:
                    from app.llm.thesis_inferer import infer_thesis_async
                    _signal_dict = {
                        "trace_id": str(_trace_id),
                        "direction": direction,
                        "strength": int(_ctx_t3.get("signal_strength", _final_strength) or 0),
                        "confidence": float(_ctx_t3.get("signal_confidence", 0) or 0),
                        "factor_detail": _factor_t3,
                    }
                    infer_thesis_async(symbol, _signal_dict, _ctx_t3)
                except Exception as _te:
                    logger.debug(f"[L3·thesis·scalping] {symbol} 触发失败 (不影响实盘): {_te}")

                # 钉钉/飞书通知 (统一入口，与主管线格式一致)
                # P0 修复 (2026-05-09): 统一使用 notify_order_filled，source_type=SCALPING
                if self.notifier:
                    self._notify_scalping_open(symbol, direction, price, position_size, levels, signals)
            else:
                logger.warning(
                    f"[ScalpingEngine] {symbol} 下单失败: {order_result}"
                )
                # P0 修复 (2026-05-13): 设置下单失败冷却，防止无限重试同一错误
                self._order_fail_cooldown[symbol] = time.time()
                logger.warning(
                    f"[ScalpingEngine] {symbol} 设置下单失败冷却 {self._order_fail_cooldown_sec}s"
                )
                # 回滚 position_store 预占位
                self._unregister_from_position_store(symbol)
                # R1 (2026-05-16): 下单失败 — 释放 acquire 拿的 SymbolLock
                self._r1_release_lock_if_held(symbol, signal_context)

        except Exception as e:
            logger.error(f"[ScalpingEngine] {symbol} 下单异常: {e}", exc_info=True)
            # P0 修复 (2026-05-13): 异常也设置冷却
            self._order_fail_cooldown[symbol] = time.time()
            # 回滚 position_store 预占位
            self._unregister_from_position_store(symbol)
            # R1 (2026-05-16): 异常路径同样释放
            self._r1_release_lock_if_held(symbol, signal_context)

    def _r1_release_lock_if_held(self, symbol: str, signal_context: Dict = None):
        """R1 (2026-05-16): scalping 下单失败/异常时释放 SymbolLock.

        正常路径 _close_trade 才释放, 此处覆盖失败路径.
        """
        try:
            if not (signal_context or {}).get("_r1_lock_acquired"):
                return
            sched = getattr(self, "scheduler", None)
            coord = getattr(sched, "coordinator", None) if sched else None
            if coord is not None and getattr(coord, "enabled", False) and not coord.is_observe_only():
                coord.lock.release(symbol, "scalping")
                logger.debug(f"[R1·lock·release·scalping] {symbol} (下单失败) 主动释放")
            # 清标记防重复 release
            if signal_context is not None:
                signal_context["_r1_lock_acquired"] = False
        except Exception as _re:
            logger.debug(f"[R1·release·scalping] 异常: {_re}")

    def _place_order(
        self, symbol: str, direction: str, price: float, quantity: int,
        signals: str = "",  # RFC-053: 传递信号投票用于 strength 计算
    ) -> Optional[Dict]:
        """
        通过 ExecutionEngine 下单

        P1 修复 (2026-05-07): ExecutionEngine.submit_order 接受 Order 对象，不是 kwargs。
        修复前：submit_order(symbol=..., direction=...) → TypeError → 静默失败
        修复后：构造 Order 对象 → submit_order(order) → 真实下单

        P0-X (2026-05-15, 基于 14 日 ARM/BA/ORCL 6 次 "无法同时买多和卖空" 错误):
        下单前查 broker 端持仓, 反向已存在 → 拒下单

        Returns:
            {"success": bool, "order_id": str, ...}
        """
        if not self.execution_engine:
            logger.error("[ScalpingEngine] execution_engine 未配置")
            return None

        # P0-X: 标的级日亏损护栏 (开仓前)
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            blocked, reason = get_symbol_daily_loss_guard().is_blocked(symbol)
            if blocked:
                logger.warning(f"[ScalpingEngine·daily_loss_guard] {symbol} 拒下单: {reason}")
                return {"success": False, "error": f"daily_loss_guard: {reason}"}
        except Exception:
            pass

        # CR-1 + R1 (2026-05-15): 走 risk_engine.pre_trade_check (单一闸门 SoT)
        # R1 重构: 之前 CR-1 只调 PreTradeChecker, 漏了 RiskEngine 包装层 (熔断器三层 + 回撤 + 仓位限制)
        # 现在: 与主线 signal_pipeline.py:896 同一入口, 1 个闸门, 9+3 道检查
        try:
            from app.core.models import SignalScore, Direction as _D
            class _ScalpSig:
                pass
            sig = _ScalpSig()
            sig.symbol = symbol
            sig.direction = _D.LONG if direction == "LONG" else _D.SHORT
            # RFC-053 (2026-05-30): 从信号投票计算 strength, 不再硬编码70
            # 2票(vwap+bb) = 66, 3票(rsi+vwap+bb) = 99, 1票 = 33 (被min 50拦截)
            _votes = len(signals.split("/")) if signals else 2
            sig.strength = min(99, _votes * 33)
            sig.confidence = 0.5 + (_votes * 0.1)  # 2票=0.7, 3票=0.8
            sig.position_pct = (price * quantity / 100000.0) if price > 0 else 0.05
            sig.stop_loss_pct = 0.02
            sig.strategy_id = "scalping"
            sig.metadata = {"_current_positions": [], "owner_strategy": "scalping"}
            sig.is_expired = lambda: False

            # 优先走 risk_engine 包装层 (含熔断器/回撤/仓位限制)
            if self.risk_engine and hasattr(self.risk_engine, "pre_trade_check"):
                allowed = self.risk_engine.pre_trade_check(sig)
                if not allowed:
                    logger.warning(f"[ScalpingEngine·RiskKernel] {symbol} 拒下单 (risk_engine.pre_trade_check)")
                    return {"success": False, "error": "risk_engine.pre_trade_check: rejected"}
            else:
                # 兜底：仅 PreTradeChecker (无 risk_engine 时)
                from app.risk.pre_trade_check import PreTradeChecker
                from app.core.config_container import get_config
                _cfg = get_config()
                _min_strength = _cfg.get_dotted("scheduler.min_signal_strength", 99)
                # P1 修复 (2026-05-22): 传 None 走 MinConfidenceResolver，不再硬编码 0.5 覆盖 YAML
                _ck = PreTradeChecker(min_confidence=None, min_signal_strength=_min_strength)
                _result = _ck.check(sig)
                if not _result.allowed:
                    logger.warning(f"[ScalpingEngine·PreTradeCheck·fallback] {symbol} 拒: {_result.reason}")
                    return {"success": False, "error": f"pre_trade_check: {_result.reason}"}
        except Exception as _ptce:
            logger.debug(f"[ScalpingEngine·RiskKernel] 异常 (放行): {_ptce}")

        # P0-X: broker 端反向持仓检查 (防 "无法同时买多和卖空")
        try:
            broker = getattr(self.execution_engine, "broker_adapter", None)
            if broker and hasattr(broker, "get_positions"):
                positions = broker.get_positions() or []
                for p in positions:
                    p_sym = (p.get("code") if isinstance(p, dict) else getattr(p, "symbol", "")) or ""
                    if str(p_sym).upper() != symbol.upper():
                        continue
                    p_qty = p.get("qty") if isinstance(p, dict) else getattr(p, "qty", 0)
                    if not p_qty:
                        continue
                    p_dir = "LONG" if p_qty > 0 else "SHORT"
                    if p_dir != direction:
                        logger.warning(
                            f"[ScalpingEngine·hedge_guard] {symbol} broker 已有 {p_dir} 仓位 "
                            f"(qty={p_qty}), 拒 {direction} 下单"
                        )
                        return {"success": False, "error": f"hedge_blocked: existing {p_dir} position"}
                    break
        except Exception as _be:
            logger.debug(f"[ScalpingEngine·hedge_guard] 检查异常 (放行): {_be}")

        # P1-A (2026-05-15, 基于 14 日 99% 流量绕过 coordinator 教训):
        # 调 coordinator.arb.arbitrate() 写 signal_arbitration 表 — 让 Phase A 真正生效
        # T4 (2026-05-16): sandbox enforce — SymbolLock 真实阻断重复持仓
        try:
            sched = getattr(self, "scheduler", None)
            coord = getattr(sched, "coordinator", None) if sched else None
            if coord is not None and getattr(coord, "enabled", False):
                from app.strategy.adapters import ScalpingAdapter
                _adapters = getattr(sched, "_strategy_adapters", []) or []
                _scalp_adapter = next((a for a in _adapters if a.name == "scalping"), None)
                if _scalp_adapter is None:
                    _scalp_adapter = ScalpingAdapter()
                # 构造一个最小 signal 对象 (供 arbitrator 使用)
                class _ScalpSignal:
                    pass
                _sig = _ScalpSignal()
                _sig.symbol = symbol
                _sig.direction = direction
                _sig.confidence = 0.7  # 通过 scalping 内部 filter 的信号默认高 conf
                # RFC-053 (2026-05-30): 从信号投票计算 strength
                _votes2 = len(signals.split("/")) if signals else 2
                _sig.strength = min(99, _votes2 * 33)
                _sig.position_pct = 0.05
                _sig.metadata = {"owner_strategy": "scalping", "source": "scalping_engine"}
                # 直接调底层 arbitrator (写审计) — 不阻塞下单
                # T8b: 传 ctx (含 regime) 走 effective_priority
                coord.arb.arbitrate(symbol, [(_scalp_adapter, _sig)], ctx=signal_context or {})

                # T4: enforce 模式下申请 SymbolLock, 被主线占用时立即放弃
                # R1 (2026-05-16): acquire 后任何失败路径都要 release; 仅成功创仓时 lock 归 _close_trade
                _r1_lock_acquired = False
                if not coord.is_observe_only():
                    try:
                        _ok = coord.lock.acquire(symbol, "scalping")
                        if not _ok:
                            _holder = coord.lock.owner(symbol) or "?"
                            logger.warning(
                                f"[T4·SymbolLock·Scalping] {symbol} held by {_holder}, "
                                f"scalping REJECTED — 跳过短线入场 (防多桶重复持仓)"
                            )
                            return {"success": False, "error": f"symbol_locked_by_{_holder}"}
                        _r1_lock_acquired = True
                    except Exception as _le:
                        logger.debug(f"[T4·SymbolLock·Scalping] 异常 (放行): {_le}")
                # R1: 标记到 signal_context, 出口判断释放
                if signal_context is not None and _r1_lock_acquired:
                    signal_context["_r1_lock_acquired"] = True
        except Exception as _ae:
            logger.debug(f"[ScalpingEngine·coordinator] arbitrate 失败 (放行): {_ae}")

        try:
            # 构造 Order 对象
            from app.core.models import Order, Direction as ModelDirection, OrderType
            import uuid

            # P0 fix (2026-05-07): Round price to 2 decimals for Futu API precision requirements
            price = round(price, 2)

            order = Order(
                client_order_id=f"scalping_{symbol}_{uuid.uuid4().hex[:8]}",
                symbol=symbol,
                direction=ModelDirection.SHORT if direction == "SHORT" else ModelDirection.LONG,
                order_type=OrderType.LIMIT,  # scalping 用限价单
                quantity=float(quantity),
                price=price,
            )

            if hasattr(self.execution_engine, "submit_order"):
                result = self.execution_engine.submit_order(order)
                order_id = getattr(result, 'order_id', getattr(result, 'futu_order_id', str(id(result))))
                return {"success": True, "order_id": str(order_id), "result": result}
            else:
                logger.warning(
                    "[ScalpingEngine] execution_engine 不支持 submit_order"
                )
                return None
        except Exception as e:
            logger.error(f"[ScalpingEngine] 下单失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _wait_for_fill(self, order_result: Dict, timeout: int = 15) -> bool:
        """
        等待订单成交确认
        
        P0 修复 (2026-05-07): 模拟盘 execution_engine 已修复为直接 FILLED，
        此方法对模拟盘立即返回 True；实盘轮询 Futu 确认成交。
        
        Returns:
            True 如果订单已成交，False 如果超时
        """
        result = order_result.get("result")
        if result is None:
            logger.warning(f"[ScalpingEngine] _wait_for_fill: result is None")
            return False
        
        # 检查 Order 对象的 state
        order_state = getattr(result, 'state', None)
        symbol = getattr(result, 'symbol', 'unknown')
        
        # 模拟盘: execution_engine 已修复，直接返回 FILLED
        if order_state is not None:
            state_value = getattr(order_state, 'value', str(order_state))
            logger.info(f"[ScalpingEngine] _wait_for_fill: {symbol} state={state_value}")
            if state_value == "FILLED":
                return True
        
        # 实盘/模拟盘连接 Futu: 轮询 Futu 确认成交 (最多 timeout 秒)
        if self.execution_engine and hasattr(self.execution_engine, 'query_order'):
            order_id = order_result.get("order_id")
            if order_id:
                logger.info(f"[ScalpingEngine] _wait_for_fill: {symbol} polling order_id={order_id}")
                start = time.time()
                while time.time() - start < timeout:
                    try:
                        order = self.execution_engine.query_order(order_id)
                        if order and getattr(order, 'state', None):
                            s = getattr(order.state, 'value', str(order.state))
                            logger.info(f"[ScalpingEngine] _wait_for_fill: {symbol} query state={s}")
                            if s == "FILLED":
                                return True
                            elif s in ("REJECTED", "CANCELED", "FAILED"):
                                return False
                    except Exception as e:
                        logger.debug(f"[ScalpingEngine] _wait_for_fill: {symbol} query error: {e}")
                    time.sleep(1)
                logger.warning(f"[ScalpingEngine] _wait_for_fill: {symbol} timeout after {timeout}s")
        
        return False

    def _monitor_active_trades(self, scalping_codes: List[str]):
        """监控活跃交易 (止盈止损/时间止损)"""
        with self._lock:
            symbols_to_remove = []

            for symbol, info in list(self._active_trades.items()):
                trade = info["trade"]
                if trade.exit_price is not None:
                    continue  # 已平仓

                # P0 修复 (2026-05-07): 验证 position_store 中是否有真实持仓
                # 防止未成交订单进入监控循环产生幽灵记录
                if self.risk_engine and hasattr(self.risk_engine, 'position_store'):
                    ps = self.risk_engine.position_store
                    if ps and not ps.has(symbol):
                        logger.debug(f"[ScalpingEngine] {symbol} 无真实持仓，从活跃交易移除")
                        symbols_to_remove.append(symbol)
                        continue

                # 获取当前价格
                try:
                    current_price = self._get_current_price(symbol)
                    if not current_price:
                        continue
                except Exception:
                    continue

                # 检查止盈止损
                levels = info["levels"]
                exit_mode = levels.get("exit_mode", "fixed")

                # ═══════════════════════════════════════════════════
                # P2 修复 (2026-05-08): 根据退出模式选择检查器
                # ATR 模式用 ATRExit.check_exit，否则用 FixedExit
                # ═══════════════════════════════════════════════════
                
                # P0 新增 (2026-05-09): 更新 peak_price 供轻量追踪使用
                peak_price = self._update_peak_price(symbol, current_price, info)
                
                if exit_mode == "atr" and self.atr_exit:
                    # ATR 模式: 注入追踪参数 (追踪开启时才传入 peak_price)
                    if self.trail_enabled:
                        levels["peak_price"] = peak_price
                        levels["trail_mult"] = self.trail_mult
                        levels["min_profit_to_activate"] = self.min_profit_to_activate
                        should_exit, exit_reason = self.atr_exit.check_exit(
                            current_price, levels, peak_price=peak_price
                        )
                    else:
                        should_exit, exit_reason = self.atr_exit.check_exit(
                            current_price, levels
                        )
                else:
                    should_exit, exit_reason = self.fixed_exit.check_exit(
                        current_price, levels
                    )

                if should_exit:
                    # P0 修复 (2026-05-09): 实际提交平仓订单到富途
                    close_direction = "LONG" if info["direction"] == "SHORT" else "SHORT"
                    if self._submit_close_order(symbol, close_direction, current_price, info):
                        self._close_trade(symbol, current_price, exit_reason, info)
                    else:
                        # P0 修复 (2026-05-26): 券商拒绝平仓 (持仓不足等) → 立即关闭 trade_record 防止孤儿循环
                        self._close_trade_on_broker_reject(symbol, current_price, f"broker_reject:{exit_reason}", info)
                    # P0 修复 (2026-05-09): 无论成功/失败都移除，防止每 tick 重复刷单
                    symbols_to_remove.append(symbol)
                    continue

                # 检查时间止损 (使用该交易的实际 max_hold, 非全局默认)
                entry_time = info["entry_time"]
                trade_max_hold = info.get("max_hold_minutes", 5)
                pnl_pct = (
                    (current_price - info["entry_price"]) / info["entry_price"]
                    if info["direction"] == "LONG"
                    else (info["entry_price"] - current_price) / info["entry_price"]
                )
                self.time_exit.update_params(max_hold_minutes=trade_max_hold)
                should_time_exit, time_reason = self.time_exit.should_exit(
                    entry_time, pnl_pct
                )

                if should_time_exit:
                    # P0 修复 (2026-05-09): 实际提交平仓订单到富途
                    close_direction = "LONG" if info["direction"] == "SHORT" else "SHORT"
                    if self._submit_close_order(symbol, close_direction, current_price, info):
                        self._close_trade(symbol, current_price, time_reason, info)
                    else:
                        # P0 修复 (2026-05-26): 券商拒绝平仓 → 立即关闭 trade_record 防止孤儿循环
                        self._close_trade_on_broker_reject(symbol, current_price, f"broker_reject:{time_reason}", info)
                    # P0 修复 (2026-05-09): 无论成功/失败都移除，防止每 tick 重复刷单
                    symbols_to_remove.append(symbol)

            # 清理已平仓记录
            for symbol in symbols_to_remove:
                self._active_trades.pop(symbol, None)

    def _update_peak_price(self, symbol: str, current_price: float, info: dict) -> float:
        """更新持仓期间的峰值价格（LONG取最高，SHORT取最低）
        
        P0 新增 (2026-05-09): 轻量追踪止损的峰值跟踪
        """
        direction = info.get("direction", "LONG")
        peak = info.get("peak_price", info["entry_price"])
        
        if direction == "LONG":
            if current_price > peak:
                peak = current_price
                logger.debug(f"[追踪止损] {symbol} LONG peak 更新: ${peak:.2f}")
        else:  # SHORT
            if current_price < peak:
                peak = current_price
                logger.debug(f"[追踪止损] {symbol} SHORT peak 更新: ${peak:.2f}")
        
        info["peak_price"] = peak
        return peak

    def _get_current_price(self, symbol: str) -> Optional[float]:
        """获取当前价格"""
        if self.data_router:
            try:
                # P0 修复 (2026-05-05): get_realtime_price 不存在, 用 get_full_market_context
                ctx = self.data_router.get_full_market_context(symbol)
                if ctx:
                    price = ctx.get("price", ctx.get("last_price", 0))
                    if price > 0:
                        return price
                # 降级: get_quote
                quote = self.data_router.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    return quote["price"]
            except Exception:
                pass
        return None

    def _close_trade(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        info: Dict,
    ):
        """平仓处理"""
        trade = info["trade"]
        direction = info["direction"]

        # 记录统计
        self.stats_tracker.record_exit(symbol, exit_price, exit_reason)

        # 统一交易记录 — 更新退出信息
        record_id = info.get("record_id")
        if self.trade_store and record_id:
            # 计算盈亏 (期权需 ×100 合约乘数)
            multiplier = _get_contract_multiplier(symbol)
            if direction == "LONG":
                pnl_pct = (exit_price - info["entry_price"]) / info["entry_price"]
                pnl = pnl_pct * info["entry_price"] * info["position_size"] * multiplier
            else:
                pnl_pct = (info["entry_price"] - exit_price) / info["entry_price"]
                pnl = pnl_pct * info["entry_price"] * info["position_size"] * multiplier

            # P0 新增 (2026-05-12): 记录交易结果用于每日次数限制
            self._record_trade_result(pnl_pct, symbol)

            # C5 归因 (2026-05-13): 记录 scalping 交易盈亏供跨策略归因
            try:
                from app.analysis.strategy_attribution import get_attribution
                get_attribution().record_trade_pnl(
                    strategy="scalping",
                    symbol=symbol,
                    direction=direction,
                    pnl_pct=pnl_pct,
                    pnl_amount=pnl,
                    exit_reason=exit_reason,
                    entry_time=info.get("entry_time"),
                    exit_time=exit_time,
                )
            except Exception:
                pass

            # 映射 exit_reason 到标签
            reason_tag = exit_reason
            if "止盈" in exit_reason or "tp" in exit_reason.lower():
                reason_tag = "tp_hit"
            elif "止损" in exit_reason or "sl" in exit_reason.lower():
                reason_tag = "sl_hit"
            elif "时间" in exit_reason or "time" in exit_reason.lower() or "moc" in exit_reason.lower():
                reason_tag = "time_stop"

            # P0 修复 (2026-05-08): 标的级冷却 — 记录 sl_hit 次数
            if reason_tag == "sl_hit":
                today = now_et().strftime("%Y-%m-%d")
                if symbol not in self._symbol_sl_hit_count:
                    self._symbol_sl_hit_count[symbol] = {"count": 0, "date": today}
                if self._symbol_sl_hit_count[symbol]["date"] != today:
                    self._symbol_sl_hit_count[symbol] = {"count": 0, "date": today}
                self._symbol_sl_hit_count[symbol]["count"] += 1
                # P2 修复 (2026-05-19): 启动 sl_hit 二级冷却 (90s, 防刚损刚再入)
                with self._lock:
                    self._loss_cooldown[symbol] = time.time()
                cnt = self._symbol_sl_hit_count[symbol]["count"]
                logger.warning(
                    f"[Scalping冷却] {symbol} 今日第{cnt}次 sl_hit, "
                    f"{'触发冷却' if cnt >= 3 else f'距冷却还剩{3-cnt}次'}"
                )

            self.trade_store.update_exit(
                record_id=record_id,
                exit_price=exit_price,
                exit_reason=reason_tag,
                realized_pnl=pnl,  # 已含合约乘数
                pnl_pct=pnl_pct,
            )

        # 计算盈亏 (期权需 ×100 合约乘数) + 摩擦成本追踪
        multiplier = _get_contract_multiplier(symbol)
        if direction == "LONG":
            pnl_pct = (exit_price - info["entry_price"]) / info["entry_price"]
        else:
            pnl_pct = (info["entry_price"] - exit_price) / info["entry_price"]
        gross_pnl = pnl_pct * info["entry_price"] * info["position_size"] * multiplier

        # 摩擦成本: 佣金 ($0.60固定 + $0.005/股) + 滑点 (约 0.05%)
        pos = info["position_size"]
        est_commission = 0.60 + (pos * 0.005)  # 双边约 $1.20 + $0.01/股
        est_slippage = abs(gross_pnl) * 0.001   # 保守 0.1%
        friction_cost = est_commission + est_slippage
        net_pnl = gross_pnl - friction_cost
        net_pnl_pct = net_pnl / (info["entry_price"] * info["position_size"] * multiplier)

        # 记录亏损/盈利 (使用 Net PnL 更贴合券商实际)
        if net_pnl < 0:
            loss_amount = abs(net_pnl)
            self.budget.record_loss(loss_amount)
            # C.2 修复 (2026-05-19): 把 gross_pnl_pct 传给 CB, 噪声波动不计连续亏损.
            # 5/18 实测: gross_pnl=0.00% (摩擦造成 net 微亏) 不应触发熔断.
            self.circuit_breaker.record_loss(gross_pnl_pct=pnl_pct)
        else:
            self.circuit_breaker.record_win()

        # P0 修复 (2026-05-08): STRUCTURE_EXIT 连续亏损冷却追踪
        # 记录所有退出（不仅是 STRUCTURE_EXIT），用于追踪同标的连续亏损
        self._record_structure_exit(symbol, direction, is_loss=(net_pnl < 0))
        # P0-X (2026-05-15): 标的日亏损护栏喂数据
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            get_symbol_daily_loss_guard().record_trade(symbol, float(net_pnl))
        except Exception:
            pass
        # Y5 (2026-05-15): PnL 异常值监控 (基于 ORCL +$17K / GOOGL -$6K 教训)
        try:
            from app.risk.pnl_outlier_monitor import get_pnl_outlier_monitor
            get_pnl_outlier_monitor().check(symbol, float(net_pnl), {
                "direction": direction, "source": "scalping",
            })
        except Exception:
            pass

        # O2 (2026-05-16): Scalping 平仓也镜像 (与 mirror_entry 配对, 否则 shadow_trades closed=0 永不闭环)
        try:
            from app.evolution.shadow_mirror import get_shadow_mirror
            _trace_id = (info.get("signal_context") or {}).get("trace_id") or info.get("order_id") or f"scalp_{symbol}_{int(info['entry_time'].timestamp())}"
            get_shadow_mirror().mirror_exit(
                real_trace_id=str(_trace_id),
                symbol=symbol,
                exit_price=float(exit_price),
                exit_reason=str(exit_reason),
            )
        except Exception as _sme:
            logger.debug(f"[ShadowMirror·Scalping] exit 失败 (不影响实盘): {_sme}")

        # RFC-050 (P1-2): Scalping 退出路径回填 trade_attribution
        try:
            from app.pipeline.order_executor import _update_trade_attribution
            _entry_time = info.get("entry_time")
            _holding_hrs = 0.0
            if _entry_time and hasattr(_entry_time, 'timestamp'):
                _holding_hrs = (time.time() - _entry_time.timestamp()) / 3600.0
            _sig_ctx = info.get("signal_context") or {}
            _attr_trace_id = _sig_ctx.get("trace_id") or info.get("order_id") or ""
            if _attr_trace_id:
                _update_trade_attribution(
                    trace_id=str(_attr_trace_id),
                    symbol=symbol,
                    pnl=float(net_pnl),
                    pnl_pct=float(net_pnl_pct),
                    exit_reason=str(exit_reason),
                    holding_hours=_holding_hrs,
                    signal_strength=int(_sig_ctx.get("strength", 0) or 0),
                    signal_confidence=float(_sig_ctx.get("confidence", 0) or 0),
                )
        except Exception as _attr_e:
            logger.debug(f"[Scalping·归因] {symbol} trade_attribution UPDATE 失败 (不影响平仓): {_attr_e}")

        # 策略级日亏损预算追踪
        self._strategy_realized_pnl_today += net_pnl

        # 释放资金 (非 observe 模式)
        if not info.get("is_observe"):
            cost = info["entry_price"] * info["position_size"]
            self.budget.release(cost)
            # P0 修复 (2026-04-30): 从 position_store SSOT 移除，主管线恢复可交易
            self._unregister_from_position_store(symbol)
            # T4 (2026-05-16): enforce 模式下释放 SymbolLock, 让主线恢复可入场
            try:
                sched = getattr(self, "scheduler", None)
                coord = getattr(sched, "coordinator", None) if sched else None
                if coord is not None and getattr(coord, "enabled", False) and not coord.is_observe_only():
                    coord.lock.release(symbol, "scalping")
            except Exception as _re:
                logger.debug(f"[T4·SymbolLock·release scalping] 异常: {_re}")

        # 日志 (显示 Gross PnL 和 Net PnL)
        gross_str = f"+{pnl_pct:.2%}" if pnl_pct > 0 else f"{pnl_pct:.2%}"
        net_str = f"+{net_pnl_pct:.2%}" if net_pnl_pct > 0 else f"{net_pnl_pct:.2%}"
        logger.info(
            f"[ScalpingEngine] 平仓 | "
            f"{symbol} {direction} | "
            f"${info['entry_price']:.2f} → ${exit_price:.2f} | "
            f"Gross PnL: {gross_str} | Net PnL: {net_str} (摩擦: ${friction_cost:.2f}) | "
            f"原因: {exit_reason}"
        )

        # ═══════════════════════════════════════════════════════════
        # P0 修复 (2026-05-08): 写入 signal_outcomes 表
        # 打通 Scalping 策略学习闭环 — 让因子/信号质量评分基于真实交易结果
        # ═══════════════════════════════════════════════════════════
        # P0 防御 (2026-05-09): 幂等检查，防止同一笔交易重复写入
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            db_path = str(get_trade_records_db())
            entry_price = info.get("entry_price", exit_price)
            entry_time = info.get("entry_time", now_et())
            hold_minutes = (now_et() - entry_time).total_seconds() / 60.0
            holding_periods = max(1, int(hold_minutes / 5))  # 5分钟K线 = 1周期
            
            # 幂等检查: 是否已有相同 (code, strategy, entry_price, exit_price) 的记录
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM signal_outcomes "
                    "WHERE code=? AND strategy='scalping' "
                    "AND entry_price=? AND exit_price=? AND pnl_pct=?",
                    (symbol, entry_price, exit_price, round(net_pnl_pct * 100, 4))
                )
                if cur.fetchone()[0] > 0:
                    logger.debug(f"[信号结果] {symbol} 已存在于 signal_outcomes，跳过重复写入")
                else:
                    # 从 signal_context 提取策略上下文
                    signal_ctx = info.get("signal_context", {}) or {}
                    rsi_val = signal_ctx.get("rsi_14", 50)
                    vwap_val = signal_ctx.get("vwap", entry_price)
                    
                    # 映射 exit_reason 到 market_regime (简化版)
                    regime = signal_ctx.get("market_regime", "UNKNOWN")
                    if regime == "UNKNOWN":
                        if "止盈" in exit_reason or "tp" in exit_reason.lower():
                            regime = "trending"
                        elif "止损" in exit_reason or "sl" in exit_reason.lower():
                            regime = "high_volatility"
                        else:
                            regime = "ranging"
                    
                    conn.execute(
                        """INSERT INTO signal_outcomes 
                           (code, action, strategy, entry_price, exit_price, pnl_pct, 
                            holding_periods, market_regime, scenario, adx_value, 
                            mtf_alignment, sentiment_score, consensus_score, 
                            quality_score, max_drawdown, max_profit, confidence, signal_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            symbol,
                            direction,
                            "scalping",
                            entry_price,
                            exit_price,
                            net_pnl_pct * 100,  # 数据库存储百分比
                            holding_periods,
                            regime,
                            signal_ctx.get("lifecycle", "unknown"),
                            0.0,  # adx_value (scalping 不使用 ADX)
                            0.0,  # mtf_alignment
                            0.0,  # sentiment_score
                            info.get("signal_strength", 0.5),  # consensus_score
                            1.0,  # quality_score (default)
                            abs(net_pnl_pct) if net_pnl_pct < 0 else 0.0,  # max_drawdown
                            abs(net_pnl_pct) if net_pnl_pct > 0 else 0.0,  # max_profit
                            info.get("signal_confidence", 0.5),  # confidence
                            info.get("signal_id", ""),  # P0-2 (2026-05-20): 信号-交易关联
                        )
                    )
                    conn.commit()
                    logger.info(
                        f"✅ [信号结果] {symbol} Scalping 已写入 signal_outcomes | "
                        f"pnl={net_pnl_pct*100:+.2f}% | regime={regime} | "
                        f"exit={exit_reason[:30]}"
                    )
        except Exception as outcome_e:
            logger.warning(f"[信号结果] Scalping 写入 signal_outcomes 失败 (不影响主流程): {outcome_e}")
        # ═══════════════════════════════════════════════════════════

        # 通知 (P0 修复 2026-05-09: 统一使用 notify_exit，与主管线格式一致)
        if self.notifier and not info.get("is_observe"):
            self._notify_scalping_close(symbol, direction, info, exit_price, exit_reason, net_pnl)

    # ═══════════════════════════════════════════════════════════
    # ── 通知统一入口 (P0 修复 2026-05-09) ──
    # 与主管线统一使用 notify_order_filled / notify_exit，source_type 标注来源

    def _notify_scalping_open(self, symbol: str, direction: str, price: float,
                               position_size: int, levels: Dict, signals: str):
        """Scalping 开仓通知 — 统一走 notify_order_filled 入口"""
        try:
            from app.core.models import Position, Direction as DirEnum, Order, MarketRegime, OrderType

            # 构造 Position 对象 (与 _register_in_position_store 对齐)
            pos = Position(
                strategy_id="scalping",
                symbol=symbol,
                direction=DirEnum.LONG if direction == "LONG" else DirEnum.SHORT,
                size=float(position_size),
                avg_entry_price=price,
                initial_sl=levels.get("stop_loss", 0),
                initial_tp=levels.get("take_profit", 0),
                current_sl=levels.get("stop_loss", 0),
                take_profit_levels=[{"ratio": 1.0, "price": levels.get("take_profit", 0)}],
                time_stop_hours=5,
                market_regime=MarketRegime.RANGE,
            )
            pos.entry_time = now_et()
            pos.metadata = {"source": "scalping", "source_type": "SCALPING", "signals": signals}

            # 构造 Order 对象
            order = Order(
                client_order_id=f"scalping_{symbol}_notify",
                symbol=symbol,
                direction=DirEnum.LONG if direction == "LONG" else DirEnum.SHORT,
                order_type=OrderType.LIMIT,
                quantity=float(position_size),
                price=price,
            )
            order.avg_fill_price = price  # 模拟已成交

            # P1 修复 (2026-05-09): 类型感知调用，防止 notifier 切换导致 TypeError
            notifier_type = type(self.notifier).__name__
            if "DingTalk" in notifier_type:
                # DingTalkNotifier: (order, position, voting_result)
                self.notifier.notify_order_filled(order, pos)
            else:
                # FeishuNotifier 或其他: 使用扁平参数
                self.notifier.notify_order_filled(
                    symbol=symbol,
                    direction="BUY" if direction == "LONG" else "SELL",
                    filled_price=price,
                    quantity=position_size,
                    stop_loss=levels.get("stop_loss", 0),
                    take_profit=[levels.get("take_profit", 0)],
                    source_type="SCALPING",
                )
            logger.debug(f"[Scalping通知] {symbol} 开仓通知已推送 (统一格式)")
        except Exception as e:
            logger.warning(f"[Scalping通知] 开仓通知失败 (降级): {e}")

    def _notify_scalping_close(self, symbol: str, direction: str, info: Dict,
                                exit_price: float, exit_reason: str, net_pnl: float):
        """Scalping 平仓通知 — 统一走 notify_exit 入口"""
        try:
            from app.core.models import Position, Direction as DirEnum, ExitReason, MarketRegime

            # 映射退出原因到 ExitReason enum (fallback 到 MANUAL)
            er_lower = exit_reason.lower()
            if "止损" in er_lower or "sl" in er_lower or "stop" in er_lower:
                exit_enum = ExitReason.STOP_LOSS
            elif "止盈" in er_lower or "tp" in er_lower:
                exit_enum = ExitReason.TAKE_PROFIT
            elif "time" in er_lower or "时间" in er_lower or "moc" in er_lower:
                exit_enum = ExitReason.TIME_STOP
            else:
                exit_enum = ExitReason.MANUAL

            entry_time = info.get("entry_time", now_et())
            holding_hours = (now_et() - entry_time).total_seconds() / 3600.0

            # 构造 Position 对象
            pos = Position(
                strategy_id="scalping",
                symbol=symbol,
                direction=DirEnum.LONG if direction == "LONG" else DirEnum.SHORT,
                size=float(info["position_size"]),
                avg_entry_price=info["entry_price"],
                exit_price=exit_price,
                initial_sl=info.get("levels", {}).get("stop_loss", 0),
                initial_tp=info.get("levels", {}).get("take_profit", 0),
                current_sl=info.get("levels", {}).get("stop_loss", 0),
                take_profit_levels=[{"ratio": 1.0, "price": info.get("levels", {}).get("take_profit", 0)}],
                time_stop_hours=5,
                market_regime=MarketRegime.RANGE,
            )
            pos.entry_time = entry_time
            pos.exit_time = now_et()
            # holding_hours is a @property computed from entry_time/exit_time — do NOT assign
            pos.metadata = {"source": "scalping", "source_type": "SCALPING", "net_pnl": net_pnl}

            # P1 修复 (2026-05-09): 类型感知调用，防止 notifier 切换导致 TypeError
            notifier_type = type(self.notifier).__name__
            if "DingTalk" in notifier_type:
                # DingTalkNotifier: (position, reason, pnl, voting_result)
                self.notifier.notify_exit(pos, exit_enum, net_pnl)
            else:
                # FeishuNotifier 或其他: 使用扁平参数
                entry_price = info["entry_price"]
                pnl_pct = (exit_price - entry_price) / entry_price if direction == "LONG" else (entry_price - exit_price) / entry_price
                self.notifier.notify_exit(
                    symbol=symbol,
                    direction="BUY" if direction == "LONG" else "SELL",
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=int(info["position_size"]),
                    pnl=net_pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=str(exit_enum),
                    holding_hours=holding_hours,
                    source_type="SCALPING",
                )
            logger.debug(f"[Scalping通知] {symbol} 平仓通知已推送 (统一格式)")
        except Exception as e:
            logger.warning(f"[Scalping通知] 平仓通知失败 (降级): {e}")

    # ── position_store SSOT 同步 (P0 修复 2026-04-30) ──

    def _register_in_position_store(
        self, symbol: str, direction: str, price: float, size: int,
        tp_price: float, sl_price: float, max_hold_minutes: int,
    ):
        """Scalping 开仓后写入 position_store，使主管线感知并防止重复开仓
        
        P0 修复 (2026-05-07): 同时持久化到 open_positions 表，
        防止服务重启后 scalping 活跃交易丢失导致止盈止损不执行。
        """
        try:
            if not self.risk_engine or not hasattr(self.risk_engine, 'position_store'):
                return
            ps = self.risk_engine.position_store
            if ps is None:
                return
            if ps.has(symbol):
                # C3 修复 (2026-05-13): 检查现有持仓来源
                # - 如果是 scalping 自己 → 重复注册, 跳过
                # - 如果是主管线 → 同向允许 scalping 独立注册 (跨引擎协同已在入场前处理)
                existing = ps.get(symbol) if hasattr(ps, 'get') else None
                if existing:
                    existing_source = (getattr(existing, 'metadata', None) or {}).get("source", "")
                    if existing_source == "scalping":
                        logger.debug(f"[Scalping SSOT] {symbol} 已有 scalping 仓位, 跳过注册")
                        return
                    # 主管线持仓: 允许 scalping 独立追踪 (方向冲突已在 _execute_entry 阶段裁决)
                    logger.info(f"[Scalping SSOT] {symbol} 与主管线共存, scalping 独立注册追踪")
                else:
                    return

            from app.core.models import Position, Direction as DirEnum, MarketRegime
            pos = Position(
                strategy_id="scalping",
                symbol=symbol,
                direction=DirEnum.LONG if direction == "LONG" else DirEnum.SHORT,
                size=float(size),
                avg_entry_price=price,
                initial_sl=sl_price,
                initial_tp=tp_price,
                current_sl=sl_price,
                take_profit_levels=[{"ratio": 1.0, "price": tp_price}],
                time_stop_hours=max_hold_minutes // 60,
                market_regime=MarketRegime.RANGE,
            )
            pos.entry_time = now_et()
            pos.metadata = {"source": "scalping", "max_hold_minutes": max_hold_minutes}
            # B.4 (2026-05-14): scalping 路径标 owner_strategy + position_pct
            pos.owner_strategy = "scalping"
            try:
                from app.risk.capital_safety import get_capital_safety_system
                _eq = float(get_capital_safety_system()._state.total_equity or 0)
                if _eq > 0:
                    pos.position_pct = round(price * size / _eq, 4)
            except Exception:
                pass
            ps.add(symbol, pos)
            logger.info(f"[Scalping SSOT] {symbol} 已注册到 position_store (size={size}, dir={direction}, owner=scalping)")

            # B.4: 通知 coordinator (同主线路径)
            try:
                _scheduler = getattr(self, "scheduler", None) or getattr(self.risk_engine, "scheduler", None)
                _coord = getattr(_scheduler, "coordinator", None) if _scheduler else None
                if _coord is not None and _coord.enabled:
                    _coord.record_position_owner(symbol, "scalping", float(pos.position_pct))
            except Exception as _ce:
                logger.debug(f"[Scalping SSOT] coordinator 通知失败 (放行): {_ce}")

            # P0 修复: 持久化到 open_positions 表
            self._persist_scalping_position(symbol, direction, price, size, tp_price, sl_price, max_hold_minutes)
        except Exception as e:
            logger.error(f"[Scalping SSOT] position_store 注册失败: {e}")

    def _unregister_from_position_store(self, symbol: str):
        """Scalping 平仓后从 position_store 移除"""
        try:
            if not self.risk_engine or not hasattr(self.risk_engine, 'position_store'):
                return
            ps = self.risk_engine.position_store
            if ps is None:
                return
            # B.4 (2026-05-14): 移除前抓 position_pct 通知 coordinator 释放
            _pct = 0.0
            try:
                _existing = ps.get(symbol) if hasattr(ps, "get") else None
                if _existing:
                    _pct = float(getattr(_existing, "position_pct", 0.0) or 0.0)
            except Exception:
                pass
            ps.remove(symbol)
            logger.info(f"[Scalping SSOT] {symbol} 已从 position_store 移除")
            try:
                _scheduler = getattr(self, "scheduler", None) or getattr(self.risk_engine, "scheduler", None)
                _coord = getattr(_scheduler, "coordinator", None) if _scheduler else None
                if _coord is not None and _coord.enabled:
                    _coord.on_position_closed(symbol, "scalping", _pct)
            except Exception as _ce:
                logger.debug(f"[Scalping SSOT] coordinator close 通知失败 (放行): {_ce}")

            # P0 修复 (2026-05-07): 同时从 open_positions 表移除
            self._remove_scalping_position(symbol)
        except Exception as e:
            logger.error(f"[Scalping SSOT] position_store 移除失败 (降级): {e}")

    # ============================================================
    #  持久化层 (P0 修复 2026-05-07: 防止服务重启后 scalping 交易丢失)
    # ============================================================

    def _persist_scalping_position(self, symbol: str, direction: str, price: float,
                                     size: int, tp_price: float, sl_price: float,
                                     max_hold_minutes: int):
        """将 scalping 活跃交易写入 open_positions 表"""
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            import json
            now = now_et().isoformat()
            meta = json.dumps({
                "source": "scalping",
                "max_hold_minutes": max_hold_minutes,
                "take_profit": tp_price,
                "stop_loss": sl_price,
            })
            cursor.execute("""
                INSERT OR REPLACE INTO open_positions 
                    (symbol, direction, size, avg_entry_price, initial_sl, current_sl,
                     initial_tp, take_profit_json, trail_state_json, entry_time,
                     time_stop_hours, strategy_id, source, sync_status, is_filled,
                     is_anomalous, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, direction, float(size), price,
                sl_price, sl_price,
                tp_price, "[]",
                json.dumps({"active": True, "scalping": True}),
                now, max_hold_minutes // 60,
                "scalping", "scalping", "synced",
                1, 0, 0, now,
            ))
            conn.commit()
            conn.close()
            logger.info(f"[Scalping 持久化] {symbol} 已写入 open_positions")
        except Exception as e:
            logger.error(f"[Scalping 持久化] {symbol} 写入失败: {e}")

    def _remove_scalping_position(self, symbol: str):
        """从 open_positions 表移除 scalping 持仓"""
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM open_positions WHERE symbol=? AND strategy_id='scalping'",
                (symbol,)
            )
            conn.commit()
            conn.close()
            logger.info(f"[Scalping 持久化] {symbol} 已从 open_positions 移除")
        except Exception as e:
            logger.error(f"[Scalping 持久化] {symbol} 移除失败: {e}")

    def _restore_scalping_positions(self):
        """启动时从 open_positions 恢复 scalping 活跃交易"""
        try:
            import sqlite3
            from pathlib import Path
            import json
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "ftseek_us.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM open_positions WHERE strategy_id='scalping'"
            )
            rows = cursor.fetchall()
            conn.close()

            restored = 0
            for row in rows:
                symbol = row["symbol"]
                direction = row["direction"]
                entry_price = row["avg_entry_price"]
                size = int(row["size"])
                tp_price = row["initial_tp"]
                sl_price = row["current_sl"]
                max_hold = row["time_stop_hours"] * 60 if row["time_stop_hours"] else 5

                # 重建内存状态
                from app.scalping.stats import TradeRecord
                trade = TradeRecord(
                    symbol=symbol, direction=direction,
                    entry_price=entry_price, take_profit=tp_price,
                    stop_loss=sl_price, signals="restored",
                    phase="restored", quantity=size,
                )
                with self._lock:
                    self._active_trades[symbol] = {
                        "trade": trade,
                        "levels": {"take_profit": tp_price, "stop_loss": sl_price},
                        "entry_time": datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else now_et(),
                        "max_hold_minutes": max_hold,
                        "direction": direction,
                        "entry_price": entry_price,
                        "position_size": size,
                        "is_observe": False,
                        "record_id": None,  # 无法恢复 trade_store record_id
                        "signal_record": None,
                        "signal_context": {},  # P0 修复: 恢复路径无信号上下文，置空避免回写假数据
                        # P0 新增 (2026-05-09): 轻量追踪止损
                        "peak_price": entry_price,
                    }
                    # 重新注册到 position_store
                    try:
                        if self.risk_engine and hasattr(self.risk_engine, 'position_store'):
                            from app.core.models import Position, Direction as DirEnum, MarketRegime
                            pos = Position(
                                strategy_id="scalping", symbol=symbol,
                                direction=DirEnum.LONG if direction == "LONG" else DirEnum.SHORT,
                                size=float(size), avg_entry_price=entry_price,
                                initial_sl=sl_price, initial_tp=tp_price,
                                current_sl=sl_price,
                                take_profit_levels=[{"ratio": 1.0, "price": tp_price}],
                                time_stop_hours=max_hold // 60,
                                market_regime=MarketRegime.RANGE,
                            )
                            pos.metadata = {"source": "scalping", "restored": True}
                            # B.4 (2026-05-14): scalping 路径标 owner_strategy
                            pos.owner_strategy = "scalping"
                            self.risk_engine.position_store.add(symbol, pos)
                    except Exception:
                        pass
                    restored += 1

            if restored > 0:
                logger.info(f"[Scalping 恢复] 从 open_positions 恢复 {restored} 笔活跃交易: {[r['symbol'] for r in rows]}")
            else:
                logger.info("[Scalping 恢复] 无需要恢复的活跃交易")
        except Exception as e:
            logger.error(f"[Scalping 恢复] 恢复失败: {e}")

    # ── 跨引擎协同 ──

    def _get_existing_main_position(self, symbol: str) -> Optional[Dict]:
        """
        查询主管线是否已有该标的持仓

        Returns:
            {"direction": "LONG"/"SHORT", ...} or None
        """
        try:
            if self.risk_engine and hasattr(self.risk_engine, 'position_store'):
                ps = self.risk_engine.position_store
                if ps and hasattr(ps, 'get'):
                    return ps.get(symbol)
        except Exception:
            pass
        return None

    # ── 平仓订单提交 (P0 修复 2026-05-09) ──

    def _submit_close_order(
        self, symbol: str, close_direction: str, close_price: float, info: Dict
    ) -> bool:
        """提交平仓订单到富途，等待成交确认

        P0 修复 (2026-05-09): 统一平仓订单提交逻辑，修复了以下缺陷:
        - _close_trade 只做账面记录，从未调用 _place_order
        - TP/SL 和 MOC 平仓均未实际提交订单到券商
        - 导致 broker_reconcile 反复发现"幽灵持仓"

        Returns:
            True 如果订单成功提交并成交
        """
        position_size = info["position_size"]

        logger.info(
            f"[Scalping平仓] {symbol} 提交订单: "
            f"{info['direction']} {info['position_size']}股 → {close_direction} @ ${close_price:.2f}"
        )

        order_result = self._place_order(
            symbol=symbol,
            direction=close_direction,
            price=close_price,
            quantity=position_size,
        )

        if order_result and order_result.get("success"):
            filled = self._wait_for_fill(order_result, timeout=30)
            if filled:
                logger.info(f"[Scalping平仓] {symbol} 平仓订单已成交")
                return True
            else:
                logger.warning(f"[Scalping平仓] {symbol} 平仓订单未成交 (超时)")
        else:
            err = order_result.get("error", "unknown") if order_result else "no order result"
            logger.error(f"[Scalping平仓] {symbol} 下单失败: {err}")

        return False

    def _close_trade_on_broker_reject(
        self, symbol: str, current_price: float, exit_reason: str, info: Dict
    ):
        """P0 修复 (2026-05-26): 券商拒绝平仓 (持仓不足等) → 立即关闭 trade_record 防止孤儿循环

        当 _submit_close_order 返回 False 时，broker 拒绝了平仓请求 (如"持仓不足")。
        此时 trade_record 仍然 open (exit_time IS NULL)，scalping 引擎会在下一 tick
        再次尝试，导致无限重试循环。

        修复：立即用 entry_price 作为 exit_price (无实际成交) 关闭 trade_record，
        realized_pnl=0，防止孤儿记录和无限重试。
        """
        record_id = info.get("record_id")
        if self.trade_store and record_id:
            try:
                entry_price = info.get("entry_price", current_price)
                self.trade_store.update_exit(
                    record_id=record_id,
                    exit_price=entry_price,
                    exit_reason=exit_reason,
                    realized_pnl=0.0,
                    pnl_pct=0.0,
                )
                logger.warning(
                    f"[Scalping·BrokerReject] {symbol} trade_record 已关闭: "
                    f"reason={exit_reason} @ ${entry_price:.2f} (无实际成交)"
                )
            except Exception as e:
                logger.error(f"[Scalping·BrokerReject] {symbol} trade_record 关闭失败: {e}")
        else:
            logger.warning(
                f"[Scalping·BrokerReject] {symbol} 无 record_id (observe 模式或 record 未创建), "
                f"仅从 _active_trades 移除"
            )

    # ── MOC 收盘保护 ──

    def _force_close_all_positions(self, reason: str):
        """15:55 强制平仓所有 scalping 仓位，避免 MOC 失衡风险

        P0 修复 (2026-05-09): 实际提交平仓订单到富途 + 清理 _active_trades
        修复前：_close_trade 只做账面记录，未调用 _place_order，导致
               1) 券商端真实持仓未平
               2) _active_trades 未清理，每 tick 重复触发 MOC 通知
        """
        with self._lock:
            if not self._active_trades:
                return
            # P0 修复 (2026-05-09): MOC session 守卫，防止同一轮 MOC 重复触发
            if self._moc_fired_this_session:
                return
            self._moc_fired_this_session = True
            symbols = list(self._active_trades.keys())
            logger.warning(
                f"[MOC保护] 强制平仓 {len(symbols)} 笔 scalping 仓位: {reason}"
            )

        symbols_to_remove = []
        for symbol in symbols:
            try:
                with self._lock:
                    info = self._active_trades.get(symbol)
                if not info:
                    continue

                current_price = self._get_current_price(symbol)
                if not current_price:
                    logger.warning(f"[MOC保护] {symbol} 无法获取价格，跳过平仓")
                    continue

                direction = info["direction"]
                close_direction = "LONG" if direction == "SHORT" else "SHORT"

                if self._submit_close_order(symbol, close_direction, current_price, info):
                    self._close_trade(symbol, current_price, reason, info)
                    symbols_to_remove.append(symbol)
                # P0 修复 (2026-05-09): 无论订单是否成交，MOC 尝试后都从 _active_trades 移除
                # 防止 _submit_close_order 失败后每 tick 重复触发
                else:
                    symbols_to_remove.append(symbol)
                    logger.warning(f"[MOC保护] {symbol} 平仓订单未成交，仍从活跃交易移除 (防止重复触发)")

            except Exception as e:
                logger.error(f"[MOC保护] {symbol} 平仓异常: {e}", exc_info=True)
                # 异常也移除，防止死循环
                symbols_to_remove.append(symbol)

        # P0 修复: 清理所有已处理仓位 (成功+失败)，防止下次 tick 重复触发
        with self._lock:
            for symbol in symbols_to_remove:
                self._active_trades.pop(symbol, None)
        if symbols_to_remove:
            logger.info(f"[MOC保护] 已从活跃交易移除 {len(symbols_to_remove)} 笔: {symbols_to_remove}")

    def _verify_global_risk_gate(self, symbol: str, direction: str, price: float, quantity: int) -> bool:
        """
        P0 修复 (2026-04-30): Scalping 全局风控门禁
        确保 Scalping 交易不违反全局资本安全、风险状态和持仓限制。
        即使 Scalping 有自己的预算，也不能突破系统的总风控底线。
        """
        logger.info(f"[Scalping 风控] {symbol} 开始全局风控检查...")
        
        # 1. 全局资本安全检查 (Capital Safety)
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            allowed, reason = capital.can_open_position()
            if not allowed:
                logger.warning(f"🚫 [Scalping 风控] {symbol} 资本安全拦截: {reason}")
                return False
            logger.debug(f"[Scalping 风控] {symbol} 资本安全检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} 资本安全检查异常 (放行): {e}")

        # 2. 风险状态机检查 (Risk State Machine)
        try:
            from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskAction
            sm = RiskStateMachine()
            if not sm.is_trade_allowed(RiskAction.OPEN):
                current_state = sm.get_state().value
                logger.warning(f"🚫 [Scalping 风控] {symbol} 状态机拦截: 系统处于 {current_state}")
                return False
            logger.debug(f"[Scalping 风控] {symbol} 状态机检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} 状态机检查异常 (放行): {e}")

        # 3. 全局持仓限制检查 (Global Position Limits)
        # Scalping 必须遵守全局最大持仓数 (8) 和单品种限制
        try:
            if self.risk_engine and hasattr(self.risk_engine, 'position_store'):
                all_positions = self.risk_engine.position_store.all()
                logger.info(f"[Scalping 风控] {symbol} 当前全局持仓数: {len(all_positions)}")
                
                # 3.1 检查最大持仓数 (Default: 8)
                max_positions = 8 # 可以从 config 读取，这里用安全默认值
                if len(all_positions) >= max_positions:
                    logger.warning(
                        f"🚫 [Scalping 风控] {symbol} 持仓已满 ({len(all_positions)}/{max_positions})"
                    )
                    return False

                # 3.2 检查单品种持仓 (防止 Scalping 和主管线同时开同一个票)
                for p in all_positions:
                    if p.symbol == symbol:
                        logger.warning(
                            f"🚫 [Scalping 风控] {symbol} 主管线已有持仓，Scalping 禁止重复开仓"
                        )
                        return False
            logger.debug(f"[Scalping 风控] {symbol} 持仓限制检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} 持仓限制检查异常 (放行): {e}")

        # 4. Scalping 内部预算检查
        try:
            scalping_value = price * quantity
            if self.budget:
                available = self.budget.get_available_budget()
                if scalping_value > available:
                    logger.warning(
                        f"🚫 [Scalping 风控] {symbol} Scalping 预算不足: 需要 ${scalping_value:,.0f}, "
                        f"可用 ${available:,.0f}"
                    )
                    return False
            logger.debug(f"[Scalping 风控] {symbol} 预算检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} 预算检查异常: {e}")

        # 5. P0 修复 (2026-05-07): PDT 检查 — 防止 scalping 触发 PDT 限制
        try:
            from app.risk.pdt_guard import get_pdt_guard
            pdt = get_pdt_guard()
            allowed, reason, risk_level = pdt.check_trade_permission(is_overnight=False)
            if not allowed:
                logger.warning(f"🚫 [Scalping 风控] {symbol} PDT 拦截: {reason}")
                return False
            if risk_level.value == "WARNING":
                logger.warning(f"⚠️ [Scalping 风控] {symbol} PDT 警告: {reason}")
            logger.debug(f"[Scalping 风控] {symbol} PDT 检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} PDT 检查异常 (放行): {e}")

        # 6. P1 修复 (2026-05-09): 美股保证金风控 (对标港股 FutuMarginMonitor)
        try:
            from app.risk.us_margin_guard import get_margin_guard
            margin_guard = get_margin_guard()

            # 延迟注入 broker 并初始化 (修复: 之前只 set_broker 不 initialize 导致 DATA_ERROR)
            if not margin_guard._initialized and self.execution_engine:
                adapter = getattr(self.execution_engine, "broker_adapter", None)
                if adapter and adapter.is_connected():
                    margin_guard.initialize(broker_adapter=adapter)
                    logger.debug(f"[Scalping 风控] MarginGuard 延迟初始化完成")
            elif margin_guard._broker is None and self.execution_engine:
                adapter = getattr(self.execution_engine, "broker_adapter", None)
                if adapter:
                    margin_guard.set_broker(adapter)

            estimated_cost = price * quantity
            if not margin_guard.is_safe_to_open(estimated_cost=estimated_cost):
                logger.warning(f"🚫 [Scalping 风控] {symbol} 保证金拦截: 压力测试不通过")
                return False
            logger.debug(f"[Scalping 风控] {symbol} 保证金检查通过")
        except Exception as e:
            logger.error(f"[Scalping 风控] {symbol} 保证金检查异常 (放行): {e}")

        logger.info(f"[Scalping 风控] {symbol} 全局风控检查全部通过")
        return True

    def get_status(self) -> Dict:
        """获取引擎状态 (用于监控和诊断)"""
        return {
            "running": self.running,
            "mode": self.mode,
            "active_trades": len(self._active_trades),
            "budget": self.budget.get_state() if self.budget else {},
            "circuit_breaker": (
                self.circuit_breaker.get_state() if self.circuit_breaker else {}
            ),
            "stats": self.stats_tracker.get_stats() if self.stats_tracker else {},
            "today_stats": (
                self.stats_tracker.get_today_stats() if self.stats_tracker else {}
            ),
            # P1 修复 (2026-05-09): 保证金风控状态
            "margin_guard": self._get_margin_guard_status(),
        }

    def _get_margin_guard_status(self) -> Dict:
        """获取 MarginGuard 状态 (不阻塞)"""
        try:
            from app.risk.us_margin_guard import get_margin_guard
            mg = get_margin_guard()
            return mg.get_comprehensive_status()
        except Exception:
            return {"status": "unavailable"}


def get_scalping_engine(
    execution_engine=None,
    data_router=None,
    risk_engine=None,
    notifier=None,
    daily_active_pool=None,
    **kwargs,
) -> ScalpingEngine:
    """获取 Scalping 引擎实例"""
    return ScalpingEngine(
        execution_engine=execution_engine,
        data_router=data_router,
        risk_engine=risk_engine,
        notifier=notifier,
        daily_active_pool=daily_active_pool,
        **kwargs,
    )
