"""Portfolio Correlation Check - 组合相关性与集中度检查

提供组合层面的板块集中度和方向集中度检查，
在新标的入场前评估是否与现有持仓过度相关，
并能从作战计划targets中推荐分散度更高的替代标的。
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CorrelationAlert:
    """组合相关性告警结果。"""
    level: str  # "HIGH" / "MEDIUM" / "LOW"
    message: str
    sector_concentration: Dict[str, float] = field(default_factory=dict)
    direction_concentration: Dict[str, float] = field(default_factory=dict)
    max_sector_pct: float = 0.0
    suggestion: str = ""
    alternative_symbol: Optional[str] = None


# ── 板块映射（fallback） ──
# 覆盖常见美股；factor_engine._SECTOR_MEMBERS 优先使用
SECTOR_MAP: Dict[str, str] = {
    # Technology
    "US.AAPL": "Technology", "US.MSFT": "Technology", "US.NVDA": "Technology",
    "US.AMD": "Technology", "US.GOOGL": "Technology", "US.GOOG": "Technology",
    "US.META": "Technology", "US.AMZN": "Technology", "US.TSLA": "Technology",
    "US.AVGO": "Technology", "US.CRM": "Technology", "US.ORCL": "Technology",
    "US.INTC": "Technology", "US.NFLX": "Technology",
    # Financials
    "US.JPM": "Financials", "US.GS": "Financials", "US.BAC": "Financials",
    "US.MS": "Financials", "US.WFC": "Financials", "US.C": "Financials",
    "US.BLK": "Financials", "US.SCHW": "Financials", "US.V": "Financials",
    "US.MA": "Financials",
    # Healthcare
    "US.LLY": "Healthcare", "US.UNH": "Healthcare", "US.JNJ": "Healthcare",
    "US.PFE": "Healthcare", "US.MRK": "Healthcare", "US.ABBV": "Healthcare",
    # Energy
    "US.XOM": "Energy", "US.CVX": "Energy", "US.COP": "Energy",
    "US.SLB": "Energy", "US.OXY": "Energy",
    # Consumer
    "US.WMT": "Consumer", "US.COST": "Consumer", "US.HD": "Consumer",
    "US.NKE": "Consumer", "US.MCD": "Consumer", "US.SBUX": "Consumer",
    # Industrials
    "US.CAT": "Industrials", "US.BA": "Industrials", "US.GE": "Industrials",
    "US.UPS": "Industrials", "US.HON": "Industrials",
    # Semiconductors
    "US.TSM": "Semiconductors", "US.ASML": "Semiconductors",
    "US.QCOM": "Semiconductors", "US.MU": "Semiconductors",
    # China ADR
    "US.BABA": "China_ADR", "US.PDD": "China_ADR", "US.JD": "China_ADR",
    "US.BIDU": "China_ADR", "US.NIO": "China_ADR", "US.NTES": "China_ADR",
    "US.BILI": "China_ADR", "US.LI": "China_ADR",
    # ETF
    "US.SPY": "ETF", "US.QQQ": "ETF", "US.IWM": "ETF", "US.DIA": "ETF",
}


def _strip_prefix(symbol: str) -> str:
    """去掉 'US.' 前缀，便于与 factor_engine 中的 ticker（无前缀）匹配。"""
    if not symbol:
        return ""
    if symbol.startswith("US."):
        return symbol[3:]
    return symbol


def get_sector(symbol: str) -> str:
    """获取标的所属板块；未知返回 'Unknown'。

    优先复用 factor_engine 的 _SECTOR_MEMBERS（无前缀ticker）。
    若导入失败或未命中，则查询内置 SECTOR_MAP（带 'US.' 前缀）。
    """
    if not symbol:
        return "Unknown"

    bare = _strip_prefix(symbol)

    # 优先尝试 factor_engine 的板块成员表
    try:
        from app.plan.factor_engine import _SECTOR_MEMBERS  # type: ignore
        for sector, members in _SECTOR_MEMBERS.items():
            if bare in members:
                return sector
    except (ImportError, AttributeError):
        pass

    # fallback：直接查 SECTOR_MAP（同时尝试带/不带前缀两种 key）
    if symbol in SECTOR_MAP:
        return SECTOR_MAP[symbol]
    prefixed = f"US.{bare}"
    if prefixed in SECTOR_MAP:
        return SECTOR_MAP[prefixed]

    return "Unknown"


def get_sector_concentration(positions: List[dict]) -> Dict[str, float]:
    """获取当前持仓的板块集中度分布（板块 → 占比）。

    边界：positions 为空时返回空 dict。
    """
    if not positions:
        return {}
    total = len(positions)
    counts = Counter(get_sector(p.get("symbol", "")) for p in positions)
    return {sector: round(cnt / total, 4) for sector, cnt in counts.items()}


def _direction_concentration(positions: List[dict]) -> Dict[str, float]:
    """方向（LONG/SHORT）占比分布。"""
    if not positions:
        return {}
    total = len(positions)
    counts = Counter((p.get("direction") or "LONG").upper() for p in positions)
    return {d: round(cnt / total, 4) for d, cnt in counts.items()}


def suggest_diversified_alternative(
    original_symbol: str,
    plan_targets: List[dict],
    current_positions: List[dict],
) -> Optional[str]:
    """从作战计划 targets 中挑选一个板块不同且评分较高的替代标的。

    选择条件：
      - 板块不在已持仓板块集合中（也不等于原标的板块）
      - 不是原标的本身
      - verdict 为 VALID / CONDITIONAL（若提供）
    按 composite_rank 降序排列后取第一个匹配项。
    """
    if not plan_targets:
        return None

    original_sector = get_sector(original_symbol)
    held_sectors = {get_sector(p.get("symbol", "")) for p in (current_positions or [])}
    held_sectors.add(original_sector)

    sorted_targets = sorted(
        plan_targets,
        key=lambda x: x.get("composite_rank", x.get("composite_score", 0)) or 0,
        reverse=True,
    )

    for t in sorted_targets:
        t_symbol = t.get("symbol", "")
        if not t_symbol or t_symbol == original_symbol:
            continue
        t_sector = get_sector(t_symbol)
        if t_sector == "Unknown" or t_sector in held_sectors:
            continue
        verdict = t.get("verdict")
        if verdict is None or verdict in ("VALID", "CONDITIONAL"):
            return t_symbol
    return None


def check_portfolio_fit(
    new_symbol: str,
    new_direction: str,
    current_positions: List[dict],
    plan_targets: Optional[List[dict]] = None,
) -> CorrelationAlert:
    """检查新标的是否与现有持仓过度相关。

    告警阈值：
      - HIGH:   单一板块占比 ≥ 60%
      - MEDIUM: 单一板块占比 ≥ 40%
      - LOW:    其他

    Args:
        new_symbol: 准备入场的标的（如 "US.JPM"）
        new_direction: "LONG" / "SHORT"
        current_positions: [{symbol, direction, ...}, ...]
        plan_targets: 作战计划 targets 列表（用于建议替代）
    """
    new_direction = (new_direction or "LONG").upper()

    # 边界：首笔交易
    if not current_positions:
        return CorrelationAlert(
            level="LOW",
            message="首笔交易，无集中度风险",
            sector_concentration={get_sector(new_symbol): 1.0},
            direction_concentration={new_direction: 1.0},
            max_sector_pct=1.0,
            suggestion="",
        )

    all_positions = list(current_positions) + [
        {"symbol": new_symbol, "direction": new_direction}
    ]
    total = len(all_positions)

    sector_counts = Counter(get_sector(p.get("symbol", "")) for p in all_positions)
    sector_pcts = {k: round(v / total, 4) for k, v in sector_counts.items()}
    max_sector_name = max(sector_pcts, key=sector_pcts.get)
    max_sector = sector_pcts[max_sector_name]

    dir_pcts = _direction_concentration(all_positions)

    # ── 告警分级 ──
    if max_sector >= 0.6:
        alt = suggest_diversified_alternative(
            new_symbol, plan_targets or [], current_positions
        )
        if alt:
            suggestion = f"建议替换为 {alt}（不同板块，降低集中风险）"
        else:
            suggestion = "建议将仓位缩减至正常的50%"
        return CorrelationAlert(
            level="HIGH",
            message=(
                f"添加{new_symbol}后，{max_sector_name}板块占比达"
                f"{max_sector * 100:.0f}%（超过60%阈值）"
            ),
            sector_concentration=sector_pcts,
            direction_concentration=dir_pcts,
            max_sector_pct=max_sector,
            suggestion=suggestion,
            alternative_symbol=alt,
        )

    if max_sector >= 0.4:
        return CorrelationAlert(
            level="MEDIUM",
            message=(
                f"{max_sector_name}板块占比{max_sector * 100:.0f}%，"
                f"接近集中度阈值"
            ),
            sector_concentration=sector_pcts,
            direction_concentration=dir_pcts,
            max_sector_pct=max_sector,
            suggestion="注意板块集中度，考虑分散到其他板块",
            alternative_symbol=None,
        )

    return CorrelationAlert(
        level="LOW",
        message=f"组合分散度良好（最大板块{max_sector_name} {max_sector * 100:.0f}%）",
        sector_concentration=sector_pcts,
        direction_concentration=dir_pcts,
        max_sector_pct=max_sector,
        suggestion="",
        alternative_symbol=None,
    )


__all__ = [
    "CorrelationAlert",
    "SECTOR_MAP",
    "get_sector",
    "get_sector_concentration",
    "suggest_diversified_alternative",
    "check_portfolio_fit",
]
