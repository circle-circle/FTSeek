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
  1.5. Day Trade Mode: 紧止损(1.5xATR/3%) + 主动止盈(+4%/+8%/+15%) + 时间衰减 (R/R≥1.33)
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
import math
import time
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

# ATR 止损定价引擎（纯函数）
from .atr_stop_engine import compute_stop_lines, compute_profit_atr_signed, resolve_trail_multiplier

# Task #19 (2026-05-27): 退出规则模块化 — 各规则已拆分到 exit_rules/
# ExitMonitor 自身只负责编排 (规则调度 + 质量评分 + 执行平仓)
from .exit_rules import (
    FlashExitRule,
    DayTradeExitRule,
    ScaleOutRule,
    TrailingStopRule,
    TimeStopRule,
    PreCloseExitRule,
    check_flash_exit as _impl_flash_exit,
    check_day_trade_exit as _impl_day_trade_exit,
    should_use_day_trade_mode as _impl_should_use_day_trade_mode,
    check_scale_out as _impl_scale_out,
    check_trailing_stop as _impl_trailing_stop,
    check_time_stop as _impl_time_stop,
    check_pre_close_exit as _impl_pre_close_exit,
    # Task #48: 优先级注册表 (渐进式引入, 现仅作为辅助入口)
    ExitRuleRegistry,
    ExitRuleContext,
    ExitReason,
)

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

# 退出规则质量评分 (P2 2026-05-27): 多规则触发时选最优
try:
    from app.risk.exit_quality_scorer import get_exit_quality_scorer as _get_scorer
    _EXIT_SCORER_AVAILABLE = True
except Exception:
    _get_scorer = None  # type: ignore
    _EXIT_SCORER_AVAILABLE = False

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

# P0 修复 (2026-05-31): 统一使用 timezone.py 的 now_et() 作为时间 SSOT
from app.utils.timezone import now_et as _now_et


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
        except Exception as _silent_e:
            logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
        t = now_et.hour * 60 + now_et.minute
        return 570 <= t <= 960
    except Exception as e:
        # P0 修复 (2026-05-28): fail-closed 而非 fail-open
        # 旧行为: 异常返回 True(假设市场开) → 周末/假日可能提交退出订单, 面临跳空风险
        # 新行为: 异常返回 False(保守跳过), 记录 WARNING 便于排查
        logger.warning(f"[is_us_market_open] 判断异常, 保守返回False: {e}")
        return False


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
        # Task #24 (2026-05-27): 财报退出去重集合，防止同一标的在同一批财报计划下被重复触发
        self._earnings_exit_handled: set = set()
        # 成本模型（可选；注入失败则 cost_bps=0，退化为无 buffer）
        self._cost_model = None
        if _COST_MODEL_AVAILABLE:
            try:
                self._cost_model = CostModel(CostModelConfig())
            except Exception as e:
                logger.debug(f"[ExitMonitor] CostModel 初始化失败，将退化为无 cost_buffer: {e}")
                self._cost_model = None

        # P0 修复 (2026-05-31): Cache exit policy to avoid 300+ instantiations per minute
        try:
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            self._exit_policy_guardian = ExitPolicyGuardian()
            self._exit_policy_guardian.load()
            self._cached_exit_policy = self._exit_policy_guardian.policy or {}
        except Exception as e:
            logger.debug(f"[ExitMonitor] Failed to load exit policy in __init__: {e}")
            self._cached_exit_policy = {}

        # Phase 3 (2026-05-31): 从 exit_policy.yaml 读取调度参数 (覆盖类常量, 实现外部化)
        self._reload_exit_monitor_params()

        # Phase 3 (2026-05-31): ExitRuleDispatcher (影子模式) 与 EarningsGuard 注入
        self._unified_dispatcher = None
        self._dispatcher_shadow_mode = False
        self._dispatcher_use_main = False
        self._dispatcher_log_diff_only = True
        self._earnings_guard_phase3 = None
        self._init_phase3_components()

        # Task #19: 初始化退出规则实例 (注入 monitor 引用以复用 ATR/VIX/cost_model)
        # 注意: 顺序决定了规则在 scan_positions 中的遍历顺序;
        #       Pre-Close 仍由编排器单独优先处理 (不参与质量评分竞争)
        self._rule_flash_exit = FlashExitRule(self)
        self._rule_day_trade = DayTradeExitRule(self)
        self._rule_scale_out = ScaleOutRule(self)
        self._rule_trailing_stop = TrailingStopRule(self)
        self._rule_time_stop = TimeStopRule(self)
        self._rule_pre_close = PreCloseExitRule(self)

        # Task #48 (2026-05-28): 退出规则优先级注册表 (渐进式引入)
        # 现阶段作为“辅助入口”使用: scan_positions 的主流程仍走原逐条检查逻辑 +
        # ExitQualityScorer 质量评分; 但优先级与互斥关系已被编码化在 ExitRuleRegistry 中,
        # 后续可逐步将 _check_xxx 包装为 checker 注册进去, 完成全量切换。
        self._rule_registry = ExitRuleRegistry()
        self._rule_registry.register(
            "pre_close",
            lambda pos, md, ts, pg: self._check_pre_close_exit(
                pos, md.get("current_price"), md.get("effective_atr", 0.0),
                ts, md.get("direction"), pg,
            ),
        )
        self._rule_registry.register(
            "flash_exit",
            lambda pos, md, ts, pg: self._check_flash_exit(
                pos, md.get("current_price"), md.get("effective_atr", 0.0),
                ts, md.get("now_ts"), md.get("direction"), pg,
                md.get("earnings_guard"),
            ),
        )
        self._rule_registry.register(
            "day_trade",
            lambda pos, md, ts, pg: self._check_day_trade_exit(
                pos, md.get("current_price"), md.get("entry_price"),
                md.get("effective_atr", 0.0), ts, md.get("now_ts"),
                md.get("direction"), md.get("pnl_pct", 0.0), pg,
            ),
        )
        self._rule_registry.register(
            "trailing_stop",
            lambda pos, md, ts, pg: self._check_trailing_stop(
                pos, md.get("current_price"), md.get("entry_price"),
                md.get("effective_atr", 0.0), md.get("profit_atr_signed", 0.0),
                ts, md.get("now_ts"), md.get("direction"), pg,
            ),
        )
        self._rule_registry.register(
            "time_stop",
            lambda pos, md, ts, pg: self._check_time_stop(
                pos, md.get("current_price"), md.get("now_ts"),
                md.get("direction"), pg,
            ),
        )
        self._rule_registry.register(
            "scale_out",
            lambda pos, md, ts, pg: self._check_scale_out(
                pos, md.get("current_price"), ts, md.get("direction"), market_data=md,
            ),
        )
        # Phase 3 (2026-05-31): 速度捕捉退出
        try:
            from app.risk.exit_rules.velocity_exit import check_velocity_exit as _impl_velocity_exit
            _velocity_cfg = (self._policy_guardian.policy if hasattr(self, "_policy_guardian") and self._policy_guardian else {}) or {}
            _ve_cfg = _velocity_cfg.get("exit_rules", {}).get("velocity_exit", {}) if isinstance(_velocity_cfg, dict) else {}
            self._rule_registry.register(
                "velocity_exit",
                lambda pos, md, ts, pg: _impl_velocity_exit(pos, md, ts, _ve_cfg),
            )
        except Exception as _ve_err:
            logger.warning(f"[ExitRuleRegistry] velocity_exit 注册失败: {_ve_err}")

        # [P0 2026-06-13] 读取 exit_policy.yaml enabled 字段，禁用 yaml 中标记为 false 的规则
        _exit_rules_cfg = self._cached_exit_policy.get("exit_rules", {}) if hasattr(self, "_cached_exit_policy") else {}
        for _rule_name in list(self._rule_registry._enabled_rules):
            _rule_cfg = _exit_rules_cfg.get(_rule_name, {})
            if isinstance(_rule_cfg, dict) and _rule_cfg.get("enabled") is False:
                self._rule_registry.disable(_rule_name)
                logger.info(f"[ExitRuleRegistry] {_rule_name} disabled by exit_policy.yaml (enabled=false)")

        logger.info(
            f"\u2705 [ExitRuleRegistry] \u521d\u59cb\u5316\u5b8c\u6210, \u542f\u7528\u89c4\u5219(\u6309\u4f18\u5148\u7ea7): "
            f"{self._rule_registry.list_enabled()}"
        )

        # Task #53 (2026-05-28): Feature Flag 与 ExitRuleContext
        # use_exit_registry=True 时, scan_positions 走新的 Registry 路径;
        # 异常时可自动 fallback 到 _check_via_legacy (逐条检查).
        self._exit_context = ExitRuleContext(
            cooldown_sec=float(self.COOLDOWN_SECONDS),
            min_hold_minutes=float(getattr(self, "_min_hold_minutes_cfg", 10.0)),
        )
        self._use_registry, self._registry_fallback_on_error = self._load_registry_flags()
        logger.info(
            f"\u2705 [ExitMonitor] use_exit_registry={self._use_registry}, "
            f"registry_fallback_on_error={self._registry_fallback_on_error}"
        )

    def _load_registry_flags(self) -> tuple:
        """从配置读取 Registry Feature Flag (Task #53)

        Returns:
            (use_registry, fallback_on_error)
        """
        use_registry = True
        fallback = True
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            if cfg is not None:
                use_registry = bool(cfg.get_dotted("exit_policy.use_exit_registry", True))
                fallback = bool(cfg.get_dotted("exit_policy.registry_fallback_on_error", True))
        except Exception as e:
            logger.debug(f"[ExitMonitor] 读取 Registry Feature Flag 失败, 使用默认: {e}")
        return use_registry, fallback

    # ============================================================
    # Phase 3 (2026-05-31): 退出参数外部化 + ExitRuleDispatcher 影子模式
    # ============================================================

    def _load_exit_policy_dict(self) -> Dict[str, Any]:
        """Return cached exit policy (loaded once in __init__)"""
        return self._cached_exit_policy

    def _reload_exit_monitor_params(self):
        """从 exit_policy.yaml::exit_monitor 读取调度参数 (Phase 3 外部化)"""
        try:
            policy = self._load_exit_policy_dict()
            em_cfg = (policy.get("exit_monitor") or {}) if policy else {}
            scan = float(em_cfg.get("scan_interval_seconds", self.SCAN_INTERVAL_SECONDS))
            cooldown = float(em_cfg.get("cooldown_seconds", self.COOLDOWN_SECONDS))
            min_hold = float(em_cfg.get("min_hold_minutes", 10))
            # 使用实例字段覆盖类常量 (向后兼容, 不修改调用点)
            self.SCAN_INTERVAL_SECONDS = scan
            self.COOLDOWN_SECONDS = cooldown
            self._min_hold_minutes_cfg = min_hold
        except Exception as e:
            logger.debug(f"[ExitMonitor] _reload_exit_monitor_params 失败 (使用默认): {e}")
            self._min_hold_minutes_cfg = 10.0

    def _init_phase3_components(self):
        """初始化 ExitRuleDispatcher 与 EarningsGuard (Phase 3)"""
        # 1. 读 feature flag
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            if cfg is not None:
                self._dispatcher_use_main = bool(cfg.get_dotted(
                    "risk.exit_routing.use_unified_dispatcher", False))
                self._dispatcher_shadow_mode = bool(cfg.get_dotted(
                    "risk.exit_routing.log_comparison_mode", False))
                self._dispatcher_log_diff_only = bool(cfg.get_dotted(
                    "risk.exit_routing.log_diff_only", True))
        except Exception as e:
            logger.debug(f"[ExitMonitor] 读取 risk.exit_routing flags 失败: {e}")

        # 2. 初始化 dispatcher (使用 exit_policy 作为配置源)
        if self._dispatcher_shadow_mode or self._dispatcher_use_main:
            try:
                from app.risk.exit_rule_dispatcher import get_exit_rule_dispatcher
                policy = self._load_exit_policy_dict()
                self._unified_dispatcher = get_exit_rule_dispatcher(policy)
                logger.info(
                    f"✅ [ExitRuleDispatcher] 启用 "
                    f"shadow={self._dispatcher_shadow_mode}, main={self._dispatcher_use_main}"
                )
            except Exception as e:
                logger.warning(f"[ExitMonitor] ExitRuleDispatcher 初始化失败: {e}")
                self._unified_dispatcher = None

        # 3. 初始化 EarningsGuard (Phase 3 ATR 数学保护)
        try:
            from app.risk.earnings_guard import get_earnings_guard
            policy = self._load_exit_policy_dict()
            eg_cfg = (policy.get("exit_rules") or {}).get("earnings_guard", {}) or {}
            self._earnings_guard_phase3 = get_earnings_guard(config=eg_cfg)
        except Exception as e:
            logger.debug(f"[ExitMonitor] EarningsGuard (Phase 3) 初始化失败: {e}")
            self._earnings_guard_phase3 = None

    def _shadow_mode_enabled(self) -> bool:
        """是否开启影子模式评估"""
        return bool(self._dispatcher_shadow_mode and self._unified_dispatcher is not None)

    def _shadow_evaluate(
        self,
        position,
        current_price: float,
        effective_atr: float,
        trail_state: Dict[str, Any],
        direction: str,
        now_ts: float,
        legacy_decision: Optional[Dict[str, Any]],
    ) -> None:
        """在影子模式下使用 ExitRuleDispatcher 评估 + 对比旧决策 (仅记录差异)"""
        if not self._shadow_mode_enabled():
            return
        try:
            # 构造 dispatcher 需要的代理数据
            entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
            holding_hours = float(getattr(position, "holding_hours", 0) or 0)
            pos_dict = {
                "symbol": getattr(position, "symbol", "?"),
                "position_id": getattr(position, "position_id", "") or getattr(position, "symbol", "?"),
                "entry_price": entry_price,
                "direction": direction,
                "holding_hours": holding_hours,
                "owner_strategy": getattr(position, "owner_strategy", "main"),
            }
            md_dict = {
                "current_price": current_price,
                "atr": effective_atr,
                "now_ts": now_ts,
            }
            new_decision = self._unified_dispatcher.evaluate(pos_dict, md_dict, trail_state or {})
            self._log_shadow_comparison(position, legacy_decision, new_decision)
        except Exception as e:
            logger.debug(f"[ExitMonitor] shadow evaluate 异常: {e}")

    def _log_shadow_comparison(
        self,
        position,
        legacy_decision: Optional[Dict[str, Any]],
        new_decision,
    ) -> None:
        """输出影子模式下两路决策的差异日志"""
        legacy_reason = None
        if isinstance(legacy_decision, dict):
            legacy_reason = legacy_decision.get("reason")
        new_reason = getattr(new_decision, "rule_name", None) if new_decision else None

        # log_diff_only=True 时不一致才输出
        if self._dispatcher_log_diff_only and legacy_reason == new_reason:
            return

        logger.info(
            "[shadow] symbol=%s legacy=%s new=%s suppressed=%s",
            getattr(position, "symbol", "?"),
            legacy_reason or "NONE",
            new_reason or "NONE",
            getattr(new_decision, "suppressed_rules", []) if new_decision else [],
        )

    def _has_exit_record(self, symbol: str, exit_reason: str, max_minutes: int = 1440) -> bool:
        """
        P1 修复 (2026-05-01): 检查该标的是否在指定时间内已有相同原因的退出记录
        
        防止因服务重启/状态不一致导致的重复退出和重复交易记录。
        """
        try:
            import sqlite3, os
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
            if not os.path.exists(db_path):
                return False
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cutoff = (_now_et() - __import__('datetime', fromlist=['timedelta']).timedelta(minutes=max_minutes)).isoformat()
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

        # P0 修复 (2026-05-27): 启动时打印实际生效的止损参数，便于审计配置-代码一致性
        self._log_effective_stop_loss_params()
        # P0 (2026-05-27): R/R ratio 安全校验 — 任何退出规则 R/R<1.0 时告警
        self._validate_rr_ratios()

    def update_quote_ctx(self, quote_ctx):
        """更新行情上下文 (重连后调用)"""
        self._quote_ctx = quote_ctx

    def _log_effective_stop_loss_params(self):
        """P0 (2026-05-27): 启动时打印实际生效的止损参数，便于审计配置-代码一致性"""
        try:
            # 1. exit_policy.yaml 参数 (正股 ATR 止损)
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            guardian = ExitPolicyGuardian()
            policy = guardian.policy if hasattr(guardian, 'policy') else {}
            rules = policy.get("exit_rules", {})
            atr_stop_cfg = rules.get("atr_stop", {})
            stock_max_loss = atr_stop_cfg.get("max_loss_pct", 15.0)
            short_overrides = policy.get("short_overrides", {})
            short_max_loss = short_overrides.get("max_loss_pct", 10.0)

            # 2. 期权止损参数 (unified_config.yaml)
            try:
                from app.core.config_container import get_config
                opt_cfg = get_config().get_section("options", {})
                option_stop_loss_pct = opt_cfg.get("stop_loss_pct", -0.30)
            except Exception:
                option_stop_loss_pct = -0.30

            # 3. Day Trade Mode 参数
            dt_cfg = policy.get("day_trade_mode", {})
            dt_initial = dt_cfg.get("initial_stop", {})
            dt_max_loss = dt_initial.get("max_loss_pct", 5.0)
            dt_atr_mult = dt_initial.get("atr_multiplier", 1.5)

            logger.info(
                f"✅ [ExitMonitor 止损参数校验] "
                f"正股硬止损: -{stock_max_loss}% | "
                f"做空硬止损: -{short_max_loss}% | "
                f"期权止损: {option_stop_loss_pct*100:.0f}% | "
                f"DayTrade止损: -{dt_max_loss}% (ATR {dt_atr_mult}x)"
            )

            # 一致性警告: 检查代码默认值与配置值是否匹配
            from app.risk.option_risk_monitor import OptionRiskConfig
            code_default = OptionRiskConfig.stop_loss_premium_pct
            config_value = abs(option_stop_loss_pct)
            if abs(code_default - config_value) > 0.01:
                logger.warning(
                    f"⚠️ [止损配置不一致] OptionRiskConfig.stop_loss_premium_pct="
                    f"{code_default:.0%} vs unified_config options.stop_loss_pct="
                    f"{option_stop_loss_pct:.0%}，请检查配置文件"
                )
        except Exception as e:
            logger.warning(f"[ExitMonitor] 止损参数校验失败 (不影响运行): {e}")

    def _validate_rr_ratios(self):
        """P0 (2026-05-27): 退出规则 R/R ratio 安全校验

        遍历所有退出规则, 计算 Reward/Risk ratio。
        如果任何规则的 R/R < 1.0, 打印 WARNING 日志告警。
        负期望值策略在数学上长期必亏 (亏损概率 > 盈利概率)。
        """
        try:
            from app.risk.exit_policy_guardian import ExitPolicyGuardian
            guardian = ExitPolicyGuardian()
            policy = guardian.policy if hasattr(guardian, 'policy') else {}

            checks = []  # [(rule_name, tp_pct, sl_pct, rr_ratio)]

            # 1. Day Trade Mode LONG
            dt_cfg = policy.get("day_trade_mode", {})
            if dt_cfg.get("enabled", True):
                init_stop = dt_cfg.get("initial_stop", {})
                sl_pct = float(init_stop.get("max_loss_pct", 5.0))
                tp_cfg = dt_cfg.get("take_profit", {})
                tp1_pct = float(tp_cfg.get("tier_1", {}).get("profit_pct", 2.0))
                rr = tp1_pct / sl_pct if sl_pct > 0 else 0
                checks.append(("DayTrade_LONG_Tier1", tp1_pct, sl_pct, rr))

                # Day Trade Mode SHORT
                short_cfg = dt_cfg.get("short", {})
                short_sl = float(short_cfg.get("max_loss_pct", 4.0))
                short_tp1 = float(short_cfg.get("take_profit_tier_1", 1.5))
                short_rr = short_tp1 / short_sl if short_sl > 0 else 0
                checks.append(("DayTrade_SHORT_Tier1", short_tp1, short_sl, short_rr))

            # 2. Dynamic Exit (unified_config default)
            try:
                from app.core.config_container import get_config
                dyn_cfg = get_config().get_section("dynamic_exit", {}).get("default", {})
                dyn_sl = abs(float(dyn_cfg.get("stop_loss", -0.045))) * 100
                dyn_tp = abs(float(dyn_cfg.get("take_profit", 0.025))) * 100
                dyn_rr = dyn_tp / dyn_sl if dyn_sl > 0 else 0
                checks.append(("DynamicExit_default", dyn_tp, dyn_sl, dyn_rr))
            except Exception as _silent_e:
                logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")

            # 3. Scalping base_params
            try:
                from app.core.config_container import get_config
                scalp_cfg = get_config().get_section("scalping", {}).get("phases", {}).get("base_params", {})
                scalp_sl = float(scalp_cfg.get("sl_pct", 0.02)) * 100
                scalp_tp = float(scalp_cfg.get("tp_pct", 0.025)) * 100
                scalp_rr = scalp_tp / scalp_sl if scalp_sl > 0 else 0
                checks.append(("Scalping_base", scalp_tp, scalp_sl, scalp_rr))
            except Exception as _silent_e:
                logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")

            # 输出校验结果
            has_warning = False
            for rule_name, tp, sl, rr in checks:
                if rr < 1.0:
                    logger.warning(
                        f"⚠️ WARNING: {rule_name} exit rule R/R={rr:.2f} < 1.0, "
                        f"negative expected value (TP={tp:.1f}%, SL={sl:.1f}%). "
                        f"R/R ratio < 1.0, mathematically unprofitable long-term."
                    )
                    has_warning = True
                else:
                    logger.info(
                        f"✅ [R/R Check] {rule_name}: R/R={rr:.2f} "
                        f"(TP={tp:.1f}%, SL={sl:.1f}%) — OK"
                    )

            if not has_warning:
                logger.info("✅ [R/R Validation] All exit rules have R/R ≥ 1.0 — positive expected value")

        except Exception as e:
            logger.warning(f"[ExitMonitor] R/R ratio 校验失败 (不影响运行): {e}")

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
            except Exception as _silent_e:
                logger.critical(f"[退出监控·P0] 组合PnL计算异常，回撤检测可能失效: {_silent_e}", exc_info=True)

        # P0 修复 (2026-05-28): 使用账户净值作为回撤分母，而非持仓成本
        # 旧行为: total_cost 作为分母 → 半仓时回撤被放大 2 倍，误触发 REDUCE/FLAT
        # 新行为: 优先用 capital_safety.current_equity; 不可用时降级到 total_cost
        equity = 0.0
        try:
            from app.risk.capital_safety import get_capital_safety_system
            equity = float(get_capital_safety_system()._state.current_equity or 0)
        except Exception:
            pass

        total_cost = sum(getattr(p, 'avg_entry_price', 0) * getattr(p, 'size', 0) for p in positions)
        denominator = equity if equity > 0 else total_cost
        if denominator <= 0:
            return None
        drawdown = -total_pnl / denominator

        # P0 (2026-06-14): 账户级回撤补充检查 — 含手动仓的 broker 真实浮亏
        # 原问题: total_pnl 只算 managed 仓位, 手动大仓位亏损不触发 FLAT_ALL
        # 修复: 额外查 broker unrealized PnL, 如果账户级回撤超限也触发
        _broker_drawdown = 0.0
        try:
            from app.risk.capital_safety import get_capital_safety_system
            _cs = get_capital_safety_system()
            _cs_equity = float(_cs._state.current_equity or 0)
            _cs_peak = float(_cs._state.peak_equity or _cs_equity)
            if _cs_peak > 0 and _cs_equity > 0:
                _broker_drawdown = (_cs_peak - _cs_equity) / _cs_peak
                if _broker_drawdown > drawdown:
                    logger.info(
                        f"[组合止损·账户级] broker_drawdown={_broker_drawdown*100:.2f}% "
                        f"> managed_drawdown={drawdown*100:.2f}%, 使用账户级回撤"
                    )
                    drawdown = _broker_drawdown
        except Exception as _bd_err:
            logger.debug(f"[组合止损·账户级] 检查跳过: {_bd_err}")

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
                    f"equity=${equity:.0f} total_cost=${total_cost:.0f} total_pnl=${total_pnl:+.0f} positions=[{detail}]"
                )
            except Exception as _silent_e:
                logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
        return action

    # ============================================================
    #  仓位归属判定助手 (P0 SSOT 2026-05-28)
    # ============================================================

    @staticmethod
    def _is_scalping_position(position) -> bool:
        """判断仓位是否属于 scalping 策略 (由 ScalpingEngine 自行管理退出)

        这里是主线 ExitMonitor 与 ScalpingEngine 退出职责划分的唯一闸门 —
        其他代码路径不得重复实现同名逻辑。

        判定优先级 (Task #7 2026-05-31: owner_strategy 优先):
          1. position.metadata['source'] == 'scalping' (orderbook 中最可靠的记号)
          2. position.metadata['strategy'] == 'scalping' (历史兼容)
          3. position.owner_strategy == 'scalping' (Task #7: 优先读取新字段)
          4. position.strategy_id == 'scalping' (向后兼容, 老字段)

        对于空列表、缺失属性、类型异常等场景需保守返回 False (不过滤)。
        """
        try:
            meta = getattr(position, 'metadata', None) or {}
            if isinstance(meta, dict):
                if meta.get("source") == "scalping":
                    return True
                if meta.get("strategy") == "scalping":
                    return True
            # Task #7 (2026-05-31): owner_strategy 优先, strategy_id 仅作向后兼容 fallback
            if getattr(position, 'owner_strategy', '') == "scalping":
                return True
            if getattr(position, 'strategy_id', '') == "scalping":
                return True
        except Exception:
            return False
        return False

    @staticmethod
    def _is_orb_position(position) -> bool:
        """判断仓位是否属于 ORB 策略 (由 ORBEngine._monitor_exits 自管退出)

        P0 (2026-06-07): 主线 ExitMonitor 不得干预 ORB 仓位 —
        ORB 有独立的退出栈 (ORBExitStack) 和 5秒轮询 daemon,
        两套退出引擎并行扫同一仓位会导致归因混乱和双重平仓。

        判定: owner_strategy 以 'orb_' 开头 (orb_engine / orb_pipeline)
        """
        try:
            _owner = (getattr(position, 'owner_strategy', '') or '').lower()
            if _owner.startswith('orb_'):
                return True
            meta = getattr(position, 'metadata', None) or {}
            if isinstance(meta, dict):
                if meta.get("source") == "orb" or meta.get("strategy", "").startswith("orb"):
                    return True
        except Exception:
            return False
        return False

    # Broker-first (2026-06-03): _is_broker_synced_position 已删除
    # 所有仓位统一进入退出监控，双重平仓防护下沉到 _close_position 的持仓校验
    @staticmethod
    def _is_broker_synced_position(position) -> bool:
        """DEPRECATED: 保留方法签名供外部引用不报错，但永远返回 False。"""
        return False

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
        # P0 修复 (2026-05-31): 用美东时间作为日期 key, 避免 CST 跨日错位
        today = _now_et().strftime('%Y-%m-%d')

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
                # 源头隔离 (2026-06-11): is_strategy_managed() 基于 metadata.source 判断
                # MANUAL/LEGACY/UNKNOWN → PROTECTED → 自动排除, 无需额外 DB 查询
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
            # P0 (2026-06-12): REDUCE_50 后必须 return
            # 根因: 减仓后继续执行 per-position 检查 → 同一标的再触发全平
            # → 两笔卖单同时到达 broker → 可能产生反向仓位 (SNDK 类事故)
            return closed
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
        skipped_orb = 0
        skipped_protected = 0
        skipped_broker = 0
        skipped_manual = 0

        for p in positions:
            # 1. scalping 自管 (重构 2026-05-28: 统一走 _is_scalping_position 助手方法)
            if self._is_scalping_position(p):
                skipped_scalping += 1
                continue
            # 1.5 ORB 自管 (P0 2026-06-07: ORBEngine._monitor_exits 独立退出栈)
            if self._is_orb_position(p):
                skipped_orb += 1
                continue
            # Broker-first (2026-06-03): 不再跳过 broker_sync 仓位，全部进入退出监控
            # 双重平仓防护下沉到 _close_position 的执行前持仓校验
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
            # 3. 源头隔离 (2026-06-11): is_protected() 检查 MANUAL/LEGACY → PROTECTED
            # 替代原来的 DB get_all_manual_symbols() 查询
            if is_protected(p):
                skipped_manual += 1
                continue
            main_positions.append(p)

        if skipped_scalping > 0:
            logger.debug(f"[退出监控] 跳过 {skipped_scalping} 个 scalping 仓位 (ScalpingEngine 自管)")
        if skipped_orb > 0:
            logger.debug(f"[退出监控] 跳过 {skipped_orb} 个 ORB 仓位 (ORBEngine 自管)")
        if skipped_broker > 0:
            logger.info(f"[退出监控·broker_sync] 跳过 {skipped_broker} 个 broker 同步仓位 (防双重平仓)")
        if skipped_protected > 0:
            logger.warning(
                f"[退出监控·M1] 跳过 {skipped_protected} 个 PROTECTED/FROZEN 仓位 "
                f"(默认隔离; takeover 接管: scripts/position_takeover.py)"
            )
        if skipped_manual > 0:
            logger.info(f"[退出监控·manual] 跳过 {skipped_manual} 个 owner=manual 仓位 (web手动/自管)")

        # P0-2 (2026-05-14): ExitDispatcher 路由 — 按 owner_strategy 选 ExitPolicy
        # Phase A 多数 ExitPolicy 返回 NO_ACTION (兜底走原 6 规则), 但 thesis-driven exit 可在此抢先决断
        try:
            from app.coordinator.exit_dispatcher import get_exit_dispatcher
            _dispatcher = get_exit_dispatcher()
        except Exception:
            _dispatcher = None

        # Phase 1 (2026-05-31): 读取策略隔离 feature flag
        # 开启后，owner_strategy 为空 / 'unknown' 的仓位不会被退出监控处置，
        # 避免跨策略双写入者误平其他策略仓位。
        try:
            from app.core.config_container import get_config
            _strict_owner = bool(get_config().get_dotted(
                "risk.strategy_enforcement.strict_owner_validation", False))
        except Exception:
            _strict_owner = False

        for position in main_positions:
            try:
                symbol = position.symbol
                # Phase 1 (2026-05-31): owner_strategy 严格校验 — 防双重执行竞争
                # 修复 (2026-06-02 P1): null/unknown → 默认 "main" 而非跳过, 防止退出检查丢失
                if _strict_owner:
                    _owner = (getattr(position, "owner_strategy", None) or "").strip().lower()
                    if not _owner or _owner in ("unknown", "none", "null"):
                        logger.warning(
                            f"[策略隔离·兜底] {symbol} owner_strategy 为空/未知 ('{_owner}'), "
                            f"默认赋值 'main' 以确保退出检查不丢失"
                        )
                        position.owner_strategy = "main"
                        _owner = "main"
                direction = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
                entry_price = position.avg_entry_price
                if entry_price <= 0:
                    continue

                # ============================================================
                # Task #24 (2026-05-27): 财报退出强制检查 (P0优先级，优于任何其他退出规则)
                # 仅在开盘后30分钟内主动触发，P0 优先级跳过评分直接执行
                # ============================================================
                _earnings_exit = self._check_earnings_exit(position)
                if _earnings_exit and symbol not in self._earnings_exit_handled:
                    self._earnings_exit_handled.add(symbol)
                    try:
                        _cur_p = get_price_fn(symbol)
                        if not isinstance(_cur_p, (int, float)) or _cur_p <= 0:
                            _cur_p = entry_price
                        _earnings_exit["exit_price"] = _cur_p
                        if direction == "LONG":
                            _ee_pnl = (_cur_p - entry_price) / entry_price if entry_price > 0 else 0.0
                        else:
                            _ee_pnl = (entry_price - _cur_p) / entry_price if entry_price > 0 else 0.0
                        _ee_trail = position.trail_state if position.trail_state else {}
                        self._execute_exit(position, _earnings_exit, execute_close_fn, _ee_trail, now_ts)
                        closed.append({"symbol": symbol, "reason": _earnings_exit["reason"], "pnl_pct": _ee_pnl})
                    except Exception as _ee:
                        logger.error(f"[财报退出] {symbol} 执行异常: {_ee}", exc_info=True)
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

                # P0 修复 (2026-05-29): trail_state 完整性验证
                # 防止进程重启后 highest_price 重置为异常值导致追踪止损失效
                if trail_state.get("highest_price") and trail_state["highest_price"] < entry_price:
                    logger.warning(
                        f"[trail_state] {symbol} highest_price异常："
                        f"{trail_state['highest_price']:.2f} < entry {entry_price:.2f}，重置"
                    )
                    trail_state["highest_price"] = entry_price
                if trail_state.get("lowest_price") and trail_state["lowest_price"] > entry_price:
                    logger.warning(
                        f"[trail_state] {symbol} lowest_price异常："
                        f"{trail_state['lowest_price']:.2f} > entry {entry_price:.2f}，重置"
                    )
                    trail_state["lowest_price"] = entry_price

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
                    except Exception as _silent_e:
                        logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
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

                # 2. 平滑融合：入场 ratio → 当前 ratio（cosine 缓入缓出过渡）
                #    Phase 3 (2026-05-31): 过渡窗口外部化 (session_blend_window_minutes)
                entry_ratio = trail_state.get("entry_session_ratio", current_session_ratio)
                entry_time_ts = trail_state.get("entry_time_ts", now_ts)
                held_minutes = (now_ts - entry_time_ts) / 60.0
                _blend_window = float(
                    (self._load_exit_policy_dict().get("progressive_trailing") or {}).get(
                        "session_blend_window_minutes", 90.0)
                )
                # 融合权重: cosine缓入缓出
                raw_weight = min(held_minutes / _blend_window, 1.0) if _blend_window > 0 else 1.0
                blend_weight = 0.5 * (1 - math.cos(math.pi * raw_weight))
                session_ratio = entry_ratio * (1.0 - blend_weight) + current_session_ratio * blend_weight

                effective_atr = atr * session_ratio
                # P1 修复 (2026-05-29): effective_atr 下限保护
                # session_ratio 在 midday 可能压到 0.6，导致 effective_atr 极小
                # Phase 3 (2026-05-31): 下限系数外部化 (effective_atr_floor_ratio)
                _pt_floor_cfg = {}
                try:
                    _pt_floor_cfg = (self._load_exit_policy_dict().get("progressive_trailing") or {})
                except Exception:
                    _pt_floor_cfg = {}
                _eff_atr_floor_ratio = float(_pt_floor_cfg.get("effective_atr_floor_ratio", 0.35))
                min_effective_atr = atr * _eff_atr_floor_ratio
                effective_atr = max(effective_atr, min_effective_atr)

                # 2026-06-05 根因修复: ATR=0 绝对底限 (price × 0.5%)
                # 场景: broker_reconcile 发现的仓位 (如 PG) 没有 ATR 数据
                # 无 ATR → effective_atr=0 → SL 计算全部失效 → 无保护敞口
                _price_atr_floor = entry_price * 0.005  # 0.5% of price
                if effective_atr < _price_atr_floor:
                    logger.warning(
                        f"[ATR底限] {symbol} effective_atr={effective_atr:.4f} "
                        f"< price_floor=${_price_atr_floor:.2f} (0.5%×${entry_price:.2f}), "
                        f"使用底限值"
                    )
                    effective_atr = _price_atr_floor

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
                # held_minutes 已在 L788 从 (now_ts - entry_time_ts) / 60.0 计算

                # M3 (2026-06-03): Grace period 保护 — legacy 路径也需要
                # 与 ExitRuleDispatcher._check_grace_period_block 对齐
                _grace_minutes = 10.0
                _grace_max_loss = 0.03
                try:
                    _policy = self._load_exit_policy_dict()
                    _ghp = _policy.get("global_hold_protection", {}) or {}
                    if _ghp.get("enabled", True):
                        _grace_minutes = float(_ghp.get("min_hold_minutes_after_entry", 10))
                        _grace_max_loss = float(_ghp.get("max_grace_loss_pct", 0.03))
                except Exception:
                    pass
                if held_minutes < _grace_minutes:
                    # 硬底兜底: 浮亏 >= max_loss 时解除保护
                    if pnl_pct > -_grace_max_loss:
                        logger.debug(
                            f"[M3·grace] {symbol} 持仓 {held_minutes:.1f}min < {_grace_minutes}min, "
                            f"pnl={pnl_pct:.2%} > -{_grace_max_loss:.0%}, 跳过退出评估"
                        )
                        continue

                # ============================================================
                # Task #53 (2026-05-28): 退出规则检查路径分发
                #   - use_exit_registry=True  → 走 ExitRuleRegistry (优先级+互斥+上下文)
                #   - use_exit_registry=False → 走 _check_via_legacy (逐条 _check_xxx + ExitQualityScorer)
                #   - Registry 异常且 fallback_on_error=True → 自动降级到 legacy
                # ============================================================
                selected_decision = self._check_exit_for_position(
                    position=position,
                    current_price=current_price,
                    entry_price=entry_price,
                    effective_atr=effective_atr,
                    profit_atr_signed=profit_atr_signed,
                    trail_state=trail_state,
                    now_ts=now_ts,
                    direction=direction,
                    pnl_pct=pnl_pct,
                    held_minutes=held_minutes,
                    policy_guardian=policy_guardian,
                    earnings_guard=earnings_guard,
                )

                if selected_decision:
                    trail_state['effective_atr'] = effective_atr  # ATR观察日志需要
                    # Phase 3 (2026-05-31): 影子模式 — 不干预主路径, 仅记录差异
                    if self._shadow_mode_enabled():
                        self._shadow_evaluate(
                            position, current_price, effective_atr,
                            trail_state, direction, now_ts, selected_decision,
                        )
                    self._execute_exit(position, selected_decision, execute_close_fn, trail_state, now_ts)
                    closed.append({"symbol": symbol, "reason": selected_decision["reason"], "pnl_pct": pnl_pct})
                    continue

                # Phase 3: 未触发任何退出时也走一轮 shadow 评估 (记录“应该退”却未退出的场景)
                if self._shadow_mode_enabled():
                    self._shadow_evaluate(
                        position, current_price, effective_atr,
                        trail_state, direction, now_ts, None,
                    )

                # 无规则触发 → 更新 trail_state 后跳到下一个持仓
                self._update_trail_state(
                    position, current_price, trail_state, direction,
                    entry_price, effective_atr, policy_guardian,
                )

            except Exception as e:
                logger.error(f"[退出监控] {position.symbol} 检查异常: {e}", exc_info=True)

        return closed

    # ============================================================
    #  Task #53 (2026-05-28): 退出检查路径分发 (Registry vs Legacy)
    # ============================================================

    def _check_exit_for_position(
        self,
        position,
        current_price,
        entry_price,
        effective_atr,
        profit_atr_signed,
        trail_state,
        now_ts,
        direction,
        pnl_pct,
        held_minutes,
        policy_guardian=None,
        earnings_guard=None,
    ) -> Optional[Dict]:
        """单个持仓的退出检查路径分发 (Registry vs Legacy)

        返回:
            退出决策 dict 或 None (None 表示本轮不退出)
        """
        if self._use_registry:
            try:
                return self._check_via_registry(
                    position, current_price, entry_price, effective_atr,
                    profit_atr_signed, trail_state, now_ts, direction, pnl_pct,
                    held_minutes, policy_guardian, earnings_guard,
                )
            except Exception as e:
                logger.error(
                    f"[ExitMonitor] Registry 检查异常 ({getattr(position, 'symbol', '?')}): {e}, "
                    f"fallback={self._registry_fallback_on_error}",
                    exc_info=True,
                )
                if self._registry_fallback_on_error:
                    return self._check_via_legacy(
                        position, current_price, entry_price, effective_atr,
                        profit_atr_signed, trail_state, now_ts, direction, pnl_pct,
                        held_minutes, policy_guardian, earnings_guard,
                    )
                return None
        return self._check_via_legacy(
            position, current_price, entry_price, effective_atr,
            profit_atr_signed, trail_state, now_ts, direction, pnl_pct,
            held_minutes, policy_guardian, earnings_guard,
        )

    def _check_via_registry(
        self,
        position,
        current_price,
        entry_price,
        effective_atr,
        profit_atr_signed,
        trail_state,
        now_ts,
        direction,
        pnl_pct,
        held_minutes,
        policy_guardian=None,
        earnings_guard=None,
    ) -> Optional[Dict]:
        """通过 ExitRuleRegistry 检查退出 (Task #53 新路径)

        Registry 内部完成:
          - 优先级排序 + 互斥判定
          - 防振荡冷却 + 最小持仓时间 + 硬止损豁免
        Registry 返回后还需要做:
          - pre_close 幂等检查 + close_qty 调整
        """
        market_data = {
            "current_price": current_price,
            "entry_price": entry_price,
            "effective_atr": effective_atr,
            "profit_atr_signed": profit_atr_signed,
            "now_ts": now_ts,
            "direction": direction,
            "pnl_pct": pnl_pct,
            "earnings_guard": earnings_guard,
        }

        # P1-7 互斥保护: 其他规则锁定时 临时禁用 pre_close (检查后恢复)
        _meta = getattr(position, "metadata", None) or {}
        _preclose_locked = bool(_meta.get("_exit_locked", False))
        _preclose_was_enabled = self._rule_registry.is_enabled("pre_close")
        if _preclose_locked and _preclose_was_enabled:
            self._rule_registry.disable("pre_close")
        try:
            result = self._rule_registry.check_all_rules_with_context(
                position, market_data, trail_state,
                context=self._exit_context,
                policy_guardian=policy_guardian,
                held_minutes=held_minutes,
            )
        finally:
            if _preclose_locked and _preclose_was_enabled:
                self._rule_registry.enable("pre_close")

        if not result:
            return None

        symbol = getattr(position, 'symbol', '?')
        logger.info(
            f"EXIT_DECISION: symbol={symbol}, path=registry, "
            f"selected={result.get('reason', 'N/A')}"
        )
        return self._post_process_preclose(position, result)

    def _check_via_legacy(
        self,
        position,
        current_price,
        entry_price,
        effective_atr,
        profit_atr_signed,
        trail_state,
        now_ts,
        direction,
        pnl_pct,
        held_minutes,
        policy_guardian=None,
        earnings_guard=None,
    ) -> Optional[Dict]:
        """旧的逐条退出检查逻辑 (保留作为永久 fallback, 不删除)

        原逻辑总结:
          1. 最小持仓时间: held_minutes < 10min 跳过非 Pre-Close 规则
          2. 逐条检查 flash/day_trade/scale_out/trailing/time_stop
          3. Pre-Close 优先级最高 (不参与评分)
          4. 多规则触发 → ExitQualityScorer 选最优
        """
        symbol = getattr(position, 'symbol', '?')

        # 最小持仓时间检查 — Phase 3 外部化 (exit_policy.exit_monitor.min_hold_minutes)
        min_hold_minutes = float(getattr(self, "_min_hold_minutes_cfg", 10))
        _skip_non_hard_exits = held_minutes < min_hold_minutes
        if _skip_non_hard_exits:
            logger.debug(
                f"[最小持仓] {symbol} 持仓 {held_minutes:.0f}min < {min_hold_minutes}min, "
                f"仅允许硬止损 / Pre-Close 退出"
            )

        # ── 收集所有触发的退出建议 ──
        _triggered_exits: List[Dict[str, Any]] = []

        flash_result = self._check_flash_exit(
            position, current_price, effective_atr, trail_state,
            now_ts, direction, policy_guardian, earnings_guard,
        )
        if flash_result and not _skip_non_hard_exits:
            _triggered_exits.append(flash_result)

        daytrade_result = self._check_day_trade_exit(
            position, current_price, entry_price, effective_atr,
            trail_state, now_ts, direction, pnl_pct, policy_guardian,
        )
        if daytrade_result and not _skip_non_hard_exits:
            _triggered_exits.append(daytrade_result)

        scale_result = self._check_scale_out(
            position, current_price, trail_state, direction,
            market_data={"current_price": current_price, "atr": effective_atr, "direction": direction},
        )
        if scale_result and not _skip_non_hard_exits:
            _triggered_exits.append(scale_result)

        stop_result = self._check_trailing_stop(
            position, current_price, entry_price, effective_atr,
            profit_atr_signed, trail_state, now_ts, direction, policy_guardian,
        )
        if stop_result and not _skip_non_hard_exits:
            _triggered_exits.append(stop_result)

        time_result = self._check_time_stop(
            position, current_price, now_ts, direction, policy_guardian,
        )
        if time_result and not _skip_non_hard_exits:
            _triggered_exits.append(time_result)

        # Pre-Close (P1-7 互斥保护)
        _meta = getattr(position, "metadata", None) or {}
        _preclose_locked = _meta.get("_exit_locked", False)
        preclose_result = None
        if not _preclose_locked:
            preclose_result = self._check_pre_close_exit(
                position, current_price, effective_atr, trail_state, direction, policy_guardian,
            )

        # ── Pre-Close 优先级最高，直接返回（不参与评分竞争）──
        if preclose_result:
            return self._post_process_preclose(position, preclose_result)

        if not _triggered_exits:
            return None

        # ── P0 (2026-06-07): scale_out 互斥 — 压制 trailing_stop ──
        # 根因: scale_out 平50%锁利+SL移成本, trailing 同时触发会100%全平
        # 当 SCALE_OUT 在触发列表中时, 移除所有 trailing_stop/ATR_STOP 类退出
        _has_scale_out = any(
            e.get("reason", "") in ("SCALE_OUT", "SCALE_OUT_TIER1", "SCALE_OUT_TIER2")
            for e in _triggered_exits
        )
        if _has_scale_out and len(_triggered_exits) > 1:
            _trail_reasons = {"ATR_STOP", "CHANDELIER_TRAIL", "SL_HIT", "BREAKEVEN_STOP", "STOP_LOSS"}
            _before = len(_triggered_exits)
            _triggered_exits = [
                e for e in _triggered_exits
                if e.get("reason", "") not in _trail_reasons
            ]
            if len(_triggered_exits) < _before:
                logger.info(
                    f"[互斥·scale_out] {symbol} scale_out 触发, 压制 trailing_stop "
                    f"(剩余 {len(_triggered_exits)} 条规则竞争)"
                )

        # ── 单规则触发 → 直接返回 ──
        if len(_triggered_exits) == 1:
            _selected = _triggered_exits[0]
            logger.info(
                f"EXIT_DECISION: symbol={symbol}, path=legacy, triggered_rules="
                f"[{{{_selected['reason']}: N/A}}], selected={_selected['reason']}(score=N/A)"
            )
            return _selected

        # ── 多规则触发 → 质量评分选最优 ──
        if _EXIT_SCORER_AVAILABLE:
            scorer = _get_scorer()
            _scored_exits = []
            for _exit in _triggered_exits:
                _score = scorer.score(
                    exit_result=_exit,
                    position=position,
                    current_price=current_price,
                    atr=effective_atr,
                    pnl_pct=pnl_pct,
                    held_minutes=held_minutes,
                    direction=direction,
                )
                _scored_exits.append((_exit, _score))
            _scored_exits.sort(key=lambda x: x[1], reverse=True)
            _selected, _best_score = _scored_exits[0]
            _rules_log = [{e["reason"]: round(s, 1)} for e, s in _scored_exits]
            logger.info(
                f"EXIT_DECISION: symbol={symbol}, path=legacy, triggered_rules={_rules_log}, "
                f"selected={_selected['reason']}(score={_best_score:.1f})"
            )
            return _selected

        # Scorer 不可用 → 取第一个 (向后兼容)
        _selected = _triggered_exits[0]
        _rules_log = [e["reason"] for e in _triggered_exits]
        logger.info(
            f"EXIT_DECISION: symbol={symbol}, path=legacy, triggered_rules={_rules_log}, "
            f"selected={_selected['reason']}(score=FALLBACK, scorer unavailable)"
        )
        return _selected

    def _post_process_preclose(self, position, result: Dict) -> Optional[Dict]:
        """Pre-Close 后处理 (幂等检查 + close_qty 调整)

        Registry / Legacy 两路径共用。
        如果不是 Pre-Close 则原样返回; 如果是则:
          - 幂等命中 → 返回 None (跳过本轮退出)
          - close_ratio<1 → 计算 close_qty
        """
        if not isinstance(result, dict):
            return result
        reason = str(result.get("reason", ""))
        if "PRE_CLOSE" not in reason.upper():
            return result

        # P1 幂等检查
        if self._has_exit_record(position.symbol, reason, max_minutes=1440):
            logger.debug(f"[盘末退出·幂等] {position.symbol} 已有 {reason} 记录，跳过")
            return None

        # close_ratio<1 计算 close_qty
        close_ratio = result.get("close_ratio")
        if close_ratio and close_ratio < 1.0:
            orig_size = getattr(position, 'size', 0)
            close_qty = max(1, int(orig_size * close_ratio)) if orig_size else 0
            if orig_size and close_qty >= orig_size:
                logger.info(
                    f"[盘末减仓·跳过] {position.symbol} 持仓过小({orig_size}股), "
                    f"减仓={close_qty}股 >= 全仓, 改为全平退出"
                )
                result["close_ratio"] = 1.0
                result.pop("close_qty", None)
            elif orig_size:
                result["close_qty"] = close_qty
                logger.info(
                    f"[盘末减仓] {position.symbol} {result.get('reason_cn', '')}, "
                    f"计划平仓 {close_qty}/{orig_size} 股"
                )
        return result

    # ============================================================
    #  Task #24 (2026-05-27): 财报退出强制检查
    # ============================================================

    def _check_earnings_exit(self, position) -> Optional[Dict]:
        """
        检查该持仓是否需要财报退出 (P0 优先级，不可被其他规则覆盖)

        设计原则:
        - 财报退出是风控硬约束, 优先级高于所有技术性退出规则
        - 仅在开盘后30分钟内主动触发 (09:30-10:00 ET, 给足出场时间)
        - P0 退出会跳过 ExitQualityScorer 评分直接执行
        - P1 优先级仅记录警告不强制执行
        """
        try:
            from app.risk.pre_trade_check import check_earnings_exit_required
            from app.core.scheduler_triggers import ET as _ET

            now_et = datetime.now(_ET) if _ET else datetime.now()
            # 仅在开盘后30分钟内触发 (09:30-10:00 ET, 含边界)
            in_window = (
                (now_et.hour == 9 and now_et.minute >= 30)
                or (now_et.hour == 10 and now_et.minute == 0)
            )
            if not in_window:
                return None

            result = check_earnings_exit_required(position.symbol)
            if not result:
                return None

            priority = str(result.get("priority", "P1")).upper()
            if priority == "P0":
                logger.warning(
                    f"[财报退出] 🚨 {position.symbol} P0 强制退出: {result.get('reason', '')}"
                )
                return {
                    "reason": "earnings_exit_p0",
                    "reason_cn": "财报前强制退出(P0)",
                    "description": result.get("reason", "财报退出"),
                    "exit_price": 0,  # 市价退出, 由上层填充
                    "close_ratio": 1.0,
                    "priority": "P0",
                    "skip_scorer": True,  # 跳过 ExitQualityScorer
                    "deadline": result.get("deadline", ""),
                }
            else:
                # P1: 仅记录警告, 不强制退出
                logger.warning(
                    f"[财报退出] ⚠️ {position.symbol} P1 建议退出: "
                    f"{result.get('reason', '')} (仅警告, 不强制)"
                )
                return None
        except Exception as e:
            logger.debug(f"[财报退出] 检查异常: {e}")
        return None

    def _check_flash_exit(self, position, current_price, atr, trail_state, now_ts, direction, policy, earnings_guard):
        """Flash Exit 委托 (实现在 exit_rules/flash_exit.py)

        Task #19 拆分后: 保留原方法签名以兑现现有测试/编排调用。
        实际逻辑以 monitor 为首参传入 check_flash_exit。
        """
        return _impl_flash_exit(self, position, current_price, atr, trail_state, now_ts, direction, policy, earnings_guard)

    # ============================================================
    #  规则 1.5: Day Trade Mode (短线主动管理)
    # ============================================================

    def _should_use_day_trade_mode(self, position, policy_guardian=None) -> bool:
        """委托 (实现在 exit_rules/day_trade_exit.py)"""
        return _impl_should_use_day_trade_mode(position, policy_guardian)

    def _check_day_trade_exit(self, position, current_price, entry_price, atr, trail_state, now_ts,
                               direction, pnl_pct, policy_guardian=None):
        """Day Trade Mode 委托 (实现在 exit_rules/day_trade_exit.py, 含 profit_lock)"""
        return _impl_day_trade_exit(position, current_price, entry_price, atr, trail_state, now_ts,
                                     direction, pnl_pct, policy_guardian)

    def _check_scale_out(self, position, current_price, trail_state, direction, market_data=None):
        """分批止盈委托 (实现在 exit_rules/scale_out.py)"""
        return _impl_scale_out(position, current_price, trail_state, direction, market_data=market_data)

    # ============================================================
    #  规则 3: Time Stop
    # ============================================================

    def _check_time_stop(self, position, current_price, now_ts, direction, policy_guardian=None):
        """Time Stop 委托 (实现在 exit_rules/time_stop.py)"""
        return _impl_time_stop(self, position, current_price, now_ts, direction, policy_guardian)

    # ============================================================
    #  规则 3.5: Pre-Close Exit 盘末强制退出
    # ============================================================

    def _check_pre_close_exit(self, position, current_price, atr, trail_state, direction, policy_guardian=None):
        """Pre-Close Exit 委托 (实现在 exit_rules/pre_close.py)"""
        return _impl_pre_close_exit(position, current_price, atr, trail_state, direction, policy_guardian)

    # ============================================================
    #  规则 2: 统一跟踪止损
    # ============================================================

    def _check_trailing_stop(self, position, current_price, entry_price, atr, profit_atr_signed, trail_state, now_ts, direction, policy):
        """Trailing Stop 委托 (实现在 exit_rules/trailing_stop.py)"""
        return _impl_trailing_stop(self, position, current_price, entry_price, atr,
                                    profit_atr_signed, trail_state, now_ts, direction, policy)

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

        # ── 持久化 trail_state 到 trail_state_store (2026-05-27) ──
        # P0 修复 (2026-05-29): 提升日志级别 + 重试机制，防止静默丢失导致重启后 peak_price 重置
        try:
            import time as _time_mod
            from app.risk.trail_state_store import save_trail_state
            _persist_ok = False
            for _retry_i in range(3):  # 最多尝试 3 次 (首次 + 2 次重试)
                if save_trail_state(position.symbol, trail_state):
                    _persist_ok = True
                    break
                if _retry_i < 2:
                    _time_mod.sleep(0.1)  # 重试间隔 100ms
            if not _persist_ok:
                logger.warning(
                    f"[trail_state] {position.symbol} 持久化失败(重试3次均失败)，"
                    f"重启可能导致 peak_price 重置"
                )
        except Exception as _persist_err:
            logger.warning(f"[trail_state] {position.symbol} 持久化异常: {_persist_err}")

        if atr > 0:
            profit_atr_signed = compute_profit_atr_signed(direction, entry_price, current_price, atr)
            policy_dict = {}
            if policy_guardian is not None and hasattr(policy_guardian, "policy"):
                policy_dict = policy_guardian.policy or {}

            # ── 根因修复 (2026-06-05): progressive_trailing.enabled 检查 ──
            _pt_cfg = (policy_dict.get("progressive_trailing") or {}) if policy_dict else {}
            _pt_enabled = bool(_pt_cfg.get("enabled", False))

            if not _pt_enabled:
                # progressive_trailing 禁用 → 仅使用 atr_stop 固定止损, 不移动 SL
                # SL 由 atr_stop 规则管理 (entry ± 4×ATR), trail_state 只更新 peak
                _old_sl = float(getattr(position, "current_sl", 0) or 0)
                # ── SL 契约 (2026-06-06): initial_sl 是绝对地板, 退出永远不能放宽 ──
                _floor_sl = float(getattr(position, "initial_sl", 0) or 0)
                atr_mult_val = float(
                    (policy_dict.get("exit_rules", {}).get("atr_stop", {}).get(
                        "multiplier_by_profit", {}).get("initial", 4.0) if policy_dict else 4.0)
                )
                if direction == "LONG":
                    _fixed_sl = entry_price - atr * atr_mult_val
                    # SL 契约: 不得低于 initial_sl (入场的精心推理不被一刀切覆盖)
                    if _floor_sl > 0:
                        _fixed_sl = max(_fixed_sl, _floor_sl)
                    # SL 只升不降 (单调保护)
                    if _old_sl > 0:
                        position.current_sl = max(_old_sl, _fixed_sl)
                    else:
                        position.current_sl = _fixed_sl
                else:  # SHORT
                    # 2026-06-05 根因修复: SHORT SL 统一使用 atr_stop.initial (4.0x)
                    # 原用 short_overrides.atr_multiplier (2.0x), 与 atr_stop 不一致
                    tier1_mult = float(
                        (policy_dict.get("exit_rules", {}).get("atr_stop", {}).get(
                            "multiplier_by_profit", {}).get("initial", 4.0) if policy_dict else 4.0)
                    )
                    _fixed_sl = entry_price + atr * tier1_mult
                    # SHORT SL 地板: 不得紧于 atr_stop initial
                    _short_sl_floor = entry_price + atr * tier1_mult
                    _fixed_sl = max(_fixed_sl, _short_sl_floor)
                    # SL 契约: 不得高于 initial_sl (SHORT 方向 ceiling)
                    if _floor_sl > 0:
                        _fixed_sl = min(_fixed_sl, _floor_sl)
                    if _old_sl > 0:
                        position.current_sl = min(_old_sl, _fixed_sl)
                    else:
                        position.current_sl = _fixed_sl
                # SL 契约生效日志
                if _floor_sl > 0 and _fixed_sl == _floor_sl:
                    logger.debug(
                        f"[SL·契约] {position.symbol} {direction} floor applied: "
                        f"initial_sl=${_floor_sl:.2f} 阻止放宽到 "
                        f"entry±{atr_mult_val}×ATR=${entry_price - atr * atr_mult_val:.2f}"
                    )
                return  # progressive_trailing 已禁用 (2026-06-14), current_sl 无人读取
                        # 唯一活跃止损: atr_stop (entry ± 4×ATR), 由 trailing_stop.py 独立计算

        else:
            # 兜底: atr <= 0 时使用固定百分比止损
            _pt_cfg = {}
            try:
                _pt_cfg = self._load_exit_policy_dict().get("progressive_trailing") or {}
            except Exception:
                _pt_cfg = {}
            _fb_long = float(_pt_cfg.get("fallback_long_sl_pct", 0.92))
            _fb_short = float(_pt_cfg.get("fallback_short_sl_pct", 1.08))
            _old_sl = float(getattr(position, "current_sl", 0) or 0)
            if direction == "LONG":
                _new_sl = entry_price * _fb_long
                position.current_sl = max(_old_sl, _new_sl) if _old_sl > 0 else _new_sl
            else:
                _new_sl = entry_price * _fb_short
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
        # Phase 1 (2026-05-31): close_trigger_time 写入 snapshot 表 (仅 best-effort)
        try:
            from app.risk.trail_state_store import get_trail_state_store
            _store = get_trail_state_store()
            _pid = getattr(position, "position_id", None) or position.symbol
            _trigger_iso = _now_et().isoformat()
            trail_state["close_trigger_time"] = _trigger_iso
            _store.mark_close_triggered(str(_pid), _trigger_iso)
        except Exception as _silent_e:
            logger.debug(f"[trail_snapshot] mark_close_triggered 失败: {_silent_e}")
        position.trail_state = trail_state
        # 类级别冷却记录：防止position对象重建后丢失trail_state导致重复退出
        self._exit_cooldowns[position.symbol] = now_ts

        # ============================================================
        # Task #15 (2026-05-27): 退出链路 trace_id 传递
        #   - entry_trace_id: 从 position 读取（信号生成时创建的 trace）
        #   - exit_trace_id : 退出执行时新生成的唯一ID
        # 写入 position.metadata + reason，供下游 _close_position / save_trade 读取。
        # ============================================================
        entry_trace_id = getattr(position, 'trace_id', '') or ''
        exit_trace_id = f"EXIT-{position.symbol}-{_now_et().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        try:
            if not isinstance(getattr(position, 'metadata', None), dict):
                position.metadata = {}
            position.metadata["entry_trace_id"] = entry_trace_id
            position.metadata["exit_trace_id"] = exit_trace_id
            position.metadata["trace_id"] = entry_trace_id  # 统一 key, 下游按需取用
        except Exception as _silent_e:
            logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
        try:
            reason["entry_trace_id"] = entry_trace_id
            reason["exit_trace_id"] = exit_trace_id
            reason["trace_id"] = entry_trace_id
        except Exception as _silent_e:
            logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")

        _trace_pair = f"[trace:{entry_trace_id or 'NONE'}\u2192{exit_trace_id}]"
        logger.warning(
            f"[EXIT]{_trace_pair} Position closed for {position.symbol} | "
            f"{position.direction.value if hasattr(position.direction, 'value') else position.direction} | "
            f"reason={reason.get('reason_cn', reason.get('reason'))} | "
            f"description={reason.get('description', '')}"
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
                exit_time=_now_et(),
            )
        except Exception as _silent_e:
            logger.critical(f"[退出监控·P0] 交易记录保存异常，可能产生幽灵交易: {_silent_e}", exc_info=True)

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
            logger.info(f"[EXIT]{_trace_pair} ✅ Close confirmed: {position.symbol} | {reason.get('reason', '')}")
            # ATR观察日志: 退出决策关键参数 (2026-05-30)
            try:
                _hold_minutes = getattr(position, 'holding_hours', 0) * 60
                _effective_atr = reason.get('effective_atr', trail_state.get('effective_atr', 0.0))
                _pnl = pnl_pct * 100
                _profit_atr = (exit_price - entry_price) / _effective_atr if _effective_atr > 0 and direction_str == 'LONG' else \
                              (entry_price - exit_price) / _effective_atr if _effective_atr > 0 else 0.0
                logger.info(
                    f"[ATR_EXIT] symbol={position.symbol} exit_reason={reason.get('reason', 'UNKNOWN')} "
                    f"hold_time={_hold_minutes:.0f}min pnl={_pnl:.2f}% "
                    f"profit_atr={_profit_atr:.2f}x effective_atr={_effective_atr:.4f}"
                )
            except Exception:
                pass  # 日志增强不影响核心逻辑
            # ── P0 Fix: 写入 trade_attribution 归因记录 ──
            # position_exit_monitor 是主要退出路径, 之前从未调用 _update_trade_attribution,
            # 导致所有通过此路径退出的交易无归因记录 (8 次 EXIT 全部缺失)
            try:
                from app.pipeline.order_executor import _update_trade_attribution
                meta = getattr(position, 'metadata', {}) or {}
                trace_id = entry_trace_id or getattr(position, 'position_id', '')
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
            logger.error(f"[EXIT]{_trace_pair} ❌ Close failed: {position.symbol} | {reason.get('reason', '')}")

    # ============================================================
    #  ATR 获取 (复用主进程 quote_ctx)
    # ============================================================

    def _get_atr(self, symbol: str) -> float:
        """动态 ATR: 日K基线 + VIX缩放 + 日内波动底限 (2026-06-07)

        修复: 原实现按日缓存, 今天开盘用昨天波动率 → 大波动日被过早止损.
        新实现:
          1. 日K ATR-14 作为基线 (5分钟刷新, 不再按日缓存)
          2. VIX 缩放: VIX/20 作为波动放大因子 (VIX=30 → 1.5×)
          3. 日内波动底限: 取当日 5min K 线的 high-low 作为 real-time TR
        """
        now = time.time()
        cached = self._atr_cache.get(symbol)
        # 5 分钟刷新 (原来按日缓存, 现在动态更新)
        if cached and (now - cached.get('ts', 0)) < 300:
            return cached.get('value', 0.0)

        # 1. 日K ATR 基线
        daily_atr = 0.0
        try:
            daily_atr = self._calc_atr_from_kline(symbol)
        except Exception as e:
            logger.debug(f"ATR 获取失败 {symbol}: {e}")

        if daily_atr <= 0:
            if cached:
                return cached.get('value', 0.0)
            return 0.0

        # 2. VIX 缩放 (VIX=20 为基线, VIX 越高 ATR 越大)
        vix = self._get_vix() or 20.0
        vix_baseline = 20.0
        vix_factor = max(1.0, vix / vix_baseline)
        # 限制: 最多放大 2.5×, 防止 VIX 极端值过度放大
        vix_factor = min(vix_factor, 2.5)
        atr = daily_atr * vix_factor

        # 3. 日内波动底限 (取当日 5min K 的 high-low 作为实时 TR 代理)
        try:
            intraday_range = self._get_intraday_range(symbol)
            if intraday_range > 0:
                # 日内底限 = 当日极差的 50% (保守估计, 不要求覆盖全天)
                atr = max(atr, intraday_range * 0.5)
        except Exception:
            pass

        self._atr_cache[symbol] = {'ts': now, 'value': atr}

        # 首次或 VIX 变化时打日志
        if not cached or abs(cached.get('value', 0) - atr) / max(cached.get('value', 1), 0.01) > 0.1:
            logger.debug(
                f"[ATR动态] {symbol}: daily={daily_atr:.3f} × vix_factor={vix_factor:.2f} "
                f"(VIX={vix:.1f}) = {atr:.3f}"
            )

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
        except Exception as _silent_e:
            logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
        # 降级: 从 MarketDataRouter / alphavantage 获取
        try:
            from app.data.alphavantage_provider import AlphaVantageProvider
            provider = AlphaVantageProvider()
            vix = provider.get_vix()
            if vix and vix > 0:
                self._vix_cached = float(vix)
                self._vix_ts = now
                return self._vix_cached
        except Exception as _silent_e:
            logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")
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
                        # P1 修复 (2026-05-29): floor系数0.7→0.85，防止极端缩水
                        # Phase 3 (2026-05-31): 系数外部化 (atr_floor_coef)
                        try:
                            _floor_coef = float(
                                (self._load_exit_policy_dict().get("progressive_trailing") or {}).get(
                                    "atr_floor_coef", 0.85)
                            )
                        except Exception:
                            _floor_coef = 0.85
                        atr = max(atr, median_tr * _floor_coef)
                except Exception as _silent_e:
                    logger.warning(f"[silent_except:position_exit_monitor.py] {_silent_e}")

                return atr
        except Exception as e:
            logger.debug(f"ATR 计算失败 {symbol}: {e}")

        return 0.0

    def _get_intraday_range(self, symbol: str) -> float:
        """获取当日盘中极差 (high - low), 用作实时波动底限 (2026-06-07)

        从 Futu 1min K 线取今日数据, 计算 high - low.
        开盘前/无数据时返回 0.
        """
        if not self._quote_ctx:
            return 0.0
        try:
            from futu import KLType, AuType
            today_et = _now_et().strftime('%Y-%m-%d')
            ret, data, _ = self._quote_ctx.request_history_kline(
                symbol, ktype=KLType.K_1M, max_count=390, autype=AuType.QFQ,
                start=today_et, end=f"{today_et} 23:59:59",
            )
            if ret == 0 and data is not None and len(data) >= 2:
                today_high = float(data['high'].max())
                today_low = float(data['low'].min())
                return max(0.0, today_high - today_low)
        except Exception:
            pass
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
