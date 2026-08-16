"""
Strategy Registry — 策略属性统一读取器

从 unified_config.yaml 的 strategy_registry 段读取策略定义。
所有需要查 strategy_id 属性的代码都通过这里，不硬编码。

核心接口:
  get_strategy_config(strategy_id) → dict  # 获取单个策略属性
  get_disabled_strategy_ids() → list        # 获取所有disabled策略
  get_all_strategy_ids() → list             # 获取所有注册策略

设计原则:
  1. 纯读取器，不持有状态，5分钟缓存
  2. 未注册的策略返回安全默认值（enabled=True, 默认门槛）
  3. 不依赖任何 app.* 模块（避免循环导入）
"""
import time
import logging
from typing import Dict, List, Any

logger = logging.getLogger("ftseek_us.strategy_registry")

_registry_cache: Dict[str, Dict] = None  # type: ignore
_registry_cache_ts: float = 0.0
_CACHE_TTL = 300  # 5分钟

# 安全默认值 — 未注册的策略用这些
_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "config_name": None,  # None = 用 strategy_id 本身
    "direction_filter": {
        "BUY_min_strength": 50,
        "BUY_max_std": 20,  # 2026-06-30: 15→20, 允许风险审查官有不同意见
        # [DEB-fullchain #16 2026-08-16] 移除 SHORT_min_confidence:65 缺省 —
        #   pipeline 消费方 .get(SHORT_min_confidence, yaml supply_conf_gate_short=55),
        #   本缺省恒遮蔽 yaml → 实际 BUY 55 / SHORT 65 对空头更严 (VOTE 通道 8/9 后无行,
        #   当前零流量影响)。移除后 SSOT=playbook.supply_conf_gate_short, 多空对称。
    },
    "ic_gate": {
        "hard_block_threshold": -0.15,
        "warning_threshold": 0.05,
        "min_signals": 30,
    },
    "regime_filter": False,
}


def get_strategy_config(strategy_id: str) -> Dict[str, Any]:
    """获取策略配置。未注册的策略返回默认值（安全降级）。

    Args:
        strategy_id: 信号快照中的 strategy_id 字段值

    Returns:
        {
            "enabled": bool,
            "config_name": str | None,
            "direction_filter": {BUY_min_strength, BUY_max_std, ...entry overrides},
            "ic_gate": {hard_block_threshold, warning_threshold, min_signals},
            "regime_filter": bool,
        }
    """
    _ensure_loaded()
    entry = (_registry_cache or {}).get(strategy_id, {})
    return {
        "enabled": entry.get("enabled", _DEFAULTS["enabled"]),
        "config_name": entry.get("config_name", strategy_id),
        "direction_filter": {**_DEFAULTS["direction_filter"], **entry.get("direction_filter", {})},
        "ic_gate": {**_DEFAULTS["ic_gate"], **entry.get("ic_gate", {})},
        "regime_filter": entry.get("regime_filter", _DEFAULTS["regime_filter"]),
    }


def get_disabled_strategy_ids() -> List[str]:
    """返回所有 enabled=false 的 strategy_id 列表。

    用于 IC tracker 排除已禁用策略的历史信号。
    """
    _ensure_loaded()
    if not _registry_cache:
        return []
    return [
        sid for sid, entry in _registry_cache.items()
        if not entry.get("enabled", True)
    ]


def get_all_strategy_ids() -> List[str]:
    """返回所有注册的 strategy_id 列表。"""
    _ensure_loaded()
    return list((_registry_cache or {}).keys())


def _ensure_loaded():
    """从 config 加载（带缓存）。"""
    global _registry_cache, _registry_cache_ts
    if _registry_cache is not None and (time.time() - _registry_cache_ts) < _CACHE_TTL:
        return
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        raw = cfg.get_dotted("strategy_registry", {}) or {}
        if isinstance(raw, dict):
            _registry_cache = raw
        else:
            _registry_cache = {}
    except Exception as e:
        logger.warning(f"[StrategyRegistry] 加载失败，使用空注册表: {e}")
        _registry_cache = {}
    _registry_cache_ts = time.time()
    if _registry_cache:
        logger.debug(
            f"[StrategyRegistry] 已加载 {len(_registry_cache)} 个策略: "
            f"disabled={get_disabled_strategy_ids()}"
        )


def reload():
    """强制重新加载（用于热重载测试）。"""
    global _registry_cache, _registry_cache_ts
    _registry_cache = {}  # type: ignore
    _registry_cache_ts = 0.0
    _ensure_loaded()
