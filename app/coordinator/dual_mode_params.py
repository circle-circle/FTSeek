"""
双链路并行模式参数访问工具 (Task #3 2026-06-15)

根据 ModeSwitchService 当前的 auto_aggression level (conservative/normal/aggressive)
返回对应级别的参数值, 供 order_executor / signal_pipeline 等模块按需读取。

参数定义来源 (优先级从高到低):
    1. config/unified_config.yaml `dual_mode.<level>` (运行时可热重载)
    2. 本模块内置 fallback 字典 (yaml 缺失或读取异常时使用)

同时提供:
    - get_today_auto_trade_count(): 当日自动交易计数 (内存计数 + 跨日自动重置)
    - increment_today_auto_trade_count(): 提交订单成功后递增
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Fallback 参数 (yaml 不可用时兜底) ───────────────────────────────
_CONSERVATIVE: Dict[str, Any] = {
    "position_scale_factor": 0.5,
    "max_position_pct": 0.06,
    "max_total_exposure": 0.30,
    "min_signal_strength": 70,
    "min_confidence": 0.70,
    "max_per_trade_usd": 10000,
    "max_daily_auto_trades": 3,
    "stop_loss_tighten_factor": 0.7,
}

_NORMAL: Dict[str, Any] = {
    "position_scale_factor": 1.0,
    "max_position_pct": 0.12,
    "max_total_exposure": 0.60,
    "min_signal_strength": 50,
    "min_confidence": 0.55,
    "max_per_trade_usd": 20000,
    "max_daily_auto_trades": 4,
    "stop_loss_tighten_factor": 1.0,
}

_AGGRESSIVE: Dict[str, Any] = {
    "position_scale_factor": 1.5,
    "max_position_pct": 0.20,
    "max_total_exposure": 0.75,
    "min_signal_strength": 40,
    "min_confidence": 0.45,
    "max_per_trade_usd": 30000,
    "max_daily_auto_trades": 5,
    "stop_loss_tighten_factor": 1.2,
}

_FALLBACK_LEVEL_PARAMS: Dict[str, Dict[str, Any]] = {
    "conservative": _CONSERVATIVE,
    "normal": _NORMAL,
    "aggressive": _AGGRESSIVE,
}


def _get_current_level() -> str:
    """获取当前自动交易激进等级, 异常时降级为 conservative (最安全)."""
    try:
        from app.web_dashboard.mode_switch_service import get_mode_switch_service
        level = get_mode_switch_service().get_auto_aggression()
        if level in _FALLBACK_LEVEL_PARAMS:
            return level
        logger.debug(f"[dual_mode] 未知 level={level}, 降级为 conservative")
    except Exception as e:
        logger.debug(f"[dual_mode] 获取 level 异常 (降级为 conservative): {e}")
    return "conservative"


def _get_yaml_params(level: str) -> Optional[Dict[str, Any]]:
    """从 unified_config.yaml dual_mode.<level> 读取参数 (失败返回 None)."""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        params = cfg.get_dotted(f"dual_mode.{level}", None)
        if isinstance(params, dict):
            return params
    except Exception as e:
        logger.debug(f"[dual_mode] yaml 读取 dual_mode.{level} 失败: {e}")
    return None


def get_conservative_param(key: str) -> Any:
    """根据当前自动交易激进程度获取对应参数值.

    Args:
        key: 参数键, 如 "position_scale_factor"/"min_signal_strength" 等

    Returns:
        参数值, 找不到则返回 None
    """
    level = _get_current_level()

    # 优先 yaml, 缺失则 fallback
    yaml_params = _get_yaml_params(level)
    if yaml_params and key in yaml_params:
        return yaml_params[key]

    return _FALLBACK_LEVEL_PARAMS.get(level, _CONSERVATIVE).get(key)


def get_current_aggression_level() -> str:
    """便捷接口: 返回当前等级字符串 (保留供日志使用)."""
    return _get_current_level()


# ── 当日自动交易计数器 ───────────────────────────────────────────────
_counter_lock = threading.Lock()
_counter_state: Dict[str, Any] = {
    "date": None,        # date 对象, 跨日自动重置
    "count": 0,
}


def _maybe_reset_counter() -> None:
    today = date.today()
    if _counter_state["date"] != today:
        _counter_state["date"] = today
        _counter_state["count"] = 0


def get_today_auto_trade_count() -> int:
    """返回今日已执行的自动交易笔数 (内存计数, 进程重启后归零)."""
    with _counter_lock:
        _maybe_reset_counter()
        return int(_counter_state["count"])


def increment_today_auto_trade_count(n: int = 1) -> int:
    """递增计数, 返回递增后的新值."""
    with _counter_lock:
        _maybe_reset_counter()
        _counter_state["count"] = int(_counter_state["count"]) + int(n)
        return int(_counter_state["count"])


def reset_today_auto_trade_count() -> None:
    """重置计数 (供测试/Dashboard 手动调用)."""
    with _counter_lock:
        _counter_state["date"] = date.today()
        _counter_state["count"] = 0
