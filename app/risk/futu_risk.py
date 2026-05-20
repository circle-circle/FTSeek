"""
FTSeek-US Futu Margin Risk Level Calculator

基于富途真实保证金数据计算风险等级，作为双重防御体系的"硬风控"层。

设计原则:
  1. 使用富途官方字段: initial_margin, maintain_margin, debt, net_liquidation
  2. 秒级实时 (依赖 AccountRefresher 的 30s 缓存)
  3. 与水位系统互补: 硬风控 (爆仓) + 软风控 (回撤)
  4. 纯函数，可单测

作者: FTSeek Team
日期: 2026-05-02
"""

import logging
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("ftseek.futu_risk")


# ============================================================
#  富途风险等级
# ============================================================

class FutuRiskLevel(Enum):
    """富途保证金风险等级"""
    SAFE = "safe"           # 无风险: 含贷权益 >= 初始保证金
    MODERATE = "moderate"   # 适中: 有杠杆但可控
    WARNING = "warning"     # 预警: 剩余流动性 < 10%
    DANGER = "danger"       # 危险: 含贷权益 <= 维持保证金


@dataclass
class FutuRiskResult:
    """富途风控计算结果"""
    level: FutuRiskLevel
    net_liquidation: float      # 含贷权益 (总资产 - 负债)
    maintain_margin: float      # 维持保证金
    initial_margin: float       # 初始保证金
    excess_liquidity: float     # 剩余流动性
    liquidity_ratio: float      # 流动性比率 (excess / net_liquidation)
    margin_utilization: float   # 保证金利用率 (maintain / net_liquidation)
    reason: str
    warnings: list = field(default_factory=list)

    @property
    def level_name(self) -> str:
        return self.level.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "net_liquidation": round(self.net_liquidation, 2),
            "maintain_margin": round(self.maintain_margin, 2),
            "initial_margin": round(self.initial_margin, 2),
            "excess_liquidity": round(self.excess_liquidity, 2),
            "liquidity_ratio": round(self.liquidity_ratio * 100, 1),
            "margin_utilization": round(self.margin_utilization * 100, 1),
            "reason": self.reason,
            "warnings": self.warnings,
        }


# ============================================================
#  核心计算函数 (纯函数)
# ============================================================

def calc_futu_risk(account_info: Dict[str, Any]) -> FutuRiskResult:
    """
    基于富途真实保证金数据计算风险等级。

    富途官方定义:
    - 无风险 (safe): 含贷权益 >= 初始保证金
    - 适中 (moderate): 有杠杆，剩余流动性 >= 含贷权益 × 10%
    - 预警 (warning): 剩余流动性 < 含贷权益 × 10%
    - 危险 (danger): 含贷权益 <= 维持保证金 (强平风险)

    Args:
        account_info: broker_adapter.get_account_info() 返回值

    Returns:
        FutuRiskResult: 风控计算结果
    """
    # C1 修复 (2026-05-02): 空账户保护 — 避免 API 返回空 dict 时误判 DANGER
    total_assets = float(account_info.get("total_assets", 0))
    if total_assets <= 0:
        return FutuRiskResult(
            level=FutuRiskLevel.SAFE, net_liquidation=0, maintain_margin=0,
            initial_margin=0, excess_liquidity=0, liquidity_ratio=0,
            margin_utilization=0, reason="账户数据不可用，默认安全",
            warnings=["broker 未返回账户数据"],
        )

    debt = float(account_info.get("debt", 0))
    initial_margin = float(account_info.get("initial_margin", 0))
    maintain_margin = float(account_info.get("maintain_margin", 0))
    buying_power = float(account_info.get("buying_power", 0))
    frozen_cash = float(account_info.get("frozen_cash", 0))
    interest_debt = float(account_info.get("interest_debt", 0))

    warnings = []

    # W1 修复 (2026-05-02): 模拟盘特殊处理
    # 模拟盘 maintain_margin 可能始终为 0，0 <= 0 会误判 DANGER
    trade_env = account_info.get("_trade_env", 1)
    if trade_env == 1 and maintain_margin == 0 and debt == 0:
        return FutuRiskResult(
            level=FutuRiskLevel.SAFE, net_liquidation=total_assets,
            maintain_margin=0, initial_margin=initial_margin,
            excess_liquidity=total_assets, liquidity_ratio=1.0,
            margin_utilization=0, reason="模拟盘无保证金要求",
        )

    # ── 1. 含贷权益 (Net Liquidation Value) = 总资产 - 负债 ──
    net_liquidation = total_assets - debt

    # ── 2. 剩余流动性 (Excess Liquidity) = 含贷权益 - 维持保证金 ──
    excess_liquidity = net_liquidation - maintain_margin

    # ── 3. 流动性比率 = 剩余流动性 / 含贷权益 ──
    liquidity_ratio = excess_liquidity / net_liquidation if net_liquidation > 0 else 0

    # ── 4. 保证金利用率 = 维持保证金 / 含贷权益 ──
    margin_utilization = maintain_margin / net_liquidation if net_liquidation > 0 else 0

    # ── 5. 判定等级 (从最严格开始) ──

    # 危险: 含贷权益 <= 维持保证金 → 强平风险
    if net_liquidation <= maintain_margin:
        level = FutuRiskLevel.DANGER
        reason = f"含贷权益 ${net_liquidation:,.0f} <= 维持保证金 ${maintain_margin:,.0f} (强平风险)"
        if maintain_margin > 0:
            warnings.append(f"保证金利用率 {margin_utilization*100:.0f}% >= 100%")
        return FutuRiskResult(
            level=level,
            net_liquidation=net_liquidation,
            maintain_margin=maintain_margin,
            initial_margin=initial_margin,
            excess_liquidity=excess_liquidity,
            liquidity_ratio=liquidity_ratio,
            margin_utilization=margin_utilization,
            reason=reason,
            warnings=warnings,
        )

    # 预警: 剩余流动性 < 含贷权益 × 10%
    if liquidity_ratio < 0.10:
        level = FutuRiskLevel.WARNING
        reason = f"剩余流动性 ${excess_liquidity:,.0f} < 含贷权益 10% (${net_liquidation*0.1:,.0f})"
        warnings.append(f"流动性比率仅 {liquidity_ratio*100:.1f}%")
        return FutuRiskResult(
            level=level,
            net_liquidation=net_liquidation,
            maintain_margin=maintain_margin,
            initial_margin=initial_margin,
            excess_liquidity=excess_liquidity,
            liquidity_ratio=liquidity_ratio,
            margin_utilization=margin_utilization,
            reason=reason,
            warnings=warnings,
        )

    # 无风险: 含贷权益 >= 初始保证金 (无杠杆)
    if initial_margin == 0 or net_liquidation >= initial_margin:
        level = FutuRiskLevel.SAFE
        reason = "无杠杆风险" if initial_margin == 0 else f"含贷权益 ${net_liquidation:,.0f} >= 初始保证金 ${initial_margin:,.0f}"
        return FutuRiskResult(
            level=level,
            net_liquidation=net_liquidation,
            maintain_margin=maintain_margin,
            initial_margin=initial_margin,
            excess_liquidity=excess_liquidity,
            liquidity_ratio=liquidity_ratio,
            margin_utilization=margin_utilization,
            reason=reason,
            warnings=warnings,
        )

    # 适中: 有杠杆但可控
    level = FutuRiskLevel.MODERATE
    reason = f"有杠杆 (利用率 {margin_utilization*100:.1f}%)，但流动性充足 ({liquidity_ratio*100:.1f}%)"
    return FutuRiskResult(
        level=level,
        net_liquidation=net_liquidation,
        maintain_margin=maintain_margin,
        initial_margin=initial_margin,
        excess_liquidity=excess_liquidity,
        liquidity_ratio=liquidity_ratio,
        margin_utilization=margin_utilization,
        reason=reason,
        warnings=warnings,
    )


# ============================================================
#  配置查询 (从 unified_config.yaml)
# ============================================================

def get_futu_risk_config(config_container=None) -> Dict[str, Any]:
    """
    从 YAML 加载富途风控配置

    Returns:
        配置字典，包含:
          safe_threshold, moderate_threshold, warning_threshold
    """
    defaults = {
        "warning_liquidity_pct": 0.10,     # 剩余流动性预警阈值 10%
        "danger_margin_util_pct": 1.0,      # 保证金利用率危险阈值 100%
    }
    if config_container is None:
        return defaults

    try:
        from app.core.config_container import get_config
        cfg = get_config()
        cs = cfg.get_dotted("risk.capital_safety", {})
        margin = cs.get("futu_margin", {})
        return {**defaults, **margin}
    except Exception:
        return defaults
