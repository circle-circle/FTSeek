"""配置键枚举 - 防止 typo, 确保 SSOT (Task #46, 2026-05-28)

将所有关键业务阈值/资金预算的 YAML 路径常量化, 配合
``ConfigContainer.get_dotted(ConfigKeys.XXX.value, default)`` 使用,
避免在多个模块中分别硬编码字符串路径或数值。

约束:
- 仅 import Enum, 不引入任何项目内部依赖, 避免循环导入。
- 默认值在调用处显式传入 (与 unified_config.yaml 当前值保持一致),
  保证 get_config() 未初始化时仍可降级。
"""
from enum import Enum


class ConfigKeys(str, Enum):
    """所有业务配置在 unified_config.yaml 中的 dotted path"""

    # ── 风控 / 信号质量 ──────────────────────────────────────
    MIN_CONFIDENCE = "risk.signal_quality.min_confidence"
    # DEB-20260721 A-1: 原路径少 "_floor" 后缀 → YAML 声明值永不生效, 恒回退 50.
    MIN_EFFECTIVE_STRENGTH = "risk.pre_trade.min_effective_strength_floor"
    SHORT_MIN_STRENGTH = "risk.pre_trade.short_min_strength"
    # BATTLE_PLAN_BONUS 已删除 (2026-08-24): 旧作战计划轨道 7/13 废弃冻结 (ADR-012),
    # bonus 键零消费 (battle_plan_bonus_enabled=false 永不启用).

    # ── 资金预算 ────────────────────────────────────────────
    MAIN_BUDGET_PCT = "strategies.main.capital_pct"
    MAX_TOTAL_POSITION = "risk_limits.max_total_position"

    # ── 推荐系统 ────────────────────────────────────────────
    CONFIDENCE_HIGH = "recommendations.confidence_thresholds.high"
    CONFIDENCE_MEDIUM = "recommendations.confidence_thresholds.medium"
    MAX_RECOMMENDATIONS = "recommendations.max_recommendations"

    # ── 投票系统 ────────────────────────────────────────────
    VOTING_ENABLE_4_ROLES = "voting.enable_4_roles"
    VOTING_MODELS = "voting.models"

    # ── 退出策略 SSOT 路径 (2026-06-21 修复: 旧路径均不存在, 永远命中 fallback) ──
    # 旧路径 'exit_policy.stop_loss_pct' 在 exit_policy.yaml 中不存在 → fallback 0.045
    # 正确路径: dynamic_exit.default.stop_loss (unified_config top-level)
    #          或 exit_policy.exit_rules.atr_stop.max_loss_pct (exit_policy.yaml)
    STOP_LOSS_PCT = "dynamic_exit.default.stop_loss"
    TAKE_PROFIT_PCT = "dynamic_exit.default.take_profit"
    ATR_STOP_ENABLED = "exit_policy.exit_rules.atr_stop.enabled"
    ATR_MULTIPLIER = "exit_policy.exit_rules.atr_stop.multiplier_by_profit.initial"
