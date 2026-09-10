"""
配置一致性审计 — FTSeek-US SSOT v2.0 (2026-06-16)

检测 unified_config.yaml / 环境变量 / 代码默认值之间的漂移。
在 main.py 启动时调用，strict=True 模式下关键键不一致直接 raise。

用法:
    from app.core.config_audit import print_config_audit, ConfigAuditError

    # 观察模式 (只打日志)
    print_config_audit(yaml_data, strict=False)

    # 严格模式 (BLOCKING 会 raise) — 实盘启动必须使用
    print_config_audit(yaml_data, strict=True)
"""
import logging
from typing import Dict, Any, List, Tuple

logger = logging.getLogger("ftseek_us.config_audit")


# 需要审计的关键配置项: (yaml_key, cfg_attr, description)
# yaml_key 使用 ConfigContainer 的 get() 路径格式 (小写，点分隔)
_AUDIT_ITEMS: List[Tuple[str, str, str]] = [
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
    # (scheduler.min_confidence removed 2026-07-26 — 已废弃, SSOT=risk.signal_quality.min_confidence
    #  + MinConfidenceResolver, yaml 残留键已删, 见 validator.DEPRECATED_FIELDS)
    ("scheduler.max_positions", "SCHEDULER_MAX_POSITIONS", "调度器最大持仓"),
    ("scheduler.min_signal_strength", "SCHEDULER_MIN_SIGNAL_STRENGTH", "最小信号强度"),
    ("scheduler.min_consensus_level", "SCHEDULER_MIN_CONSENSUS", "调度器最小共识"),
    ("scheduler.scan_interval_minutes", "SCHEDULER_SCAN_INTERVAL", "扫描间隔(分钟)"),
    # 信号冷却 — SoT 是 strategy.signal_cooldown (非 decision.signal_timing.cooldown_seconds)
    ("strategy.signal_cooldown", "SIGNAL_COOLDOWN", "信号冷却时间"),
    # 信号过期
    ("decision.signal_timing.expire_seconds", "SIGNAL_EXPIRE_SECONDS", "信号过期时间"),
    # 止损止盈
    # (stop_loss.default_stop_loss_pct / take_profit.default_take_profit_pct removed 2026-07-26 —
    #  cleanup-r3 (70cecbdb) 已从 yaml 删除整段 (无代码消费者, 止损止盈 SSOT=exit_policy.yaml),
    #  审计项同步移除, 否则 strict 审计报 2 个假 BLOCKING)
    # 信号融合
    ("signal_fusion.us.technical_weight", "FUSION_TECHNICAL_WEIGHT", "技术面权重"),
    ("signal_fusion.us.sentiment_weight", "FUSION_SENTIMENT_WEIGHT", "情绪面权重"),
    ("signal_fusion.us.macro_weight", "FUSION_MACRO_WEIGHT", "宏观面权重"),
    # 决策引擎
    ("decision.signal_thresholds.min_score", "DECISION_MIN_SCORE", "决策最低分数"),
    ("decision.signal_thresholds.medium_score", "DECISION_MEDIUM_SCORE", "决策中等分数"),
    ("decision.signal_thresholds.high_score", "DECISION_HIGH_SCORE", "决策高分分数"),
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
    from app.core.config_defaults import CFG, CRITICAL_KEYS

    warnings = []

    for yaml_key, cfg_attr, desc in _AUDIT_ITEMS:
        yaml_value = _get_nested(yaml_data, yaml_key)
        expected_default = getattr(CFG, cfg_attr, None)
        is_critical = cfg_attr in CRITICAL_KEYS

        if yaml_value is None:
            # YAML 中缺少该键
            if is_critical:
                warnings.append(
                    f"🔴 [BLOCKING] YAML 缺少关键键 {yaml_key} "
                    f"(CFG.{cfg_attr}={expected_default}) — {desc}"
                )
            else:
                warnings.append(
                    f"⚠️ [INFO] YAML 缺少键 {yaml_key} "
                    f"(将使用 CFG.{cfg_attr}={expected_default} 兜底) — {desc}"
                )
        elif expected_default is not None and yaml_value != expected_default:
            # YAML 值与代码默认值不一致
            if is_critical:
                warnings.append(
                    f"🔴 [BLOCKING] CFG.{cfg_attr}({expected_default}) ≠ "
                    f"YAML {yaml_key}({yaml_value}) — {desc}必须同步修改!"
                )
            else:
                warnings.append(
                    f"⚠️ [DRIFT] CFG.{cfg_attr}({expected_default}) ≠ "
                    f"YAML {yaml_key}({yaml_value}) — {desc} (非关键，仅记录)"
                )

    # 检查 YAML 内部 min_confidence 一致性
    _check_yaml_internal_consistency(yaml_data, warnings)

    return warnings


def _check_yaml_internal_consistency(yaml_data: Dict, warnings: List[str]):
    """检查 YAML 内部同一参数在不同位置的值一致性"""
    # min_confidence 出现在多处，应保持一致
    risk_mc = _get_nested(yaml_data, "risk.signal_quality.min_confidence")
    decision_mc = _get_nested(yaml_data, "decision.risk.signal_quality.min_confidence")

    if risk_mc is not None and decision_mc is not None and risk_mc != decision_mc:
        warnings.append(
            f"⚠️ YAML 内部不一致: risk.signal_quality.min_confidence({risk_mc}) ≠ "
            f"decision.risk.signal_quality.min_confidence({decision_mc})"
        )

    # signal_cooldown 内部一致性检查
    strategy_cooldown = _get_nested(yaml_data, "strategy.signal_cooldown")
    decision_cooldown = _get_nested(yaml_data, "decision.signal_timing.cooldown_seconds")
    if (strategy_cooldown is not None and decision_cooldown is not None
            and strategy_cooldown != decision_cooldown):
        # 这是已知的设计: strategy.signal_cooldown 是主线 SoT,
        # decision.signal_timing.cooldown_seconds 仅供 LLM 子系统
        logger.info(
            "ℹ️ strategy.signal_cooldown(%s) ≠ decision.signal_timing.cooldown_seconds(%s) "
            "— 已知设计差异 (主线 vs LLM子系统)",
            strategy_cooldown, decision_cooldown
        )


def print_config_audit(yaml_data: Dict[str, Any], strict: bool = False) -> int:
    """
    打印配置审计报告到日志。

    Args:
        yaml_data: 小写化后的 YAML 原始数据
        strict: True 时 BLOCKING 错误会 raise ConfigAuditError (实盘必须使用)

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
            # 2026-09-10 (DEB-20260910 §3 议题三, 用户裁「按照建议执行优化」): WARNING → INFO。
            #   病灶: 这些项**自带** `⚠️ [INFO]` / `⚠️ [DRIFT]` 标签 (见 :125/:137) 却用
            #   logger.warning 输出 ⇒ 消息自称 INFO 而级别是 WARNING, 名实相矛盾; 且紧接着
            #   main.py:490 打 `[ConfigAudit] ✅ SSOT 配置一致性审计通过` (INFO) ⇒ 相邻两行互相
            #   打脸, 每次重启都出现 (昨日 8 次)。实测 7 日本族 120 条 WARNING (占全仓 7.0%),
            #   零阻断价值。
            #   **blocking 项仍走 logger.critical** (下方) ⇒ 阻断语义与 strict=True 的 raise 不变;
            #   本改动只让"非阻断"与"阻断"在级别上可区分 (DEB §3.4 判据 2)。
            #   合成单 record: handler 锁保证原子, 启动期多线程共写一条 stdout 时清单不被穿插。
            logger.info("ℹ️ 配置一致性审计发现 %d 个非阻断项 (INFO/DRIFT — 不阻断启动):\n  %s",
                        len(non_blocking), "\n  ".join(non_blocking))

        if blocking:
            logger.critical(f"🚨 配置一致性审计发现 {len(blocking)} 个 BLOCKING 错误!")
            for w in blocking:
                logger.critical(f"  {w}")
            if strict:
                raise ConfigAuditError(
                    f"FTSeek-US 配置一致性审计: {len(blocking)} 个 BLOCKING 错误 — "
                    f"关键配置不一致, 实盘启动已阻止",
                    blocking
                )
            logger.critical("⛔ [观察模式] BLOCKING 错误已记录但未阻止启动 (使用 strict=True 启用阻止)")

        return len(blocking)


class ConfigAuditError(Exception):
    """配置审计失败异常 — 关键配置不一致，实盘启动被阻止"""
    def __init__(self, message: str, errors: list):
        super().__init__(message)
        self.errors = errors
