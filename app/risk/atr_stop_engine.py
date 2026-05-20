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
_REGIME_MAP = {
    "normal": "normal", "range": "volatile", "ranging": "volatile",
    "volatile": "volatile", "high_vol": "volatile", "chop": "volatile",
    "trend": "trending", "trending": "trending",
    "uptrend": "trending", "downtrend": "trending",
    "bull": "trending", "bear": "trending",
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
    cost_buffer_price: float        # 绝对价格空间
    final_stop: float
    triggered: bool
    reason_code: str                # HARD_STOP / ATR_STOP / CHANDELIER / BREAKEVEN / NONE
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
    chandelier_stop = 0.0
    trail_mult = resolve_trail_multiplier(policy, regime, direction)
    activation = float(ch_cfg.get("activation_profit_atr", 2.0))
    if ch_cfg.get("enabled", True) and atr > 0 and profit_atr_signed >= activation:
        if direction == "LONG":
            chandelier_stop = peak_price - atr * trail_mult
        else:
            chandelier_stop = peak_price + atr * trail_mult

    # === 5. Breakeven：仅在"盈利"≥ trigger 时激活 ===
    # Y4 (2026-05-15): bug 修复 — SHORT break-even 应在 entry 之上 (价格反弹回到 entry 附近触发保利)
    # 旧 bug: SHORT 用 entry × (1 - buffer), 导致 stop_line 低于 entry, 入场即满足触发条件
    # 真凶: GOOGL SHORT entry $335.99 → breakeven_stop=$335.32 → 入场 0.5h 后异常 +$6K 损失
    breakeven_stop = 0.0
    be_trigger = float(be_cfg.get("trigger_profit_atr", 1.5))
    buffer_pct = float(be_cfg.get("buffer_pct", 0.2)) / 100.0
    if be_cfg.get("enabled", True) and profit_atr_signed >= be_trigger:
        if direction == "LONG":
            # LONG: 价格回到 entry 附近 (略高于 entry) 触发保利
            breakeven_stop = entry_price * (1 + buffer_pct)
        else:
            # SHORT: 价格反弹到 entry 附近 (略高于 entry) 触发保利
            # 修复前是 entry × (1 - buffer) = entry 之下, 错误!
            breakeven_stop = entry_price * (1 + buffer_pct)

    # === 6. 聚合候选止损线 ===
    if direction == "LONG":
        candidates = [hard_stop, atr_stop]
        if chandelier_stop > 0:
            candidates.append(chandelier_stop)
        if breakeven_stop > 0:
            candidates.append(breakeven_stop)
        stop_line = max(candidates)
    else:
        candidates = [hard_stop, atr_stop]
        if chandelier_stop > 0:
            candidates.append(chandelier_stop)
        if breakeven_stop > 0:
            candidates.append(breakeven_stop)
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
                "activation_profit_atr": 2.0,
                "multiplier_by_regime": {"normal": 2.0, "trending": 3.0, "volatile": 1.5},
            },
            "breakeven": {"enabled": True, "trigger_profit_atr": 1.5, "buffer_pct": 0.2},
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

    print("\natr_stop_engine self-test 全部通过")
