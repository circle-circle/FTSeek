"""
配置一致性审计 (2026-04-19 新增, 2026-04-26 更新 SSOT)

P1 修复: 检测配置漂移 — 当同一配置项在 unified_config.yaml、环境变量、代码默认值中存在不同值时告警。

在 app/main.py 启动时调用，输出配置一致性报告到日志。
"""
import logging
from typing import Dict, Any, List, Tuple

logger = logging.getLogger("ftseek.config_audit")


# 需要审计的跨源配置项映射
# (yaml_key, env_var_name, code_default, description)
# 2026-04-26 更新: key 路径对齐 unified_config.yaml 实际结构
_AUDIT_ITEMS = [
    ("strategy.cooldown.signal_cooldown", "SIGNAL_COOLDOWN", 120, "信号冷却时间"),
    ("strategy.consensus.window_seconds", "CONSENSUS_WINDOW", 90, "共识窗口"),
    ("strategy.consensus.threshold", "CONSENSUS_THRESHOLD", 1.0, "共识阈值"),  # 2026-05-22: 对齐 config_defaults.py (05-19提至1.5导致零触发，已恢复1.0)
    ("trading.max_open_positions", "MAX_OPEN_POSITIONS", 5, "最大持仓数"),
    ("risk_limits.min_confidence", "MIN_CONFIDENCE", 0.45, "最小置信度"),  # 2026-05-19: 提高信号门槛 (原0.35)
    ("risk_limits.max_single_position", "MAX_SINGLE_POSITION", 0.08, "单标的最大仓位"),
    ("risk_limits.max_total_position", "MAX_TOTAL_POSITION", 0.75, "总仓位上限"),
    ("stop_loss.default_percent", "DEFAULT_STOP_LOSS", 0.03, "默认止损比例"),
    ("take_profit.default_percent", "DEFAULT_TAKE_PROFIT", 0.10, "默认止盈比例"),
]


def audit_config_consistency(config_yaml: Dict[str, Any], config_module) -> List[str]:
    """
    审计配置一致性，返回冲突/警告信息列表。
    
    Args:
        config_yaml: unified_config.yaml 加载后的字典
        config_module: app.config 模块对象
        
    Returns:
        冲突/警告信息列表，空列表表示无问题
    """
    warnings = []
    
    for yaml_key, env_name, code_default, desc in _AUDIT_ITEMS:
        # 从 unified_config.yaml 获取值
        yaml_value = _get_nested(config_yaml, yaml_key)
        
        # 从环境变量获取值
        import os
        env_value = os.getenv(env_name)
        env_parsed = _parse_env_value(env_name, env_value) if env_value else None
        
        # 从 config 模块获取值 (fallback)
        config_value = getattr(config_module, env_name, code_default)
        
        # 检查三层是否一致
        issues = []
        if yaml_value is not None and env_parsed is not None and yaml_value != env_parsed:
            issues.append(f"YAML({yaml_value}) ≠ ENV({env_parsed})")
        if env_parsed is not None and env_parsed != config_value:
            issues.append(f"ENV({env_parsed}) ≠ CODE({config_value})")
        if yaml_value is not None and config_value != code_default and yaml_value != config_value:
            # YAML 和实际使用值不同（可能被环境变量覆盖）
            pass  # This is expected if env var overrides
        
        if issues:
            warnings.append(
                f"⚠️ 配置漂移 [{desc}]: {', '.join(issues)} | "
                f"YAML={yaml_value}, ENV={env_parsed}, CODE={config_value}"
            )
    
    # 检查订阅槽位是否超限
    watch_list = config_yaml.get("watchlist", {}).get("hk_stocks", [])
    sub_types = config_yaml.get("futu", {}).get("subscribe_types", ["QUOTE", "K_1M"])
    if isinstance(watch_list, list):
        total_slots = len(watch_list) * len(sub_types)
        if total_slots > 100:
            warnings.append(
                f"🔴 订阅槽位超限: {total_slots} > 100 | "
                f"{len(watch_list)} 股 × {len(sub_types)} 种 = {total_slots}"
            )
    
    # 检查 watchlist 与 HK_LOT_SIZES 一致性
    try:
        from config import WATCH_LIST_HK, HK_LOT_SIZES
        missing_lots = [s for s in WATCH_LIST_HK if s not in HK_LOT_SIZES]
        if missing_lots:
            warnings.append(
                f"⚠️ 缺少整手配置: {len(missing_lots)} 只股票未配置 HK_LOT_SIZES | "
                f"{missing_lots[:5]}{'...' if len(missing_lots) > 5 else ''}"
            )
    except Exception:
        pass  # Module import may fail in test context
    
    # 2026-04-24 新增: 检查 unified_config.yaml 内部 min_confidence 一致性
    # 所有 min_confidence 必须 >= risk_limits.min_confidence，不能低于统一基准
    risk_min_conf = config_yaml.get("risk_limits", {}).get("min_confidence")
    if risk_min_conf is not None:
        # 检查 decision_buffer.min_confidence
        db_min_conf = config_yaml.get("strategy", {}).get("decision_buffer", {}).get("min_confidence")
        if db_min_conf is not None and db_min_conf != risk_min_conf:
            warnings.append(
                f"🔴 [BLOCKING] decision_buffer.min_confidence({db_min_conf}) ≠ "
                f"risk_limits.min_confidence({risk_min_conf}) — 必须一致"
            )
        
        # 检查 confidence_adjuster.min_confidence
        ca_min_conf = config_yaml.get("strategy", {}).get("confidence_adjuster", {}).get("min_confidence")
        if ca_min_conf is not None and ca_min_conf != risk_min_conf:
            warnings.append(
                f"🔴 [BLOCKING] confidence_adjuster.min_confidence({ca_min_conf}) ≠ "
                f"risk_limits.min_confidence({risk_min_conf}) — 必须一致"
            )
        
        # 检查 trading.min_signal_confidence (已废弃)
        ms_conf = config_yaml.get("trading", {}).get("min_signal_confidence")
        if ms_conf is not None:
            warnings.append(
                f"🔴 [BLOCKING] trading.min_signal_confidence({ms_conf}) 已废弃，"
                f"请使用 risk_limits.min_confidence({risk_min_conf})"
            )
    
    # 2026-04-24 新增: 验证 config_defaults.py 与 unified_config.yaml 一致性
    # 这是防止配置漂移的核心机制 — 所有代码 fallback 必须来自 config_defaults.py
    try:
        from app.core.config_defaults import CFG
        defaults_to_check = {
            'risk_limits.min_confidence': CFG.MIN_CONFIDENCE,
            'strategy.decision_buffer.max_pending': CFG.DECISION_BUFFER_MAX_PENDING,
            'strategy.decision_buffer.min_confidence': CFG.DECISION_BUFFER_MIN_CONFIDENCE,
            'strategy.decision_buffer.signal_cooldown': CFG.DECISION_BUFFER_SIGNAL_COOLDOWN,
            'strategy.confidence_adjuster.min_confidence': CFG.CONFIDENCE_ADJUSTER_MIN_CONFIDENCE,
            'stop_loss.default_percent': CFG.DEFAULT_STOP_LOSS_PCT,
            'take_profit.default_percent': CFG.DEFAULT_TAKE_PROFIT_PCT,
            'risk_limits.max_single_position': CFG.MAX_SINGLE_POSITION,
            'risk_limits.max_total_position': CFG.MAX_TOTAL_POSITION,
            'strategy.consensus.window_seconds': CFG.CONSENSUS_WINDOW,
            'strategy.consensus.threshold': CFG.CONSENSUS_THRESHOLD,
        }
        
        for yaml_key, expected_default in defaults_to_check.items():
            yaml_val = _get_nested(config_yaml, yaml_key)
            if yaml_val is not None and yaml_val != expected_default:
                warnings.append(
                    f"🔴 [BLOCKING] config_defaults.{yaml_key}({expected_default}) ≠ "
                    f"unified_config.yaml({yaml_val}) — 必须同步修改!"
                )
    except Exception as e:
        warnings.append(f"⚠️ config_defaults 验证失败: {e}")
    
    return warnings


def _get_nested(d: Dict[str, Any], key_path: str) -> Any:
    """从嵌套字典中获取值，如 'strategy.signal_cooldown.signal_cooldown'"""
    keys = key_path.split(".")
    current = d
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def _parse_env_value(env_name: str, value: str) -> Any:
    """解析环境变量值为正确的类型"""
    if value is None:
        return None
    
    # 数字类型
    if env_name in ("SIGNAL_COOLDOWN", "CONSENSUS_WINDOW", "MAX_OPEN_POSITIONS"):
        return int(value)
    if env_name in ("CONSENSUS_THRESHOLD", "MIN_CONFIDENCE", "MAX_SINGLE_POSITION", 
                     "MAX_TOTAL_POSITION", "DEFAULT_STOP_LOSS", "DEFAULT_TAKE_PROFIT",
                     "CONSENSUS_ADX_TREND", "CONSENSUS_ADX_RANGE"):
        return float(value)
    
    return value


def print_config_audit(config_yaml: Dict[str, Any], config_module, strict: bool = False) -> int:
    """
    打印配置审计报告到日志。

    Args:
        strict: 如果为 True，BLOCKING 错误会 raise ConfigAuditError 而非仅日志。

    Returns:
        BLOCKING 错误数量。

    Raises:
        ConfigAuditError: 仅在 strict=True 且发现 BLOCKING 错误时。
    """
    warnings = audit_config_consistency(config_yaml, config_module)

    if not warnings:
        logger.info("✅ 配置一致性审计通过 — 无漂移检测")
        return 0
    else:
        blocking = [w for w in warnings if "[BLOCKING]" in w]
        if blocking:
            logger.critical(f"🚨 配置一致性审计发现 {len(blocking)} 个 BLOCKING 错误!")
            for w in blocking:
                logger.critical(f"  {w}")
            if strict:
                raise ConfigAuditError(
                    f"配置一致性审计: {len(blocking)} 个 BLOCKING 错误",
                    blocking
                )
            logger.critical("⛔ 系统启动失败! 请先修复 BLOCKING 配置冲突!")
        else:
            logger.warning(f"⚠️ 配置一致性审计发现 {len(warnings)} 个警告")
            for w in warnings:
                logger.warning(f"  {w}")
        return len(blocking)


class ConfigAuditError(Exception):
    """配置审计失败异常"""
    def __init__(self, message: str, errors: list):
        super().__init__(message)
        self.errors = errors
