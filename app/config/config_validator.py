#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置一致性验证器 (Config Validator) - 2026-04-15 新增

核心原则：config.yaml 是唯一权威数据源
1. 启动时自动检测所有参数冲突
2. 输出冲突详情和修复建议
3. 可选择是否阻止启动 (strict_mode)

使用方式：
  from app.config.config_validator import ConfigValidator
  validator = ConfigValidator(strict=True)
  validator.validate()  # 失败时抛异常
"""

import logging
import yaml
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("ftseek.config_validator")

# ============================================================
#  冲突等级
# ============================================================
class Severity:
    CRITICAL = "🔴 CRITICAL"   # 阻止启动
    WARNING  = "🟡 WARNING"    # 警告但不阻止
    INFO     = "🟢 INFO"       # 信息提示


# ============================================================
#  冲突定义
# ============================================================
@dataclass
class ConflictRule:
    """参数冲突规则"""
    name: str                                    # 参数名称
    yaml_key: str                                # config.yaml 中的键
    code_default: Optional[float] = None         # 代码中的硬编码默认值
    code_path: str = ""                          # 代码文件路径 (用于日志)
    aliases: Dict[str, str] = field(default_factory=dict)  # YAML 内部别名映射
    tolerance: float = 0.001                     # 浮点容差


# ============================================================
#  配置验证器
# ============================================================
class ConfigValidator:
    """
    配置一致性验证器

    检查项：
    1. config.yaml 内部重复键冲突 (trading vs risk_limits)
    2. config.yaml 与代码硬编码默认值冲突
    3. 参数值合理性检查 (范围、类型)
    """

    # ===== 冲突规则定义 =====
    # 所有可能冲突的参数都在这里定义，新增参数只需加一行
    CONFLICT_RULES: List[ConflictRule] = [
        # ── 止盈止损 (最高优先级) ──
        ConflictRule("止损百分比", "stop_loss.default_percent",
                     code_default=0.06, code_path="app/risk/dynamic_exit_manager.py",
                     aliases={"stop_loss_pct": "config_container.py"}),
        ConflictRule("止盈百分比", "take_profit.default_percent",
                     code_default=0.12, code_path="app/risk/dynamic_exit_manager.py",
                     aliases={"take_profit_pct": "config_container.py"}),

        # ── 仓位控制 ──
        ConflictRule("最大开仓数", "trading.max_open_positions",
                     code_default=4, code_path="app/core/config_container.py"),
        ConflictRule("单股最大仓位", "risk_limits.max_single_position",
                     code_default=0.20, code_path="app/risk/risk_manager.py",
                     aliases={"trading.max_single_position": "config.yaml 重复定义"}),
        ConflictRule("总仓位上限", "risk_limits.max_total_position",
                     code_default=0.80, code_path="app/risk/risk_manager.py",
                     aliases={"trading.max_total_position": "config.yaml 重复定义"}),

        # ── 交易频率 ──
        ConflictRule("最大日交易次数", "risk_limits.max_daily_trades",
                     code_default=10, code_path="app/risk/risk_manager.py",
                     aliases={"trading.max_daily_trades": "config.yaml 重复定义"}),
        ConflictRule("最大周亏损", "risk_limits.max_weekly_loss",
                     code_default=0.10, code_path="app/risk/risk_manager.py",
                     aliases={"trading.max_weekly_loss": "config.yaml 重复定义"}),

        # ── 风险控制 ──
        ConflictRule("最大回撤", "risk_limits.max_drawdown",
                     code_default=0.15, code_path="app/risk/risk_manager.py"),
        ConflictRule("最大日亏损", "risk_limits.max_daily_loss",
                     code_default=0.05, code_path="app/risk/risk_manager.py",
                     aliases={"trading.max_daily_loss": "config.yaml 重复定义"}),
        ConflictRule("连续亏损限制", "risk_limits.consecutive_loss_limit",
                     code_default=5, code_path="app/risk/risk_manager.py"),
        ConflictRule("策略最低胜率", "risk_limits.strategy_min_winrate",
                     code_default=0.40, code_path="app/risk/risk_manager.py"),
        ConflictRule("最大单笔亏损", "risk_limits.max_single_loss",
                     code_default=0.10, code_path="app/risk/risk_manager.py"),
        ConflictRule("最小置信度", "risk_limits.min_confidence",
                     code_default=0.35, code_path="app/risk/risk_manager.py"),

        # ── 共识引擎 ──
        ConflictRule("共识窗口", "strategy.consensus.window_seconds",
                     code_default=60, code_path="app/core/config_container.py"),
        ConflictRule("共识阈值", "strategy.consensus.threshold",
                     code_default=0.6, code_path="app/core/config_container.py"),

        # ── 决策缓冲 ──
        ConflictRule("决策缓冲冷却", "strategy.decision_buffer.signal_cooldown",
                     code_default=50, code_path="app/decision_buffer.py"),
        ConflictRule("决策缓冲队列", "strategy.decision_buffer.max_pending",
                     code_default=500, code_path="app/decision_buffer.py"),

        # ── 信号冷却 ──
        ConflictRule("信号冷却", "signal_cooldown.default_seconds",
                     code_default=120, code_path="app/core/config_container.py"),
    ]

    def __init__(self, strict: bool = True, config_path: str = None):
        """
        Args:
            strict: True=检测到CRITICAL冲突时抛异常阻止启动
            config_path: config.yaml 路径
        """
        self.strict = strict
        self.config_path = config_path or str(
            Path(__file__).resolve().parent.parent.parent / "config" / "config.yaml"
        )
        self.conflicts: List[Dict] = []
        self.warnings: List[Dict] = []
        self._yaml_cache: Optional[Dict] = None

    def validate(self) -> bool:
        """
        执行所有验证

        Returns:
            True=所有检查通过, False=有冲突
        """
        self.conflicts.clear()
        self.warnings.clear()

        # 1. 加载配置
        config = self._load_config()
        if not config:
            return False

        # 2. 检查 YAML 内部重复定义冲突
        self._check_yaml_internal_conflicts(config)

        # 3. 检查 YAML vs 代码硬编码冲突
        self._check_yaml_vs_code_conflicts(config)

        # 4. 参数合理性检查
        self._check_parameter_reasonableness(config)

        # 5. 输出结果
        self._print_report()

        # 6. 严格模式：有 CRITICAL 冲突时抛异常
        critical_count = sum(1 for c in self.conflicts if c.get("severity") == Severity.CRITICAL)
        if critical_count > 0 and self.strict:
            raise ValueError(
                f"配置验证失败：{critical_count} 个严重冲突，请修复后重启。\n"
                f"详细报告见上文日志。"
            )

        return len(self.conflicts) == 0

    def _load_config(self) -> Optional[Dict]:
        """加载 config.yaml"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            self._yaml_cache = config
            logger.info(f"✅ 配置加载成功：{self.config_path}")
            return config
        except Exception as e:
            logger.error(f"❌ 配置加载失败：{e}")
            return None

    def _get_nested(self, data: Dict, key: str, default=None):
        """从嵌套字典中获取值，支持 'a.b.c' 格式"""
        keys = key.split('.')
        current = data
        for k in keys:
            if isinstance(current, dict):
                current = current.get(k, default)
            else:
                return default
        return current

    def _check_yaml_internal_conflicts(self, config: Dict):
        """检查 YAML 内部重复定义冲突"""
        for rule in self.CONFLICT_RULES:
            if not rule.aliases:
                continue

            primary = self._get_nested(config, rule.yaml_key)
            if primary is None:
                continue

            for alias_key, alias_desc in rule.aliases.items():
                alias = self._get_nested(config, alias_key)
                if alias is None:
                    continue

                if self._values_conflict(primary, alias):
                    self.conflicts.append({
                        "severity": Severity.CRITICAL,
                        "name": rule.name,
                        "primary_key": rule.yaml_key,
                        "primary_value": primary,
                        "alias_key": alias_key,
                        "alias_value": alias,
                        "message": (
                            f"YAML 内部冲突 [{rule.name}]: "
                            f"{rule.yaml_key}={primary} vs {alias_key}={alias} ({alias_desc})"
                        ),
                        "suggestion": f"删除 {alias_key}，统一使用 {rule.yaml_key} 作为唯一源"
                    })

    def _check_yaml_vs_code_conflicts(self, config: Dict):
        """检查 YAML 与代码硬编码默认值的冲突"""
        for rule in self.CONFLICT_RULES:
            if rule.code_default is None:
                continue

            yaml_val = self._get_nested(config, rule.yaml_key)
            if yaml_val is None:
                self.warnings.append({
                    "severity": Severity.WARNING,
                    "name": rule.name,
                    "message": f"配置缺失：{rule.yaml_key} 未在 config.yaml 中定义，使用代码默认值 {rule.code_default}"
                })
                continue

            if self._values_conflict(yaml_val, rule.code_default):
                # 区分严重程度
                ratio = abs(yaml_val - rule.code_default) / max(abs(rule.code_default), 0.001)
                if ratio > 0.5:  # 差异 > 50%
                    severity = Severity.CRITICAL
                elif ratio > 0.1:  # 差异 > 10%
                    severity = Severity.WARNING
                else:
                    severity = Severity.INFO

                self.conflicts.append({
                    "severity": severity,
                    "name": rule.name,
                    "yaml_key": rule.yaml_key,
                    "yaml_value": yaml_val,
                    "code_default": rule.code_default,
                    "code_path": rule.code_path,
                    "message": (
                        f"YAML vs 代码冲突 [{rule.name}]: "
                        f"config.yaml={yaml_val} vs {rule.code_path} 默认值={rule.code_default} "
                        f"(差异 {ratio:.0%})"
                    ),
                    "suggestion": (
                        f"确保 {rule.code_path} 从 config_center 读取配置，"
                        f"而非使用硬编码默认值"
                    )
                })

    def _check_parameter_reasonableness(self, config: Dict):
        """参数合理性检查"""
        reasonableness_checks = [
            ("止损不应>5%", "stop_loss.default_percent", lambda v: abs(v) <= 0.05,
             "止损超过5%过大，建议 ≤5%"),
            ("止盈不应<5%", "take_profit.default_percent", lambda v: v >= 0.05,
             "止盈低于5%过小，建议 ≥5%"),
            ("仓位不应>30%", "risk_limits.max_single_position", lambda v: v <= 0.30,
             "单股仓位超过30%过于集中"),
            ("日亏损不应>5%", "risk_limits.max_daily_loss", lambda v: v <= 0.05,
             "日亏损超过5%过大"),
            ("总回撤不应>20%", "risk_limits.max_drawdown", lambda v: v <= 0.20,
             "总回撤超过20%过大"),
            ("置信度应在0-1之间", "risk_limits.min_confidence", lambda v: 0 <= v <= 1,
             "置信度应在0-1之间"),
        ]

        for name, key, check_fn, suggestion in reasonableness_checks:
            val = self._get_nested(config, key)
            if val is not None and not check_fn(val):
                self.conflicts.append({
                    "severity": Severity.WARNING,
                    "name": name,
                    "key": key,
                    "value": val,
                    "message": f"参数不合理 [{name}]: {key}={val}",
                    "suggestion": suggestion
                })

    def _values_conflict(self, a, b, tolerance: float = None) -> bool:
        """判断两个值是否冲突"""
        tol = tolerance if tolerance is not None else 0.001
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(a - b) > tol
        return a != b

    def _print_report(self):
        """打印验证报告"""
        critical = [c for c in self.conflicts if c.get("severity") == Severity.CRITICAL]
        warning = [c for c in self.conflicts if c.get("severity") == Severity.WARNING]
        info = [c for c in self.conflicts if c.get("severity") == Severity.INFO]

        if not critical and not warning and not info and not self.warnings:
            logger.info("✅ 配置验证通过：所有参数一致且合理")
            return

        logger.warning("=" * 70)
        logger.warning("📋 配置验证报告")
        logger.warning("=" * 70)

        for item in critical:
            logger.error(f"🔴 {item['message']}")
            logger.error(f"   💡 {item.get('suggestion', '')}")

        for item in warning:
            logger.warning(f"🟡 {item.get('message', item)}")
            if isinstance(item, dict) and 'suggestion' in item:
                logger.warning(f"   💡 {item['suggestion']}")

        for item in info:
            logger.info(f"🟢 {item.get('message', item)}")

        for item in self.warnings:
            logger.warning(f"⚠️ {item.get('message', item)}")

        logger.warning("-" * 70)
        logger.warning(f"总计: {len(critical)} CRITICAL, {len(warning)} WARNING, "
                      f"{len(info)} INFO, {len(self.warnings)} 其他")
        logger.warning("=" * 70)


# ============================================================
#  全局单例
# ============================================================
_validator: Optional[ConfigValidator] = None


def get_config_validator(strict: bool = True) -> ConfigValidator:
    """获取配置验证器单例"""
    global _validator
    if _validator is None:
        _validator = ConfigValidator(strict=strict)
    return _validator


def validate_config(strict: bool = True) -> bool:
    """便捷函数：执行配置验证"""
    try:
        validator = get_config_validator(strict=strict)
        return validator.validate()
    except ValueError as e:
        logger.error(f"配置验证失败: {e}")
        return False
