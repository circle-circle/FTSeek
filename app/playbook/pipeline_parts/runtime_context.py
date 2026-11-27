"""运行上下文/依赖装配域 (阶段 2b 迁自 app/playbook/pipeline.py)。

时间/交易日函数暂留 pipeline: 其 datetime/_now_et 是既有测试 monkeypatch 缝,
待调用面全部迁移后再解除。本模块先承接配置读取、依赖构造与 scheduler 上下文。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.playbook.pipeline_parts.bridge_adapters import NullVotingAdapter

logger = logging.getLogger(__name__)

_horizon_selector: Optional[Any] = None
_UNSET = object()


def _get_horizon_selector() -> Any:
    """获取 HorizonSelector 单例 (lazy init)"""
    global _horizon_selector
    if _horizon_selector is None:
        from app.playbook.horizon_selector import HorizonSelector
        _horizon_selector = HorizonSelector()
    return _horizon_selector


def _playbook_enabled() -> bool:
    """总闸门: 读取 config playbook.enabled (默认 False)。
    关闭时三个 pipeline 入口全部早退, 不调 Provider/LLM/DB/飞书。
    这是 shadow 上线的安全开关 — 确认就绪前保持 false。"""
    try:
        from app.core.config_container import get_config
        return bool(get_config().get_dotted("playbook.enabled", False))
    except Exception:
        # 配置不可用时默认关闭 (上线安全优先, 宁可漏跑不可误跑)
        return False


def _playbook_cfg(key: str, default=None):
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(f"playbook.{key}", default)
    except Exception:
        return default


def _build_data_collector():
    """构造 PlaybookDataCollector, 复用项目既有 Provider"""
    from app.playbook.data_collector import PlaybookDataCollector
    from app.data.providers.fundamental_provider import FundamentalProvider
    from app.data.finnhub_provider import FinnhubProvider
    from app.data.sec_form4_provider import SECForm4Provider
    from app.data.earnings_calendar import EarningsCalendar

    # FutuExtendedProvider (可选 — OpenD 10.7+ 新增 API)
    futu_ext = None
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        futu_ext = FutuExtendedProvider()
    except Exception as e:
        logger.debug(f"FutuExtendedProvider init skipped: {e}")

    return PlaybookDataCollector(
        fundamental_provider=FundamentalProvider(),
        finnhub_provider=FinnhubProvider(),
        insider_provider=SECForm4Provider(),
        earnings_calendar=EarningsCalendar(),
        futu_extended=futu_ext,
    )


def _get_watchlist(scheduler) -> List[str]:
    """从 scheduler.watchlist 取候选 + 合并 factor_engine 动态池.

    动态池来源 (DEB-20260629):
      - daily_active_pool.json (持仓 + 手动添加)
      - dragonhead_pool (行业龙头, 按 regime 切换)
      - Finnhub 异动扫描 (盘前 >2% 涨跌幅)
      - factor_engine 质量门禁 (过滤低流动性)

    纯加法: 静态 watchlist 保留不变, 动态池只增不减.
    若 factor_engine 不可用, fallback 到纯静态 watchlist.

    RFC-218 §16 (2026-11-27): 全局黑名单守卫 PLAYBOOK_BLACKLIST_GUARD (默认 OFF,
    关闭时逐字节旧行为)。静态侧 get_stock_codes 已滤黑名单, 但 scheduler.watchlist
    经 daily_active_pool 合并可携带被禁标的 (scheduler_core._load_watchlist 无复核),
    动态池 factor_engine 亦零黑名单引用 → 在 Playbook 新候选边界统一复核,
    合并路径与 fallback 路径同口径。只滤新候选, 不触碰持仓/退出链。
    """
    wl = list(getattr(scheduler, "watchlist", []) or [])

    # 2026-07-02: 修复 US.US.X 双重前缀 (factor_engine对已含US.前缀的symbol重复添加)
    wl = [s[3:] if s.startswith("US.US.") else s for s in wl]

    # 2026-06-30: 过滤 #SIM 后缀 (SIM账户持仓不进入pipeline)
    wl = [s for s in wl if '#' not in s]

    # RFC-218 §16: 黑名单守卫 (fail-open: 守卫不可用时保持旧口径, 不瘫痪候选面)
    try:
        from app.utils.watchlist_manager import filter_global_blacklist as _bl_guard
    except Exception as e:
        logger.debug(f"[playbook] 黑名单守卫不可用, 保持旧口径: {e}")
        _bl_guard = lambda syms: syms  # noqa: E731
    wl = _bl_guard(wl)

    try:
        from app.plan.factor_engine import _get_stock_pool
        dynamic_pool, meta = _get_stock_pool()
        if dynamic_pool:
            merged = list(set(wl) | set(dynamic_pool))
            # 双保险: 合并后也过滤#SIM + 修复US.US.双前缀
            merged = [s[3:] if s.startswith("US.US.") else s for s in merged]
            merged = [s for s in merged if '#' not in s]
            # RFC-218 §16: 动态池贡献同样复核 (覆盖 dragonhead/异动扫描/daily_active_pool)
            merged = _bl_guard(merged)
            logger.info(
                f"[playbook] watchlist 合并动态池: "
                f"static={len(wl)} + dynamic={len(dynamic_pool)} "
                f"(overlap={len(set(wl) & set(dynamic_pool))}) → merged={len(merged)} | "
                f"dynamic_meta={meta.get('final_pool_size', '?')}"
            )
            return sorted(merged)
    except Exception as e:
        logger.debug(f"[playbook] 动态池合并失败, 使用静态 watchlist: {e}")

    return wl


def _get_positions_as_dicts(scheduler) -> List[Dict[str, Any]]:
    """把 position_store 的 Position 对象转成 screener 期望的 dict(symbol, sector, direction)"""
    out: List[Dict[str, Any]] = []
    try:
        store = getattr(scheduler, "position_store", None)
        if not store:
            return out
        positions = store.all() if hasattr(store, "all") else []
        for p in positions:
            sym = getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else None)
            if not sym:
                continue
            sector = getattr(p, "sector", None) or getattr(p, "industry", None) or \
                (p.get("sector") or p.get("industry") if isinstance(p, dict) else None)
            direction = getattr(p, "direction", None) or \
                (p.get("direction") if isinstance(p, dict) else None)
            out.append({"symbol": sym, "sector": sector, "direction": str(direction) if direction else ""})
    except Exception as e:
        logger.debug(f"[playbook] 持仓读取失败 (行业集中度检查将降级): {e}")
    return out


def _get_total_equity(scheduler) -> float:
    """复用盘前计划的 equity 取数逻辑"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cs = get_capital_safety_system()
        if cs._state and cs._state.current_equity:
            return float(cs._state.current_equity)
    except Exception:
        pass
    try:
        from app.core.config_container import get_config
        return float(get_config().get_dotted("capital.fixed_amount", 82_000))
    except Exception:
        return 100_000.0


def _resolve_sizing_base(pb_cfg=None) -> float:
    """R4 统一风险基数 (SSOT): 单笔风险 + 日名义额度共用同一基数, 消除双尺子。

    面向未来切实盘: 优先取富途服务端真实净值 (capital_safety.current_equity,
    由 accinfo_query 每 30s 刷新, 跟随 AUTO_TRADE_ACCOUNT 自动切 sim/real)。
    净值随实盘变化 → 单笔/日额度同步缩放, 亏损后基数自动缩小 = 天然防爆仓。
    仅在净值取不到时兜底到 config risk_base_capital (默认 82000), 不再作为主口径。
    """
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        if _cs._state and _cs._state.current_equity and _cs._state.current_equity > 0:
            return float(_cs._state.current_equity)
    except Exception:
        pass
    try:
        if pb_cfg is None:
            from app.playbook.config import get_playbook_config
            pb_cfg = get_playbook_config()
        return float(pb_cfg.get("risk_base_capital", 82000.0))
    except Exception:
        return 82000.0


def _get_voting_coordinator(scheduler):
    """从 scheduler 获取决策适配器 (Bridge/TechnicalFallback/Null Adapter).

    2026-07-09: scheduler_core 初始化时根据 bridge 状态赋值
    (有 bridge → BridgeVotingAdapter; 无 bridge → Technical/Null)。
    scheduler.voting_coordinator 是兼容字段名; 真·3模型 VotingCoordinator 已退休。
    万一为 None → NullVotingAdapter
    (全 SKIP), 不复活噪声投票。
    """
    vc = getattr(scheduler, "voting_coordinator", None)
    if vc is not None:
        return vc
    logger.warning("[playbook] scheduler.voting_coordinator 为 None → NullVotingAdapter (全 SKIP)")
    return NullVotingAdapter()


@dataclass
class RuntimeContext:
    """显式 scheduler 运行上下文; 依赖按属性首次使用时装配, 不改变原调用顺序。"""

    scheduler: Any
    watchlist_provider: Callable[[Any], List[str]]
    collector_factory: Callable[[], Any]
    positions_provider: Callable[[Any], List[Dict[str, Any]]]
    voting_coordinator_provider: Callable[[Any], Any]
    total_equity_provider: Callable[[Any], float]

    _watchlist: Optional[List[str]] = field(default=None, init=False, repr=False)
    _collector: Any = field(default_factory=lambda: _UNSET, init=False, repr=False)
    _positions: Optional[List[Dict[str, Any]]] = field(default=None, init=False, repr=False)
    _voting_coordinator: Any = field(default_factory=lambda: _UNSET, init=False, repr=False)
    _total_equity: Optional[float] = field(default=None, init=False, repr=False)
    _data_router: Any = field(default_factory=lambda: _UNSET, init=False, repr=False)

    @property
    def watchlist(self) -> List[str]:
        if self._watchlist is None:
            self._watchlist = self.watchlist_provider(self.scheduler)
        return self._watchlist

    @property
    def collector(self) -> Any:
        if self._collector is _UNSET:
            self._collector = self.collector_factory()
        return self._collector

    @property
    def positions(self) -> List[Dict[str, Any]]:
        if self._positions is None:
            self._positions = self.positions_provider(self.scheduler)
        return self._positions

    @property
    def voting_coordinator(self) -> Any:
        if self._voting_coordinator is _UNSET:
            self._voting_coordinator = self.voting_coordinator_provider(self.scheduler)
        return self._voting_coordinator

    @property
    def total_equity(self) -> float:
        if self._total_equity is None:
            self._total_equity = self.total_equity_provider(self.scheduler)
        return self._total_equity

    @property
    def data_router(self) -> Any:
        if self._data_router is _UNSET:
            self._data_router = (getattr(self.scheduler, "data_router", None)
                                 or getattr(self.scheduler, "market_data", None))
        return self._data_router


def build_runtime_context(
    scheduler: Any,
    *,
    watchlist_provider: Optional[Callable[[Any], List[str]]] = None,
    collector_factory: Optional[Callable[[], Any]] = None,
    positions_provider: Optional[Callable[[Any], List[Dict[str, Any]]]] = None,
    voting_coordinator_provider: Optional[Callable[[Any], Any]] = None,
    total_equity_provider: Optional[Callable[[Any], float]] = None,
) -> RuntimeContext:
    """装配 RuntimeContext; 缺省用本域函数, pipeline 过渡期显式注入 facade 缝。"""
    return RuntimeContext(
        scheduler=scheduler,
        watchlist_provider=watchlist_provider or _get_watchlist,
        collector_factory=collector_factory or _build_data_collector,
        positions_provider=positions_provider or _get_positions_as_dicts,
        voting_coordinator_provider=voting_coordinator_provider or _get_voting_coordinator,
        total_equity_provider=total_equity_provider or _get_total_equity,
    )
