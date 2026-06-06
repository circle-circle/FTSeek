"""
FTSeek-US Trailing Stop 规则 (规则 2)

Task #19 拆分自 position_exit_monitor.py。
ATR 动态止损 + Chandelier 追踪 + 保本触发 (调用 atr_stop_engine 纯函数)。

P0-2 修正 (2026-04-28):
  - profit_atr_signed 为有向：盈利>0，亏损<0；亏损禁止激活 chandelier/breakeven
  - Regime 三档 trail_mult (normal/trending/volatile)
  - cost buffer: 止损线额外让出 k × total_cost_bps
  - 核心定价逻辑下沉到 atr_stop_engine.compute_stop_lines
"""
import logging
from typing import Optional, Dict, Any

from .base import BaseExitRule
from ..atr_stop_engine import compute_stop_lines

logger = logging.getLogger("ftseek.exit_monitor")


def check_trailing_stop(monitor, position, current_price, entry_price, atr,
                        profit_atr_signed, trail_state, now_ts, direction, policy):
    """ATR 动态止损 + Chandelier 追踪 + 保本触发 (从 ExitMonitor._check_trailing_stop 原样迁移)"""
    # P2 debug (2026-05-01): Validate types early
    if not isinstance(current_price, (int, float)):
        raise TypeError(f"current_price is {type(current_price).__name__}: {current_price!r}")

    # 1. 解析 policy dict
    policy_dict = policy.policy if (policy is not None and hasattr(policy, "policy")) else {}

    # 2. ATR 倍数 — SSOT 统一读取 (2026-06-05 修复)
    # 此前 policy.get_atr_multiplier() 读的是 regime 差异化 multiplier (2.5),
    # 而 position_sync 写 initial_sl 用的是 multiplier_by_profit.initial (4.0),
    # 导致 L1/L2 显示 4.0 但 L3 触发用 2.5, SL 比预期紧 37.5%.
    # 修复: 统一读 multiplier_by_profit.initial, 与 position_sync 对齐.
    profit_atr_abs = abs(profit_atr_signed)
    _exit_rules = policy_dict.get("exit_rules", {}) if isinstance(policy_dict, dict) else {}
    _atr_stop_cfg = _exit_rules.get("atr_stop", {})
    _ssot_mult = float(_atr_stop_cfg.get("multiplier_by_profit", {}).get("initial", 4.0))

    if policy is not None and hasattr(policy, "get_atr_multiplier"):
        try:
            atr_mult = float(policy.get_atr_multiplier(position.symbol, direction, profit_atr_abs))
            # 确保不低于 SSOT 配置的 initial multiplier
            atr_mult = max(atr_mult, _ssot_mult)
        except Exception:
            atr_mult = _ssot_mult
    else:
        if profit_atr_signed <= 0:
            # 亏损头寸：禁止追踪止损，交由固定ATR止损管控
            return None
        elif profit_atr_abs < 0.5:
            # 微利(0~0.5 ATR)：不激活追踪，允许正常波动
            return None
        else:
            # P0 修复 (2026-05-29): 连续函数计算ATR倍数，消除离散阶梯跳跃导致SL突变
            # 原离散逻辑: 0.9→1.0 ATR 时 atr_mult 从3.0跳到1.5（40%突变）
            # 新连续逻辑: 分段线性插值，确保平滑过渡
            #   0.5→1.0 ATR: 3.0→2.5 (宽松→适中)
            #   1.0→2.0 ATR: 2.5→2.0 (适中→标准)
            #   2.0→3.0 ATR: 2.0→1.8 (标准→紧凑)
            #   3.0→5.0 ATR: 1.8→1.5 (紧凑→最紧)
            #   ≥5.0 ATR: 固定1.5
            if profit_atr_abs < 1.0:
                t = (profit_atr_abs - 0.5) / 0.5
                atr_mult = 3.0 - 0.5 * t
            elif profit_atr_abs < 2.0:
                t = (profit_atr_abs - 1.0) / 1.0
                atr_mult = 2.5 - 0.5 * t
            elif profit_atr_abs < 3.0:
                t = (profit_atr_abs - 2.0) / 1.0
                atr_mult = 2.0 - 0.2 * t
            elif profit_atr_abs < 5.0:
                t = (profit_atr_abs - 3.0) / 2.0
                atr_mult = 1.8 - 0.3 * t
            else:
                atr_mult = 1.5  # 大盈利最紧凑追踪
            # SSOT 地板: 亏损头寸追踪不得比初始止损更紧
            atr_mult = max(atr_mult, _ssot_mult)

    # 3. peak/trough 更新 (LONG=最高, SHORT=最低)
    if direction == "LONG":
        peak_price = trail_state.get("highest_price", entry_price)
        if current_price > peak_price:
            peak_price = current_price
        trail_state["highest_price"] = peak_price
    else:
        peak_price = trail_state.get("lowest_price", entry_price)
        if current_price < peak_price:
            peak_price = current_price
        trail_state["lowest_price"] = peak_price

    # 4. Regime: trail_state / position 注入, 默认 normal
    # 2026-06-05 修复: regime 值标准化 — DB 存 "RANGE"/"TREND_UP" 等大写枚举,
    # 但 regime_mults 用小写键 ("normal"/"trending"), 导致查找永远命中默认值.
    _raw_regime = ""
    if isinstance(trail_state, dict) and trail_state.get("regime"):
        _raw_regime = str(trail_state["regime"])
    elif hasattr(position, "market_regime") and getattr(position, "market_regime"):
        _raw = getattr(position, "market_regime")
        _raw_regime = _raw.value if hasattr(_raw, "value") else str(_raw)
    elif hasattr(position, "regime") and getattr(position, "regime"):
        _raw_regime = str(getattr(position, "regime"))
    # 标准化映射: 大写枚举 → regime_mults 键
    _REGIME_TO_KEY = {
        "RANGE": "normal", "range": "normal", "NORMAL": "normal", "normal": "normal",
        "TREND_UP": "trending", "TREND_DOWN": "trending", "trending": "trending",
        "HIGH_VOL": "volatile", "volatile": "volatile", "high_volatility": "volatile",
        "range_bound": "normal", "RANGE_BOUND": "normal",
    }
    regime = _REGIME_TO_KEY.get(_raw_regime, "normal")

    # 4.5 Task #55 (2026-05-29): Regime 自适应 atr_mult 调整
    # 在基础 atr_mult (Task #54 修复后的分支逻辑) 之上叠加 regime 乘数；
    # 取 max 以选择更宽松者，避免在 trending 行情中被噪声扫出，同时不破坏小盈利保护。
    try:
        trailing_cfg = policy_dict.get("trailing_stop", {}) if isinstance(policy_dict, dict) else {}
        regime_mults = trailing_cfg.get("regime_multipliers", {
            "normal": 2.5,
            "trending": 3.5,
            "volatile": 3.0,
        })
        regime_mult = float(regime_mults.get(regime, regime_mults.get("normal", 2.5)))
        atr_mult = max(float(atr_mult), regime_mult)
    except Exception as e:
        logger.debug(f"[regime_adjust] {position.symbol} regime={regime} (raw={_raw_regime}) 调整失败, 沿用基础 atr_mult: {e}")

    # 5. cost_bps 估算 (可选, 失败退化为 0)
    cost_bps = 0.0
    cost_model = getattr(monitor, "_cost_model", None) if monitor is not None else None
    if cost_model is not None:
        try:
            if not isinstance(current_price, (int, float)):
                logger.error(f"[_check_trailing_stop] {position.symbol} current_price type={type(current_price).__name__}, value={current_price!r}")
            asset_class = "option" if (len(position.symbol) > 12 and any(c in position.symbol for c in ("P0", "C0"))) else "stock"
            fake_signal = {
                "symbol": position.symbol,
                "asset_class": asset_class,
                "direction": direction,
                "quantity": int(getattr(position, "size", 0) or 0),
                "price": float(current_price),
            }
            market_context = {
                "spread_bps": 3.0,
                "adv_usd": 0.0,
                "session": "RTH",
            }
            cost = cost_model.estimate(fake_signal, market_context)
            if cost is not None and not getattr(cost, "rejected", False):
                cost_bps = float(getattr(cost, "total_bps", 0.0))
        except Exception as e:
            logger.debug(f"[{position.symbol}] cost 估算失败, cost_bps=0: {e}")
            cost_bps = 0.0

    # 6. 调用纯函数引擎
    r = compute_stop_lines(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        atr=atr,
        peak_price=peak_price,
        policy=policy_dict,
        regime=regime,
        atr_mult=atr_mult,
        cost_bps=cost_bps,
    )

    # ── SL 契约 (2026-06-06): initial_sl 是绝对地板, 退出永远不能放宽 ──
    _floor_sl = float(getattr(position, "initial_sl", 0) or 0)
    if _floor_sl > 0:
        if direction == "LONG" and r.final_stop < _floor_sl:
            r.final_stop = _floor_sl
        elif direction == "SHORT" and r.final_stop > _floor_sl:
            r.final_stop = _floor_sl

    # 7. 幻方增强 (2026-04-29): 做空技术破位快速平仓
    if direction == "SHORT" and atr > 0:
        try:
            breach_cfg = policy_dict.get("short_overrides", {}).get("technical_breach", {})
            if not breach_cfg.get("enabled", True):
                logger.debug(f"[技术破位] {position.symbol} SHORT 已禁用, 跳过")
            else:
                breach_atr_mult = float(breach_cfg.get("trigger_atr_mult", 0.3))
                resistance = getattr(position, "trail_state", {}).get("resistance_20d", 0)
                if resistance <= 0:
                    logger.debug(
                        f"[技术破位] {position.symbol} SHORT 无 resistance_20d, 跳过"
                        f"(peak_price={peak_price:.2f} 不能作为阻力位代理)"
                    )
                else:
                    min_loss_pct = float(breach_cfg.get("min_loss_pct", 0.5))
                    pnl_pct_now = (entry_price - current_price) / entry_price
                    if pnl_pct_now > -min_loss_pct / 100.0:
                        logger.debug(
                            f"[技术破位] {position.symbol} SHORT pnl={pnl_pct_now*100:.2f}% "
                            f"> -{min_loss_pct}%, 跳过 (未达亏损门槛)"
                        )
                    else:
                        entry_ts = trail_state.get("entry_time_ts", now_ts)
                        cooldown_minutes = float(breach_cfg.get("entry_cooldown_minutes", 30))
                        held_minutes = (now_ts - entry_ts) / 60.0
                        if held_minutes < cooldown_minutes:
                            logger.debug(
                                f"[技术破位] {position.symbol} SHORT 入场冷却中 "
                                f"({held_minutes:.0f}min < {cooldown_minutes:.0f}min), 跳过"
                            )
                        elif current_price > resistance + atr * breach_atr_mult:
                            r.triggered = True
                            r.reason_code = "TECHNICAL_BREACH"
                            r.reason_cn = "做空技术破位平仓"
                            r.final_stop = resistance
                            logger.warning(
                                f"[技术破位] {position.symbol} SHORT "
                                f"current=${current_price:.2f} > resistance=${resistance:.2f} + "
                                f"{breach_atr_mult}×ATR={atr:.3f} | pnl={pnl_pct_now*100:.2f}%"
                            )
        except Exception as e:
            logger.debug(f"[技术破位] 检查失败 {position.symbol}: {e}")

    # 8. 未触发 → 返回 None
    if not r.triggered:
        return None

    # 9. 触发 → 组装结果
    trail_state["last_trigger_time"] = now_ts
    pnl_pct = (current_price - entry_price) / entry_price if direction == "LONG" \
        else (entry_price - current_price) / entry_price
    return {
        "reason": r.reason_code,
        "reason_cn": r.reason_cn,
        "exit_price": current_price,
        "pnl_pct": pnl_pct,
        "stop_price": r.final_stop,
        "description": (
            f"触及止损 {r.final_stop:.2f} (Entry={entry_price}, Peak={peak_price}, "
            f"ATR={atr:.3f}, atr_mult={r.atr_mult}, trail_mult={r.trail_mult}, "
            f"profit_atr={r.profit_atr_signed:+.2f}, cost_buf={r.cost_buffer_price:.3f})"
        ),
    }


class TrailingStopRule(BaseExitRule):
    """统一跟踪止损 (ATR 动态止损 + Chandelier 追踪 + 保本)"""

    rule_name = "TRAILING_STOP"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_trailing_stop(
            self.monitor,
            position,
            market_data["current_price"],
            market_data["entry_price"],
            market_data.get("effective_atr", market_data.get("atr", 0.0)),
            market_data["profit_atr_signed"],
            trail_state,
            market_data["now_ts"],
            market_data["direction"],
            market_data.get("policy_guardian"),
        )
