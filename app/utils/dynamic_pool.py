"""
FTSeek-US Dynamic Stock Pool Generator

根据 tier (资金分层) + unified_risk_level (双重风控融合等级)
动态生成选股池过滤配置。

设计原则:
  1. 安全状态 → 全面出击 (宽条件，多标的)
  2. 适中状态 → 收紧条件 (提高置信度，过滤噪声)
  3. 预警状态 → 只选高确定性 (高置信 + 低波动 + 催化剂)
  4. 危险状态 → 停止开新仓

作者: FTSeek Team
日期: 2026-05-02
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger("ftseek.dynamic_pool")


# ═══════════════════════════════════════════════════════════════════
#  SSOT 加载辅助函数 (P0 2026-05-04)
#  优先从 unified_config.yaml > strategies.dynamic_pool 读取,
#  回退到硬编码默认值
# ═══════════════════════════════════════════════════════════════════

def _load_config_section(dotted_key: str, default: Any = None) -> Any:
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(dotted_key, default)
    except Exception:
        return default


def _load_base_profiles_from_config() -> Optional[Dict[str, Dict]]:
    tiers = _load_config_section("strategies.dynamic_pool.tiers")
    if tiers and isinstance(tiers, dict):
        return tiers
    return None


def _load_risk_adjustments_from_config() -> Optional[Dict[str, Dict]]:
    adj = _load_config_section("strategies.dynamic_pool.risk_adjustments")
    if adj and isinstance(adj, dict):
        return adj
    return None


# ============================================================
#  选股池过滤配置
# ============================================================

@dataclass
class StockFilterProfile:
    """动态选股池配置"""
    max_symbols: int              # 最多监控多少只股票
    min_confidence: float         # 最低置信度
    min_score: int                # 最低信号分
    max_price: float              # 最高股价 (过滤高价股降低滑点)
    min_price: float              # 最低股价
    max_volatility: float         # 最大日波动率 (过滤极端波动)
    min_avg_volume: int           # 最低日均成交量 (流动性要求)
    max_position_pct: float       # 单只最大仓位占比
    max_concurrent: int           # 最大同时持仓数
    allow_short: bool             # 是否允许做空
    require_catalyst: bool        # 是否要求新闻催化剂
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_symbols": self.max_symbols,
            "min_confidence": self.min_confidence,
            "min_score": self.min_score,
            "max_price": self.max_price,
            "min_price": self.min_price,
            "max_volatility": self.max_volatility,
            "min_avg_volume": self.min_avg_volume,
            "max_position_pct": self.max_position_pct,
            "max_concurrent": self.max_concurrent,
            "allow_short": self.allow_short,
            "require_catalyst": self.require_catalyst,
            "label": self.label,
        }


# ============================================================
#  动态选股矩阵: (tier, risk_level) → StockFilterProfile
# ============================================================

# 基础 tier 配置 (安全状态)
_BASE_PROFILES = {
    "micro": StockFilterProfile(3,  0.70, 65, 100,  5,  0.15, 500_000,  0.35, 2, False, False, "微型资金-安全"),
    "small": StockFilterProfile(5,  0.65, 60, 200,  5,  0.12, 1_000_000, 0.30, 3, False, False, "小资金-安全"),
    "medium": StockFilterProfile(8,  0.60, 55, 500,  5,  0.10, 2_000_000, 0.25, 5, True,  False, "中等资金-安全"),
    "large": StockFilterProfile(12, 0.55, 50, 1000, 5,  0.08, 5_000_000, 0.20, 8, True,  False, "大资金-安全"),
    "inst":  StockFilterProfile(20, 0.50, 50, 2000, 5,  0.06, 10_000_000, 0.15, 12, True, False, "机构资金-安全"),
}

# 风险收紧系数
_RISK_ADJUSTMENTS = {
    "moderate": {  # 适中
        "max_symbols_mult": 0.6,     # 减少 40%
        "min_confidence_add": 0.10,  # 置信度 +10%
        "min_score_add": 5,
        "max_price_mult": 0.75,
        "max_volatility_mult": 0.80,
        "min_avg_volume_mult": 2.0,
        "max_position_mult": 0.65,
        "max_concurrent_mult": 0.60,
        "require_catalyst": True,
    },
    "warning": {  # 预警
        "max_symbols_mult": 0.3,     # 减少 70%
        "min_confidence_add": 0.25,  # 置信度 +25%
        "min_score_add": 15,
        "max_price_mult": 0.5,
        "max_volatility_mult": 0.50,
        "min_avg_volume_mult": 3.0,
        "max_position_mult": 0.35,
        "max_concurrent_mult": 0.25,
        "allow_short": False,
        "require_catalyst": True,
    },
    "danger": {  # 危险 → 停止开新仓
        "max_symbols": 0,
    },
    "blackout": {  # 熔断 → 停止一切
        "max_symbols": 0,
    },
}


def generate_dynamic_pool_profile(
    tier: str,
    risk_level: str,
    custom_overrides: Optional[Dict[str, Any]] = None,
) -> StockFilterProfile:
    """
    根据资金分层和风控等级生成动态选股池配置。

    P0 2026-05-04: 优先从 unified_config.yaml > strategies.dynamic_pool 读取,
    回退到硬编码 _BASE_PROFILES / _RISK_ADJUSTMENTS.

    Args:
        tier: 资金分层 (micro/small/medium/large/inst)
        risk_level: 统一风控等级 (safe/moderate/warning/danger/blackout)
        custom_overrides: 自定义覆盖 (可选)

    Returns:
        StockFilterProfile: 动态选股配置
    """
    # P0 SSOT: 尝试从 YAML 加载
    yaml_tiers = _load_base_profiles_from_config()
    yaml_adjustments = _load_risk_adjustments_from_config()

    # 构建 base profile (优先 YAML, 回退硬编码)
    if yaml_tiers and tier in yaml_tiers:
        t = yaml_tiers[tier]
        base = StockFilterProfile(
            max_symbols=int(t.get("max_symbols", 5)),
            min_confidence=float(t.get("min_confidence", 0.50)),
            min_score=int(t.get("min_score", 50)),
            max_price=float(t.get("max_price", 500)),
            min_price=float(t.get("min_price", 5)),
            max_volatility=float(t.get("max_volatility", 0.10)),
            min_avg_volume=int(t.get("min_avg_volume", 1_000_000)),
            max_position_pct=float(t.get("max_position_pct", 0.25)),
            max_concurrent=int(t.get("max_concurrent", 5)),
            allow_short=bool(t.get("allow_short", True)),
            require_catalyst=bool(t.get("require_catalyst", False)),
            label=f"{tier}-yaml",
        )
    else:
        base = _BASE_PROFILES.get(tier, _BASE_PROFILES["small"])

    if risk_level == "safe":
        # 安全状态 → 直接使用基础配置
        profile = StockFilterProfile(
            max_symbols=base.max_symbols,
            min_confidence=base.min_confidence,
            min_score=base.min_score,
            max_price=base.max_price,
            min_price=base.min_price,
            max_volatility=base.max_volatility,
            min_avg_volume=base.min_avg_volume,
            max_position_pct=base.max_position_pct,
            max_concurrent=base.max_concurrent,
            allow_short=base.allow_short,
            require_catalyst=base.require_catalyst,
            label=f"{tier}-{risk_level}",
        )
    else:
        # 风险状态 → 收紧配置 (优先 YAML, 回退硬编码)
        if yaml_adjustments and risk_level in yaml_adjustments:
            adj = yaml_adjustments[risk_level]
        else:
            adj = _RISK_ADJUSTMENTS.get(risk_level, {})

        if adj.get("max_symbols", -1) == 0:
            # 危险/熔断 → 停止开新仓
            return StockFilterProfile(
                max_symbols=0, min_confidence=1.0, min_score=100,
                max_price=0, min_price=0, max_volatility=0,
                min_avg_volume=0, max_position_pct=0, max_concurrent=0,
                allow_short=False, require_catalyst=False,
                label=f"{tier}-{risk_level} (停止开仓)",
            )

        profile = StockFilterProfile(
            max_symbols=max(1, int(base.max_symbols * adj.get("max_symbols_mult", 1.0))),
            min_confidence=min(1.0, base.min_confidence + adj.get("min_confidence_add", 0)),
            min_score=int(base.min_score + adj.get("min_score_add", 0)),
            max_price=base.max_price * adj.get("max_price_mult", 1.0),
            min_price=base.min_price,
            max_volatility=base.max_volatility * adj.get("max_volatility_mult", 1.0),
            min_avg_volume=int(base.min_avg_volume * adj.get("min_avg_volume_mult", 1.0)),
            max_position_pct=base.max_position_pct * adj.get("max_position_mult", 1.0),
            max_concurrent=max(1, int(base.max_concurrent * adj.get("max_concurrent_mult", 1.0))),
            allow_short=adj.get("allow_short", base.allow_short),
            require_catalyst=adj.get("require_catalyst", base.require_catalyst),
            label=f"{tier}-{risk_level}",
        )

    # 应用自定义覆盖
    if custom_overrides:
        for k, v in custom_overrides.items():
            if hasattr(profile, k):
                setattr(profile, k, v)

    return profile


# ============================================================
#  配置加载器
# ============================================================

def load_pool_config(config_container=None) -> Dict[str, Any]:
    """
    从 unified_config.yaml 加载动态池配置

    Returns:
        配置字典
    """
    defaults = {
        "enable_dynamic_pool": True,
        "fallback_profile": "small",  # 无法确定 tier 时的默认 tier
    }
    if config_container is None:
        return defaults

    try:
        from app.core.config_container import get_config
        cfg = get_config()
        dp = cfg.get_dotted("dynamic_pool", {})
        return {**defaults, **dp}
    except Exception:
        return defaults
