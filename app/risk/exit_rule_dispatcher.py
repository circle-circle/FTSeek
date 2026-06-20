"""
统一退出规则调度器 (Phase 3, 2026-05-31)
=============================================

所有退出决策的唯一入口 — 影子模式优先, 验证一致性后再切主路径。

优先级 (从高到低):
  1. flash_exit       (闪崩紧急退出)
  2. pre_close        (收盘前强平)
  3. circuit_breaker  (组合熔断)
  4. earnings_guard   (财报前/后强制退出)
  5. trailing_stop    (Chandelier 追踪止损)
  6. day_trade        (日内止盈/超时)
  7. time_decay       (时间衰减)
  8. scale_out        (分批止盈)

互斥定义来源 exit_policy.yaml :: exit_rules_exclusivity
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ftseek.exit_rule_dispatcher")


# ============================================================
#  数据结构
# ============================================================

@dataclass
class ExitDecision:
    """退出决策结果"""
    should_exit: bool
    rule_name: str                              # 触发的规则名
    priority: int                               # 优先级 (数字越小越高)
    exit_price: Optional[float] = None
    reason: str = ""
    suppressed_rules: List[str] = field(default_factory=list)  # 被互斥压制的规则
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_exit": self.should_exit,
            "rule_name": self.rule_name,
            "priority": self.priority,
            "exit_price": self.exit_price,
            "reason": self.reason,
            "suppressed_rules": list(self.suppressed_rules),
            "metadata": dict(self.metadata),
        }


# ============================================================
#  调度器
# ============================================================

class ExitRuleDispatcher:
    """统一退出规则调度器 (无状态评估器, 实例可单例化)"""

    # 规则优先级定义 (数字越小越优先)
    # P0 (2026-06-07): scale_out 优先级提升至 trailing_stop 之前
    # 原理: scale_out 是唯一正EV退出(100%WR), 应优先于 trailing_stop 执行
    RULE_PRIORITIES: Dict[str, int] = {
        "flash_exit": 1,
        "pre_close": 2,
        "circuit_breaker": 3,
        "earnings_guard": 4,
        "scale_out": 5,           # was 8 → 5, 止盈优先
        "day_trade": 6,
        "trailing_stop": 7,       # was 5 → 7, 让 scale_out 先锁利
        "time_decay": 8,
    }

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: 从 exit_policy.yaml 加载的配置字典
        """
        self.config: Dict[str, Any] = config or {}
        self.exit_rules: Dict[str, Any] = self.config.get("exit_rules", {}) or {}
        # 兼容多种命名: exit_rules_exclusivity / exclusivity
        self.exclusivity: Dict[str, List[str]] = (
            self.config.get("exit_rules_exclusivity")
            or self.config.get("exclusivity")
            or {}
        )
        self._last_decisions: Dict[str, ExitDecision] = {}

    # ------------------------------------------------------------------
    # 评估入口
    # ------------------------------------------------------------------

    def evaluate(
        self,
        position: Dict[str, Any],
        market_data: Dict[str, Any],
        trail_state: Optional[Dict[str, Any]] = None,
    ) -> Optional[ExitDecision]:
        """
        评估所有退出规则, 返回最高优先级的触发决策。

        Args:
            position: 持仓信息 dict (symbol, entry_price, quantity, owner_strategy, holding_hours, position_id ...)
            market_data: 实时行情 dict (current_price, vix, atr, minutes_to_close, daily_loss_pct, near_earnings ...)
                【ATR 一致性】market_data['atr'] 必须由调用方从 ATRProvider
                (app.core.atr_provider) 获取后注入，以与 TrendFirst / atr_stop_engine 等
                同根同源、数据一致 (Task #4)。本 Dispatcher 不独立计算 ATR。
            trail_state: 追踪止损状态 dict (trail_high, trail_status ...)

        Returns:
            ExitDecision | None — None 表示没有规则触发
        """
        triggered: List[ExitDecision] = []

        # 按优先级遍历
        ordered_rules = sorted(self.RULE_PRIORITIES.keys(), key=lambda r: self.RULE_PRIORITIES[r])
        enabled_count = 0
        for rule_name in ordered_rules:
            rule_cfg = self._rule_cfg(rule_name)
            if not rule_cfg.get("enabled", False):
                continue
            enabled_count += 1
            decision = self._evaluate_rule(rule_name, rule_cfg, position, market_data, trail_state or {})
            if decision and decision.should_exit:
                triggered.append(decision)

        # Fail-open 保护: 所有退出规则都禁用时告警 (legacy fallback 仍会运行, 但应引起注意)
        if enabled_count == 0 and not getattr(self, '_alerted_no_rules', False):
            self._alerted_no_rules = True
            logger.critical(
                "🚨 [ExitRuleDispatcher] 所有退出规则均禁用! "
                "持仓将仅依赖 legacy exit_monitor 保护。"
                "请检查 exit_policy.yaml 配置。"
            )

        if not triggered:
            return None

        # 取最高优先级 (列表第一个由于排序保证)
        triggered.sort(key=lambda d: d.priority)
        best = triggered[0]

        # 互斥检查: 记录被压制规则
        suppressed: List[str] = []
        active_exclusions = self.exclusivity.get(best.rule_name, []) or []
        for other in triggered[1:]:
            if other.rule_name in active_exclusions:
                suppressed.append(other.rule_name)
        best.suppressed_rules = suppressed

        # 缓存
        pos_id = str(position.get("position_id") or position.get("symbol", ""))
        if pos_id:
            self._last_decisions[pos_id] = best

        logger.info(
            "ExitRuleDispatcher: %s -> rule=%s(P%d), reason=%s, suppressed=%s",
            position.get("symbol", "?"), best.rule_name, best.priority,
            best.reason, suppressed,
        )
        return best

    # ------------------------------------------------------------------
    # 互斥/优先级查询 (供外部调试/影子模式对比)
    # ------------------------------------------------------------------

    def check_exclusivity(self, triggered_rule: str, position: Dict[str, Any]) -> List[str]:
        """返回 triggered_rule 触发时被压制的规则列表"""
        return list(self.exclusivity.get(triggered_rule, []) or [])

    def last_decision(self, position_id: str) -> Optional[ExitDecision]:
        return self._last_decisions.get(position_id)

    # ------------------------------------------------------------------
    # 内部: 规则配置查询 (兼容多种 YAML key 命名)
    # ------------------------------------------------------------------

    _RULE_KEY_ALIASES = {
        "flash_exit": ["flash_exit"],
        "pre_close": ["pre_close", "pre_close_exit"],
        "circuit_breaker": ["circuit_breaker"],
        "earnings_guard": ["earnings_guard"],
        "trailing_stop": ["trailing_stop", "chandelier_trail"],
        "day_trade": ["day_trade", "day_trade_mode"],
        "time_decay": ["time_decay"],
        "scale_out": ["scale_out"],
    }

    def _rule_cfg(self, rule_name: str) -> Dict[str, Any]:
        for key in self._RULE_KEY_ALIASES.get(rule_name, [rule_name]):
            cfg = self.exit_rules.get(key)
            if isinstance(cfg, dict):
                return cfg
            # day_trade_mode 在 YAML 中位于顶层而非 exit_rules 下
            cfg2 = self.config.get(key)
            if isinstance(cfg2, dict):
                return cfg2
        return {}

    # ------------------------------------------------------------------
    # 单条规则评估分发
    # ------------------------------------------------------------------

    def _evaluate_rule(
        self,
        rule_name: str,
        rule_config: Dict[str, Any],
        position: Dict[str, Any],
        market_data: Dict[str, Any],
        trail_state: Dict[str, Any],
    ) -> Optional[ExitDecision]:
        evaluators: Dict[str, Callable[..., Optional[ExitDecision]]] = {
            "flash_exit": self._eval_flash_exit,
            "pre_close": self._eval_pre_close,
            "circuit_breaker": self._eval_circuit_breaker,
            "earnings_guard": self._eval_earnings_guard,
            "trailing_stop": self._eval_trailing_stop,
            "day_trade": self._eval_day_trade,
            "time_decay": self._eval_time_decay,
            "scale_out": self._eval_scale_out,
        }
        evaluator = evaluators.get(rule_name)
        if evaluator is None:
            return None
        try:
            return evaluator(rule_config, position, market_data, trail_state)
        except Exception as e:
            logger.error("Rule evaluation error [%s]: %s", rule_name, e, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 各规则实现 (尽量保守、只读 — 与 ExitMonitor.legacy 路径并行运行)
    # ------------------------------------------------------------------

    def _eval_flash_exit(self, cfg, position, market_data, trail_state):
        """闪崩检测 — 价格速度异常 (非累计偏移)

        P0 根因修复 (2026-06-02):
        ─────────────────────────
        旧逻辑: adverse_pct = |entry - current| / entry  (累计偏移)
        问题: 1× session-blended ATR ≈ 正常波动, 不是闪崩
              → 今天 173 次误触发, -$11K 亏损

        新逻辑: velocity = |prev_scan - current| / prev_scan  (扫描间速度)
              真正的闪崩 = 30s 内价格暴跌 > 2× ATR
              正常波动 ~0.5× ATR/30s, 不会触发
        """
        import time as _time

        # ── min_holding_seconds 保护 (默认 120s, 给信号发展时间) ──
        min_hold = int(cfg.get("min_holding_seconds", 120))
        entry_ts = float(position.get("entry_time_ts", 0) or 0)
        now_ts = float(market_data.get("now_ts", _time.time()))
        if entry_ts > 0 and min_hold > 0:
            elapsed = now_ts - entry_ts
            if elapsed < min_hold:
                return None

        current_price = float(market_data.get("current_price", 0))
        if current_price <= 0:
            return None

        # ── 速度检测: 两次扫描间的价格变化 (非入场以来累计) ──
        prev_price = float(trail_state.get("prev_scan_price", 0) or 0)
        if prev_price <= 0:
            # 首次扫描, 无速度数据, 不触发
            return None

        direction = str(position.get("direction", "LONG")).upper()
        if direction == "LONG":
            velocity = (prev_price - current_price) / prev_price  # 正=下跌(不利)
        else:
            velocity = (current_price - prev_price) / prev_price  # 正=上涨(不利)

        if velocity <= 0:
            return None  # 价格向有利方向移动, 无闪崩

        # ── 阈值: 使用 raw ATR (不做 session blending) ──
        # session blending 是为 trailing stop 设计的, 不适用于闪崩检测
        # 闪崩是绝对速度事件, 不因"早盘波动正常"而降低警觉
        entry_price = float(position.get("entry_price", 0))
        atr_raw = float(market_data.get("atr_raw", 0) or market_data.get("atr", 0) or 0)
        if atr_raw <= 0 or entry_price <= 0:
            return None

        mult = float(cfg.get("flash_multiplier_default", 2.0))  # 默认 2.0× ATR (从 1.0 提高)
        # 将 ATR 转换为百分比阈值
        # ATR 是 1-min bar 的平均真实波幅, 一次扫描间隔 ~15-30s
        # 正常: 一次扫描移动 ~0.3-0.5× ATR
        # 闪崩: 一次扫描移动 > 2.0× ATR
        threshold = (mult * atr_raw) / entry_price

        if velocity >= threshold:
            return ExitDecision(
                should_exit=True,
                rule_name="flash_exit",
                priority=self.RULE_PRIORITIES["flash_exit"],
                exit_price=current_price,
                reason=(
                    f"Flash velocity {velocity*100:.3f}% >= {threshold*100:.3f}% "
                    f"({mult}×ATR_raw={atr_raw:.3f})"
                ),
                metadata={
                    "velocity_pct": velocity,
                    "threshold": threshold,
                    "atr_raw": atr_raw,
                    "prev_price": prev_price,
                    "elapsed_sec": (now_ts - entry_ts) if entry_ts > 0 else -1,
                },
            )
        return None

    def _eval_pre_close(self, cfg, position, market_data, trail_state):
        """收盘前退出 (Task #3, 2026-05-31): 调用自适应分级退出.

        优先使用 mode=adaptive 调用 check_adaptive_pre_close_exit;
        避免单一 ATR 阈值导致 54% 仓位被动强平。
        同时保留 legacy 单一阈值分支以兼容老配置。
        """
        try:
            from app.risk.exit_rules.pre_close import check_adaptive_pre_close_exit
        except Exception as e:  # pragma: no cover
            logger.warning("adaptive pre_close import failed: %s", e)
            check_adaptive_pre_close_exit = None  # type: ignore

        mode = str(cfg.get("mode", "adaptive")).lower()
        if check_adaptive_pre_close_exit is not None and mode == "adaptive":
            result = check_adaptive_pre_close_exit(position, market_data, trail_state, cfg)
            if not result:
                return None
            close_ratio = float(result.get("close_ratio", 1.0) or 1.0)
            meta = dict(result.get("metadata") or {})
            meta.setdefault("close_ratio", close_ratio)
            return ExitDecision(
                should_exit=True,
                rule_name="pre_close",
                priority=self.RULE_PRIORITIES["pre_close"],
                exit_price=result.get("exit_price") or market_data.get("current_price"),
                reason=str(result.get("reason_cn") or result.get("reason") or "pre_close"),
                metadata=meta,
            )

        # ---- legacy fallback: 单一窗口强平 (仅在 mode != adaptive 时生效) ----
        # 兼容: minutes_before_close (任务) | window_minutes (现 YAML)
        minutes_before = float(cfg.get("minutes_before_close", cfg.get("window_minutes", 15)))
        minutes_to_close = market_data.get("minutes_to_close")
        if minutes_to_close is None:
            return None
        try:
            mtc = float(minutes_to_close)
        except (TypeError, ValueError):
            return None
        if mtc <= minutes_before:
            return ExitDecision(
                should_exit=True,
                rule_name="pre_close",
                priority=self.RULE_PRIORITIES["pre_close"],
                exit_price=market_data.get("current_price"),
                reason=f"Pre-close legacy: {mtc:.0f}min to close <= {minutes_before:.0f}min threshold",
                metadata={"minutes_to_close": mtc, "mode": mode},
            )
        return None

    def _eval_circuit_breaker(self, cfg, position, market_data, trail_state):
        """组合熔断 (基于 daily_loss_pct)"""
        threshold = float(cfg.get("threshold_pct", 5.0)) / 100.0
        daily_loss_pct = market_data.get("daily_loss_pct")
        if daily_loss_pct is None:
            return None
        try:
            loss = float(daily_loss_pct)
        except (TypeError, ValueError):
            return None
        if loss >= threshold:
            return ExitDecision(
                should_exit=True,
                rule_name="circuit_breaker",
                priority=self.RULE_PRIORITIES["circuit_breaker"],
                exit_price=market_data.get("current_price"),
                reason=f"Circuit breaker: daily_loss={loss*100:.2f}% >= {threshold*100:.2f}%",
                metadata={"daily_loss_pct": loss},
            )
        return None

    def _eval_earnings_guard(self, cfg, position, market_data, trail_state):
        """财报期保护 - 临近财报强制退出"""
        # 仅当 market_data 显式标记为 near_earnings 才触发, 避免误杀
        if not market_data.get("near_earnings"):
            return None
        # 默认仅"建议退出"模式: 只在 force_exit=True 时才实际触发
        if not cfg.get("force_exit_on_dispatcher", False):
            return None
        return ExitDecision(
            should_exit=True,
            rule_name="earnings_guard",
            priority=self.RULE_PRIORITIES["earnings_guard"],
            exit_price=market_data.get("current_price"),
            reason="Earnings guard: near earnings window, force exit",
            metadata={"days_before": cfg.get("days_before"), "days_after": cfg.get("days_after")},
        )

    def _eval_trailing_stop(self, cfg, position, market_data, trail_state):
        """Chandelier 追踪止损"""
        atr = float(market_data.get("atr", 0))
        if atr <= 0:
            return None
        current_price = float(market_data.get("current_price", 0))
        direction = str(position.get("direction", "LONG")).upper()
        # P0: trailing stop is a pure profit-locking mechanism — never fire on losing positions
        # Losing positions are handled exclusively by the hard ATR_STOP (4x ATR)
        entry_price = float(position.get("entry_price", 0))
        if entry_price > 0 and current_price > 0:
            if direction == "LONG":
                unrealized_pnl = current_price - entry_price
            else:
                unrealized_pnl = entry_price - current_price
            if unrealized_pnl <= 0:
                return None

        if direction == "LONG":
            peak = float(trail_state.get("trail_high") or trail_state.get("highest_price") or 0)
            if peak <= 0:
                return None
        else:
            peak = float(trail_state.get("trail_low") or trail_state.get("lowest_price") or 0)
            if peak <= 0:
                return None

        # 兼容: trail_distance_atr (任务) | multiplier_by_regime.normal (现)
        if "trail_distance_atr" in cfg:
            trail_dist = float(cfg.get("trail_distance_atr", 1.2)) * atr
        else:
            mbr = cfg.get("multiplier_by_regime") or {}
            mult = mbr.get(market_data.get("regime", "normal"), 2.0)
            try:
                mult = float(mult)
            except (TypeError, ValueError):
                mult = 2.0
            trail_dist = mult * atr

        # 激活门槛
        activation = float(cfg.get("activation_threshold_atr", cfg.get("activation_profit_atr", 0.8)))
        if entry_price > 0:
            if direction == "LONG":
                profit_atr = (current_price - entry_price) / atr
            else:
                profit_atr = (entry_price - current_price) / atr
            if profit_atr < activation:
                return None

        if direction == "LONG":
            triggered = (peak - current_price) >= trail_dist
        else:
            triggered = (current_price - peak) >= trail_dist

        if triggered:
            return ExitDecision(
                should_exit=True,
                rule_name="trailing_stop",
                priority=self.RULE_PRIORITIES["trailing_stop"],
                exit_price=current_price,
                reason=f"Trailing stop: drop from peak={peak:.2f}, distance={trail_dist:.2f}",
                metadata={"trail_high": peak, "trail_distance": trail_dist},
            )
        return None

    def _eval_day_trade(self, cfg, position, market_data, trail_state):
        """日内止盈 / 超时"""
        atr = float(market_data.get("atr", 0))
        entry_price = float(position.get("entry_price", 0))
        current_price = float(market_data.get("current_price", 0))
        holding_hours = float(position.get("holding_hours", 0))
        direction = str(position.get("direction", "LONG")).upper()
        if entry_price <= 0 or current_price <= 0:
            return None

        # 目标价止盈 (ATR 倍数)
        if atr > 0:
            tp_target_atr = float(cfg.get("tp1_target_atr", 1.5))
            if direction == "LONG":
                profit = current_price - entry_price
            else:
                profit = entry_price - current_price
            if profit >= tp_target_atr * atr:
                return ExitDecision(
                    should_exit=True,
                    rule_name="day_trade",
                    priority=self.RULE_PRIORITIES["day_trade"],
                    exit_price=current_price,
                    reason=f"Day trade TP: profit={profit:.2f} >= {tp_target_atr}*ATR({atr:.2f})",
                    metadata={"profit": profit, "tp_target_atr": tp_target_atr},
                )

        # 超时
        max_hours = float(cfg.get("max_holding_hours", 6))
        if holding_hours >= max_hours:
            return ExitDecision(
                should_exit=True,
                rule_name="day_trade",
                priority=self.RULE_PRIORITIES["day_trade"],
                exit_price=current_price,
                reason=f"Day trade timeout: {holding_hours:.1f}h >= {max_hours}h",
                metadata={"holding_hours": holding_hours, "max_hours": max_hours},
            )
        return None

    def _eval_time_decay(self, cfg, position, market_data, trail_state):
        """时间衰减止损"""
        max_hours = float(cfg.get("max_hours", cfg.get("max_holding_hours", 48)))
        holding_hours = float(position.get("holding_hours", 0))
        if holding_hours >= max_hours:
            return ExitDecision(
                should_exit=True,
                rule_name="time_decay",
                priority=self.RULE_PRIORITIES["time_decay"],
                exit_price=market_data.get("current_price"),
                reason=f"Time decay: {holding_hours:.1f}h >= max {max_hours}h",
                metadata={"holding_hours": holding_hours, "max_hours": max_hours},
            )
        return None

    def _eval_scale_out(self, cfg, position, market_data, trail_state):
        """分批止盈 - 基础实现 (仅按总收益百分比触发首层, 后续可扩展)"""
        tiers = cfg.get("tiers") or []
        if not tiers:
            return None
        entry_price = float(position.get("entry_price", 0))
        current_price = float(market_data.get("current_price", 0))
        if entry_price <= 0 or current_price <= 0:
            return None
        direction = str(position.get("direction", "LONG")).upper()
        if direction == "LONG":
            profit_pct = (current_price - entry_price) / entry_price * 100
        else:
            profit_pct = (entry_price - current_price) / entry_price * 100

        executed_tiers = set((trail_state.get("scale_out_executed") or []))
        for t in tiers:
            level = t.get("level")
            trigger = float(t.get("trigger_profit_pct", 1e9))
            if level in executed_tiers:
                continue
            if profit_pct >= trigger:
                return ExitDecision(
                    should_exit=True,
                    rule_name="scale_out",
                    priority=self.RULE_PRIORITIES["scale_out"],
                    exit_price=current_price,
                    reason=f"Scale out level {level}: profit={profit_pct:.2f}% >= {trigger}%",
                    metadata={"level": level, "close_pct": t.get("close_pct"), "profit_pct": profit_pct},
                )
        return None


# ============================================================
#  单例工厂
# ============================================================

_singleton: Optional[ExitRuleDispatcher] = None


def get_exit_rule_dispatcher(config: Optional[Dict[str, Any]] = None) -> Optional[ExitRuleDispatcher]:
    """获取单例 (首次调用必须传 config; 后续调用可省略)"""
    global _singleton
    if _singleton is None:
        if config is None:
            return None
        _singleton = ExitRuleDispatcher(config)
    elif config is not None:
        # 允许重新加载配置
        _singleton.config = config
        _singleton.exit_rules = config.get("exit_rules", {}) or {}
        _singleton.exclusivity = (
            config.get("exit_rules_exclusivity") or config.get("exclusivity") or {}
        )
    return _singleton


def reset_exit_rule_dispatcher():
    """测试用: 重置单例"""
    global _singleton
    _singleton = None
