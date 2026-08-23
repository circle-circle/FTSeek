"""
做空专项检查模块 (Short-Specific Risk Checks)

幻方 (High-Flyer) 原则:
3. 市况适配: 强牛市中做空需要更高置信度门槛

历史注记 (DEB-20260823-system-health-check, 用户裁"不值得, 删除"):
- 原 Principle 1 借券成本门控 / Principle 2 轧空检测 依赖 data/hard_to_borrow.json
  与 data/short_interest.json, 两文件自 2026-04-29 起为空 {} 且无生产者接线,
  两门恒放行 = 名义存在实际死亡 ("带死门上实盘"禁止项). 经用户裁决删除;
  数据源接线与否随毕业门禁 checklist 再议 (届时连同本模块一起重评).
- check_regime_for_short 不依赖数据文件、持续有效, 保留.

设计原则 (Karpathy):
- 所有阈值来自 ConfigContainer (SSOT), 无硬编码
- 纯函数, 无副作用, 独立可测试
"""

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


def _load_config_section(dotted_path: str, default: Any = None) -> Any:
    """从 ConfigContainer 读取配置节"""
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(dotted_path, default)
    except Exception:
        return default


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
