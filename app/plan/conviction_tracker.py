"""Live Conviction Tracker - 动态确信度追踪

为每个作战计划target维护"活的确信度"分数，随时间和市场变化自动衰减。

衰减因子:
- time:   计划生成至今的小时数 (0.70 ~ 1.0)
- sector: 所属板块涨跌幅相对plan生成时的变化 (0.5 ~ 1.2)
- vix:    VIX相对plan生成时的变化 (0.7 ~ 1.0)
- price:  现价相对entry_zone中点的偏离 (0.6 ~ 1.0)

线程安全: 内部使用RLock保护convictions字典与全局快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from threading import RLock
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class LiveConviction:
    """单个标的的实时确信度快照。"""
    symbol: str
    direction: str  # "LONG" / "SHORT"
    base_confidence: int  # 原始置信度(0-100)
    live_confidence: int  # 当前实时置信度(0-100)
    status: str  # "ACTIVE" / "WEAKENED" / "EXPIRED"
    factors: Dict[str, float] = field(default_factory=dict)
    decay_reasons: List[str] = field(default_factory=list)
    last_updated: str = ""
    entry_zone: List[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _normalize_confidence(value) -> int:
    """将confidence统一为0-100整数。

    支持 float(0-1) 与 int/float(0-100) 两种输入。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v <= 1.0:
        v = v * 100.0
    if v < 0:
        v = 0.0
    if v > 100:
        v = 100.0
    return int(round(v))


def _parse_plan_time(plan: dict) -> Optional[datetime]:
    """从plan中尽力解析生成时间。

    优先级: pipeline_stats.timestamp > generated_at > date(取当日0点)
    """
    stats = plan.get("pipeline_stats") or {}
    for key in ("timestamp", "generated_at", "created_at"):
        v = stats.get(key) or plan.get(key)
        if not v:
            continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(float(v))
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
    date_str = plan.get("date")
    if date_str:
        try:
            return datetime.fromisoformat(str(date_str))
        except Exception:  # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------------------
# 单例追踪器
# ---------------------------------------------------------------------------

class ConvictionTracker:
    """单例: 维护当前作战计划全部标的的实时确信度。"""

    _instance: Optional["ConvictionTracker"] = None
    _lock = RLock()

    def __init__(self):
        self._convictions: Dict[str, LiveConviction] = {}
        self._plan_generated_at: Optional[datetime] = None
        # plan生成时的板块快照: {sector_name: pct_change}
        self._plan_sector_snapshot: Dict[str, float] = {}
        # plan生成时的VIX
        self._plan_vix: float = 0.0
        self._rlock = RLock()

    # ------------------------------------------------------------------
    # 单例入口
    # ------------------------------------------------------------------
    @classmethod
    def get_instance(cls) -> "ConvictionTracker":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 初始化 / 写入
    # ------------------------------------------------------------------
    def init_from_plan(self, plan: dict) -> None:
        """从作战计划初始化所有targets的确信度。

        多次调用会清空已有状态后重新初始化。
        """
        if not isinstance(plan, dict):
            logger.warning("init_from_plan: plan不是dict, 已忽略")
            return

        with self._rlock:
            self._convictions.clear()
            self._plan_generated_at = _parse_plan_time(plan)
            self._plan_sector_snapshot = dict(plan.get("sector_momentum") or {})
            try:
                self._plan_vix = float(plan.get("vix") or plan.get("plan_vix") or 0.0)
            except (TypeError, ValueError):
                self._plan_vix = 0.0

            now_iso = datetime.now().isoformat(timespec="seconds")
            targets = plan.get("targets") or []
            for t in targets:
                if not isinstance(t, dict):
                    continue
                symbol = t.get("symbol")
                if not symbol:
                    continue
                base = _normalize_confidence(t.get("confidence", 0))
                direction = str(t.get("direction") or "LONG").upper()
                entry_zone_raw = t.get("entry_zone") or []
                entry_zone: List[float] = []
                try:
                    entry_zone = [float(x) for x in entry_zone_raw][:2]
                except (TypeError, ValueError):
                    entry_zone = []

                self._convictions[symbol] = LiveConviction(
                    symbol=symbol,
                    direction=direction,
                    base_confidence=base,
                    live_confidence=base,
                    status="ACTIVE",
                    factors={"time": 1.0, "sector": 1.0, "vix": 1.0, "price": 1.0},
                    decay_reasons=[],
                    last_updated=now_iso,
                    entry_zone=entry_zone,
                )

            logger.info(
                "ConvictionTracker initialized: %d targets, plan_time=%s, vix=%.2f",
                len(self._convictions),
                self._plan_generated_at,
                self._plan_vix,
            )

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------
    def update_conviction(
        self, symbol: str, market_context: Optional[dict] = None
    ) -> Optional[LiveConviction]:
        """更新单个标的的实时确信度。

        market_context 字段 (全部可选):
            - vix: float 当前VIX
            - current_price: float 现价
            - sector_change_since_plan: float 板块自plan生成以来的%变化
            - sector: str 标的所属板块名 (与plan_sector_snapshot配合使用)
            - sector_change_now: dict {sector: pct} 当前板块涨幅
        """
        ctx = market_context or {}
        with self._rlock:
            conv = self._convictions.get(symbol)
            if conv is None:
                return None
            live, status, factors, reasons = self._calculate_live(symbol, ctx)
            conv.live_confidence = live
            conv.status = status
            conv.factors = factors
            conv.decay_reasons = reasons
            conv.last_updated = datetime.now().isoformat(timespec="seconds")
            return conv

    def update_all(
        self, market_context: Optional[dict] = None
    ) -> Dict[str, LiveConviction]:
        """批量更新所有标的，返回快照。"""
        ctx = market_context or {}
        with self._rlock:
            for symbol in list(self._convictions.keys()):
                self.update_conviction(symbol, ctx)
            return dict(self._convictions)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_conviction(self, symbol: str) -> Optional[LiveConviction]:
        with self._rlock:
            return self._convictions.get(symbol)

    def should_still_execute(self, symbol: str, threshold: int = 50) -> bool:
        """判断当前确信度是否仍达到执行门槛。"""
        with self._rlock:
            conv = self._convictions.get(symbol)
            if conv is None:
                return False
            if conv.status == "EXPIRED":
                return False
            return conv.live_confidence >= threshold

    def get_all_convictions(self) -> Dict[str, LiveConviction]:
        with self._rlock:
            return dict(self._convictions)

    # 测试 / 复位用
    def reset(self) -> None:
        with self._rlock:
            self._convictions.clear()
            self._plan_generated_at = None
            self._plan_sector_snapshot = {}
            self._plan_vix = 0.0

    # ------------------------------------------------------------------
    # 核心计算
    # ------------------------------------------------------------------
    def _calculate_live(
        self, symbol: str, ctx: dict
    ) -> Tuple[int, str, Dict[str, float], List[str]]:
        """返回 (live_confidence, status, factors_dict, reasons)"""
        conv = self._convictions[symbol]
        base = conv.base_confidence
        reasons: List[str] = []

        # ---- Factor 1: 时间衰减 (0.70 ~ 1.0) ----
        if self._plan_generated_at is not None:
            try:
                hours = (
                    datetime.now() - self._plan_generated_at
                ).total_seconds() / 3600.0
                if hours < 0:
                    hours = 0.0
            except Exception:  # noqa: BLE001
                hours = 0.0
            time_factor = max(0.70, 1.0 - (hours / 8.0) * 0.30)
            if time_factor < 0.85:
                reasons.append(f"时间衰减({hours:.0f}h)")
        else:
            time_factor = 1.0

        # ---- Factor 2: 板块变化 (0.5 ~ 1.2) ----
        sector_change = ctx.get("sector_change_since_plan")
        if sector_change is None:
            # 尝试从 sector_change_now & 快照中推导
            sector_name = ctx.get("sector")
            now_map = ctx.get("sector_change_now") or {}
            if sector_name and sector_name in now_map:
                try:
                    snap = float(self._plan_sector_snapshot.get(sector_name, 0.0))
                    sector_change = float(now_map[sector_name]) - snap
                except (TypeError, ValueError):
                    sector_change = 0.0
            else:
                sector_change = 0.0
        try:
            sector_change = float(sector_change)
        except (TypeError, ValueError):
            sector_change = 0.0

        if conv.direction == "LONG":
            sector_factor = max(0.5, min(1.2, 1.0 + sector_change * 0.15))
            if sector_change < -1.0:
                reasons.append(f"板块转弱{sector_change:.1f}%")
        else:
            sector_factor = max(0.5, min(1.2, 1.0 - sector_change * 0.15))
            if sector_change > 1.0:
                reasons.append(f"板块转强{sector_change:+.1f}%")

        # ---- Factor 3: VIX变化 (0.7 ~ 1.0) ----
        try:
            vix_now = float(ctx.get("vix", 20.0) or 20.0)
        except (TypeError, ValueError):
            vix_now = 20.0
        vix_change = vix_now - self._plan_vix if self._plan_vix > 0 else 0.0
        vix_factor = max(0.7, 1.0 - max(0.0, vix_change) * 0.03)
        if vix_change > 3:
            reasons.append(f"VIX上升{vix_change:.1f}")

        # ---- Factor 4: 价格偏离 entry_zone (0.6 ~ 1.0) ----
        try:
            price = float(ctx.get("current_price", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        if (
            conv.entry_zone
            and len(conv.entry_zone) == 2
            and price > 0
        ):
            mid = (conv.entry_zone[0] + conv.entry_zone[1]) / 2.0
            if mid > 0:
                distance_pct = abs(price - mid) / mid
                price_factor = max(0.6, 1.0 - distance_pct * 2.0)
                if distance_pct > 0.03:
                    reasons.append(f"偏离入场区{distance_pct * 100:.1f}%")
            else:
                price_factor = 1.0
        else:
            price_factor = 1.0

        # ---- 综合 ----
        live = int(base * time_factor * sector_factor * vix_factor * price_factor)
        if live < 0:
            live = 0
        elif live > 100:
            live = 100

        # 状态判断
        if live < 40:
            status = "EXPIRED"
        elif base > 0 and live < base * 0.70:
            status = "WEAKENED"
        else:
            status = "ACTIVE"

        factors = {
            "time": round(time_factor, 3),
            "sector": round(sector_factor, 3),
            "vix": round(vix_factor, 3),
            "price": round(price_factor, 3),
        }
        return live, status, factors, reasons


# ---------------------------------------------------------------------------
# 模块级访问器
# ---------------------------------------------------------------------------

def get_conviction_tracker() -> ConvictionTracker:
    """全局单例访问器。"""
    return ConvictionTracker.get_instance()


__all__ = [
    "LiveConviction",
    "ConvictionTracker",
    "get_conviction_tracker",
]
