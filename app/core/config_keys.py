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
    MIN_EFFECTIVE_STRENGTH = "risk.pre_trade.min_effective_strength"
    SHORT_MIN_STRENGTH = "risk.pre_trade.short_min_strength"
    BATTLE_PLAN_BONUS = "risk.pre_trade.battle_plan_bonus"

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

    # ── 退出策略 (exit_policy SSOT) ─────────────────────────
    STOP_LOSS_PCT = "exit_policy.stop_loss_pct"
    TAKE_PROFIT_PCT = "exit_policy.take_profit_pct"
    ATR_STOP_ENABLED = "exit_policy.atr_stop_enabled"
    ATR_MULTIPLIER = "exit_policy.atr_multiplier"
