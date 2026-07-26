"""
做空专项检查模块 (Short-Specific Risk Checks)

幻方 (High-Flyer) 原则:
1. 借券成本门控: >5% 年化利率拒绝，高置信度可豁免
2. 轧空检测: 空头占比 >20% 拒绝
3. 市况适配: 强牛市中做空需要更高置信度门槛

设计原则 (Karpathy):
- 所有阈值来自 ConfigContainer (SSOT), 无硬编码
- 纯函数, 无副作用, 独立可测试
- Fail-open: 数据文件缺失/为空 = 不限制
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

# 数据文件路径 (项目根目录 data/)
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
HTB_FILE = _DATA_DIR / "hard_to_borrow.json"
SHORT_INTEREST_FILE = _DATA_DIR / "short_interest.json"


# ── helpers ────────────────────────────────────────────────────────

_STALE_WARN_DAYS = 7
_stale_warned: set = set()


def _warn_if_stale(path: Path, label: str) -> None:
    """数据文件超期未更新时 WARNING (每进程每文件一次) — fail-open 但不静默"""
    try:
        if not path.exists() or path in _stale_warned:
            return
        import time
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > _STALE_WARN_DAYS:
            _stale_warned.add(path)
            logger.warning(
                f"[ShortChecks] {label} 数据已 {age_days:.0f} 天未更新 (生产者未接线), "
                f"门控 fail-open 放行中 — 数据源裁决见 DEB-20260726-subtraction-audit-round2"
            )
    except OSError:
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    """加载 JSON 数据文件，文件不存在或无效返回空"""
    try:
        if not path.exists():
            return {}
        with open(path) as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"[ShortChecks] 无法读取 {path.name}: {e}")
        return {}


def _load_config_section(dotted_path: str, default: Any = None) -> Any:
    """从 ConfigContainer 读取配置节"""
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(dotted_path, default)
    except Exception:
        return default


# ── 核心门控函数 ───────────────────────────────────────────────────

def check_borrow_cost(symbol: str, config: Dict[str, Any] = None) -> Tuple[bool, str]:
    """
    幻方 Principle 1: 借券成本门控

    Args:
        symbol: 股票代码
        config: borrow_cost_gate 配置节 (可选, 缺省从 ConfigContainer 读)

    Returns:
        (allowed: bool, reason: str)
    """
    if config is None:
        config = _load_config_section(
            "options.short_strategy.stock_short.borrow_cost_gate", {}
        )

    if not config.get("enabled", True):
        return True, ""

    max_rate = float(config.get("max_annual_rate", 0.05))
    action = config.get("hard_to_borrow_action", "fallback_to_put")

    # 从数据文件检查借券成本
    _warn_if_stale(HTB_FILE, "借券成本 hard_to_borrow.json")
    htb_data = _load_json(HTB_FILE)
    symbol_key = symbol.replace("US.", "")

    if symbol_key in htb_data:
        rate = float(htb_data[symbol_key].get("borrow_rate", 0))
        if rate > max_rate:
            msg = f"[借券成本] {symbol} 年化 {rate:.1%} > {max_rate:.1%}, action={action}"
            logger.info(msg)
            return False, msg

    return True, ""


def check_borrow_cost_with_conviction(
    symbol: str, signal_strength: float, config: Dict[str, Any] = None
) -> Tuple[bool, str]:
    """
    借券成本门控 (含高置信度豁免)

    幻方原则: 如果做空置信度足够高 (>= elevated_conviction_threshold),
    可以豁免借券成本限制。
    """
    if config is None:
        config = _load_config_section(
            "options.short_strategy.stock_short.borrow_cost_gate", {}
        )

    threshold = float(config.get("elevated_conviction_threshold", 85))
    allowed, reason = check_borrow_cost(symbol, config)

    if not allowed and signal_strength >= threshold:
        logger.info(
            f"[借券成本豁免] {symbol} strength={signal_strength} >= {threshold}, "
            f"忽略借券成本限制"
        )
        return True, ""

    return allowed, reason


def check_short_squeeze(symbol: str, config: Dict[str, Any] = None) -> Tuple[bool, str]:
    """
    幻方 Principle 2: 轧空风险检测

    Args:
        symbol: 股票代码
        config: squeeze_detector 配置节 (可选)

    Returns:
        (allowed: bool, reason: str)
    """
    if config is None:
        config = _load_config_section(
            "options.short_strategy.stock_short.squeeze_detector", {}
        )

    if not config.get("enabled", True):
        return True, ""

    max_float = float(config.get("max_short_float_pct", 20.0))
    action = config.get("action", "block")

    _warn_if_stale(SHORT_INTEREST_FILE, "空头占比 short_interest.json")
    si_data = _load_json(SHORT_INTEREST_FILE)
    symbol_key = symbol.replace("US.", "")

    if symbol_key in si_data:
        short_float = float(si_data[symbol_key].get("short_float_pct", 0))
        if short_float > max_float:
            msg = (
                f"[轧空风险] {symbol} 空头占比 {short_float:.1f}% > {max_float:.1f}%, "
                f"action={action}"
            )
            logger.warning(msg)
            return False, msg

    return True, ""


def check_regime_for_short(
    market_regime: str, signal_strength: float, config: Dict[str, Any] = None
) -> Tuple[bool, str]:
    """
    幻方 Principle 3: 市况适配

    在强牛市中做空需要更高置信度门槛。
    熊市/震荡市中做空使用正常门槛。

    Args:
        market_regime: 当前市场阶段 (TREND_UP / TREND_DOWN / RANGE / HIGH_VOL)
        signal_strength: 做空信号强度 (对应有效强度, 已做 100 - raw 转换)
        config: 可选参数, 直接传入 elevated_threshold

    Returns:
        (allowed: bool, reason: str)
    """
    regime = (market_regime or "").upper()

    # 非牛市: 不限
    if "TREND_UP" not in regime and "RISK_ON" not in regime:
        return True, ""

    # 牛市: 提升门槛
    if config is None:
        config = _load_config_section(
            "options.short_strategy.stock_short.borrow_cost_gate", {}
        )

    elevated = float(config.get("elevated_conviction_threshold", 85))
    effective = float(signal_strength)

    if effective >= elevated:
        return True, ""

    msg = (
        f"[市况适配] 当前 {market_regime}, "
        f"做空有效强度 {effective:.0f} < {elevated:.0f}, 拒绝反向交易"
    )
    logger.info(msg)
    return False, msg


# ── 数据更新接口 (供定时任务调用) ──────────────────────────────────

def update_hard_to_borrow(data: Dict[str, Any]) -> None:
    """更新借券难易度数据"""
    HTB_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HTB_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"[ShortChecks] 借券数据已更新: {len(data)} 只")


def update_short_interest(data: Dict[str, Any]) -> None:
    """更新空头占比数据"""
    SHORT_INTEREST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SHORT_INTEREST_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"[ShortChecks] 空头占比数据已更新: {len(data)} 只")
