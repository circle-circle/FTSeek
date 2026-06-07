"""
FTSeek-US Position Sizer — 纯函数仓位计算模块

设计原则:
  1. 纯函数: 零副作用，无 DB/网络依赖，可单测，可回测
  2. 低耦合: 只依赖输入参数，不依赖全局状态
  3. 资金自适应: 根据 equity 自动匹配 tier 参数
  4. 安全护栏: 滑点/集中度/上下限/整手对齐

作者: FTSeek Team
日期: 2026-05-02
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("ftseek.position_sizer")


# ============================================================
#  数据结构
# ============================================================

@dataclass
class SizingResult:
    """仓位计算结果"""
    shares: int                     # 最终股数
    risk_amount: float              # 风险金额 ($)
    position_value: float           # 仓位市值 ($)
    position_pct: float             # 仓位占净值比例
    tier: str                       # 资金分层 (micro/small/medium/large/inst)
    water_level: str               # 水位等级 (green/yellow/orange/red)
    risk_per_share: float          # 每股风险 ($)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.shares > 0 and self.position_value > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shares": self.shares,
            "risk_amount": round(self.risk_amount, 2),
            "position_value": round(self.position_value, 2),
            "position_pct": round(self.position_pct * 100, 2),
            "tier": self.tier,
            "water_level": self.water_level,
            "risk_per_share": round(self.risk_per_share, 2),
            "warnings": self.warnings,
        }


# ============================================================
#  默认配置 (fallback，当 YAML 不可用时)
# ============================================================

DEFAULT_TIERS = {
    "micro":    {"min_equity": 0,       "max_equity": 10000,   "risk_pct": 0.05,  "total_pct": 0.30, "max_positions": 3,  "min_trade_usd": 100,   "max_position_pct": 0.35},
    "small":    {"min_equity": 10000,   "max_equity": 50000,   "risk_pct": 0.035, "total_pct": 0.25, "max_positions": 5,  "min_trade_usd": 500,   "max_position_pct": 0.30},
    "medium":   {"min_equity": 50000,   "max_equity": 200000,  "risk_pct": 0.03,  "total_pct": 0.20, "max_positions": 8,  "min_trade_usd": 2000,  "max_position_pct": 0.25},
    "large":    {"min_equity": 200000,  "max_equity": 1000000, "risk_pct": 0.025, "total_pct": 0.15, "max_positions": 10, "min_trade_usd": 5000,  "max_position_pct": 0.20},
    "inst":     {"min_equity": 1000000, "max_equity": None,    "risk_pct": 0.02,  "total_pct": 0.10, "max_positions": 15, "min_trade_usd": 10000, "max_position_pct": 0.15},
}

DEFAULT_WATER_LEVELS = {
    "green":  {"threshold": -0.05, "risk_mult": 1.0,  "pos_mult": 1.0},
    "yellow": {"threshold": -0.10, "risk_mult": 0.5,  "pos_mult": 0.5},
    "orange": {"threshold": -0.20, "risk_mult": 0.25, "pos_mult": 0.25},
    "red":    {"threshold": -0.35, "risk_mult": 0.0,  "pos_mult": 0.0},
}

DEFAULT_GUARDS = {
    "max_single_order_pct": 0.05,
    "max_daily_turnover_pct": 0.30,
    "min_shares": 1,
    "max_shares": 10000,
    "round_lot": True,
    "slippage_check": True,
}


# ============================================================
#  纯函数：仓位计算
# ============================================================

def calc_position_size(
    equity: float,
    buying_power: float,
    entry_price: float,
    stop_loss_price: float,
    tiers: Optional[Dict[str, Any]] = None,
    water_levels: Optional[Dict[str, Any]] = None,
    drawdown_pct: float = 0.0,
    guards: Optional[Dict[str, Any]] = None,
) -> SizingResult:
    """
    纯函数：根据输入参数计算仓位。零副作用，可单测，可回测。

    Args:
        equity: 当前账户净值
        buying_power: 可用购买力
        entry_price: 入场价格
        stop_loss_price: 止损价格
        tiers: 资金分层配置 (dict of dicts)
        water_levels: 水位配置
        drawdown_pct: 当前回撤比例 (负值)
        guards: 安全护栏配置

    Returns:
        SizingResult: 仓位计算结果
    """
    warnings = []

    # ── 参数校验 ──
    if entry_price <= 0:
        return SizingResult(0, 0, 0, 0, "unknown", "unknown", 0, ["入场价格 <= 0"])

    if stop_loss_price <= 0:
        return SizingResult(0, 0, 0, 0, "unknown", "unknown", 0, ["止损价格 <= 0"])

    # 计算每股风险
    risk_per_share = abs(entry_price - stop_loss_price)
    if risk_per_share <= 0:
        return SizingResult(0, 0, 0, 0, "unknown", "unknown", 0, ["止损 = 入场价，无风险距离"])

    # 加载配置 (默认 fallback)
    tiers = tiers or DEFAULT_TIERS
    water_levels = water_levels or DEFAULT_WATER_LEVELS
    guards = guards or DEFAULT_GUARDS

    # ── 1. 确定资金分层 ──
    tier_name, tier_cfg = _select_tier(equity, tiers)

    # ── 2. 确定水位等级 ──
    wl_name, wl_cfg = _select_water_level(drawdown_pct, water_levels)

    risk_mult = wl_cfg.get("risk_mult", 1.0)
    pos_mult = wl_cfg.get("pos_mult", 1.0)

    # ── 3. 风险基数 (P0 fix 2026-06-07: 用 equity，不用 buying_power) ──
    # 根因: buying_power 包含保证金杠杆 (Futu SIMULATE 账户 buying_power 可达 equity×1.3)
    #        用 buying_power 做 risk_base 导致风险预算虚高 29%，仓位失控
    # 原则: 风险预算 = 你实际拥有的钱 × 风险比例，不包含借来的钱
    # buying_power 仅用于 "能否买得起" 检查 (step 6 max_cap_shares)
    risk_base = equity if equity > 0 else 0
    if risk_base <= 0:
        warnings.append("风险基数为 0，无法计算仓位")
        return SizingResult(0, 0, 0, 0, tier_name, wl_name, risk_per_share, warnings)

    # ── 4. 风险金额 = risk_base × risk_pct × risk_mult ──
    tier_risk_pct = tier_cfg.get("risk_pct", 0.035)
    risk_amount = risk_base * tier_risk_pct * risk_mult

    # ── 5. 风险驱动股数 ──
    risk_shares = int(risk_amount / risk_per_share)

    # ── 6. 资金上限股数 ──
    tier_total_pct = tier_cfg.get("total_pct", 0.20)
    max_position_value = equity * tier_total_pct * pos_mult
    max_cap_shares = int(max_position_value / entry_price)

    # ── 7. 单只仓位上限 ──
    max_pos_pct = tier_cfg.get("max_position_pct", 0.25)
    max_single_shares = int((equity * max_pos_pct) / entry_price)

    # ── 8. 取最小值 ──
    shares = min(risk_shares, max_cap_shares, max_single_shares)

    # ── 9. 安全护栏 ──
    shares, guard_warnings = _apply_guards(shares, entry_price, equity, guards)
    warnings.extend(guard_warnings)

    # ── 10. 整手对齐 ──
    shares = _round_lot(shares, guards.get("round_lot", True))

    # ── 11. 最小交易金额过滤 ──
    position_value = shares * entry_price
    min_trade = tier_cfg.get("min_trade_usd", 100)
    if shares > 0 and position_value < min_trade:
        warnings.append(f"仓位 ${position_value:.0f} < 最小 ${min_trade}，降为 0")
        shares = 0
        position_value = 0

    # ── 12. 计算最终指标 ──
    final_risk_amount = shares * risk_per_share
    final_position_pct = position_value / equity if equity > 0 else 0

    result = SizingResult(
        shares=shares,
        risk_amount=final_risk_amount,
        position_value=position_value,
        position_pct=final_position_pct,
        tier=tier_name,
        water_level=wl_name,
        risk_per_share=risk_per_share,
        warnings=warnings,
    )

    if result.is_valid:
        logger.info(
            f"[PositionSizer] equity=${equity:,.0f} | tier={tier_name} | "
            f"wl={wl_name} | price=${entry_price:.2f} | sl=${stop_loss_price:.2f} | "
            f"risk/share=${risk_per_share:.2f} → {shares}股 (${position_value:,.0f}, {final_position_pct*100:.1f}%)"
        )
    else:
        logger.warning(
            f"[PositionSizer] 无法开仓: equity=${equity:,.0f} | price=${entry_price:.2f} | "
            f"sl=${stop_loss_price:.2f} | tier={tier_name} | wl={wl_name} | "
            f"原因: {'; '.join(warnings) if warnings else '计算结果为 0'}"
        )

    return result


# ============================================================
#  辅助函数 (纯函数)
# ============================================================

def _select_tier(equity: float, tiers: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """根据净值选择资金分层"""
    # 按 min_equity 降序排列，找到第一个匹配的 tier
    sorted_tiers = sorted(
        tiers.items(),
        key=lambda x: x[1].get("min_equity", 0),
        reverse=True,
    )
    for name, cfg in sorted_tiers:
        min_eq = cfg.get("min_equity", 0) or 0
        max_eq = cfg.get("max_equity")
        if equity >= min_eq and (max_eq is None or equity < max_eq):
            return name, cfg
    # fallback: 返回 min_equity 最小的 tier
    return sorted_tiers[-1] if sorted_tiers else ("micro", DEFAULT_TIERS["micro"])


def _select_water_level(drawdown_pct: float, water_levels: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """根据回撤选择水位等级"""
    # 按 threshold 降序排列 (最宽松优先: green > yellow > orange > red)
    sorted_levels = sorted(
        water_levels.items(),
        key=lambda x: x[1].get("threshold", 0),
        reverse=True,
    )
    for name, cfg in sorted_levels:
        threshold = cfg.get("threshold", -0.05)
        if drawdown_pct >= threshold:
            return name, cfg
    # fallback: 返回最严格的层级 (red)
    return sorted_levels[-1] if sorted_levels else ("red", DEFAULT_WATER_LEVELS["red"])


def _apply_guards(shares: int, entry_price: float, equity: float, guards: Dict[str, Any]) -> Tuple[int, List[str]]:
    """应用安全护栏"""
    warnings = []

    min_shares = guards.get("min_shares", 1)
    max_shares = guards.get("max_shares", 10000)
    max_order_pct = guards.get("max_single_order_pct", 0.05)

    # 最小股数
    if shares < min_shares and shares > 0:
        warnings.append(f"股数 {shares} < 最小 {min_shares}")
        shares = 0

    # 最大股数 (防乌龙指)
    if shares > max_shares:
        warnings.append(f"股数 {shares} > 最大 {max_shares}，截断")
        shares = max_shares

    # 单笔最大金额
    max_order_value = equity * max_order_pct
    position_value = shares * entry_price
    if position_value > max_order_value and shares > 0:
        max_allowed = int(max_order_value / entry_price)
        warnings.append(f"仓位 ${position_value:,.0f} > 单笔上限 ${max_order_value:,.0f}，截断至 {max_allowed} 股")
        shares = max_allowed

    return shares, warnings


def _round_lot(shares: int, enabled: bool = True) -> int:
    """整手对齐 (美股 = 1 股)"""
    if not enabled or shares <= 0:
        return shares
    # 美股最小交易单位 = 1 股，已经是整手
    return max(1, shares)


# ============================================================
#  配置加载器 (从 YAML 读取)
# ============================================================

def load_config_from_yaml(config_container=None) -> Tuple[Dict, Dict, Dict]:
    """
    从 unified_config.yaml 加载仓位计算配置

    Args:
        config_container: ConfigContainer 实例

    Returns:
        (tiers, water_levels, guards) 三元组
    """
    if config_container is None:
        try:
            from app.core.config_container import get_config
            config_container = get_config()
        except Exception:
            pass

    if config_container is None:
        return DEFAULT_TIERS, DEFAULT_WATER_LEVELS, DEFAULT_GUARDS

    cs = config_container.get_dotted("risk.capital_safety", {})

    tiers = cs.get("tiers", DEFAULT_TIERS)
    water_levels = cs.get("water_levels", DEFAULT_WATER_LEVELS)
    guards = cs.get("safety_guards", DEFAULT_GUARDS)

    return tiers, water_levels, guards


def get_sizing_method() -> str:
    """获取当前仓位计算方法 (P3 2026-05-27)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        return cfg.get_dotted("risk.position_sizing.method", "fixed_risk")
    except Exception:
        return "fixed_risk"


def calc_volatility_adjusted_size(
    equity: float,
    buying_power: float,
    entry_price: float,
    stop_loss_price: float,
    atr_current: float = 0,
    atr_mean: float = 0,
    tiers: Optional[Dict[str, Any]] = None,
    water_levels: Optional[Dict[str, Any]] = None,
    drawdown_pct: float = 0.0,
    guards: Optional[Dict[str, Any]] = None,
) -> "SizingResult":
    """波动率自适应仓位计算 (P3 2026-05-27)

    当 ATR 高于均值时缩小仓位 (高波动期减仓),
    当 ATR 低于均值时放大仓位 (低波动期加仓).

    scale = clamp(atr_mean / atr_current, 0.5, 2.0)
    """
    # 先按标准方法计算基础仓位
    base_result = calc_position_size(
        equity=equity,
        buying_power=buying_power,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        tiers=tiers,
        water_levels=water_levels,
        drawdown_pct=drawdown_pct,
        guards=guards,
    )

    # ATR 波动率调整
    if atr_current > 0 and atr_mean > 0:
        scale = max(0.5, min(2.0, atr_mean / atr_current))
        adjusted_shares = int(base_result.shares * scale)
        return SizingResult(
            shares=adjusted_shares,
            risk_amount=base_result.risk_amount * scale,
            position_value=adjusted_shares * entry_price,
            position_pct=base_result.position_pct * scale,
            tier=base_result.tier,
            water_level=base_result.water_level,
            risk_per_share=base_result.risk_per_share,
            warnings=base_result.warnings + [f"ATR调整: scale={scale:.2f}"],
        )

    return base_result

