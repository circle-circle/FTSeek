"""ATR 止损定价引擎（纯函数，可单测）

职责
----
- 基于 exit_policy + cost_model 计算多候选止损线
- 修正 P0-2 数学期望错误：profit_atr 有向（盈利>0 / 亏损<0）
  亏损时禁止激活 Chandelier / Breakeven（原实现用 abs() 会把亏损也视作盈利，
  误触发追踪线上移，加速亏损）
- 接入 Regime 三档差异化 trail multiplier（normal / trending / volatile）
- 接入成本缓冲（cost buffer）：止损线额外让出 k × total_cost_bps 的价格空间，
  规避被噪声成本打穿

ATR 来源说明 (Task #4 统一ATR计算)
----------------------------------------
本模块为纯函数式止损计算器，本身不独立计算 ATR：atr 严格以参数形式传入。
调用方（position_exit_monitor / exit_rules/* / risk pipeline 等）需从统一的
ATRProvider（app.core.atr_provider）获取 ATR 后传入本模块，以保证与其他
ATR 消费方（TrendFirst / ExitRuleDispatcher 等）同根同源、数据一致。
限 fallback：调用方如拿不到 ATR，可以 entry_price * 0.02 作为保底估计。

模块严格无状态（无 I/O、无持久化），便于单元测试与回测重放。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("ftseek.atr_stop_engine")


# ============================================================
#  Regime 标准化
# ============================================================

# 外部可能传入的 regime 字符串 → 三档标准键
# 2026-06-05 修复: "range"/"RANGE" 映射到 "normal" (非 "volatile"),
# 与 trailing_stop.py 和 exit_policy_guardian 对齐.
# range-bound = 横盘震荡 = normal stops; volatile = 高波动 = wider stops.
_REGIME_MAP = {
    "normal": "normal", "range": "normal", "ranging": "normal",
    "range_bound": "normal", "RANGE": "normal", "RANGE_BOUND": "normal",
    "volatile": "volatile", "high_vol": "volatile", "chop": "volatile",
    "HIGH_VOL": "volatile", "high_volatility": "volatile",
    "trend": "trending", "trending": "trending",
    "uptrend": "trending", "downtrend": "trending",
    "bull": "trending", "bear": "trending",
    "TREND_UP": "trending", "TREND_DOWN": "trending",
}


def _normalize_regime(regime: Optional[str]) -> str:
    if not regime:
        return "normal"
    return _REGIME_MAP.get(str(regime).strip().lower(), "normal")


# ============================================================
#  结果数据结构
# ============================================================

@dataclass
class StopLinesResult:
    direction: str                  # LONG / SHORT
    entry_price: float
    current_price: float
    atr: float
    profit_atr_signed: float        # 有向：盈利>0，亏损<0
    atr_mult: float
    trail_mult: float
    hard_stop: float
    atr_stop: float
    chandelier_stop: float          # 0 表示未激活
    breakeven_stop: float           # 0 表示未激活
    scale_out_floor: float          # 2026-06-20: scale_out ratchet 下限 (0=未激活)
    cost_buffer_price: float        # 绝对价格空间
    final_stop: float
    triggered: bool
    reason_code: str                # HARD_STOP / ATR_STOP / CHANDELIER / BREAKEVEN / SCALE_OUT_FLOOR / NONE
    reason_cn: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "direction": self.direction,
            "profit_atr_signed": round(self.profit_atr_signed, 3),
            "atr_mult": round(self.atr_mult, 2),
            "trail_mult": round(self.trail_mult, 2),
            "hard_stop": round(self.hard_stop, 4),
            "atr_stop": round(self.atr_stop, 4),
            "chandelier_stop": round(self.chandelier_stop, 4),
            "breakeven_stop": round(self.breakeven_stop, 4),
            "scale_out_floor": round(self.scale_out_floor, 4),
            "cost_buffer_price": round(self.cost_buffer_price, 4),
            "final_stop": round(self.final_stop, 4),
            "triggered": self.triggered,
            "reason_code": self.reason_code,
            "reason_cn": self.reason_cn,
        }


# ============================================================
#  纯函数 API
# ============================================================

def compute_profit_atr_signed(
    direction: str,
    entry_price: float,
    current_price: float,
    atr: float,
) -> float:
    """计算有向 ATR 盈利数：盈利>0，亏损<0，输入无效返回 0.0"""
    # P0 修复 (2026-05-01): 类型防御 — 防止 dict/str 传入导致 TypeError
    if not isinstance(current_price, (int, float)):
        return 0.0
    if not isinstance(entry_price, (int, float)):
        return 0.0
    if atr <= 0 or entry_price <= 0 or current_price <= 0:
        return 0.0
    diff = (current_price - entry_price) if direction == "LONG" else (entry_price - current_price)
    return diff / atr


def resolve_trail_multiplier(
    policy: Dict[str, Any],
    regime: Optional[str] = "normal",
    direction: str = "LONG",
) -> float:
    """解析 Chandelier trail 倍数：regime 差异化 + 做空更紧"""
    rules = (policy or {}).get("exit_rules", {})
    by_regime = rules.get("chandelier_trail", {}).get("multiplier_by_regime", {})
    key = _normalize_regime(regime)
    
    # P0 修复 (2026-05-01): 防御性检查 — by_regime[key] 可能返回 dict 而非 float
    raw_trail = by_regime.get(key, 2.0)
    if isinstance(raw_trail, dict):
        # 嵌套字典结构，尝试获取默认值
        raw_trail = raw_trail.get("value", raw_trail.get(key, 2.0))
    try:
        trail = float(raw_trail)
    except (TypeError, ValueError):
        trail = 2.0
    
    if direction == "SHORT":
        so = (policy or {}).get("short_overrides", {}) or {}
        # short_overrides.trail_multiplier 可能是标量或 regime 字典
        raw_tm = so.get("trail_multiplier", trail)
        if isinstance(raw_tm, dict):
            short_tm = raw_tm.get(key, trail)
            # raw_tm[key] itself could also be a dict (nested), fallback to trail
            if isinstance(short_tm, dict):
                short_tm = trail
        else:
            short_tm = raw_tm
        # Ensure short_tm is a valid number before float()
        try:
            short_tm = float(short_tm)
        except (TypeError, ValueError):
            short_tm = trail
        trail = min(trail, short_tm)
    return trail


def apply_atr_constraints(
    *,
    atr_raw: float,
    price: float,
    policy: Dict[str, Any],
    symbol: Optional[str] = None,
    median_atr_14d: Optional[float] = None,
    earnings_guard: Any = None,
) -> float:
    """对原始 ATR 施加 floor/cap 双重约束 (Phase 3, 2026-05-31)

    Floor (下限):
        atr ≥ price × atr_floor_price_pct/100
        防止 ATR 过小导致止损距离形同虚设

    Cap (上限 / 财报保护):
        如 symbol 临近财报期, atr ≤ median_atr_14d × atr_cap_multiplier
        防止财报跳空导致 ATR 飙升 5-10x 让止损线被锁在极远处

    Args:
        atr_raw: 原始 ATR
        price: 当前价格 (用于计算下限)
        policy: exit_policy.yaml 加载后的 dict
        symbol: 标的 (财报保护需要)
        median_atr_14d: 14 日 ATR 中位数 (用于财报上限)
        earnings_guard: EarningsGuard 实例 (None 表示不启用财报保护)

    Returns:
        约束后的 ATR
    """
    if atr_raw is None or atr_raw <= 0:
        return atr_raw or 0.0

    atr = float(atr_raw)
    pt_cfg = (policy or {}).get("progressive_trailing", {}) or {}

    # Floor 1: 价格百分比下限
    try:
        floor_pct = float(pt_cfg.get("atr_floor_price_pct", 0.0))
    except (TypeError, ValueError):
        floor_pct = 0.0
    if floor_pct > 0 and price and price > 0:
        floor_value = price * floor_pct / 100.0
        if atr < floor_value:
            logger.debug(
                "[ATR floor] %s atr_raw=%.4f -> floor=%.4f (price=%.2f x %.2f%%)",
                symbol or "?", atr, floor_value, price, floor_pct,
            )
            atr = floor_value

    # Cap: 财报期上限
    if earnings_guard is not None and symbol and median_atr_14d and median_atr_14d > 0:
        try:
            cap = earnings_guard.get_atr_cap(symbol, float(median_atr_14d))
        except Exception as e:
            logger.debug("[ATR cap] earnings_guard.get_atr_cap(%s) error: %s", symbol, e)
            cap = None
        if cap is not None and cap > 0 and atr > cap:
            logger.info(
                "[ATR cap] %s atr=%.4f capped to %.4f (earnings window)",
                symbol, atr, cap,
            )
            atr = cap

    return atr


def compute_stop_lines(
    *,
    direction: str,
    entry_price: float,
    current_price: float,
    atr: float,
    peak_price: float,
    policy: Dict[str, Any],
    regime: Optional[str] = "normal",
    atr_mult: float = 2.5,
    cost_bps: float = 0.0,
    scale_out_floor: float = 0.0,
) -> StopLinesResult:
    """核心 API：计算全部候选止损线 + 最终止损线 + 触发判定

    参数
    ----
    direction     : "LONG" | "SHORT"
    entry_price   : 开仓均价
    current_price : 当前价
    atr           : 已通过 volatility floor 处理后的 ATR（底限后的有效值）
    peak_price    : LONG=持仓期间最高价，SHORT=持仓期间最低价
    policy        : exit_policy dict（exit_policy.yaml 解析结果）
    regime        : 当前市况标签（normal/trending/volatile 或其别名）
    atr_mult      : ATR 动态止损倍数（由 guardian.get_atr_multiplier 给出）
    cost_bps      : 预期双边总成本（bp），用于 cost buffer
    scale_out_floor: 2026-06-20 scale_out ratchet 下限 (绝对价). >0 时作为候选止损线,
                    利用 max(LONG)/min(SHORT) "最保护优先" 聚合收紧剩余仓位止损.
                    来源: trail_state["scale_out_stop_floor"], 由 scale_out.py 写入.

    关键修正
    --------
    - profit_atr 有向：亏损时 < 0，则不激活 Chandelier / Breakeven
    - Regime 三档 trail_mult
    - cost buffer：final_stop = stop_line ∓ (current_price × cost_bps/1e4 × k)
    """
    rules = (policy or {}).get("exit_rules", {})
    atr_stop_cfg = rules.get("atr_stop", {})
    ch_cfg = rules.get("chandelier_trail", {})
    be_cfg = rules.get("breakeven", {})
    cb_cfg = rules.get("cost_buffer", {})

    # === 1. 有向 profit_atr（核心修正点） ===
    profit_atr_signed = compute_profit_atr_signed(direction, entry_price, current_price, atr)

    # === 2. 硬止损 ===
    if direction == "SHORT":
        max_loss_pct = float((policy or {}).get("short_overrides", {}).get("max_loss_pct", 10.0)) / 100.0
        hard_stop = entry_price * (1 + max_loss_pct)
    else:
        max_loss_pct = float(atr_stop_cfg.get("max_loss_pct", 15.0)) / 100.0
        hard_stop = entry_price * (1 - max_loss_pct)

    # === 3. ATR 动态止损（非线性倍数由上游 guardian 决定） ===
    if atr > 0:
        atr_stop = entry_price - atr * atr_mult if direction == "LONG" else entry_price + atr * atr_mult
    else:
        atr_stop = hard_stop

    # === 4. Chandelier：仅在"盈利"≥ activation 时激活 ===
    # P0 修复 (2026-05-29): 激活门槛从 2.0 降至 1.0 ATR (渐进式追踪止损)
    # Phase 3 (2026-05-31): 收紧 0.5~2.0 ATR 区间追踪止损
    chandelier_stop = 0.0
    trail_mult = resolve_trail_multiplier(policy, regime, direction)
    activation = float(ch_cfg.get("activation_profit_atr", 0.8))  # Fix 10: was 1.0, matches YAML 0.8
    pt_cfg = (policy or {}).get("progressive_trailing", {}) or {}
    tier2_mult = float(pt_cfg.get("tier2_compact_multiplier", 0.8))
    tier3_mult = float(pt_cfg.get("tier3_compact_multiplier", 1.2))
    # fail-safe (2026-06-20): 缺省=禁用。原 get("enabled", True) 会在 yaml key 被删时
    # 静默重新启用已证明亏损的 chandelier(-$34)/breakeven。显式 enabled:true 才启用。
    if ch_cfg.get("enabled", False) and atr > 0 and profit_atr_signed >= activation:
        if direction == "LONG":
            if profit_atr_signed < 1.0:
                _effective_mult = tier2_mult
            elif profit_atr_signed < 2.0:
                _effective_mult = tier3_mult
            else:
                _effective_mult = trail_mult
            chandelier_stop = peak_price - atr * _effective_mult
        else:
            if profit_atr_signed < 1.0:
                _effective_mult = tier2_mult
            elif profit_atr_signed < 2.0:
                _effective_mult = tier3_mult
            else:
                _effective_mult = trail_mult
            chandelier_stop = peak_price + atr * _effective_mult

    # === 5. Breakeven：仅在"盈利"≥ trigger 时激活 ===
    # P0 修复 (2026-05-29): 激活门槛从 1.5 降至 0.5 ATR (保本触发提前)
    # Y4 (2026-05-15): bug 修复 — SHORT break-even 应在 entry 之上 (价格反弹回到 entry 附近触发保利)
    # 旧 bug: SHORT 用 entry × (1 - buffer), 导致 stop_line 低于 entry, 入场即满足触发条件
    # 真凶: GOOGL SHORT entry $335.99 → breakeven_stop=$335.32 → 入场 0.5h 后异常 +$6K 损失
    breakeven_stop = 0.0
    be_trigger = float(be_cfg.get("trigger_profit_atr", 0.4))  # Fix 11: was 0.5, matches YAML 0.4
    buffer_pct = float(be_cfg.get("buffer_pct", 0.15)) / 100.0  # Fix 11: was 0.2, matches YAML 0.15
    # fail-safe (2026-06-20): 缺省=禁用，同 chandelier。显式 enabled:true 才启用。
    if be_cfg.get("enabled", False) and profit_atr_signed >= be_trigger:
        if direction == "LONG":
            # LONG: 价格回到 entry 附近 (略高于 entry) 触发保利
            breakeven_stop = entry_price * (1 + buffer_pct)
        else:
            # SHORT: 价格反弹到 entry 附近 (略高于 entry) 触发保利
            # 修复前是 entry × (1 - buffer) = entry 之下, 错误!
            breakeven_stop = entry_price * (1 + buffer_pct)

    # === 6. 聚合候选止损线 ===
    # 2026-06-20: scale_out_floor (ratchet 下限) 作为候选, 利用 max(LONG)/min(SHORT)
    # "最保护优先" 聚合. 使 scale_out 部分平仓后剩余仓位止损从 entry±4×ATR 收紧到 ratchet 水位.
    _so_floor = float(scale_out_floor or 0.0)
    if direction == "LONG":
        candidates = [hard_stop, atr_stop]
        if chandelier_stop > 0:
            candidates.append(chandelier_stop)
        if breakeven_stop > 0:
            candidates.append(breakeven_stop)
        if _so_floor > 0:
            candidates.append(_so_floor)
        stop_line = max(candidates)
    else:
        candidates = [hard_stop, atr_stop]
        if chandelier_stop > 0:
            candidates.append(chandelier_stop)
        if breakeven_stop > 0:
            candidates.append(breakeven_stop)
        if _so_floor > 0:
            candidates.append(_so_floor)
        stop_line = min(candidates)

    # === 7. 成本缓冲：让出 k × cost_bps ===
    k = float(cb_cfg.get("k", 1.5)) if cb_cfg else 1.5
    cost_buffer_price = current_price * (max(cost_bps, 0.0) / 10000.0) * k
    if direction == "LONG":
        final_stop = stop_line - cost_buffer_price
    else:
        final_stop = stop_line + cost_buffer_price

    # === 8. 归因：由哪条止损线"触顶"（未计成本缓冲） ===
    if stop_line == hard_stop:
        reason_code, reason_cn = "HARD_STOP", f"硬止损({max_loss_pct*100:.0f}%)"
    elif _so_floor > 0 and stop_line == _so_floor:
        reason_code, reason_cn = "SCALE_OUT_FLOOR", f"scale_out ratchet下限({_so_floor:.4f})"
    elif stop_line == atr_stop:
        reason_code, reason_cn = "ATR_STOP", f"ATR动态止损({atr_mult:.1f}x)"
    elif chandelier_stop > 0 and stop_line == chandelier_stop:
        reason_code, reason_cn = "CHANDELIER", f"Chandelier追踪({trail_mult:.1f}x,regime={_normalize_regime(regime)})"
    elif breakeven_stop > 0 and stop_line == breakeven_stop:
        reason_code, reason_cn = "BREAKEVEN", f"保本止损(buffer={buffer_pct*100:.2f}%)"
    else:
        reason_code, reason_cn = "ATR_STOP", "ATR动态止损"

    # === 9. 触发判定（用含成本缓冲的 final_stop） ===
    triggered = (current_price <= final_stop) if direction == "LONG" else (current_price >= final_stop)

    return StopLinesResult(
        direction=direction,
        entry_price=entry_price,
        current_price=current_price,
        atr=atr,
        profit_atr_signed=profit_atr_signed,
        atr_mult=atr_mult,
        trail_mult=trail_mult,
        hard_stop=hard_stop,
        atr_stop=atr_stop,
        chandelier_stop=chandelier_stop,
        breakeven_stop=breakeven_stop,
        scale_out_floor=_so_floor,
        cost_buffer_price=cost_buffer_price,
        final_stop=final_stop,
        triggered=triggered,
        reason_code=reason_code if triggered else "NONE",
        reason_cn=reason_cn if triggered else "未触发",
    )


# ============================================================
#  self-test
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    policy = {
        "exit_rules": {
            "atr_stop": {"enabled": True, "max_loss_pct": 15.0},
            "chandelier_trail": {
                "enabled": True,
                "activation_profit_atr": 0.8,
                "multiplier_by_regime": {"normal": 2.0, "trending": 3.0, "volatile": 1.5},
            },
            "breakeven": {"enabled": True, "trigger_profit_atr": 0.6, "buffer_pct": 0.15},
            "cost_buffer": {"k": 1.5},
        },
        "short_overrides": {"max_loss_pct": 10.0, "trail_multiplier": 1.5},
    }

    # case1 LONG 轻微盈利 +0.5 ATR —— chandelier/breakeven 均不应激活
    r1 = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=101.0, atr=2.0, peak_price=101.5,
        policy=policy, regime="normal", atr_mult=2.5, cost_bps=3.0,
    )
    print("[case1 LONG +0.5ATR]", r1.to_dict())
    assert r1.chandelier_stop == 0, "+0.5 ATR 不应激活 chandelier"
    assert r1.breakeven_stop == 0, "+0.5 ATR 不应激活 breakeven"

    # case2 LONG 亏损 -2 ATR —— 关键修正：profit_atr_signed<0，chandelier/breakeven 禁止激活
    r2 = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=96.0, atr=2.0, peak_price=100.0,
        policy=policy, regime="normal", atr_mult=2.5, cost_bps=3.0,
    )
    print("[case2 LONG -2ATR]", r2.to_dict())
    assert r2.profit_atr_signed < 0, "亏损时 profit_atr_signed 必须 < 0"
    assert r2.chandelier_stop == 0, "亏损时 chandelier 绝不能激活"
    assert r2.breakeven_stop == 0, "亏损时 breakeven 绝不能激活"

    # case3 LONG +3 ATR + trending —— trail_mult 应为 3.0
    r3 = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=106.0, atr=2.0, peak_price=107.0,
        policy=policy, regime="trending", atr_mult=2.0, cost_bps=3.0,
    )
    print("[case3 LONG +3ATR trending]", r3.to_dict())
    assert abs(r3.trail_mult - 3.0) < 1e-6, "trending 应使用 3.0x"
    assert r3.chandelier_stop > 0 and r3.breakeven_stop > 0

    # case4 LONG +3 ATR + volatile —— trail_mult 应为 1.5
    r4 = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=106.0, atr=2.0, peak_price=107.0,
        policy=policy, regime="volatile", atr_mult=2.0, cost_bps=3.0,
    )
    print("[case4 LONG +3ATR volatile]", r4.to_dict())
    assert abs(r4.trail_mult - 1.5) < 1e-6, "volatile 应使用 1.5x"

    # case5 SHORT +3 ATR —— 做空止损更紧
    r5 = compute_stop_lines(
        direction="SHORT", entry_price=100.0, current_price=94.0, atr=2.0, peak_price=93.0,
        policy=policy, regime="normal", atr_mult=2.0, cost_bps=15.0,
    )
    print("[case5 SHORT +3ATR]", r5.to_dict())
    assert r5.chandelier_stop > 0, "SHORT +3 ATR 应激活 chandelier"
    assert r5.cost_buffer_price > 0, "做空借券高成本应有 buffer"

    # case6 cost buffer 有效性
    r6_low = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=101.0, atr=2.0, peak_price=101.0,
        policy=policy, regime="normal", atr_mult=2.5, cost_bps=3.0,
    )
    r6_high = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=101.0, atr=2.0, peak_price=101.0,
        policy=policy, regime="normal", atr_mult=2.5, cost_bps=50.0,
    )
    print(f"[case6 cost_bps=3  buffer={r6_low.cost_buffer_price:.4f}]")
    print(f"[case6 cost_bps=50 buffer={r6_high.cost_buffer_price:.4f}]")
    assert r6_high.cost_buffer_price > r6_low.cost_buffer_price, "高成本应扩大 buffer"

    # case7 不同 regime 下 final_stop 的差异
    r7_trending = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=110.0, atr=2.0, peak_price=112.0,
        policy=policy, regime="trending", atr_mult=1.5, cost_bps=3.0,
    )
    r7_volatile = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=110.0, atr=2.0, peak_price=112.0,
        policy=policy, regime="volatile", atr_mult=1.5, cost_bps=3.0,
    )
    print(f"[case7 trending final={r7_trending.final_stop:.4f} reason={r7_trending.reason_code or 'live'}]")
    print(f"[case7 volatile final={r7_volatile.final_stop:.4f} reason={r7_volatile.reason_code or 'live'}]")
    assert r7_trending.final_stop < r7_volatile.final_stop, "trending 止损线应更松（更低）"

    # case8 scale_out_floor (ratchet 下限) — 2026-06-20 接通测试
    # LONG: 无 floor 时 final_stop=entry-4ATR(92); floor=entry(100) 时收紧到保本
    r8_nofloor = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=99.0, atr=2.0, peak_price=100.0,
        policy=policy, regime="normal", atr_mult=4.0, cost_bps=0.0, scale_out_floor=0.0,
    )
    r8_floor = compute_stop_lines(
        direction="LONG", entry_price=100.0, current_price=99.0, atr=2.0, peak_price=100.0,
        policy=policy, regime="normal", atr_mult=4.0, cost_bps=0.0, scale_out_floor=100.0,
    )
    print(f"[case8 LONG no_floor final={r8_nofloor.final_stop:.4f}]")
    print(f"[case8 LONG floor=100 final={r8_floor.final_stop:.4f} reason={r8_floor.reason_code}]")
    assert r8_floor.final_stop > r8_nofloor.final_stop, "scale_out_floor 应收紧 LONG 止损 (抬高)"
    assert r8_floor.final_stop >= 100.0, "floor=entry 时 LONG final_stop 应≥entry (保本)"
    assert r8_floor.reason_code == "SCALE_OUT_FLOOR", "归因应为 SCALE_OUT_FLOOR"

    # case9 SHORT scale_out_floor — 收紧 (降低止损线)
    r9_nofloor = compute_stop_lines(
        direction="SHORT", entry_price=100.0, current_price=101.0, atr=2.0, peak_price=100.0,
        policy=policy, regime="normal", atr_mult=4.0, cost_bps=0.0, scale_out_floor=0.0,
    )
    r9_floor = compute_stop_lines(
        direction="SHORT", entry_price=100.0, current_price=101.0, atr=2.0, peak_price=100.0,
        policy=policy, regime="normal", atr_mult=4.0, cost_bps=0.0, scale_out_floor=98.0,
    )
    print(f"[case9 SHORT no_floor final={r9_nofloor.final_stop:.4f}]")
    print(f"[case9 SHORT floor=98  final={r9_floor.final_stop:.4f} reason={r9_floor.reason_code}]")
    assert r9_floor.final_stop < r9_nofloor.final_stop, "scale_out_floor 应收紧 SHORT 止损 (降低)"
    assert r9_floor.final_stop <= 98.0, "floor=entry-1ATR 时 SHORT final_stop 应≤floor"

    print("\natr_stop_engine self-test 全部通过")
