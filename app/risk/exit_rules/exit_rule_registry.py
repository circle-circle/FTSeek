"""退出规则注册表 - 优先级与互斥关系编码化

Task #48: 退出规则优先级注册表 — 编码化优先级与互斥关系

背景:
  FTSeek-US 现有 6 条退出规则 (flash_exit, day_trade, trailing_stop,
  pre_close, time_stop, scale_out)。在原 position_exit_monitor.scan_positions
  中按"先触发先行"的顺序执行检查, 多规则同时触发时缺乏明确的优先级与
  互斥规则约束。本模块统一管理:
    - 各规则的优先级 (ExitRulePriority, IntEnum, 值越小优先级越高)
    - 规则之间的互斥关系 (EXCLUSIVE_RULES, key 触发后跳过 value 集合)
    - 注册表 ExitRuleRegistry 提供 register / enable / disable / check_all_rules 接口

向后兼容:
  - 本模块为新增辅助类, 不强制替换 position_exit_monitor 中现有的逐条检查
  - 渐进式引入: position_exit_monitor 可在 __init__ 中实例化 registry,
    内部仍调用现有 _check_xxx 方法, 后续逐步迁移到 registry.check_all_rules()
"""
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, List, Tuple, Dict, Set, Callable, Any

logger = logging.getLogger("ftseek.exit_registry")


# ----------------------------------------------------------------------
# Task #53 (2026-05-28): 退出规则上下文
#   管理防振荡冷却、最小持仓时间、硬止损豁免等前置过滤状态。
#   使 ExitRuleRegistry 能够完整替代 scan_positions 中原本散落的逐条检查逻辑。
# ----------------------------------------------------------------------
@dataclass
class ExitRuleContext:
    """规则检查的上下文信息

    管理防振荡冷却、最小持仓时间等前置过滤状态，
    使 Registry 能够完整替代 scan_positions 中的逐条检查逻辑。
    """
    # 防振荡：symbol -> last_exit_check_time
    exit_cooldowns: Dict[str, float] = field(default_factory=dict)
    cooldown_sec: float = 60.0

    # 最小持仓时间（分钟）
    # Task #67 (2026-05-29): 由 10min 提升至 30min
    # 数据依据: 0min 持仓 163 笔亏 -$15,413; 5-6h 持仓 75% 胜率 +$5,187
    # 仅约束慢速退出规则; 硬止损 (STOP_LOSS / FLASH_EXIT / PRE_CLOSE) 仍可立即执行
    min_hold_minutes: float = 30.0

    # 硬止损豁免的 reason 集合 (这些 reason 不受最小持仓时间限制)
    # Task #67: 显式纳入 PRE_CLOSE_EXIT / FLASH_EXIT / QUICK_EXIT, 避免紧急退出被阻断
    hard_stop_reasons: Set[str] = field(default_factory=lambda: {
        "STOP_LOSS", "ATR_LOSS_1X",
        "FLASH_EXIT", "FLASH_EXIT_MOMENTUM", "FLASH_EXIT_VOLUME",
        "QUICK_EXIT",
        "PRE_CLOSE", "PRE_CLOSE_EXIT",
        "SL_HIT", "PRICE_FAIL",
    })

    def is_in_cooldown(self, symbol: str) -> bool:
        """检查是否在冷却期"""
        last_time = self.exit_cooldowns.get(symbol, 0.0)
        return (time.time() - last_time) < self.cooldown_sec

    def mark_checked(self, symbol: str) -> None:
        """标记已检查（更新冷却时间）"""
        self.exit_cooldowns[symbol] = time.time()

    def should_skip_non_hard_exits(self, held_minutes: float) -> bool:
        """是否跳过非硬止损退出（最小持仓时间未满）"""
        return held_minutes < self.min_hold_minutes

    def is_hard_stop(self, exit_reason: str) -> bool:
        """判断是否为硬止损（不受最小持仓时间限制）"""
        if not exit_reason:
            return False
        return exit_reason in self.hard_stop_reasons


class ExitRulePriority(IntEnum):
    """退出规则优先级 (值越小优先级越高)

    与 exit_policy.yaml / position_exit_monitor.scan_positions 的实际语义对齐:
      - PRE_CLOSE 在原代码中即被单独前置处理, 优先级最高
      - FLASH_EXIT 是异常动量平仓, 触发后立即清仓
      - DAY_TRADE / TRAILING_STOP / GRADUATED_STOP / TIME_STOP 是常态止损止盈
      - SCALE_OUT 仅做分批减仓, 优先级最低

    Task #64 (2026-05-29): 引入 GRADUATED_STOP=45, 介于 TRAILING_STOP(40)
    与 TIME_STOP(50) 之间; 整体改用 ×10 步进, 便于后续在两规则之间插入新规则。
    """
    PRE_CLOSE = 10       # 盘末强制退出, 优先级最高
    FLASH_EXIT = 20      # 异常动量平仓
    DAY_TRADE = 30       # 日内短线止损止盈
    THESIS_FAILURE = 35  # Task #75: 入场论文失效结构性止损 (DAY_TRADE 与 TRAILING_STOP 之间)
    TRAILING_STOP = 40   # ATR 动态追踪 + Chandelier
    GRADUATED_STOP = 45  # Task #64: 分级减仓止损 (-0.5/-1.0/-1.5 ATR)
    TIME_STOP = 50       # 持仓时长止损
    VELOCITY_EXIT = 55   # Phase 3: 速度捕捉退出
    SCALE_OUT = 60       # 分批止盈


# 互斥规则定义: 如果 key 触发了, 则 value 中的规则不再检查
#   - pre_close 触发后清仓, 互斥所有其他规则
#   - flash_exit 触发后清仓, 不再做 trailing/graduated/time stop 等慢速管理
#   - Task #64: graduated_stop 与 trailing_stop 双向互斥 —
#     同一仓位、同一轮次内, 任一触发后另一方都不再触发,
#     避免 ATR 动态止损与分级减仓在相邻 ATR 区间内重复减仓。
#   - Task #64: scale_out 与 day_trade 不互斥 (Profit Lock 与分批止盈可共存)。
EXCLUSIVE_RULES: Dict[str, Set[str]] = {
    "pre_close": {
        "flash_exit", "day_trade", "trailing_stop",
        "graduated_stop", "time_stop", "scale_out",
        "thesis_failure",
    },
    "flash_exit": {"trailing_stop", "graduated_stop", "time_stop", "thesis_failure"},
    "trailing_stop": {"graduated_stop"},
    "graduated_stop": {"trailing_stop"},
    # Task #75: 入场论文失效后全平 → 不再触发慢速管理类规则
    "thesis_failure": {"trailing_stop", "graduated_stop", "time_stop", "scale_out"},
    # P0 修复 (2026-06-14): scale_out 触发后压制 trailing_stop / graduated_stop
    # 与 legacy 路径 _check_via_legacy 中的 _has_scale_out 互斥逻辑对齐
    # 根因: Registry 路径缺少此互斥 → scale_out (全平) 与 trailing_stop 可能同时触发,
    #   导致已平仓位被 trailing_stop 重复处理
    "scale_out": {"trailing_stop", "graduated_stop"},
}


# 规则名称到优先级的映射 (与 ExitRulePriority 对齐)
RULE_PRIORITIES: Dict[str, int] = {
    "pre_close": ExitRulePriority.PRE_CLOSE,
    "flash_exit": ExitRulePriority.FLASH_EXIT,
    "day_trade": ExitRulePriority.DAY_TRADE,
    "thesis_failure": ExitRulePriority.THESIS_FAILURE,
    "trailing_stop": ExitRulePriority.TRAILING_STOP,
    "graduated_stop": ExitRulePriority.GRADUATED_STOP,
    "time_stop": ExitRulePriority.TIME_STOP,
    "velocity_exit": ExitRulePriority.VELOCITY_EXIT,
    "scale_out": ExitRulePriority.SCALE_OUT,
}


# checker callable 协议:
#   (position, market_data, trail_state, policy_guardian) -> Optional[dict]
#   - 返回 None 表示不触发
#   - 返回 dict 表示触发 (结构与 position_exit_monitor._execute_exit 期望一致)
ExitChecker = Callable[..., Optional[Dict[str, Any]]]


class ExitRuleRegistry:
    """退出规则注册表

    管理所有退出规则的优先级、互斥关系, 按优先级顺序检查规则,
    尊重互斥约束。

    使用方式:
        registry = ExitRuleRegistry()
        registry.register("pre_close", check_pre_close_fn)
        registry.register("flash_exit", check_flash_exit_fn)
        ...
        decision = registry.check_all_rules(position, market_data,
                                            trail_state, policy_guardian)
        if decision:
            execute_close(position, decision)
    """

    def __init__(self):
        self._rules: Dict[str, ExitChecker] = {}  # rule_name -> checker_callable
        self._enabled_rules: Set[str] = set()

    # ------------------------------------------------------------------
    # 注册 / 启停
    # ------------------------------------------------------------------
    def register(self, rule_name: str, checker: ExitChecker) -> None:
        """注册一条退出规则

        Args:
            rule_name: 规则名 (与 RULE_PRIORITIES / EXCLUSIVE_RULES 的 key 对齐)
            checker:   可调用对象, 接收 (position, market_data, trail_state, policy_guardian),
                       返回 None 或退出决策 dict
        """
        if rule_name not in RULE_PRIORITIES:
            logger.warning(f"[ExitRegistry] 注册未知规则: {rule_name} (无优先级定义, 将以 99 兜底)")
        self._rules[rule_name] = checker
        self._enabled_rules.add(rule_name)

    def disable(self, rule_name: str) -> None:
        """禁用一条规则 (保留注册, 仅跳过检查)"""
        self._enabled_rules.discard(rule_name)

    def enable(self, rule_name: str) -> None:
        """启用一条规则 (要求已注册)"""
        if rule_name in self._rules:
            self._enabled_rules.add(rule_name)
        else:
            logger.warning(f"[ExitRegistry] enable 失败: {rule_name} 未注册")

    def is_enabled(self, rule_name: str) -> bool:
        return rule_name in self._enabled_rules

    def list_enabled(self) -> List[str]:
        """返回按优先级排序的已启用规则名列表"""
        return sorted(self._enabled_rules, key=lambda r: RULE_PRIORITIES.get(r, 99))

    # ------------------------------------------------------------------
    # 元数据查询
    # ------------------------------------------------------------------
    def get_priority(self, rule_name: str) -> int:
        """获取规则优先级 (未定义时返回 99)"""
        return int(RULE_PRIORITIES.get(rule_name, 99))

    def get_exclusions(self, rule_name: str) -> Set[str]:
        """获取规则的互斥集"""
        return set(EXCLUSIVE_RULES.get(rule_name, set()))

    def _get_priorities_for(self, position) -> Dict[str, int]:
        """返回考虑 position.direction 后的动态优先级映射

        Task #72 (2026-05-29): 退出策略简化 — Trailing为王
          SHORT 方向 trailing_stop 优先级由 40 提升至 30 (与 DAY_TRADE 同级),
          确保 trailing 成为 SHORT 持仓的主退出路径,
          避免被 STRUCTURE_EXIT 等下游规则抢先触发。
          LONG 方向优先级保持不变, 互斥规则 (GRADUATED⇄TRAILING) 不受影响。
        """
        priorities: Dict[str, int] = dict(RULE_PRIORITIES)
        direction = getattr(position, "direction", None)
        # Direction 是 (str, Enum), 也兼容纯字符串持仓对象
        direction_str = (
            direction.value if hasattr(direction, "value") else str(direction or "")
        ).upper()
        if direction_str == "SHORT":
            priorities["trailing_stop"] = int(ExitRulePriority.DAY_TRADE)  # 30
        return priorities

    # ------------------------------------------------------------------
    # 核心: 按优先级 + 互斥约束执行所有规则检查
    # ------------------------------------------------------------------
    def check_all_rules(
        self,
        position,
        market_data,
        trail_state,
        policy_guardian=None,
    ) -> Optional[Dict[str, Any]]:
        """按优先级检查所有规则, 返回首个触发的决策

        语义:
          - 已启用的规则按 RULE_PRIORITIES 升序遍历 (优先级高的先检查)
          - 任一规则触发后, 把它的互斥集加入 excluded_rules, 后续遍历到的规则会被跳过
          - PRE_CLOSE 触发即直接 break (互斥所有, 无需再检查)
          - 多规则触发时返回第一个 (优先级最高的)

        Args:
            position: 持仓对象 (需要 .symbol 等属性)
            market_data: 市场数据上下文 (current_price / atr / pnl_pct / now_ts ...)
            trail_state: 追踪止盈状态 dict
            policy_guardian: ExitPolicyGuardian 或 None

        Returns:
            退出决策 dict 或 None (无规则触发)
        """
        triggered: List[Tuple[str, Dict[str, Any]]] = []
        excluded_rules: Set[str] = set()

        # Task #72: 按 position.direction 计算动态优先级 (SHORT trailing 提至 30)
        priorities = self._get_priorities_for(position)

        # 按优先级排序 (值越小优先级越高); 同级时按规则名稳定排序, 保证可复现
        sorted_rules = sorted(
            self._enabled_rules,
            key=lambda r: (priorities.get(r, 99), r),
        )

        for rule_name in sorted_rules:
            # 跳过被互斥的规则
            if rule_name in excluded_rules:
                logger.debug(
                    f"[ExitRegistry] {getattr(position, 'symbol', '?')}: "
                    f"{rule_name} 被互斥规则跳过"
                )
                continue

            checker = self._rules.get(rule_name)
            if not checker:
                continue

            try:
                result = checker(position, market_data, trail_state, policy_guardian)
            except Exception as e:
                logger.error(
                    f"[ExitRegistry] {getattr(position, 'symbol', '?')}: "
                    f"{rule_name} 检查异常: {e}",
                    exc_info=True,
                )
                continue

            if result:
                triggered.append((rule_name, result))
                logger.info(
                    f"[ExitRegistry] {getattr(position, 'symbol', '?')}: "
                    f"{rule_name} 触发 "
                    f"(reason={result.get('reason', 'N/A')}, "
                    f"ratio={result.get('close_ratio', result.get('exit_ratio', 1.0))})"
                )

                # 应用互斥规则: 后续遍历到的规则只要在 exclusions 里都跳过
                exclusions = EXCLUSIVE_RULES.get(rule_name, set())
                if exclusions:
                    excluded_rules.update(exclusions)
                    logger.debug(
                        f"[ExitRegistry] {rule_name} 互斥: 跳过 {exclusions}"
                    )

                # 最高优先级规则直接 break (PRE_CLOSE 互斥所有)
                if rule_name == "pre_close":
                    break

        if not triggered:
            return None

        # 多规则触发时取最高优先级的 (sorted_rules 已升序, 第一个即最高)
        best_rule, best_result = triggered[0]

        if len(triggered) > 1:
            rule_names = [r for r, _ in triggered]
            logger.info(
                f"[ExitRegistry] {getattr(position, 'symbol', '?')}: "
                f"多规则触发 {rule_names} -> 选择 {best_rule}"
            )

        return best_result

    # ------------------------------------------------------------------
    # Task #53: 带上下文的规则检查 (完整版, 含前置过滤)
    # ------------------------------------------------------------------
    def check_all_rules_with_context(
        self,
        position,
        market_data,
        trail_state,
        context: "ExitRuleContext",
        policy_guardian=None,
        held_minutes: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """带上下文的规则检查 (完整版)

        包含前置过滤：
          1. 防振荡冷却 (symbol 维度, cooldown_sec 内不重复检查)
          2. 最小持仓时间检查 + 硬止损豁免 (持仓未满 min_hold_minutes 时,
             只允许 context.hard_stop_reasons 中定义的硬止损通过)

        Args:
            position:        持仓对象
            market_data:     市场数据 dict
            trail_state:     追踪止盈状态 dict
            context:         ExitRuleContext, 共享冷却 / 最小持仓 / 硬止损豁免
            policy_guardian: ExitPolicyGuardian 或 None
            held_minutes:    当前持仓已持有的分钟数

        Returns:
            退出决策 dict 或 None
        """
        symbol = getattr(position, 'symbol', str(position))

        # 前置过滤 1: 防振荡冷却
        if context.is_in_cooldown(symbol):
            logger.debug(
                f"[ExitRegistry] {symbol}: 冷却期内 (<{context.cooldown_sec}s), 跳过检查"
            )
            return None

        # 执行规则检查 (复用 check_all_rules: 优先级 + 互斥)
        result = self.check_all_rules(position, market_data, trail_state, policy_guardian)

        if result is None:
            return None

        # 前置过滤 2: 最小持仓时间检查 (硬止损豁免)
        exit_reason = result.get("reason", "") if isinstance(result, dict) else ""
        if context.should_skip_non_hard_exits(held_minutes):
            if not context.is_hard_stop(exit_reason):
                logger.debug(
                    f"[ExitRegistry] {symbol}: 持仓 {held_minutes:.1f}min < "
                    f"{context.min_hold_minutes}min, 非硬止损 ({exit_reason}) 跳过"
                )
                return None

        # 标记已检查 (进入冷却期)
        context.mark_checked(symbol)
        return result


__all__ = [
    "ExitRulePriority",
    "ExitRuleContext",
    "EXCLUSIVE_RULES",
    "RULE_PRIORITIES",
    "ExitRuleRegistry",
]
