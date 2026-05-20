"""
配置一致性审计 — FTSeek-US SSOT v1.0 (2026-05-06)

检测 unified_config.yaml / 环境变量 / 代码默认值之间的漂移。
在 main.py 启动时调用，输出配置一致性报告到日志。

用法:
    from app.core.config_audit import print_config_audit, ConfigAuditError

    # 观察模式 (只打日志)
    print_config_audit(yaml_data, strict=False)

    # 严格模式 (BLOCKING 会 raise)
    print_config_audit(yaml_data, strict=True)
"""
import logging
from typing import Dict, Any, List

logger = logging.getLogger("ftseek_us.config_audit")


# 需要审计的关键配置项: (yaml_key, cfg_attr, description)
# yaml_key 使用 ConfigContainer 的 get() 路径格式 (小写，点分隔)
_AUDIT_ITEMS = [
    # 风控 — 信号质量
    ("risk.signal_quality.min_confidence", "MIN_CONFIDENCE", "最小置信度"),
    ("risk.signal_quality.min_consensus", "MIN_CONSENSUS", "最小共识度"),
    ("risk.signal_quality.min_score", "MIN_SIGNAL_SCORE", "最小信号分数"),
    # 风控 — 交易
    ("risk.trading.max_daily_trades", "MAX_DAILY_TRADES", "每日最大交易数"),
    ("risk.trading.max_order_amount", "MAX_ORDER_AMOUNT", "最大订单金额"),
    ("risk.trading.min_liquidity_ratio", "MIN_LIQUIDITY_RATIO", "最小流动性比率"),
    # 风控 — 仓位
    ("risk.position.max_per_symbol", "MAX_PER_SYMBOL", "单标的最大仓位"),
    ("risk.position.max_per_direction", "MAX_PER_DIRECTION", "单方向最大仓位"),
    ("risk.position.max_total", "MAX_TOTAL_POSITION", "总仓位上限"),
    # 熔断器
    ("risk.circuit_breaker.strategy.consecutive_losses", "CB_CONSECUTIVE_LOSSES", "连续亏损熔断"),
    ("risk.circuit_breaker.strategy.hourly_loss_threshold", "CB_HOURLY_LOSS_THRESHOLD", "小时亏损熔断"),
    ("risk.circuit_breaker.strategy.pause_duration", "CB_PAUSE_DURATION", "熔断暂停时长"),
    # 调度器
    ("scheduler.min_confidence", "SCHEDULER_MIN_CONFIDENCE", "调度器最小置信度"),
    ("scheduler.max_positions", "SCHEDULER_MAX_POSITIONS", "调度器最大持仓"),
    ("scheduler.min_signal_strength", "SCHEDULER_MIN_SIGNAL_STRENGTH", "最小信号强度"),
    ("scheduler.min_consensus_level", "SCHEDULER_MIN_CONSENSUS", "调度器最小共识"),
    ("scheduler.scan_interval_minutes", "SCHEDULER_SCAN_INTERVAL", "扫描间隔(分钟)"),
    # 止损止盈
    ("stop_loss.default_stop_loss_pct", "DEFAULT_STOP_LOSS_PCT", "默认止损比例"),
    ("take_profit.default_take_profit_pct", "DEFAULT_TAKE_PROFIT_PCT", "默认止盈比例"),
    # 信号融合
    ("signal_fusion.us.technical_weight", "FUSION_TECHNICAL_WEIGHT", "技术面权重"),
    ("signal_fusion.us.sentiment_weight", "FUSION_SENTIMENT_WEIGHT", "情绪面权重"),
    ("signal_fusion.us.macro_weight", "FUSION_MACRO_WEIGHT", "宏观面权重"),
    # 决策引擎
    ("decision.signal_thresholds.min_score", "DECISION_MIN_SCORE", "决策最低分数"),
    ("decision.signal_thresholds.medium_score", "DECISION_MEDIUM_SCORE", "决策中等分数"),
    ("decision.signal_thresholds.high_score", "DECISION_HIGH_SCORE", "决策高分分数"),
    ("decision.signal_timing.cooldown_seconds", "SIGNAL_COOLDOWN", "信号冷却时间"),
    ("decision.signal_timing.expire_seconds", "SIGNAL_EXPIRE_SECONDS", "信号过期时间"),
    # 期权
    ("options.stop_loss_pct", "OPTIONS_STOP_LOSS_PCT", "期权止损比例"),
    ("options.take_profit_ratio", "OPTIONS_TAKE_PROFIT_RATIO", "期权止盈比例"),
    ("options.max_holding_hours", "OPTIONS_MAX_HOLDING_HOURS", "期权最大持仓时间"),
    ("options.max_cost_per_contract", "OPTIONS_MAX_COST_PER_CONTRACT", "期权最大成本"),
    ("options.min_dte", "OPTIONS_MIN_DTE", "期权最小DTE"),
    ("options.max_dte", "OPTIONS_MAX_DTE", "期权最大DTE"),
    ("options.force_close_dte", "OPTIONS_FORCE_CLOSE_DTE", "期权强制平仓DTE"),
    ("options.profit_target_pct", "OPTIONS_PROFIT_TARGET_PCT", "期权目标收益"),
    # 执行
    ("execution.slippage.max_ratio", "EXEC_MAX_SLIPPAGE", "最大滑点比例"),
    ("execution.retry.max_attempts", "EXEC_MAX_RETRY", "最大重试次数"),
    ("execution.retry.delay_seconds", "EXEC_RETRY_DELAY", "重试延迟(秒)"),
    ("execution.order_tracking.timeout_seconds", "EXEC_ORDER_TIMEOUT", "订单追踪超时"),
    ("execution.idempotency.window_seconds", "EXEC_IDEMPOTENCY_WINDOW", "幂等窗口(秒)"),
    # 策略预算
    ("strategy_budgets.main_pipeline.max_concurrent_positions", "BUDGET_MAIN_MAX_POSITIONS", "主管线最大并发"),
    ("strategy_budgets.scalping.max_concurrent_positions", "BUDGET_SCALPING_MAX_POSITIONS", "剥头皮最大并发"),
    ("strategy_budgets.system.max_total_daily_loss", "BUDGET_SYSTEM_MAX_DAILY_LOSS", "系统最大日亏损"),
]


def _get_nested(d: Dict[str, Any], key_path: str):
    """从嵌套字典获取值 (已小写化)"""
    keys = key_path.split(".")
    current = d
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def audit_config_consistency(yaml_data: Dict[str, Any]) -> List[str]:
    """
    审计 YAML vs CFG 配置一致性。

    Args:
        yaml_data: 小写化后的 unified_config.yaml 原始数据

    Returns:
        冲突/警告信息列表，空列表表示无问题
    """
    from app.core.config_defaults import CFG

    warnings = []

    for yaml_key, cfg_attr, desc in _AUDIT_ITEMS:
        yaml_value = _get_nested(yaml_data, yaml_key)
        expected_default = getattr(CFG, cfg_attr, None)

        if yaml_value is not None and expected_default is not None and yaml_value != expected_default:
            warnings.append(
                f"🔴 [BLOCKING] CFG.{cfg_attr}({expected_default}) ≠ "
                f"YAML {yaml_key}({yaml_value}) — {desc}必须同步修改!"
            )

    # 检查 YAML 内部 min_confidence 一致性
    _check_yaml_internal_consistency(yaml_data, warnings)

    return warnings


def _check_yaml_internal_consistency(yaml_data: Dict, warnings: List[str]):
    """检查 YAML 内部同一参数在不同位置的值一致性"""
    # min_confidence 出现在多处，应保持一致
    min_conf_locations = {
        "risk.signal_quality.min_confidence": _get_nested(yaml_data, "risk.signal_quality.min_confidence"),
        "scheduler.min_confidence": _get_nested(yaml_data, "scheduler.min_confidence"),
        "signal_fusion.us": None,  # not a direct min_confidence
    }

    # 检查 signal_fusion 内部
    fusion = _get_nested(yaml_data, "signal_fusion.us")
    if isinstance(fusion, dict):
        if "min_confidence" in fusion:
            min_conf_locations["signal_fusion.us.min_confidence"] = fusion["min_confidence"]

    # 检查 decision.risk 内部是否有 min_confidence
    dec_risk = _get_nested(yaml_data, "decision.risk.signal_quality.min_confidence")
    if dec_risk is not None:
        min_conf_locations["decision.risk.signal_quality.min_confidence"] = dec_risk

    # 如果有 scheduler min_confidence 且与 risk 不同，属于正常（不同场景不同阈值）
    # 只检查明显异常：同一节内出现矛盾
    risk_mc = _get_nested(yaml_data, "risk.signal_quality.min_confidence")
    decision_mc = _get_nested(yaml_data, "decision.risk.signal_quality.min_confidence")

    if risk_mc is not None and decision_mc is not None and risk_mc != decision_mc:
        warnings.append(
            f"⚠️ YAML 内部不一致: risk.signal_quality.min_confidence({risk_mc}) ≠ "
            f"decision.risk.signal_quality.min_confidence({decision_mc})"
        )


def print_config_audit(yaml_data: Dict[str, Any], strict: bool = False) -> int:
    """
    打印配置审计报告到日志。

    Args:
        yaml_data: 小写化后的 YAML 原始数据
        strict: True 时 BLOCKING 错误会 raise ConfigAuditError

    Returns:
        BLOCKING 错误数量

    Raises:
        ConfigAuditError: 仅在 strict=True 且发现 BLOCKING 错误时
    """
    warnings = audit_config_consistency(yaml_data)

    if not warnings:
        logger.info("✅ 配置一致性审计通过 — 无漂移检测")
        return 0
    else:
        blocking = [w for w in warnings if "[BLOCKING]" in w]
        non_blocking = [w for w in warnings if "[BLOCKING]" not in w]

        if non_blocking:
            logger.warning(f"⚠️ 配置一致性审计发现 {len(non_blocking)} 个警告")
            for w in non_blocking:
                logger.warning(f"  {w}")

        if blocking:
            logger.critical(f"🚨 配置一致性审计发现 {len(blocking)} 个 BLOCKING 错误!")
            for w in blocking:
                logger.critical(f"  {w}")
            if strict:
                raise ConfigAuditError(
                    f"FTSeek-US 配置一致性审计: {len(blocking)} 个 BLOCKING 错误",
                    blocking
                )
            logger.critical("⛔ [观察模式] BLOCKING 错误已记录但未阻止启动 (使用 strict=True 启用阻止)")

        return len(blocking)


class ConfigAuditError(Exception):
    """配置审计失败异常"""
    def __init__(self, message: str, errors: list):
        super().__init__(message)
        self.errors = errors
