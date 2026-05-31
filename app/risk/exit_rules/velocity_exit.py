"""速度捕捉退出规则 (Velocity Exit)

Phase 3 (2026-05-31): 检测快速有利价格变动，在回撤前锁定部分利润

触发条件:
  最近 N 根 K 线的价格变动 > K × σ (标准差)
  且方向对持仓有利

动作:
  平仓 velocity_close_pct% (默认 30%)
  收紧 trailing stop 到 velocity_trail_mult × ATR

冷却: velocity_cooldown_seconds (默认 60s)
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


def check_velocity_exit(
    position: Any,
    market_data: Dict[str, Any],
    trail_state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """检测速度飙升并触发获利了结

    Args:
        position: 持仓 dict 或对象
        market_data: 行情 dict, 需含 current_price, price_history (list of recent prices)
        trail_state: 追踪状态 (用于冷却)
        config: velocity_exit 配置

    Returns:
        触发: dict {reason, exit_price, close_pct, description, metadata}
        未触发: None
    """
    cfg = config or {}
    if not cfg.get("enabled", True):
        return None

    # ── 冷却检查 ──
    if isinstance(trail_state, dict):
        now_ts = datetime.now().timestamp()
        last_ts = float(trail_state.get("_last_velocity_exit_ts", 0) or 0)
        cooldown = int(cfg.get("cooldown_seconds", 60))
        if now_ts - last_ts < cooldown:
            return None

    # ── 关键字段 ──
    current_price = float(market_data.get("current_price", 0) or 0)
    if current_price <= 0:
        return None

    # Get direction
    if isinstance(position, dict):
        direction = str(position.get("direction", "LONG")).upper()
        entry_price = float(
            position.get("entry_price", 0)
            or position.get("avg_entry_price", 0)
            or 0
        )
        symbol = position.get("symbol", "?")
    else:
        d = getattr(position, "direction", None)
        direction = (d.value if hasattr(d, "value") else str(d or "LONG")).upper()
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        symbol = getattr(position, "symbol", "?")

    if entry_price <= 0:
        return None

    # ── 价格历史 (从 market_data 获取) ──
    price_history = market_data.get("price_history", [])
    if not isinstance(price_history, list) or len(price_history) < 5:
        # Try to use kline data if available
        klines = market_data.get("klines", [])
        if isinstance(klines, list) and len(klines) >= 5:
            price_history = [
                float(k.get("close", k.get("price", 0)) or 0)
                for k in klines[-20:]
            ]
        else:
            return None  # no price history available

    # Filter out zeros
    prices = [float(p) for p in price_history if float(p) > 0]
    if len(prices) < 5:
        return None

    # ── 计算速度指标 ──
    lookback = int(cfg.get("lookback_bars", 3))
    sigma_window = int(cfg.get("sigma_window", 20))
    sigma_mult = float(cfg.get("sigma_multiplier", 2.0))

    # Recent price change (last N bars)
    if len(prices) < lookback + 1:
        return None
    recent_change = abs(prices[-1] - prices[-1 - lookback])

    # Calculate σ from price changes
    changes = []
    for i in range(1, min(len(prices), sigma_window + 1)):
        if i + 1 <= len(prices):
            changes.append(abs(prices[-i] - prices[-i - 1]))
    changes = [c for c in changes if c > 0]
    if len(changes) < 3:
        return None

    mean_change = sum(changes) / len(changes)
    variance = sum((c - mean_change) ** 2 for c in changes) / len(changes)
    sigma = variance ** 0.5

    if sigma <= 0:
        return None

    # Check if velocity exceeds threshold
    velocity_ratio = recent_change / sigma if sigma > 0 else 0

    if velocity_ratio < sigma_mult:
        return None  # not fast enough

    # Check if movement is FAVORABLE (not adverse)
    price_move = prices[-1] - prices[-1 - lookback]
    is_favorable = False
    if direction == "LONG" and price_move > 0:
        is_favorable = True
    elif direction in ("SHORT", "SELL") and price_move < 0:
        is_favorable = True

    if not is_favorable:
        return None  # fast move but adverse direction — not a velocity exit

    # ── 触发! ──
    close_pct = float(cfg.get("close_pct", 30))
    atr = float(market_data.get("atr", 0) or 0)

    # Set cooldown
    if isinstance(trail_state, dict):
        trail_state["_last_velocity_exit_ts"] = datetime.now().timestamp()
        # Tighten trailing stop
        trail_state["velocity_trail_active"] = True
        trail_state["velocity_trail_mult"] = float(cfg.get("trail_mult", 1.0))
        trail_state["velocity_trail_expire_ts"] = (
            datetime.now().timestamp() + 300
        )  # 5min

    if direction == "LONG":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    logger.info(
        "[velocity_exit] %s %s 速度飙升触发! "
        "velocity=%.1fσ (>%sσ), "
        "profit=%.2f%%, close %.0f%%",
        symbol, direction, velocity_ratio, sigma_mult, profit_pct, close_pct,
    )

    return {
        "reason": "VELOCITY_EXIT",
        "reason_cn": (
            f"速度捕捉退出 ({velocity_ratio:.1f}σ飙升, "
            f"利润{profit_pct:.1f}%, 平仓{close_pct:.0f}%)"
        ),
        "exit_price": current_price,
        "close_pct": close_pct,
        "close_ratio": close_pct / 100.0,
        "description": (
            f"价格 {lookback} 分钟内变动 {velocity_ratio:.1f}σ, "
            f"抢在回撤前落袋"
        ),
        "metadata": {
            "velocity_ratio": round(velocity_ratio, 2),
            "sigma": round(sigma, 4),
            "recent_change": round(recent_change, 4),
            "profit_pct": round(profit_pct, 2),
            "atr": atr,
            "trail_mult": float(cfg.get("trail_mult", 1.0)),
        },
    }
