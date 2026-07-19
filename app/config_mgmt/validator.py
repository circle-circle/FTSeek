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
# 注意: 路径必须与 unified_config.yaml 实际结构匹配
RECOMMENDED_KEYS = [
    "data_sources.finnhub.api_key",
    "data_sources.twelvedata.api_key",
    "news_v2.enabled",
    "playbook.risk_parameters.atr_multiplier",  # was exit.us.atr_multiplier
    "strategy.consensus_threshold",  # was strategy.consensus.threshold
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
    {
        # S2 (2026-07-19, DEB-round2 Ω.3-5): 资金基准双 key 防漂移.
        #   capital.fixed_amount (熔断/水位兜底) 与 playbook.risk_parameters.risk_base_capital
        #   (仓位/日额度基数) 当前均 82K, 但是两个独立配置项 — 运维改一个忘改另一个会静默分叉,
        #   导致水位偏松+仓位偏保守 (风险敞口被低估). 启动校验不一致即 warning (显式可观测, 优于隐式派生).
        "name": "资金基准一致性 (S2)",
        "keys": ["capital.fixed_amount", "playbook.risk_parameters.risk_base_capital"],
        "check": lambda v1, v2: abs(float(v1) - float(v2)) < 1.0,
        "message": "资金基准漂移: capital.fixed_amount ({}) != risk_base_capital ({}), 熔断/水位与仓位口径矛盾",
    },
]

# ============================================================================
# 废弃配置字段 (Task #7 2026-05-31)
# ============================================================================
# 启动时若发现这些 key 仍存在于 unified_config.yaml, 输出 WARNING 但不阻断启动
# 格式: "<dotted.path>": "<实际生效路径说明>"
DEPRECATED_FIELDS: Dict[str, str] = {
    "llm_models": (
        "已移至 llm.models / llm.features (本文件) 与 "
        "app/llm/multi_model_client.py:DEFAULT_MODEL_FOR (别名兜底)"
    ),
    "strategies.defaults.signal_cooldown_seconds": (
        "已改为 strategy.signal_cooldown (本文件 SoT)"
    ),
    "scheduler.min_confidence": (
        "已改为 risk.signal_quality.min_confidence (SSOT) + "
        "app/risk/min_confidence_resolver.py:MinConfidenceResolver"
    ),
}


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
        self._check_deprecated_fields(result)

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

    # ------------------------------------------------------------------
    # Task #7 (2026-05-31): 废弃配置字段检测
    # ------------------------------------------------------------------

    def _check_deprecated_fields(self, result: ValidationResult) -> None:
        """检测并警告已废弃但仍出现在配置中的字段

        仅输出 WARNING (不阻断启动); 静默某个字段可在 DEPRECATED_FIELDS 中移除。
        """
        for field_path, replacement in DEPRECATED_FIELDS.items():
            try:
                value = self.config.get(field_path)
            except Exception:
                value = None
            if value is not None:
                result.warnings.append(
                    f"[Config·DEPRECATED] '{field_path}' 仍存在于配置中但已不生效, "
                    f"请改用: {replacement}"
                )


def check_deprecated_fields(config) -> List[str]:
    """独立函数入口 — 供 main.py / scripts 直接调用

    Args:
        config: ConfigLoader 实例 或 dict

    Returns:
        告警信息列表（同时已通过 logger 输出 WARNING）
    """
    warnings: List[str] = []

    def _get(cfg, dotted: str):
        # ConfigLoader 优先 (有 .get(dotted) 接口)
        if not isinstance(cfg, dict) and hasattr(cfg, "get"):
            try:
                return cfg.get(dotted)
            except Exception:
                pass
        # dict 递归
        cur: Any = cfg
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    for field_path, replacement in DEPRECATED_FIELDS.items():
        try:
            value = _get(config, field_path)
        except Exception:
            value = None
        if value is not None:
            msg = (
                f"[Config·DEPRECATED] '{field_path}' 仍存在于配置中但已不生效, "
                f"请改用: {replacement}"
            )
            warnings.append(msg)
            logger.warning(msg)

    if not warnings:
        logger.info("[Config·DEPRECATED] 未检测到废弃字段 (配置已清理)")
    return warnings
