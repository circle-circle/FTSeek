"""
FTSeek-US 配置校验器 — Layer 1: 启动时强制校验

功能:
1. 必需配置键存在性检查 (required_keys)
2. 类型校验 (type checking)
3. 范围校验 (range validation)
4. 跨键一致性检查 (cross-key consistency)

严格模式 (strict_mode=True): 任何错误都阻止启动
降级模式 (strict_mode=False): 仅警告，不阻止启动
"""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("ftseek_us.config_mgmt.validator")


# ============================================================================
# 必需配置键定义
# ============================================================================

REQUIRED_KEYS = {
    # 基础配置
    "base.market": {"type": str, "expected": "US"},
    "base.currency": {"type": str, "expected": "USD"},
    "base.timezone": {"type": str},
    "base.trade_env": {"type": int},

    # 数据源
    "opend.host": {"type": str},
    "opend.port": {"type": int},

    # watchlist
    "watchlist.us_stocks": {"type": list, "min_length": 1},

    # 风控
    "risk.account.max_daily_drawdown": {"type": (int, float)},
    "risk.position.max_per_symbol": {"type": (int, float)},
    "risk.position.max_total": {"type": (int, float)},
    "risk.signal_quality.min_confidence": {"type": (int, float)},

    # 决策引擎
    "decision.signal_thresholds.min_score": {"type": int},

    # 决策引擎
    # (features.risk_engine/features.execution_engine removed — never consumed by code, caused 97+ false warnings per restart)
}

# 推荐但非必需的配置键 (缺失仅警告)
RECOMMENDED_KEYS = [
    "data_sources.finnhub.api_key",
    "data_sources.twelvedata.api_key",
    "news_v2.enabled",
    "exit.us.atr_multiplier",
    "strategy.consensus.threshold",
]

# 跨键一致性规则
CONSISTENCY_RULES = [
    {
        "name": "止盈止损合理性",
        "keys": ["risk.stop_loss.default", "risk.take_profit.tier1"],
        "check": lambda v1, v2: abs(v2) > abs(v1),
        "message": "止盈 ({}) 必须大于止损 ({})",
    },
    {
        "name": "总敞口合理性",
        "keys": ["risk.position.max_per_symbol", "risk.position.max_total"],
        "check": lambda v1, v2: v2 >= v1,
        "message": "总敞口 ({}) 必须 >= 单股敞口 ({})",
    },
    {
        "name": "置信度范围",
        "keys": ["risk.signal_quality.min_confidence"],
        "check": lambda v: 0.0 <= v <= 1.0,
        "message": "置信度 ({}) 必须在 [0, 1] 范围内",
    },
]


class ValidationResult:
    """校验结果"""

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def summary(self) -> str:
        parts = []
        if self.errors:
            parts.append(f"❌ {len(self.errors)} 错误")
        if self.warnings:
            parts.append(f"⚠️ {len(self.warnings)} 警告")
        if self.info:
            parts.append(f"ℹ️ {len(self.info)} 信息")
        if not parts:
            return "✅ 全部通过"
        return " | ".join(parts)


class ConfigValidator:
    """
    配置校验器

    在系统启动时执行，确保所有必需配置存在且合理。
    """

    def __init__(self, config_loader, strict_mode: bool = True):
        """
        Args:
            config_loader: ConfigLoader 实例
            strict_mode: True=阻止启动, False=仅警告
        """
        self.config = config_loader
        self.strict_mode = strict_mode

    def validate_all(self) -> ValidationResult:
        """执行全部校验"""
        result = ValidationResult()

        self._check_required_keys(result)
        self._check_recommended_keys(result)
        self._check_types(result)
        self._check_ranges(result)
        self._check_consistency(result)

        # 日志输出
        level = logging.ERROR if result.errors else logging.WARNING if result.warnings else logging.INFO
        for e in result.errors:
            logger.log(logging.ERROR, "[ConfigValidator] ❌ %s", e)
        for w in result.warnings:
            logger.log(logging.WARNING, "[ConfigValidator] ⚠️ %s", w)
        for i in result.info:
            logger.log(logging.INFO, "[ConfigValidator] ℹ️ %s", i)

        logger.info("[ConfigValidator] 校验结果: %s", result.summary)
        return result

    def _check_required_keys(self, result: ValidationResult):
        """检查必需配置键是否存在"""
        for key, spec in REQUIRED_KEYS.items():
            value = self.config.get(key)
            if value is None:
                error = f"必需配置缺失: {key}"
                if self.strict_mode:
                    result.errors.append(error)
                else:
                    result.warnings.append(error + " (降级模式)")

    def _check_recommended_keys(self, result: ValidationResult):
        """检查推荐配置键"""
        for key in RECOMMENDED_KEYS:
            value = self.config.get(key)
            if value is None:
                result.warnings.append(f"推荐配置缺失: {key}")

    def _check_types(self, result: ValidationResult):
        """类型校验"""
        for key, spec in REQUIRED_KEYS.items():
            value = self.config.get(key)
            if value is None:
                continue

            expected_type = spec.get("type")
            if expected_type and not isinstance(value, expected_type):
                result.errors.append(
                    f"类型错误: {key} = {value!r} (期望 {expected_type})"
                )

    def _check_ranges(self, result: ValidationResult):
        """范围校验"""
        range_checks = {
            "risk.signal_quality.min_confidence": (0.0, 1.0, "置信度"),
            "risk.position.max_per_symbol": (0.01, 1.0, "单股敞口"),
            "risk.position.max_total": (0.01, 2.0, "总敞口"),
            "risk.account.max_daily_drawdown": (0.001, 0.2, "日回撤"),
            "decision.signal_thresholds.min_score": (0, 100, "信号分数"),
        }

        for key, (low, high, label) in range_checks.items():
            value = self.config.get(key)
            if value is None:
                continue
            if not (low <= value <= high):
                result.warnings.append(
                    f"{label} ({key}) = {value} 超出合理范围 [{low}, {high}]"
                )

    def _check_consistency(self, result: ValidationResult):
        """跨键一致性检查"""
        for rule in CONSISTENCY_RULES:
            values = []
            all_present = True
            for key in rule["keys"]:
                v = self.config.get(key)
                if v is None:
                    all_present = False
                    break
                values.append(v)

            if not all_present:
                continue

            try:
                if not rule["check"](*values):
                    result.warnings.append(
                        f"一致性检查失败 [{rule['name']}]: {rule['message'].format(*values)}"
                    )
            except Exception as e:
                result.warnings.append(f"一致性检查异常 [{rule['name']}]: {e}")
