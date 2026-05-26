"""
FTSeek-US 持仓退出监控 (Position Exit Monitor)

从 stoploss_daemon 提取的核心止损/止盈逻辑，合并到主进程内执行。
消除独立 Daemon 的双写入者竞争问题，确保：
  1. 单一写入者 (无 DB 竞争)
  2. 复用主进程 Futu 连接 (不消耗额外槽位)
  3. trail_state 实时更新 (不再全 0.0)
  4. 内存 SSOT (PositionStore → DB 单向同步)

规则 (由 exit_policy.yaml 统一配置):
  0. 防振荡: 同一持仓 60 秒内不重复触发
  1. Flash Exit: 60s 内波动 > flash_multiplier × ATR (优化: 1.0x, 60s扫描)
  1.5. Day Trade Mode: 紧止损(1.8xATR) + 主动止盈(+3%/+6%) + 利润锁定 + 时间衰减
  2. ATR 动态止损: entry - atr × multiplier (非线性状态机, initial=2.0x)
  2.5. Scale Out 分批止盈: 5%/10%/18% 三层
  3. Chandelier 追踪: 峰值 - atr × trail_mult (盈利≥1.2ATR 激活, regime差异化)
  4. 保本触发: 盈利≥1.0ATR 移至成本价+buffer
  3.5. Pre-Close Exit: 分层退出 + 时间加权 (亏损全平/0~0.5ATR减半/0.5~1ATR减1/4/≥1ATR过夜)
  5. Time Stop: > 72h 持仓

数学期望修正 (P0-2, 2026-04-28):
  - profit_atr 改为有向（signed）：盈利>0，亏损<0
  - 亏损时禁止激活 Chandelier / Breakeven（原 abs() 实现会误触发追踪线上移）
  - 止损定价逻辑下沉到 atr_stop_engine（纯函数，可单测/回测）
  - 接入 cost_buffer：止损线额外让出 k × total_cost_bps 价格空间

优化记录 (2026-05-03):
  - P0: Flash Exit 阈值 0.8→1.0x ATR, 扫描间隔 30→60s (历史2次Flash全部亏损)
  - P1: PRE_CLOSE_EXIT 改为分层退出 + 时间加权, 根因修复 54% 被动强平问题
  - P1: Day Trade Mode 参数全面放宽, 匹配 session-aware 实际波动
  - P1: Chandelier 激活门槛从 2.0ATR 降至 1.2ATR, regime乘数放宽 (给利润更多空间)
  - P2: 分层止盈门槛从 8%/15%/25% 降至 5%/10%/18%
  - P2: Chandelier regime乘数: normal 2.0→2.5, trending 3.0→3.5, volatile 1.5→2.0
"""
import logging
import time
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

# ATR 止损定价引擎（纯函数）
from .atr_stop_engine import compute_stop_lines, compute_profit_atr_signed, resolve_trail_multiplier

# 共享内核 — session-aware ATR (P2 2026-05-01)
from app.core.shared_kernel import get_shared_kernel

# C4 修复 (2026-05-13): 全局 EOD 配置 — 主线 + Scalping 共享
from app.risk.eod_config import load_eod_config

# 退出质量追踪 (P2 2026-05-01)
try:
    from app.storage.exit_quality_tracker import get_exit_quality_tracker
    _QUALITY_TRACKER_AVAILABLE = True
except Exception:
    get_exit_quality_tracker = None  # type: ignore
    _QUALITY_TRACKER_AVAILABLE = False

# 成本模型（可选，用于 cost_buffer）
try:
    from app.decision.cost_model import CostModel, CostModelConfig
    _COST_MODEL_AVAILABLE = True
except Exception:
    CostModel = None  # type: ignore
    CostModelConfig = None  # type: ignore
    _COST_MODEL_AVAILABLE = False

BASE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("ftseek.exit_monitor")

# 美东时区
try:
    from pytz import timezone
    ET = timezone('US/Eastern')
except ImportError:
    ET = None


# ============================================================
#  市场时间工具
# ============================================================

def get_market_hours_us(start_dt, end_dt) -> float:
    """计算美东交易时段内的小时数 (09:30-16:00)"""
    try:
        if ET:
            start = start_dt.astimezone(ET)
            end = end_dt.astimezone(ET)
        else:
            start = start_dt
            end = end_dt

        hours = 0.0
        current = start
        while current < end:
            if current.weekday() >= 5:
                days_ahead = 7 - current.weekday()
                current = (current + timedelta(days=days_ahead)).replace(hour=9, minute=30, second=0, microsecond=0)
                continue
            day_open = current.replace(hour=9, minute=30, second=0, microsecond=0)
            day_close = current.replace(hour=16, minute=0, second=0, microsecond=0)
            session_start = max(current, day_open)
            session_end = min(end, day_close)
            if session_end > session_start:
                hours += (session_end - session_start).total_seconds() / 3600.0
            current = (current + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
            if current > end:
                break
        return hours
    except Exception:
        return (end_dt - start_dt).total_seconds() / 3600.0


def is_us_market_open() -> bool:
    """判断美股是否在交易时段 (美东 09:30-16:00, 含节假日检查)

    P2' 修复 (2026-05-26): 原实现无节假日检查，Memorial Day 等假日会误触发退出逻辑。
    现在统一调用 holiday_calendar，与 scheduler_triggers 保持一致。
    """
    try:
        if ET:
            now_et = datetime.now(ET)
        else:
            now_et = datetime.now()
        if now_et.weekday() >= 5:
            return False
        # 节假日检查 — 统一入口
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            if get_holiday_calendar().is_us_holiday(now_et.date()):
                return False
        except Exception:
            pass
        t = now_et.hour * 60 + now_et.minute
        return 570 <= t <= 960
    except Exception:
        return True


# ============================================================
#  退出检查器
# ============================================================

class ExitMonitor:
    """
    持仓退出监控器 (单例)

    持仓退出检查逻辑，适配主进程环境：
    - 使用共享 quote_ctx 获取价格/ATR
    - 直接操作 PositionStore (内存)
    - 实时更新 position.trail_state
    """

    SCAN_INTERVAL_SECONDS = 60  # 优化 (2026-05-03 P2): 从 30s 延长到 60s, 降低假触发率
    COOLDOWN_SECONDS = 120  # 防振荡 (从60→120, 防止DIA 63s双重退出复发)

    def __init__(self):
        self._atr_cache: Dict[str, Dict] = {}  # {symbol: {date, value}}
        self._vix_cached: Optional[float] = None  # VIX 缓存 (5min TTL)
        self._vix_ts: float = 0.0
        self._quote_ctx = None
        self._initialized = False
        self._exit_cooldowns: Dict[str, float] = {}  # {symbol: last_exit_timestamp} 类级别冷却，防position对象重建后trail_state丢失
        # 成本模型（可选；注入失败则 cost_bps=0，退化为无 buffer）
        self._cost_model = None
        if _COST_MODEL_AVAILABLE:
            try:
                self._cost_model = CostModel(CostModelConfig())
            except Exception as e:
                logger.debug(f"[ExitMonitor] CostModel 初始化失败，将退化为无 cost_buffer: {e}")
                self._cost_model = None

    def _has_exit_record(self, symbol: str, exit_reason: str, max_minutes: int = 1440) -> bool:
        """
        P1 修复 (2026-05-01): 检查该标的是否在指定时间内已有相同原因的退出记录
        
        防止因服务重启/状态不一致导致的重复退出和重复交易记录。
        """
        try:
            import sqlite3, os
            db_path = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'ftseek_us.db'))
            if not os.path.exists(db_path):
                return False
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cutoff = (datetime.now() - __import__('datetime', fromlist=['timedelta']).timedelta(minutes=max_minutes)).isoformat()
            cursor.execute(
                "SELECT COUNT(*) FROM trade_records WHERE symbol=? AND exit_reason=? AND exit_time > ?",
                (symbol, exit_reason, cutoff)
            )
            count = cursor.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def initialize(self, quote_ctx=None):
        """初始化工具"""
        if self._initialized:
            return
        self._quote_ctx = quote_ctx
        self._initialized = True
        logger.info("📊 ExitMonitor 已初始化 (ExitMonitor 已就绪)")

    def update_quote_ctx(self, quote_ctx):
        """更新行情上下文 (重连后调用)"""
        self._quote_ctx = quote_ctx

    def _check_portfolio_drawdown(self, positions, get_price_fn) -> str:
        """F17 (2026-05-12): 组合层面止损 — 返回 FLAT_ALL / REDUCE_50 / WARN / None"""
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            warn_pct = cfg.get_dotted("risk.portfolio.warn_drawdown_pct", 0.02)
            reduce_pct = cfg.get_dotted("risk.portfolio.reduce_drawdown_pct", 0.025)
            stop_pct = cfg.get_dotted("risk.portfolio.stop_drawdown_pct", 0.03)
        except Exception:
            warn_pct, reduce_pct, stop_pct = 0.02, 0.025, 0.03

        total_pnl = 0.0
        for p in positions:
            try:
                price = get_price_fn(p.symbol)
                if price and price > 0:
                    if getattr(p, 'direction', None) and getattr(p.direction, 'value', 'LONG') == 'LONG':
                        total_pnl += (price - p.avg_entry_price) * getattr(p, 'size', 0)
                    else:
                        total_pnl += (p.avg_entry_price - price) * getattr(p, 'size', 0)
            except Exception:
                pass

        # 用总持仓成本估算组合净值
        total_cost = sum(getattr(p, 'avg_entry_price', 0) * getattr(p, 'size', 0) for p in positions)
        if total_cost <= 0:
            return None
        drawdown = -total_pnl / total_cost

        action = None
        if drawdown >= stop_pct:
            action = "FLAT_ALL"
        elif drawdown >= reduce_pct:
            action = "REDUCE_50"
        elif drawdown >= warn_pct:
            action = "WARN"

        # P0-3 (2026-05-19): 触发时打印具体浮亏 + 持仓清单, 便于追溯
        if action is not None:
            try:
                detail = ", ".join(
                    f"{getattr(p,'symbol','?')}@{getattr(p,'avg_entry_price',0):.2f}×{int(getattr(p,'size',0))}"
                    for p in positions
                )
                logger.warning(
                    f"[组合止损·trigger] action={action} drawdown={drawdown*100:+.2f}% "
                    f"total_cost=${total_cost:.0f} total_pnl=${total_pnl:+.0f} positions=[{detail}]"
                )
            except Exception:
                pass
        return action

    # ============================================================
    #  核心：扫描所有持仓
    # ============================================================

    def scan_positions(self, positions: List, get_price_fn, execute_close_fn, policy_guardian=None, earnings_guard=None) -> List[Dict]:
        """
        扫描所有持仓，检查是否需要平仓

        Args:
            positions: Position 对象列表 (来自 PositionStore.all())
            get_price_fn: (symbol) -> float  获取实时价格
            execute_close_fn: (position, reason_dict) -> bool  执行平仓
            policy_guardian: ExitPolicyGuardian 实例 (可选)
            earnings_guard: EarningsRiskGuard 实例 (可选)

        Returns:
            已平仓的持仓列表
        """
        closed = []
        now_ts = time.time()
        today = datetime.now().strftime('%Y-%m-%d')

        # ── 幻方 P2 (2026-05-01): session-aware ATR ──
        # 一个 session_vol_ratio 影响全部 5 条退出规则
        # effective_atr = atr × ratio → 高波动时段 ATR 自动放大, 所有止损自动放宽
        kernel = get_shared_kernel()
        current_session = kernel.get_current_session()
        current_session_ratio = kernel.get_session_vol_ratio(current_session)

        # ── P2 (2026-05-18): 组合止损仅针对系统管理的仓位 ──
        # futu_sync/scalping 仓位不参与组合止损计算，避免幽灵仓触发误杀
        # P0-3 修复 (2026-05-19): 双重过滤 (managed AND not protected) + 显式排除 ghost.
        # 5/18 ARM 紊乱: WARN×23 + REDUCE×3 + FLAT_ALL×7 在 PROTECTED 仓位上误触发,
        # 根因是 import fallback `lambda p: True` 一旦触发, 所有 PROTECTED 全进 portfolio.
        try:
            from app.positions import is_strategy_managed, is_protected
        except Exception as _ie:
            logger.error(
                f"[组合止损·安全降级] is_strategy_managed/is_protected 导入失败 ({_ie}), "
                f"为防误杀 PROTECTED 仓位, 本轮 portfolio_drawdown 跳过"
            )
            is_strategy_managed = lambda p: False  # P0-3: 安全侧默认 — 失败时跳过整段, 不误杀
            is_protected = lambda p: True
        managed_positions = []
        for p in positions:
            try:
                # 显式排除 ghost (Reconciler 标记的 metadata.is_ghost)
                _meta = getattr(p, 'metadata', None) or {}
                if _meta.get('is_ghost'):
                    continue
                # 显式排除待平仓的位置 (避免重复触发)
                if _meta.get('_pending_close'):
                    continue
                # 双重过滤: 必须是 MANAGED 且非 PROTECTED
                if is_strategy_managed(p) and not is_protected(p):
                    managed_positions.append(p)
            except Exception:
                continue  # 单仓位异常不阻断整体扫描

        # ── F17 (2026-05-12): 组合层面止损 ──
        # 在单仓位扫描之前, 先检查组合整体亏损
        portfolio_action = self._check_portfolio_drawdown(managed_positions, get_price_fn)
        if portfolio_action == "FLAT_ALL":
            logger.critical("[组合止损] 触发 FLAT_ALL, 全平所有持仓")
            for p in managed_positions:
                try:
                    _cur = get_price_fn(p.symbol)
                    _entry = getattr(p, 'avg_entry_price', 0)
                    _dir = getattr(p, 'direction', None)
                    _dir_str = _dir.value if hasattr(_dir, 'value') else str(_dir) if _dir else 'LONG'
                    _pnl = (_cur - _entry) / _entry if _entry > 0 and isinstance(_cur, (int, float)) else 0.0
                    if _dir_str == 'SHORT': _pnl = -_pnl
                    execute_close_fn(p, {"reason": "PORTFOLIO_DRAWDOWN_HARD_STOP", "close_ratio": 1.0})
                    closed.append({"symbol": p.symbol, "reason": "PORTFOLIO_DRAWDOWN_HARD_STOP", "pnl_pct": _pnl})
                except Exception as _e:
                    logger.error(f"[组合止损] 平仓 {p.symbol} 失败: {_e}")
            return closed
        elif portfolio_action == "REDUCE_50":
            logger.warning("[组合止损] 触发 REDUCE_50, 每只减仓 50%")
            for p in managed_positions:
                try:
                    close_qty = max(1, getattr(p, 'size', 100) // 2)
                    _cur = get_price_fn(p.symbol)
                    _entry = getattr(p, 'avg_entry_price', 0)
                    _dir = getattr(p, 'direction', None)
                    _dir_str = _dir.value if hasattr(_dir, 'value') else str(_dir) if _dir else 'LONG'
                    _pnl = (_cur - _entry) / _entry if _entry > 0 and isinstance(_cur, (int, float)) else 0.0
                    if _dir_str == 'SHORT': _pnl = -_pnl
                    execute_close_fn(p, {"reason": "PORTFOLIO_DRAWDOWN_REDUCE", "close_qty": close_qty, "close_ratio": 0.5})
                    closed.append({"symbol": p.symbol, "reason": "PORTFOLIO_DRAWDOWN_REDUCE", "pnl_pct": _pnl})
                except Exception as _e:
                    logger.error(f"[组合止损] 减仓 {p.symbol} 失败: {_e}")
        elif portfolio_action == "WARN":
            logger.warning("[组合止损] 触发 WARN 告警")

        # ── C2 修复 (2026-05-13): 过滤 scalping 仓位 ──
        # Scalping 仓位由 ScalpingEngine._monitor_loop 自管退出，
        # 主线 ExitMonitor 不得干预，否则两套退出引擎并行扫同一仓位导致归因混乱
        # M1 (2026-05-16): 同时过滤 PROTECTED/FROZEN 仓位 (用户手动仓默认隔离)
        try:
            from app.positions import is_strategy_managed, get_position_source, get_protection_state
            from app.positions.source import PositionSource
        except Exception:
            is_strategy_managed = lambda p: True  # 模块异常时不阻断 (向后兼容)
            get_position_source = lambda p: None
            get_protection_state = lambda p: None
            PositionSource = None

        main_positions = []
        skipped_scalping = 0
        skipped_protected = 0
        for p in positions:
            # 1. scalping 自管
            if (getattr(p, 'metadata', None) or {}).get("source") == "scalping" or getattr(p, 'strategy_id', '') == "scalping":
                skipped_scalping += 1
                continue
            # 2. M1: PROTECTED/FROZEN 由用户管
            try:
                if not is_strategy_managed(p):
                    skipped_protected += 1
                    if PositionSource is not None:
                        _src = get_position_source(p)
                        _ps = get_protection_state(p)
                        logger.info(
                            f"[退出监控·M1·跳过] {p.symbol} source={_src.value if _src else '?'} "
                            f"protection={_ps.value if _ps else '?'} (策略不接管, 用户责任)"
                        )
                    continue
            except Exception:
                # M1 检查异常 → 保守放行 (不阻断)
                pass
            main_positions.append(p)

        if skipped_scalping > 0:
            logger.debug(f"[退出监控] 跳过 {skipped_scalping} 个 scalping 仓位 (ScalpingEngine 自管)")
        if skipped_protected > 0:
            logger.warning(
                f"[退出监控·M1] 跳过 {skipped_protected} 个 PROTECTED/FROZEN 仓位 "
                f"(默认隔离; takeover 接管: scripts/position_takeover.py)"
            )

        # P0-2 (2026-05-14): ExitDispatcher 路由 — 按 owner_strategy 选 ExitPolicy
        # Phase A 多数 ExitPolicy 返回 NO_ACTION (兜底走原 6 规则), 但 thesis-driven exit 可在此抢先决断
        try:
            from app.coordinator.exit_dispatcher import get_exit_dispatcher
            _dispatcher = get_exit_dispatcher()
        except Exception:
            _dispatcher = None

        for position in main_positions:
            try:
                symbol = position.symbol
                direction = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
                entry_price = position.avg_entry_price
                if entry_price <= 0:
                    continue

                # P0-2: 先问 ExitDispatcher (Phase B 后逐步替代 6 规则)
                if _dispatcher is not None:
                    owner = getattr(position, "owner_strategy", None) or "main"
                    policy = _dispatcher.policy_for(owner)
                    if policy is not None:
                        try:
                            current_price = get_price_fn(symbol)
                            ctx = {"symbol": symbol, "current_price": current_price,
                                   "session": current_session, "now_ts": now_ts}
                            decision = policy.evaluate(position, ctx)
                            if isinstance(decision, dict) and decision.get("action") == "CLOSE":
                                logger.info(f"[ExitDispatcher] {symbol} owner={owner} → CLOSE: {decision.get('reason')}")
                                execute_close_fn(position, {
                                    "reason": decision.get("reason", "DISPATCHER_CLOSE"),
                                    "close_ratio": float(decision.get("qty_pct", 1.0)),
                                })
                                closed.append({"symbol": symbol, "reason": decision.get("reason", "DISPATCHER_CLOSE")})
                                continue
                        except Exception as _de:
                            logger.debug(f"[ExitDispatcher] {symbol} evaluate 异常 (fallback 走 6 规则): {_de}")
                # 若 dispatcher 返回 NO_ACTION 或异常 → fallback 走原 6 规则

                # P0 修复 (F15 2026-05-12): 提前初始化 trail_state, 避免 NameError
                # 之前: trail_state 在第 268 行才赋值, 但 line 238 就可能引用 → NameError
                trail_state = position.trail_state if position.trail_state else {}

                # 获取当前价格
                current_price = get_price_fn(symbol)
                # P0 修复 (2026-05-11): 价格类型异常时发送告警 + 使用备用价格继续监控
                # 之前: 仅记录日志并跳过，导致该持仓在监控周期内完全失去止损保护
                if not isinstance(current_price, (int, float)):
                    _prev_price = trail_state.get("prev_scan_price", entry_price)
                    _fallback_price = _prev_price if _prev_price > 0 else entry_price
                    logger.critical(
                        f"[退出监控·P0] {symbol} 价格类型异常: type={type(current_price).__name__}, "
                        f"value={current_price!r}, 使用备用价格 ${_fallback_price:.2f} 继续监控"
                    )
                    # 发送紧急告警
                    try:
                        from app.notifier.dingtalk import DingTalkNotifier
                        notifier = DingTalkNotifier()
                        notifier.send_text(
                            f"🚨 [紧急] {symbol} 价格类型异常\n"
                            f"type={type(current_price).__name__}, value={current_price!r}\n"
                            f"已使用备用价格 ${_fallback_price:.2f} 继续监控止损"
                        )
                    except Exception:
                        pass
                    current_price = _fallback_price

                if current_price is None or current_price <= 0:
                    logger.error(f"[退出监控] {symbol} 价格无效 ({current_price}), 跳过本轮")
                    continue

                # 计算 PnL
                if direction == "LONG":
                    pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                else:
                    pnl_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0

                # 获取 trail_state
                trail_state = position.trail_state if position.trail_state else {}

                # ── 规则 0: 防振荡 (60s) ──
                last_trigger = trail_state.get("last_trigger_time", 0)
                if last_trigger > 0 and (now_ts - last_trigger) < self.COOLDOWN_SECONDS:
                    continue
                # 类级别冷却：防止 position 对象重建后 trail_state 丢失导致重复退出
                last_exit = self._exit_cooldowns.get(symbol, 0)
                if (now_ts - last_exit) < self.COOLDOWN_SECONDS:
                    logger.debug(f"[退出监控] {symbol} 类级别冷却中 ({now_ts - last_exit:.0f}s < {self.COOLDOWN_SECONDS}s)，跳过")
                    continue

                # ── 获取 ATR (带每日缓存) + session 感知 ──
                atr = self._get_atr(symbol)

                # ── P1-1 修复 (2026-05-01): 入场/退出 session 感知对称 + 边界平滑 ──
                # 1. 入场时记录 session ratio（确保入场止损 = 退出止损标准）
                if "entry_session_ratio" not in trail_state:
                    trail_state["entry_session_ratio"] = current_session_ratio
                    trail_state["entry_session"] = current_session
                    trail_state["entry_time_ts"] = now_ts
                    logger.debug(
                        f"[退出监控] {symbol} 记录入场 session={current_session} "
                        f"vol_ratio={current_session_ratio:.2f}"
                    )

                # 2. 平滑融合：入场 ratio → 当前 ratio（60 分钟线性过渡）
                #    防止 session 切换时止损线突变（如 11:30 从 1.2→0.6 暴跌 50%）
                entry_ratio = trail_state.get("entry_session_ratio", current_session_ratio)
                entry_time_ts = trail_state.get("entry_time_ts", now_ts)
                held_minutes = (now_ts - entry_time_ts) / 60.0
                # 融合权重: 0 (刚入场) → 1 (60min 后)
                blend_weight = min(held_minutes / 60.0, 1.0)
                session_ratio = entry_ratio * (1.0 - blend_weight) + current_session_ratio * blend_weight

                effective_atr = atr * session_ratio

                # 日志: session 切换时记录
                if abs(session_ratio - entry_ratio) > 0.1 and blend_weight > 0:
                    logger.debug(
                        f"[退出监控] {symbol} session 融合: entry_ratio={entry_ratio:.2f} "
                        f"→ current={current_session_ratio:.2f} → blended={session_ratio:.2f} "
                        f"(held={held_minutes:.0f}min, blend={blend_weight:.2f})"
                    )

                # P0-2 修正 (2026-04-28): profit_atr 改为有向（signed）
                #   盈利 > 0，亏损 < 0；原 abs() 实现会导致亏损时也激活 Chandelier/保本
                # P2 (2026-05-01): profit_atr 用 effective_atr 计算，高波动时段利润在 ATR 口径下"缩水"
                #   → Chandelier/保本激活门槛更难达到 → 更保守的追踪策略
                profit_atr_signed = compute_profit_atr_signed(direction, entry_price, current_price, effective_atr)
                # 向下兼容：绝对值供 guardian.get_atr_multiplier 的三档分段查询使用
                profit_atr = abs(profit_atr_signed)

                # P5 优化 (2026-05-22): 最小持仓时间检查
                # 数据：<5min 持仓胜率仅 21.7%，过早平仓是亏损主因
                # 策略：持仓 <10min 不触发非硬止损退出（SL_HIT/硬止损除外）
                min_hold_minutes = 10
                # held_minutes already computed at L510 from (now_ts - entry_time_ts) / 60.0
                
                # 硬止损豁免：SL_HIT、STOP_LOSS 等紧急退出不受限制
                hard_stop_reasons = {"SL_HIT", "STOP_LOSS", "PRICE_FAIL"}
                
                if held_minutes < min_hold_minutes:
                    # 检查后续规则是否会触发退出
                    # 如果是硬止损，允许执行；如果是其他退出，跳过
                    _skip_non_hard_exits = True
                    logger.debug(
                        f"[最小持仓] {symbol} 持仓 {held_minutes:.0f}min < {min_hold_minutes}min, "
                        f"仅允许硬止损退出"
                    )
                else:
                    _skip_non_hard_exits = False

                # ── 规则 1: Flash Exit ──
                flash_result = self._check_flash_exit(position, current_price, effective_atr, trail_state, now_ts, direction, policy_guardian, earnings_guard)
                if flash_result and not _skip_non_hard_exits:
                    self._execute_exit(position, flash_result, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": flash_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 规则 1.5: Day Trade Mode (P1 2026-05-01) ──
                # 短线主动管理 — 紧止损 + 小幅止盈 + 利润锁定 + 时间衰减
                daytrade_result = self._check_day_trade_exit(
                    position, current_price, entry_price, effective_atr, trail_state, now_ts, direction, pnl_pct, policy_guardian
                )
                if daytrade_result and not _skip_non_hard_exits:
                    self._execute_exit(position, daytrade_result, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": daytrade_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 规则 2.5: Scale Out 分批止盈 ──
                scale_result = self._check_scale_out(position, current_price, trail_state, direction)
                if scale_result and not _skip_non_hard_exits:
                    self._execute_exit(position, scale_result, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": scale_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 规则 2: 统一跟踪止损 ──
                # Chandelier 移动止损 = 天然止盈：止损跟随峰值上移，回撤自动锁利润
                stop_result = self._check_trailing_stop(position, current_price, entry_price, effective_atr, profit_atr_signed, trail_state, now_ts, direction, policy_guardian)
                if stop_result and not _skip_non_hard_exits:
                    self._execute_exit(position, stop_result, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": stop_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 规则 3: Time Stop (盈利豁免) ──
                time_result = self._check_time_stop(position, current_price, now_ts, direction, policy_guardian)
                if time_result and not _skip_non_hard_exits:
                    self._execute_exit(position, time_result, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": time_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 规则 3.5: Pre-Close Exit ──
                # P1-7 (2026-05-14): 互斥保护 — 若 chandelier_trail 等其他规则刚触发, 跳过 PRE_CLOSE
                # 防止 1 个 position 1 分钟内被多规则连续命中, 导致重复推送
                _meta = getattr(position, "metadata", None) or {}
                if _meta.get("_exit_locked"):
                    logger.debug(f"[盘末退出·互斥] {position.symbol} 已被其他规则锁定, 跳过")
                    continue

                preclose_result = self._check_pre_close_exit(position, current_price, effective_atr, trail_state, direction, policy_guardian)
                if preclose_result:
                    # P1 修复 (2026-05-01): 幂等检查 — 防止重复触发 PRE_CLOSE_EXIT
                    # 如果该持仓已有同日的 PRE_CLOSE_EXIT 记录，跳过
                    exit_reason_key = preclose_result.get("reason", "PRE_CLOSE_EXIT")
                    if self._has_exit_record(position.symbol, exit_reason_key, max_minutes=1440):
                        logger.debug(f"[盘末退出·幂等] {position.symbol} 已有 {exit_reason_key} 记录，跳过")
                    else:
                        # 优化 (2026-05-03): 支持 close_ratio 字段控制减仓比例
                        if preclose_result.get("close_ratio") and preclose_result["close_ratio"] < 1.0:
                            # 部分退出: 计算 close_qty 注入到 preclose_result
                            # 由 _execute_close → _partial_close_position 执行实际订单
                            # P0 fix (2026-05-08): 不再手动修改 position.size —
                            # _partial_close_position 会正确更新 DB 和内存状态
                            orig_size = position.size
                            close_qty = max(1, int(orig_size * preclose_result["close_ratio"]))
                            # P0 fix (2026-05-08): 跳过 partial close 当 close_qty >= orig_size
                            # 根因: size=1, ratio=0.25 → close_qty=max(1,0)=1 → close_qty==orig_size
                            # → _execute_close 走全平路径, 但 Futu 可能拒绝 (数量不合法)
                            # 同时 partial close 对 size<=3 的持仓无意义
                            if close_qty >= orig_size:
                                logger.info(
                                    f"[盘末减仓·跳过] {position.symbol} 持仓过小({orig_size}股), "
                                    f"25%减仓={close_qty}股 >= 全仓, 改为全平退出"
                                )
                                preclose_result["close_ratio"] = 1.0  # 触发全平
                                preclose_result.pop("close_qty", None)
                            else:
                                preclose_result["close_qty"] = close_qty
                                logger.info(
                                    f"[盘末减仓] {position.symbol} {preclose_result['reason_cn']}, "
                                    f"计划平仓 {close_qty}/{orig_size} 股"
                                )
                        self._execute_exit(position, preclose_result, execute_close_fn, trail_state, now_ts)
                        closed.append({"symbol": symbol, "reason": preclose_result["reason"], "pnl_pct": pnl_pct})
                    continue

                # ── 更新 trail_state (即使未触发也要更新 peak 和 prev_scan_price) ──
                self._update_trail_state(position, current_price, trail_state, direction, entry_price, effective_atr, policy_guardian)

            except Exception as e:
                logger.error(f"[退出监控] {position.symbol} 检查异常: {e}", exc_info=True)

        return closed

    # ============================================================
    #  规则 1: Flash Exit
    # ============================================================

    def _check_flash_exit(self, position, current_price, atr, trail_state, now_ts, direction, policy, earnings_guard):
        """Flash Exit: 60s 内波动 > flash_multiplier × ATR (优化: 1.0x阈值, 60s扫描)

        修复 (2026-05-12):
        - opening_drive session 自动设置最低 1.5x ATR (开盘波动本身 2-3x ATR)
        - 避免开盘噪音误触发
        P0-D3 (2026-05-14): 拆股标的跳过 FLASH_EXIT, 防止 -50% 跳价误触发
        """
        if atr <= 0:
            return None

        # P0-D3: 拆股 symbol 跳过 flash exit
        try:
            from app.data.split_event_guard import get_split_guard
            if get_split_guard().is_split_today(position.symbol):
                return None
        except Exception:
            pass

        if policy:
            flash_multiplier = policy.get_flash_multiplier(position.symbol)
            if earnings_guard and hasattr(earnings_guard, 'is_earnings_period'):
                try:
                    if earnings_guard.is_earnings_period(position.symbol):
                        flash_multiplier = policy.policy.get('exit_rules', {}).get('earnings_guard', {}).get('flash_multiplier_override', 0.5)
                except Exception:
                    pass

            # 幻方增强 (2026-04-29): 做空 + 高VIX 环境下收紧 Flash Exit
            if direction == "SHORT":
                try:
                    vix = self._get_vix()
                    if vix and vix > 30:
                        short_vix_mult = 0.6  # 做空回补反弹更暴力
                        flash_multiplier = min(flash_multiplier, short_vix_mult)
                        logger.debug(
                            f"[FlashExit] {position.symbol} SHORT+VIX={vix:.1f}>30, "
                            f"收紧 flash_multiplier -> {flash_multiplier}"
                        )
                except Exception:
                    pass
        else:
            flash_multiplier = 0.8

        # 修复 (2026-05-12): opening_drive session 自动加倍阈值
        try:
            from app.shared.market_hours import get_shared_kernel
            session = get_shared_kernel().get_current_session()
            if session == "opening_drive":
                flash_multiplier = max(flash_multiplier, 1.5)  # 开盘最低 1.5x ATR
        except Exception:
            pass

        # 绝对下限: 1.0x ATR (防止过于敏感)
        flash_multiplier = max(flash_multiplier, 1.0)

        prev_price = trail_state.get("prev_scan_price", current_price)
        trail_state["prev_scan_price"] = current_price

        if prev_price > 0 and prev_price != current_price:
            adverse_change = prev_price - current_price if direction == "LONG" else current_price - prev_price
            if adverse_change > 0 and adverse_change >= flash_multiplier * atr:
                trail_state["last_trigger_time"] = now_ts
                return {
                    "reason": "FLASH_EXIT",
                    "reason_cn": f"异常动量平仓 (快速{'下跌' if direction == 'LONG' else '上涨'})",
                    "exit_price": current_price,
                    "description": f"30s 内波动 {adverse_change:.2f} ({adverse_change/atr:.1f}x ATR)"
                }
        return None

    # ============================================================
    #  规则 1.5: Day Trade Mode (短线主动管理)
    # ============================================================

    def _should_use_day_trade_mode(self, position, policy_guardian=None) -> bool:
        """判断是否应使用日内短线模式

        条件 (来自 exit_policy.yaml day_trade_mode.apply_conditions):
        1. 非 ETF (宽基不适合短线模式)
        2. 非财报期 (财报期波动异常, 使用趋势模式)
        3. 持仓 < 3 天
        """
        dt_cfg = {}
        if policy_guardian and hasattr(policy_guardian, "policy"):
            dt_cfg = policy_guardian.policy.get("day_trade_mode", {})

        if not dt_cfg.get("enabled", True):
            return False

        apply_cond = dt_cfg.get("apply_conditions", {})

        # 排除 ETF (使用 position_limits 中的共享 ETF 列表)
        if apply_cond.get("exclude_etf", True):
            from app.risk.position_limits import ETF_EXPOSURE_DISCOUNT
            if position.symbol in ETF_EXPOSURE_DISCOUNT:
                return False

        # 排除财报期 (由 earnings_guard 注入的标记)
        if apply_cond.get("exclude_earnings_period", True):
            if getattr(position, "trail_state", {}).get("is_earnings_period", False):
                return False

        # 持仓天数检查
        max_days = int(apply_cond.get("max_holding_days", 3))
        if position.entry_time:
            try:
                now_et = datetime.now(ET) if ET else datetime.now()
                entry_time = position.entry_time
                # 防御: 确保两个 datetime 都是 aware 或都是 naive 才能相减
                if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                    if ET and hasattr(now_et, 'tzinfo') and now_et.tzinfo is not None:
                        entry_time = entry_time.replace(tzinfo=ET)
                held_days = (now_et - entry_time).total_seconds() / 86400.0
                if held_days > max_days:
                    return False
            except Exception:
                pass  # 无法判断则放行

        return True

    def _check_day_trade_exit(self, position, current_price, entry_price, atr, trail_state, now_ts,
                               direction, pnl_pct, policy_guardian=None):
        """Day Trade Mode: 紧止损 + 主动止盈 + 利润锁定 + 时间衰减

        在 scan_positions 中 flash_exit 之后、scale_out 之前调用。
        仅对满足 day_trade_mode 条件的个股持仓生效。

        策略 (来自 exit_policy.yaml day_trade_mode):
        1. Initial Stop: 1.8x ATR 或 5% max loss (做空: 1.5x / 5%)  [优化 2026-05-03]
        2. Take Profit Tier 1: +3% 平 50% → 剩余移保本 (做空: +2%)
        3. Take Profit Tier 2: +6% 平剩余 (做空: +5%)
        4. Profit Lock: 浮盈≥2.0% → 回撤到 +0.8% 时强制退出
        5. Time Decay: 150min 无进展自动退出 (做空: 120min)

        幻方 P2 (2026-05-01):
          - atr 已是 session-aware (effective_atr = raw_atr × session_vol_ratio)
          - entry_session 路径依赖: 低置信度 session 入场 → 收紧 ×0.85
        """
        if not self._should_use_day_trade_mode(position, policy_guardian):
            return None

        dt_cfg = {}
        if policy_guardian and hasattr(policy_guardian, "policy"):
            dt_cfg = policy_guardian.policy.get("day_trade_mode", {})
        if not dt_cfg:
            return None

        is_short = (direction == "SHORT")
        short_cfg = dt_cfg.get("short", {})

        # ── 幻方 P2: entry_session 路径依赖 ──
        # OPEN 入场 → 信号置信度 0.75x → 止损收紧 0.85x
        # CLOSE 入场 → 信号置信度 1.0x → 止损正常
        entry_session = trail_state.get("entry_session", "")
        entry_session_mult = 1.0
        if entry_session == "opening_drive":
            entry_session_mult = 0.85   # 开盘入场, 止损更紧
        elif entry_session == "morning":
            entry_session_mult = 0.92   # 早盘入场, 轻微收紧

        # ── 1. Initial Stop ──
        init_stop = dt_cfg.get("initial_stop", {})
        stop_atr_mult = float(init_stop.get("atr_multiplier", 1.5)) * entry_session_mult
        max_loss = float(init_stop.get("max_loss_pct", 5.0)) / 100.0 * entry_session_mult
        if is_short:
            stop_atr_mult = float(short_cfg.get("initial_stop_atr", 1.2)) * entry_session_mult
            max_loss = float(short_cfg.get("max_loss_pct", 4.0)) / 100.0 * entry_session_mult

        if atr > 0:
            atr_stop_price = entry_price - stop_atr_mult * atr if not is_short else entry_price + stop_atr_mult * atr
        else:
            atr_stop_price = entry_price * (1 - max_loss) if not is_short else entry_price * (1 + max_loss)

        hard_stop_price = entry_price * (1 - max_loss) if not is_short else entry_price * (1 + max_loss)

        # 取更紧的止损
        if not is_short:
            effective_stop = max(atr_stop_price, hard_stop_price)
            if current_price <= effective_stop:
                return {
                    "reason": "DAYTRADE_STOP",
                    "reason_cn": f"短线止损 (ATR {stop_atr_mult:.1f}x / max -{max_loss*100:.0f}%)",
                    "exit_price": current_price,
                    "description": f"触及短线止损 ${effective_stop:.2f} (ATR止损={atr_stop_price:.2f}, 硬止损={hard_stop_price:.2f})"
                }
        else:
            effective_stop = min(atr_stop_price, hard_stop_price)
            if current_price >= effective_stop:
                return {
                    "reason": "DAYTRADE_STOP",
                    "reason_cn": f"做空短线止损 (ATR {stop_atr_mult:.1f}x / max +{max_loss*100:.0f}%)",
                    "exit_price": current_price,
                    "description": f"触及做空短线止损 ${effective_stop:.2f}"
                }

        # ── 2. Take Profit Tiers ──
        tp_cfg = dt_cfg.get("take_profit", {})
        triggered = trail_state.get("dt_triggered_tiers", [])

        # Tier 1: +2% (做空 +1.5%)
        tp1_pct = float(tp_cfg.get("tier_1", {}).get("profit_pct", 2.0)) / 100.0
        if is_short:
            tp1_pct = float(short_cfg.get("take_profit_tier_1", 1.5)) / 100.0
        tp1_close = float(tp_cfg.get("tier_1", {}).get("close_pct", 50)) / 100.0

        if 0 not in triggered and pnl_pct >= tp1_pct:
            close_qty = max(1, int(position.size * tp1_close))
            triggered.append(0)
            trail_state["dt_triggered_tiers"] = triggered
            # 移动止损到保本
            trail_state["dt_breakeven_activated"] = True
            return {
                "reason": "DAYTRADE_TP1",
                "reason_cn": f"短线止盈 Tier1 (+{tp1_pct*100:.0f}% 平 {tp1_close*100:.0f}%)",
                "exit_price": current_price,
                "close_qty": close_qty,
                "description": f"短线止盈 Tier1: +{pnl_pct*100:.1f}% ≥ {tp1_pct*100:.0f}%, 平仓 {close_qty} 股, 剩余移保本"
            }

        # Tier 2: +5% (做空 +4%)
        tp2_pct = float(tp_cfg.get("tier_2", {}).get("profit_pct", 5.0)) / 100.0
        if is_short:
            tp2_pct = float(short_cfg.get("take_profit_tier_2", 4.0)) / 100.0
        tp2_close = float(tp_cfg.get("tier_2", {}).get("close_pct", 50)) / 100.0

        if 1 not in triggered and pnl_pct >= tp2_pct:
            triggered.append(1)
            trail_state["dt_triggered_tiers"] = triggered
            return {
                "reason": "DAYTRADE_TP2",
                "reason_cn": f"短线止盈 Tier2 (+{tp2_pct*100:.0f}% 清仓)",
                "exit_price": current_price,
                "description": f"短线止盈 Tier2: +{pnl_pct*100:.1f}% ≥ {tp2_pct*100:.0f}%, 清仓"
            }

        # ── 3. Profit Lock (浮盈保护) ──
        pl_cfg = dt_cfg.get("profit_lock", {})
        if pl_cfg.get("enabled", True):
            activation = float(pl_cfg.get("activation_pct", 1.5)) / 100.0
            lock_level = float(pl_cfg.get("lock_level_pct", 0.5)) / 100.0

            if pnl_pct >= activation:
                trail_state["dt_profit_lock_activated"] = True
                trail_state["dt_peak_pnl"] = max(trail_state.get("dt_peak_pnl", 0), pnl_pct)

            if trail_state.get("dt_profit_lock_activated") and pnl_pct <= lock_level:
                return {
                    "reason": "DAYTRADE_PROFIT_LOCK",
                    "reason_cn": f"利润锁定 (峰值 +{trail_state.get('dt_peak_pnl', 0)*100:.1f}% → 当前 +{pnl_pct*100:.1f}%)",
                    "exit_price": current_price,
                    "description": f"浮盈回撤到 {lock_level*100:.1f}% 锁定线, 保护已实现利润"
                }

        # 保本检查 (Tier1 触发后)
        if trail_state.get("dt_breakeven_activated") and pnl_pct <= 0.002:  # 0.2% buffer
            return {
                "reason": "DAYTRADE_BREAKEVEN",
                "reason_cn": "短线保本退出",
                "exit_price": current_price,
                "description": "Tier1 触发后回撤至保本线, 确保不亏损"
            }

        # ── 4. Time Decay ──
        td_cfg = dt_cfg.get("time_decay", {})
        if td_cfg.get("enabled", True):
            max_idle = int(td_cfg.get("max_idle_minutes", 120)) * 60
            min_progress = float(td_cfg.get("min_progress_pct", 0.3)) / 100.0
            if is_short:
                max_idle = int(short_cfg.get("max_idle_minutes", 90)) * 60

            # ── 幻方 P2 (2026-05-01): session-aware decay rate ──
            # 波动越大 → 价格变化越快 → 信号半衰期越短 → 有效时间缩短
            # 波动越小 → 价格几乎不动 → 信号半衰期延长 → 有效时间放宽
            # 例: OPEN (ratio=1.5) → 有效衰减时间 = 120min / 1.5 = 80min
            #     LUNCH (ratio=0.6) → 有效衰减时间 = 120min / 0.6 = 200min
            session = get_shared_kernel().get_current_session()
            session_ratio = get_shared_kernel().get_session_vol_ratio(session)
            effective_max_idle = max_idle / max(session_ratio, 0.3)

            # 修复 (2026-05-12): 隔夜持仓重置基准时间
            # 隔夜持仓进入 Day Trade Mode 后, 开盘立即超时 (持仓 909min > 80min)
            if "dt_baseline_pnl" not in trail_state:
                entry_time = getattr(position, 'entry_time', None)
                if entry_time:
                    try:
                        from datetime import datetime as _dt
                        if isinstance(entry_time, str):
                            et = _dt.fromisoformat(entry_time.replace(' ', 'T'))
                        else:
                            et = entry_time
                        holding_minutes = (now_ts - et.timestamp()) / 60
                        if holding_minutes > 60:
                            # 隔夜持仓: 重置基准时间为现在, 给予新的时间窗口
                            trail_state["dt_baseline_time"] = now_ts
                            trail_state["dt_baseline_pnl"] = pnl_pct
                            logger.info(
                                f"[DayTrade·time_decay] {position.symbol} 隔夜持仓 ({holding_minutes:.0f}min), "
                                f"重置时间衰减基准 (有效超时={effective_max_idle/60:.0f}min)"
                            )
                    except Exception:
                        pass
                if "dt_baseline_pnl" not in trail_state:
                    trail_state["dt_baseline_pnl"] = pnl_pct
                    trail_state["dt_baseline_time"] = now_ts

            # 首次扫描宽限期: 前 30min 不触发 time_decay
            idle_seconds = now_ts - trail_state.get("dt_baseline_time", now_ts)
            if idle_seconds < 1800:
                return None

            baseline_pnl = trail_state.get("dt_baseline_pnl", 0)
            progress = pnl_pct - baseline_pnl

            if progress >= min_progress:
                # 有进展, 重置基准
                trail_state["dt_baseline_pnl"] = pnl_pct
                trail_state["dt_baseline_time"] = now_ts
            else:
                idle_seconds = now_ts - trail_state.get("dt_baseline_time", now_ts)
                if idle_seconds >= effective_max_idle:
                    return {
                        "reason": "DAYTRADE_TIME_DECAY",
                        "reason_cn": f"短线时间衰减 ({idle_seconds/60:.0f}min 无进展, session={session})",
                        "exit_price": current_price,
                        "description": (
                            f"持仓 {idle_seconds/60:.0f}min 无进展 "
                            f"(基准={baseline_pnl*100:.1f}%, 当前={pnl_pct*100:.1f}%, "
                            f"进展={progress*100:.1f}% < {min_progress*100:.1f}%, "
                            f"有效超时={effective_max_idle/60:.0f}min)"
                        )
                    }

        return None

    def _check_scale_out(self, position, current_price, trail_state, direction):
        """分批止盈: 触及 take_profit_levels 中未触发的价格层级则部分平仓"""
        tiers = getattr(position, 'take_profit_levels', []) or []
        if not tiers:
            return None

        triggered = trail_state.get("triggered_tiers", [])
        original_size = trail_state.get("scale_out_original_size", position.size)
        trail_state["scale_out_original_size"] = original_size

        for i, tier in enumerate(tiers):
            if i in triggered:
                continue

            trigger_price = tier.get("price", 0)
            if trigger_price <= 0:
                continue

            hit = (direction == "LONG" and current_price >= trigger_price) or \
                  (direction == "SHORT" and current_price <= trigger_price)
            if not hit:
                continue

            close_ratio = tier.get("ratio", 0.33)
            close_qty = max(1, int(original_size * close_ratio))
            if close_qty <= 0:
                continue

            triggered.append(i)
            trail_state["triggered_tiers"] = triggered
            return {
                "reason": "SCALE_OUT",
                "reason_cn": f"分批止盈 Tier {i+1}/{len(tiers)} (+{current_price/position.avg_entry_price-1:.1%})" if direction == "LONG"
                             else f"分批止盈 Tier {i+1}/{len(tiers)}",
                "exit_price": current_price,
                "close_qty": close_qty,
                "description": f"触及止盈价 ${trigger_price:.2f} (Tier {i+1}), 平仓 {close_qty} 股 ({close_ratio:.0%})"
            }

        return None

    # ============================================================
    #  规则 3: Time Stop
    # ============================================================

    def _check_time_stop(self, position, current_price, now_ts, direction, policy_guardian=None):
        """Time Stop: 持仓 > 最大市场小时数 (盈利豁免)

        P0-3 修正 (2026-04-29):
          - 从 exit_policy 读取 max_holding_hours / min_profit_to_hold / extension_hours，不再硬编码
          - 盈利豁免使用 ATR 归一化阈值 (profit_atr >= 1.0)，低波动股票兜底 min_profit_to_hold_pct

        O3 (2026-05-16): policy.time_stop.enabled=false 时整体跳过 (30d sharpe=-0.40 禁用决策)
        """
        # O3: 整体禁用闸 (yaml 优先, policy_guardian 兜底)
        try:
            if policy_guardian is not None and hasattr(policy_guardian, "policy"):
                _ts_cfg = policy_guardian.policy.get("exit_rules", {}).get("time_stop", {})
                if _ts_cfg.get("enabled", True) is False:
                    return None
        except Exception:
            pass
        try:
            entry_time = position.entry_time
            if entry_time is None:
                return None
            if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=ET) if ET else entry_time
            held_hours = get_market_hours_us(entry_time, datetime.now(ET) if ET else datetime.now())

            # 从 exit_policy 读取配置（带默认兜底）
            max_hours = 72
            min_profit_pct = 0.02
            max_extensions = 2
            extension_hours = 24
            if policy_guardian is not None and hasattr(policy_guardian, "policy"):
                ts_cfg = policy_guardian.policy.get("exit_rules", {}).get("time_stop", {})

                # 幻方增强 (2026-04-29): 做空差异化时间止损
                if direction == "SHORT":
                    short_ts = policy_guardian.policy.get("short_overrides", {}).get("time_stop", {})
                    max_hours = int(short_ts.get("max_holding_hours", 48))
                    # 时间加速收紧: day2 0.8x, day3 0.6x
                    held_days = held_hours / 24.0 if held_hours > 0 else 0
                    accel_enabled = short_ts.get("acceleration_enabled", True)
                    if accel_enabled and held_days >= 3:
                        max_hours = int(max_hours * float(short_ts.get("day3_multiplier", 0.6)))
                    elif accel_enabled and held_days >= 2:
                        max_hours = int(max_hours * float(short_ts.get("day2_multiplier", 0.8)))
                else:
                    max_hours = int(ts_cfg.get("max_holding_hours", 72))

                min_profit_pct = float(ts_cfg.get("min_profit_to_hold", 2.0)) / 100.0
                extension_hours = int(ts_cfg.get("extension_hours", 24))

            # P0-3: ATR 归一化盈利豁免 — 盈利≥1 ATR 或达到最小百分比阈值
            if current_price and current_price > 0 and position.avg_entry_price > 0:
                atr = self._get_atr(position.symbol)
                profit_atr_signed = compute_profit_atr_signed(
                    direction, position.avg_entry_price, current_price, atr
                ) if atr > 0 else 0.0

                if direction == "LONG":
                    pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
                else:
                    pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price

                # 盈利≥1 ATR (真盈利) 或 ≥ min_profit_pct (低波动兜底)
                if profit_atr_signed >= 1.0 or pnl_pct >= min_profit_pct:
                    ts = position.trail_state or {}
                    extensions = ts.get("time_stop_extensions", 0)
                    max_ext = max_extensions + int(held_hours / max_hours)  # P0-3: 累计按 max_hours 倍数加次数
                    if extensions < max_ext:
                        ts["time_stop_extensions"] = extensions + 1
                        position.trail_state = ts
                        new_limit = max_hours + extensions * extension_hours
                        logger.debug(
                            f"[时间止损·豁免] {position.symbol} 浮盈 {pnl_pct*100:.1f}% "
                            f"({profit_atr_signed:+.1f} ATR), 豁免时间止损 "
                            f"(延长次数 {extensions + 1}/{max_ext}, 新限额 {new_limit}h)"
                        )
                        return None

            if held_hours >= max_hours:
                return {
                    "reason": "TIME_STOP",
                    "reason_cn": "时间止损",
                    "exit_price": 0,
                    "description": f"持仓 {held_hours:.1f}h >= {max_hours}h"
                }
        except Exception as e:
            logger.debug(f"时间止损计算失败 {position.symbol}: {e}")
        return None

    # ============================================================
    #  规则 3.5: Pre-Close Exit 盘末强制退出
    # ============================================================

    def _check_pre_close_exit(self, position, current_price, atr, trail_state, direction, policy_guardian=None):
        """盘末强制退出: 分层退出 + 时间加权策略 (2026-05-03 P2 优化)

        根因修复: 原单一 1 ATR 阈值导致 54% 仓位被动强平, 平均利润仅 +0.08%
        
        新方案 (P2 时间加权):
        - 距收盘 <5min (紧急窗口): 所有阈值 ×0.6, 更积极退出
        - 距收盘 5~10min (警告窗口): 所有阈值 ×0.8
        - 距收盘 10~15min (标准窗口): 所有阈值 ×1.0
        - 层1: 亏损仓位 → 全平 (不持有亏损过夜)
        - 层2: 0~阈值A ATR 微利 → 减半仓 (降低隔夜风险敞口)
        - 层3: 阈值A~阈值B ATR → 减 1/4 (保留大部分仓位)
        - 层4: ≥阈值B ATR → 保留过夜

        P0 修复 (2026-05-02): 添加冷却期检查 — 防止同一标的在盘末被反复平仓/重入。
        """
        try:
            if ET:
                now_et = datetime.now(ET)
            else:
                now_et = datetime.now()

            if now_et.weekday() >= 5:
                return None

            # 从 policy 读取预收盘退出配置
            pc_cfg = {}
            if policy_guardian is not None and hasattr(policy_guardian, "policy"):
                pc_cfg = policy_guardian.policy.get("exit_rules", {}).get("pre_close_exit", {})
            
            # C4 修复 (2026-05-13): 全局 EOD 配置优先
            # 主线 + Scalping 共享同一收盘协议, exit_policy.yaml 值作为 fallback
            eod_cfg = load_eod_config()

            window_minutes = int(pc_cfg.get("window_minutes", 15))
            minutes_to_close = 16 * 60 - (now_et.hour * 60 + now_et.minute)
            if minutes_to_close > window_minutes or minutes_to_close < 0:
                return None

            # ── P0 修复 (2026-05-02): 冷却期检查 ──
            last_scan = trail_state.get('_last_pre_close_check', 0)
            now_ts = now_et.timestamp()
            if now_ts - last_scan < 60:  # 60秒冷却
                return None
            trail_state['_last_pre_close_check'] = now_ts

            # ── P1 修复 (2026-05-08): 入场后最小持有时间保护 ──
            # 根因: 信号系统在盘末生成信号并开仓 → 立即被 PRE_CLOSE_EXIT 强制平仓 → 白亏手续费
            # 修复: 持仓 < 30 分钟不触发盘末退出 (给仓位充分发展时间)
            entry_time = position.entry_time
            if entry_time:
                try:
                    if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=ET) if ET else entry_time
                    held_minutes = (now_et - entry_time).total_seconds() / 60.0
                    min_hold_minutes = float(pc_cfg.get("min_hold_minutes_before_pre_close", eod_cfg.min_hold_minutes_before_eod_exit))
                    if held_minutes < min_hold_minutes:
                        logger.debug(
                            f"[盘末退出·跳过] {position.symbol} 入场仅 {held_minutes:.0f} 分钟 "
                            f"< {min_hold_minutes:.0f} 分钟，不触发盘末退出"
                        )
                        return None
                except Exception:
                    pass  # 无法判断则放行

            if atr <= 0 or position.avg_entry_price <= 0:
                return None

            profit_atr_signed = compute_profit_atr_signed(direction, position.avg_entry_price, current_price, atr)

            # ── P2 优化 (2026-05-03): 时间加权阈值 ──
            time_weighted = pc_cfg.get("time_weighted", False)
            threshold_mult = 1.0  # 默认标准窗口
            time_window_label = "standard"

            if time_weighted:
                if minutes_to_close < 5:
                    threshold_mult = float(pc_cfg.get("urgent_threshold_mult", 0.6))
                    time_window_label = "urgent"
                elif minutes_to_close < 10:
                    threshold_mult = float(pc_cfg.get("warning_threshold_mult", 0.8))
                    time_window_label = "warning"

            # 层4: ≥hold_threshold ATR → 保留过夜 (应用时间加权)
            hold_threshold = float(pc_cfg.get("hold_threshold_atr", 1.0)) * threshold_mult
            
            # P1 修复 (2026-05-11): 隔夜风险敞口总上限 — 防止 100% 资金暴露于隔夜 gap
            # 计算所有允许过夜的持仓总价值
            max_overnight_pct = float(pc_cfg.get("max_overnight_exposure_pct", 0.50))  # 默认 50%
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                account_equity = 0.0
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    cs = get_capital_safety_system()
                    account_equity = getattr(cs._state, 'current_equity', 1000000)
                except Exception:
                    account_equity = float(cfg.get_dotted("capital.fixed_amount", 1000000))
                
                # 检查 scan_positions 传入的 positions 列表 (通过闭包或全局)
                # 这里用 trail_state 中的上下文或重新查询
                # 简化方案: 对持仓 > max_overnight_pct 的单个标的收紧门槛
                position_value = current_price * position.size
                position_pct = position_value / account_equity if account_equity > 0 else 0
                if position_pct > max_overnight_pct * 0.3:  # 单只 > 总敞口上限的 30% → 收紧
                    hold_threshold *= 1.5  # 要求更高盈利才允许过夜
                    logger.warning(
                        f"[盘末退出·隔夜收紧] {position.symbol} 单只占比 {position_pct:.0%}, "
                        f"hold_threshold 提高至 {hold_threshold:.1f} ATR"
                    )
            except Exception as e:
                logger.debug(f"[盘末退出·隔夜检查] 失败: {e}")
            
            if profit_atr_signed >= hold_threshold:
                position_value = current_price * position.size
                if position_value > 100_000:
                    logger.warning(
                        f"⚠️ [盘末退出·豁免·大额] {position.symbol} "
                        f"浮盈 {profit_atr_signed:.1f} ATR (阈值{hold_threshold:.1f}, {time_window_label}窗口), "
                        f"持仓价值 ${position_value:,.0f}, "
                        f"允许过夜 — 请确认这是有意持仓"
                    )
                else:
                    logger.debug(
                        f"[盘末退出·跳过] {position.symbol} 浮盈 {profit_atr_signed:.1f} ATR ≥ {hold_threshold:.1f} "
                        f"({time_window_label}窗口), 允许持仓过夜"
                    )
                return None

            # ── 分层退出 (应用时间加权阈值) ──
            loss_flat = pc_cfg.get("loss_flat", True)
            half_exit_atr = float(pc_cfg.get("half_exit_below_atr", 0.5)) * threshold_mult
            quarter_exit_atr = float(pc_cfg.get("quarter_exit_below_atr", 1.0)) * threshold_mult

            # 层1: 亏损仓位 → 全平
            if profit_atr_signed < 0:
                if loss_flat:
                    return {
                        "reason": "PRE_CLOSE_EXIT",
                        "reason_cn": f"盘末强制退出 (亏损仓位, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                        "exit_price": current_price,
                        "description": (
                            f"浮亏 {profit_atr_signed:.1f} ATR, "
                            f"距收盘 {minutes_to_close} 分钟 ({time_window_label}窗口), "
                            f"不持有亏损过夜"
                        ),
                        "close_ratio": 1.0,  # 全平
                    }

            # 层2: 0~half_exit_atr 微利 → 减半仓
            elif profit_atr_signed < half_exit_atr:
                return {
                    "reason": "PRE_CLOSE_EXIT_PARTIAL",
                    "reason_cn": f"盘末减仓50% (微利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                    "exit_price": current_price,
                    "description": (
                        f"浮盈 {profit_atr_signed:.1f} ATR < {half_exit_atr:.1f} "
                        f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                        f"减仓50%降低隔夜风险"
                    ),
                    "close_ratio": 0.5,  # 减半
                }

            # 层3: half_exit_atr~quarter_exit_atr → 减 1/4
            elif profit_atr_signed < quarter_exit_atr:
                return {
                    "reason": "PRE_CLOSE_EXIT_PARTIAL",
                    "reason_cn": f"盘末减仓25% (盈利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                    "exit_price": current_price,
                    "description": (
                        f"浮盈 {profit_atr_signed:.1f} ATR < {quarter_exit_atr:.1f} "
                        f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                        f"减仓25%"
                    ),
                    "close_ratio": 0.25,  # 减1/4
                }

            # 兜底: 不应到达此处 (profit_atr >= hold_threshold 已在上面返回)
            return None

        except Exception as e:
            logger.debug(f"盘末退出检查失败 {position.symbol}: {e}")
        return None

    # ============================================================
    #  规则 2: 统一跟踪止损
    # ============================================================

    def _check_trailing_stop(self, position, current_price, entry_price, atr, profit_atr_signed, trail_state, now_ts, direction, policy):
        """ATR 动态止损 + Chandelier 追踪 + 保本触发

        P0-2 修正 (2026-04-28)：
          - profit_atr_signed 为有向：盈利>0，亏损<0；亏损禁止激活 chandelier/breakeven
          - Regime 三档 trail_mult（normal/trending/volatile）
          - cost buffer：止损线额外让出 k × total_cost_bps
          - 核心定价逻辑下沉到 atr_stop_engine.compute_stop_lines
        """
        # P2 debug (2026-05-01): Validate types early
        if not isinstance(current_price, (int, float)):
            raise TypeError(f"current_price is {type(current_price).__name__}: {current_price!r}")
        # 1. 解析 policy dict
        policy_dict = policy.policy if (policy is not None and hasattr(policy, "policy")) else {}

        # 2. ATR 倍数（三档非线性；guardian 已处理做空 / 行业覆盖）
        profit_atr_abs = abs(profit_atr_signed)
        if policy is not None and hasattr(policy, "get_atr_multiplier"):
            try:
                atr_mult = float(policy.get_atr_multiplier(position.symbol, direction, profit_atr_abs))
            except Exception:
                atr_mult = 2.0  # F32 修复: exit_policy.yaml
        else:
            # 降级三档
            if profit_atr_abs >= 5.0:
                atr_mult = 1.5
            elif profit_atr_abs >= 1.0:
                atr_mult = 2.0
            else:
                atr_mult = 2.0  # F32 修复: exit_policy.yaml

        # 3. peak/trough 更新（LONG=最高，SHORT=最低）
        if direction == "LONG":
            peak_price = trail_state.get("highest_price", entry_price)
            if current_price > peak_price:
                peak_price = current_price
            trail_state["highest_price"] = peak_price
        else:
            peak_price = trail_state.get("lowest_price", entry_price)
            if current_price < peak_price:
                peak_price = current_price
            trail_state["lowest_price"] = peak_price

        # 4. Regime：trail_state / position 注入，默认 normal
        regime = "normal"
        if isinstance(trail_state, dict) and trail_state.get("regime"):
            regime = str(trail_state["regime"])
        elif hasattr(position, "regime") and getattr(position, "regime"):
            regime = str(getattr(position, "regime"))

        # 5. cost_bps 估算（可选，失败退化为 0）
        cost_bps = 0.0
        if self._cost_model is not None:
            try:
                # P2 debug (2026-05-01): Log current_price type
                if not isinstance(current_price, (int, float)):
                    logger.error(f"[_check_trailing_stop] {position.symbol} current_price type={type(current_price).__name__}, value={current_price!r}")
                asset_class = "option" if (len(position.symbol) > 12 and any(c in position.symbol for c in ("P0", "C0"))) else "stock"
                fake_signal = {
                    "symbol": position.symbol,
                    "asset_class": asset_class,
                    "direction": direction,
                    "quantity": int(getattr(position, "size", 0) or 0),
                    "price": float(current_price),
                }
                market_context = {
                    "spread_bps": 3.0,
                    "adv_usd": 0.0,
                    "session": "RTH",
                }
                cost = self._cost_model.estimate(fake_signal, market_context)
                if cost is not None and not getattr(cost, "rejected", False):
                    cost_bps = float(getattr(cost, "total_bps", 0.0))
            except Exception as e:
                logger.debug(f"[{position.symbol}] cost 估算失败，cost_bps=0: {e}")
                cost_bps = 0.0

        # 6. 调用纯函数引擎
        r = compute_stop_lines(
            direction=direction,
            entry_price=entry_price,
            current_price=current_price,
            atr=atr,
            peak_price=peak_price,
            policy=policy_dict,
            regime=regime,
            atr_mult=atr_mult,
            cost_bps=cost_bps,
        )

        # 7. 幻方增强 (2026-04-29): 做空技术破位快速平仓
        #    如果当前价突破 20 日阻力位 + ATR 缓冲，无视 ATR 止损线，立即平仓
        #    P0 修复 (2026-05-08): 禁止降级到 peak_price — 无真实阻力位时不触发
        #    P0 修复 (2026-05-08): 增加最小亏损门槛 + 入场冷却，避免反复进出收割
        if direction == "SHORT" and atr > 0:
            try:
                breach_cfg = policy_dict.get("short_overrides", {}).get("technical_breach", {})
                # P0 修复 (2026-05-08): 检查 enabled 开关
                if not breach_cfg.get("enabled", True):
                    logger.debug(f"[技术破位] {position.symbol} SHORT 已禁用，跳过")
                else:
                    breach_atr_mult = float(breach_cfg.get("trigger_atr_mult", 0.3))
                    # 尝试从 position 读取阻力位
                    resistance = getattr(position, "trail_state", {}).get("resistance_20d", 0)
                    # P0 修复 (2026-05-08): 禁止降级到 peak_price — 必须有真实阻力位
                    if resistance <= 0:
                        logger.debug(
                            f"[技术破位] {position.symbol} SHORT 无 resistance_20d，跳过"
                            f"(peak_price={peak_price:.2f} 不能作为阻力位代理)"
                        )
                    else:
                        # P0 修复 (2026-05-08): 最小亏损门槛 — 避免微亏/盈利时被误触发
                        min_loss_pct = float(breach_cfg.get("min_loss_pct", 0.5))  # 默认 -0.5%
                        pnl_pct_now = (entry_price - current_price) / entry_price  # SHORT: 正=盈利
                        if pnl_pct_now > -min_loss_pct / 100.0:
                            logger.debug(
                                f"[技术破位] {position.symbol} SHORT pnl={pnl_pct_now*100:.2f}% "
                                f"> -{min_loss_pct}%，跳过 (未达亏损门槛)"
                            )
                        else:
                            # P0 修复 (2026-05-08): 入场冷却 — 防止刚入场就被技术破位打掉
                            entry_ts = trail_state.get("entry_time_ts", now_ts)
                            cooldown_minutes = float(breach_cfg.get("entry_cooldown_minutes", 30))
                            held_minutes = (now_ts - entry_ts) / 60.0
                            if held_minutes < cooldown_minutes:
                                logger.debug(
                                    f"[技术破位] {position.symbol} SHORT 入场冷却中 "
                                    f"({held_minutes:.0f}min < {cooldown_minutes:.0f}min)，跳过"
                                )
                            elif current_price > resistance + atr * breach_atr_mult:
                                r.triggered = True
                                r.reason_code = "TECHNICAL_BREACH"
                                r.reason_cn = "做空技术破位平仓"
                                r.final_stop = resistance
                                logger.warning(
                                    f"[技术破位] {position.symbol} SHORT "
                                    f"current=${current_price:.2f} > resistance=${resistance:.2f} + "
                                    f"{breach_atr_mult}×ATR={atr:.3f} | pnl={pnl_pct_now*100:.2f}%"
                                )
            except Exception as e:
                logger.debug(f"[技术破位] 检查失败 {position.symbol}: {e}")

        # 8. 未触发 → 返回 None
        if not r.triggered:
            return None

        # 9. 触发 → 组装结果
        trail_state["last_trigger_time"] = now_ts
        pnl_pct = (current_price - entry_price) / entry_price if direction == "LONG" \
            else (entry_price - current_price) / entry_price
        return {
            "reason": r.reason_code,
            "reason_cn": r.reason_cn,
            "exit_price": current_price,
            "pnl_pct": pnl_pct,
            "stop_price": r.final_stop,
            "description": (
                f"触及止损 {r.final_stop:.2f} (Entry={entry_price}, Peak={peak_price}, "
                f"ATR={atr:.3f}, atr_mult={r.atr_mult}, trail_mult={r.trail_mult}, "
                f"profit_atr={r.profit_atr_signed:+.2f}, cost_buf={r.cost_buffer_price:.3f})"
            ),
        }

    # ============================================================
    #  trail_state 更新
    # ============================================================

    def _update_trail_state(self, position, current_price, trail_state, direction, entry_price, atr, policy_guardian=None):
        """更新持仓的 trail_state (即使未触发也需更新 peak/prev)

        P0-3 修正 (2026-04-29): 使用 exit_policy 配置的 regime 差异化 trail_mult，
        不再硬编码 1.5/2.0。调用 atr_stop_engine.resolve_trail_multiplier 保证与触发逻辑一致。
        """
        if direction == "LONG":
            highest = trail_state.get("highest_price", entry_price)
            if current_price > highest:
                trail_state["highest_price"] = current_price
        else:
            lowest = trail_state.get("lowest_price", entry_price)
            if current_price < lowest:
                trail_state["lowest_price"] = current_price

        position.trail_state = trail_state

        if atr > 0:
            profit_atr_signed = compute_profit_atr_signed(direction, entry_price, current_price, atr)
            policy_dict = {}
            if policy_guardian is not None and hasattr(policy_guardian, "policy"):
                policy_dict = policy_guardian.policy or {}

            # CR-3 (2026-05-15): SL 单调收紧守护 — 防 chandelier 保利后被 ATR 放松
            # 旧 bug: chandelier 把 SL 提到 entry 之上 (保利) → 后续 ATR 重算把 SL 拉回 entry 之下 → 保利失效
            # 新规: LONG 的 current_sl 单调升 (越高越紧); SHORT 单调降 (越低越紧)
            _old_sl = float(getattr(position, "current_sl", 0) or 0)
            _new_sl = None

            if direction == "LONG":
                if profit_atr_signed >= 2.0:
                    regime = trail_state.get("regime", "normal")
                    trail_mult = resolve_trail_multiplier(policy_dict, regime, "LONG")
                    _new_sl = trail_state.get("highest_price", entry_price) - atr * trail_mult
                else:
                    atr_mult = float(policy_dict.get("exit_rules", {}).get("atr_stop", {}).get(
                        "multiplier_by_profit", {}).get("initial", 2.5))
                    _new_sl = entry_price - atr * atr_mult
                # CR-3: LONG SL 单调升 (max), 防止放松
                if _old_sl > 0:
                    position.current_sl = max(_old_sl, _new_sl)
                    if _new_sl < _old_sl:
                        logger.debug(
                            f"[SL·CR-3] {position.symbol} LONG 守护: 拒放松 "
                            f"old=${_old_sl:.2f} new=${_new_sl:.2f} → 保持 ${_old_sl:.2f}"
                        )
                else:
                    position.current_sl = _new_sl
            else:
                if profit_atr_signed >= 2.0:
                    regime = trail_state.get("regime", "normal")
                    trail_mult = resolve_trail_multiplier(policy_dict, regime, "SHORT")
                    _new_sl = trail_state.get("lowest_price", entry_price) + atr * trail_mult
                else:
                    short_mult = float(policy_dict.get("short_overrides", {}).get("atr_multiplier", 2.0))
                    _new_sl = entry_price + atr * short_mult
                # CR-3: SHORT SL 单调降 (min)
                if _old_sl > 0:
                    position.current_sl = min(_old_sl, _new_sl)
                    if _new_sl > _old_sl:
                        logger.debug(
                            f"[SL·CR-3] {position.symbol} SHORT 守护: 拒放松 "
                            f"old=${_old_sl:.2f} new=${_new_sl:.2f} → 保持 ${_old_sl:.2f}"
                        )
                else:
                    position.current_sl = _new_sl
        else:
            # 兜底 (无 policy_guardian) 也走单调守护
            _old_sl = float(getattr(position, "current_sl", 0) or 0)
            if direction == "LONG":
                _new_sl = entry_price * 0.92
                position.current_sl = max(_old_sl, _new_sl) if _old_sl > 0 else _new_sl
            else:
                _new_sl = entry_price * 1.08
                position.current_sl = min(_old_sl, _new_sl) if _old_sl > 0 else _new_sl

    # ============================================================
    #  执行退出
    # ============================================================

    def _execute_exit(self, position, reason, execute_close_fn, trail_state, now_ts):
        """执行平仓 + 记录 + 退出质量追踪"""
        # 覆盖 exit_price 为当前价格 (Time Stop 等场景)
        if reason.get("exit_price", 0) <= 0:
            reason["exit_price"] = position.avg_entry_price  # fallback

        trail_state["last_trigger_time"] = now_ts
        position.trail_state = trail_state
        # 类级别冷却记录：防止position对象重建后丢失trail_state导致重复退出
        self._exit_cooldowns[position.symbol] = now_ts

        logger.warning(
            f"🚨 [退出触发] {position.symbol} | {position.direction.value if hasattr(position.direction, 'value') else position.direction} | "
            f"原因: {reason.get('reason_cn', reason.get('reason'))} | "
            f"描述: {reason.get('description', '')}"
        )

        # ── 计算退出指标 (供质量追踪 + 记忆存储复用) ──
        direction_str = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
        entry_price = position.avg_entry_price
        exit_price = reason.get("exit_price", entry_price)
        if direction_str == "LONG":
            pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
        else:
            pnl_pct = (entry_price - exit_price) / entry_price if entry_price > 0 else 0.0

        # ── 幻方 P2 (2026-05-01): 退出质量追踪 ──
        if _QUALITY_TRACKER_AVAILABLE:
            try:
                tracker = get_exit_quality_tracker()
                session = get_shared_kernel().get_current_session()
                tracker.record_exit(
                    symbol=position.symbol,
                    exit_reason=reason.get("reason", "UNKNOWN"),
                    exit_price=exit_price,
                    session=session,
                    direction=direction_str,
                    pnl_pct=round(pnl_pct, 4),
                )
            except Exception as e:
                logger.debug("[退出质量] 记录失败 (不影响主流程): %s", e)

        # C5 归因 (2026-05-13): 记录主线交易盈亏供跨策略归因
        try:
            from app.analysis.strategy_attribution import get_attribution
            get_attribution().record_trade_pnl(
                strategy="main",
                symbol=position.symbol,
                direction=direction_str,
                pnl_pct=pnl_pct,
                pnl_amount=0.0,
                exit_reason=reason.get("reason", "UNKNOWN"),
                entry_time=getattr(position, 'entry_time', None),
                exit_time=datetime.now(),
            )
        except Exception:
            pass

        # ── P3 G14: 退出时写入标签化记忆 ──
        try:
            from app.llm.agent_bridge import get_agent_bridge
            bridge = get_agent_bridge()
            session = get_shared_kernel().get_current_session()
            outcome = "win" if pnl_pct > 0 else "loss"
            tags = [
                f"symbol:{position.symbol}",
                f"session:{session}",
                f"outcome:{outcome}",
                f"exit_reason:{reason.get('reason', 'UNKNOWN')}",
                f"direction:{direction_str}",
            ]
            bridge.add_tagged_lesson(
                lesson=f"{position.symbol} {direction_str} exited via {reason.get('reason', 'UNKNOWN')} "
                       f"at {exit_price} with PnL {pnl_pct*100:.1f}%",
                tags=tags,
                session=session,
                symbol=position.symbol,
                outcome=outcome,
                quality=abs(pnl_pct),
            )
        except Exception as e:
            logger.debug("[记忆] 标签记忆写入失败 (不影响主流程): %s", e)

        success = execute_close_fn(position, reason)
        if success:
            logger.info(f"✅ 平仓完成: {position.symbol} | {reason.get('reason', '')}")
            # ── P0 Fix: 写入 trade_attribution 归因记录 ──
            # position_exit_monitor 是主要退出路径, 之前从未调用 _update_trade_attribution,
            # 导致所有通过此路径退出的交易无归因记录 (8 次 EXIT 全部缺失)
            try:
                from app.pipeline.order_executor import _update_trade_attribution
                meta = getattr(position, 'metadata', {}) or {}
                trace_id = getattr(position, 'trace_id', '') or getattr(position, 'position_id', '')
                pnl = (exit_price - entry_price) * position.size if direction_str == "LONG" else (entry_price - exit_price) * position.size
                _update_trade_attribution(
                    trace_id=trace_id,
                    symbol=position.symbol,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=reason.get("reason", "UNKNOWN"),
                    holding_hours=position.holding_hours,
                    signal_strength=meta.get("signal_strength", 0),
                    signal_confidence=meta.get("signal_confidence", 0.0),
                    max_drawdown=meta.get("max_drawdown", 0.0),
                )
            except Exception as e:
                logger.debug("[归因] trade_attribution 更新失败 (不影响退出): %s", e)
        else:
            logger.error(f"❌ 平仓失败: {position.symbol} | {reason.get('reason', '')}")

    # ============================================================
    #  ATR 获取 (复用主进程 quote_ctx)
    # ============================================================

    def _get_atr(self, symbol: str) -> float:
        """计算 ATR + Volatility Floor (复用主进程连接)"""
        today = datetime.now().strftime('%Y-%m-%d')
        cached = self._atr_cache.get(symbol)
        if cached and cached.get('date') == today:
            return cached.get('value', 0.0)

        atr = 0.0
        try:
            atr = self._calc_atr_from_kline(symbol)
        except Exception as e:
            logger.debug(f"ATR 获取失败 {symbol}: {e}")

        self._atr_cache[symbol] = {'date': today, 'value': atr}
        return atr

    def _get_vix(self) -> Optional[float]:
        """获取当前 VIX (缓存 5 分钟) — 幻方增强"""
        now = time.time()
        if self._vix_cached and (now - self._vix_ts) < 300:
            return self._vix_cached
        try:
            if self._quote_ctx:
                from futu import KLType, AuType
                ret, data, _ = self._quote_ctx.request_history_kline(
                    "US.VIXY", ktype=KLType.K_DAY, max_count=3, autype=AuType.QFQ
                )
                if ret == 0 and data is not None and len(data) > 0:
                    vix_close = float(data["close"].values[-1])
                    self._vix_cached = vix_close
                    self._vix_ts = now
                    return vix_close
        except Exception:
            pass
        # 降级: 从 MarketDataRouter / alphavantage 获取
        try:
            from app.data.alphavantage_provider import AlphaVantageProvider
            provider = AlphaVantageProvider()
            vix = provider.get_vix()
            if vix and vix > 0:
                self._vix_cached = float(vix)
                self._vix_ts = now
                return self._vix_cached
        except Exception:
            pass
        return self._vix_cached  # 返回旧缓存或 None

    def _calc_atr_from_kline(self, symbol: str) -> float:
        """从日 K 线计算 ATR (14 周期) + 中位 TR 底限"""
        if not self._quote_ctx:
            return 0.0

        try:
            from futu import KLType, AuType
            ret, data, _ = self._quote_ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, max_count=30, autype=AuType.QFQ
            )

            if ret == 0 and data is not None and len(data) >= 15:
                data = data.copy()
                data['prev_close'] = data['close'].shift(1)
                data['tr1'] = data['high'] - data['low']
                data['tr2'] = abs(data['high'] - data['prev_close'])
                data['tr3'] = abs(data['low'] - data['prev_close'])
                data['tr'] = data[['tr1', 'tr2', 'tr3']].max(axis=1)
                atr = float(data['tr'].rolling(window=14).mean().iloc[-1])

                # Volatility Floor
                try:
                    ret2, data200, _ = self._quote_ctx.request_history_kline(
                        symbol, ktype=KLType.K_DAY, max_count=200, autype=AuType.QFQ
                    )
                    if ret2 == 0 and data200 is not None and len(data200) > 2:
                        data200 = data200.copy()
                        data200['prev_close'] = data200['close'].shift(1)
                        data200['tr'] = data200[['high', 'low', 'prev_close']].apply(
                            lambda r: max(r['high'] - r['low'],
                                         abs(r['high'] - r['prev_close']),
                                         abs(r['low'] - r['prev_close'])), axis=1
                        )
                        median_tr = float(data200['tr'].dropna().median())
                        atr = max(atr, median_tr * 0.7)
                except Exception:
                    pass  # 静默降级

                return atr
        except Exception as e:
            logger.debug(f"ATR 计算失败 {symbol}: {e}")

        return 0.0

    # ============================================================
    #  辅助: 清理 trail_state 脏数据
    # ============================================================

    @staticmethod
    def clean_trail_state(db_path: str, active_symbols: List[str]) -> int:
        """清理 trail_state 中已平仓的脏记录"""
        try:
            conn = sqlite3.connect(db_path, timeout=10)
            cursor = conn.cursor()
            cursor.execute("SELECT symbol FROM trail_state")
            all_symbols = [row[0] for row in cursor.fetchall()]
            dirty = [s for s in all_symbols if s not in active_symbols]
            for sym in dirty:
                cursor.execute("DELETE FROM trail_state WHERE symbol = ?", (sym,))
                logger.info(f"🧹 清理 trail_state 脏记录: {sym}")
            conn.commit()
            conn.close()
            return len(dirty)
        except Exception as e:
            logger.error(f"清理 trail_state 失败: {e}")
            return 0


# 全局单例
_exit_monitor: Optional[ExitMonitor] = None


def get_exit_monitor() -> ExitMonitor:
    """获取 ExitMonitor 单例"""
    global _exit_monitor
    if _exit_monitor is None:
        _exit_monitor = ExitMonitor()
    return _exit_monitor
