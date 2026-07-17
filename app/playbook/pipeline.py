"""
Playbook 端到端编排管线 — 把 Layer 0→1→2→3 串联通电。

本模块是整个 Trading Playbook System 唯一缺失的"接线层":
    - Phase 0/1/2 的 14 个模块都已实现且单测通过 (232 tests),
      但没有任何生产代码调用它们 — scheduler 不感知 playbook 的存在。
    - 本文件提供 3 个入口, 复用项目现有调度约定 (phase-polling + 每日/每周幂等标志):
        run_playbook_generate(scheduler)   盘前  regime→收集→筛选→VotingCoordinator→入库→飞书
        run_playbook_track(scheduler)      盘后  shadow追踪→因子更新→飞书
        run_playbook_weekly(scheduler)      周五  双层因子分析→权重闭环→周报→飞书

设计原则 (与现有 run_premarket_planning / check_daily_review 对齐):
    1. 每个入口自带节假日跳过 (is_trading_day) + 每日/每周幂等 guard
    2. 单只标的/单步异常不中断整体流程 (silent_except 风格)
    3. 不引入新的 cron daemon — 完全融入 scheduler._run_llm_loop 的 phase 分发
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── ADR-008 (2026-06-28): Playbook 方向审计日志 ────────────────────────

def _log_playbook_direction_audit(symbol: str, llm_direction: str, technical_direction: Optional[str], vote_result: Dict[str, Any]):
    """记录 Playbook 方向决策审计日志 (JSONL)"""
    try:
        import os
        from datetime import datetime
        from app.core.config_container import get_config
        _cfg = get_config()
        log_path = str(_cfg.get_dotted("playbook.technical_arbitration.shadow_log_path", "data/shadow/playbook_direction_audit.jsonl"))
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except Exception:
            pass
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "llm_direction": llm_direction,
            "technical_direction": technical_direction or "",
            "llm_strength": int(vote_result.get("final_strength", 50) or 50),
            "llm_confidence": float(vote_result.get("final_confidence", 0) or 0),
            "llm_std": float(vote_result.get("std_dev", 0) or 0),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[playbook] 方向审计日志失败: {e}")


# ── 模块级单例: 避免每只候选股票都新建 DB 连接 ─────────────────────────
_horizon_selector: Optional[Any] = None


def _get_horizon_selector() -> Any:
    """获取 HorizonSelector 单例 (lazy init)"""
    global _horizon_selector
    if _horizon_selector is None:
        from app.playbook.horizon_selector import HorizonSelector
        _horizon_selector = HorizonSelector()
    return _horizon_selector


# ── 时间/节假日 (复用项目既有约定) ──────────────────────────────────────
ET = None  # lazy


def _now_et():
    global ET
    if ET is None:
        from app.core.scheduler_triggers import ET as _et
        ET = _et
    return ET


def _today_et_str() -> str:
    from datetime import datetime
    return datetime.now(_now_et()).date().isoformat()


def _is_trading_day_today() -> bool:
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        from datetime import datetime
        cal = get_holiday_calendar()
        return cal.is_trading_day("US", datetime.now(_now_et()).date())
    except Exception as e:
        # 节假日日历不可用时按"是交易日"处理 (与 run_premarket_planning 一致, 宁可跑不可漏)
        logger.debug(f"[playbook] 节假日检查异常, 按交易日处理: {e}")
        return True


def _cohort_b_max_hold_hit(entry_time_str: str) -> bool:
    """RFC-061: cohort B 持仓是否已到 ≥5 交易日 (MAX_HOLD_T5 触发).

    用交易日历 (nth_trading_day_after) 算 entry_date + 5 交易日, 遇假期自动跳过
    (如 Juneteenth). 比日历日准. now 用 ET 日期. 异常时返回 False (不强平, 保守).
    """
    if not entry_time_str:
        return False
    try:
        from datetime import datetime
        from app.utils.holiday_calendar import nth_trading_day_after
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        # entry_time 可能 naive (CST 服务器存), 取 date; 若 tz-aware 转 ET
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        deadline = nth_trading_day_after("US", _entry_date, 5)
        if deadline is None:
            return False
        return datetime.now(_et).date() >= deadline
    except Exception as e:
        logger.debug(f"[playbook·RFC-061] MAX_HOLD_T5 日期计算异常 (不强平): {e}")
        return False


def _is_overnight_position(entry_time_str: str) -> bool:
    """RFC-061 #6: 仓位是否隔夜 (entry 在之前的交易日, 非今日入场).

    用于隔夜跳空止损: cohort B 持夜仓次日开盘若跳空 >3% 快平 (OVERNIGHT_GAP_STOP).
    用 ET 日期判断. 异常返回 False (不触发, 保守).
    """
    if not entry_time_str:
        return False
    try:
        from datetime import datetime
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        return _entry_date < datetime.now(_et).date()
    except Exception:
        return False


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


# ── 依赖装配 (lazy, 单步异常隔离) ──────────────────────────────────────
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
        import logging
        logging.getLogger(__name__).debug(f"FutuExtendedProvider init skipped: {e}")

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
    """
    wl = list(getattr(scheduler, "watchlist", []) or [])
    
    # 2026-07-02: 修复 US.US.X 双重前缀 (factor_engine对已含US.前缀的symbol重复添加)
    wl = [s[3:] if s.startswith("US.US.") else s for s in wl]
    
    # 2026-06-30: 过滤 #SIM 后缀 (SIM账户持仓不进入pipeline)
    wl = [s for s in wl if '#' not in s]
    
    try:
        from app.plan.factor_engine import _get_stock_pool
        dynamic_pool, meta = _get_stock_pool()
        if dynamic_pool:
            merged = list(set(wl) | set(dynamic_pool))
            # 双保险: 合并后也过滤#SIM + 修复US.US.双前缀
            merged = [s[3:] if s.startswith("US.US.") else s for s in merged]
            merged = [s for s in merged if '#' not in s]
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


# ── VotingCoordinator → Instruction 转换器 ─────────────────────────────
# Playbook 不再跑独立 LLM, 而是消费 VotingCoordinator 的多模型投票结果.
# 方向由投票决定 (LONG/SHORT/NEUTRAL), 数值由 compute_instruction_values 确定性计算.

def _get_voting_coordinator(scheduler):
    """从 scheduler 获取 voting_coordinator (BridgeVotingAdapter / TechnicalFallbackAdapter / NullVotingAdapter).

    2026-07-09: scheduler_core 初始化时根据 bridge 状态赋值
    (有 bridge → BridgeVotingAdapter; 无 bridge → Technical/Null)。
    2026-07-11 (双脑清理): scheduler.voting_coordinator 恒被赋值, 不再 fallback 到
    di_container 的真·3模型VotingCoordinator (已退休)。万一为 None → NullVotingAdapter
    (全 SKIP), 不复活噪声投票。
    """
    vc = getattr(scheduler, "voting_coordinator", None)
    if vc is not None:
        return vc
    logger.warning("[playbook] scheduler.voting_coordinator 为 None → NullVotingAdapter (全 SKIP)")
    return NullVotingAdapter()


class NullVotingAdapter:
    """空投票适配器 — bridge文件不存在时使用, 全部候选SKIP.

    2026-07-09: 替代VotingCoordinator作为bridge不存在时的fallback。
    根因: 3模型投票方向准确率47%≈随机, 用噪声投票替代信号不如不交易。
    deep_analysis skill负责盘前生成bridge文件, bridge不存在=今天没有高质量信号。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """始终返回None → pipeline将SKIP该候选。"""
        logger.info(f"[NullVoting] {symbol} SKIP — 无bridge文件, 不用噪声投票")
        return None

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — NullVotingAdapter 不写 voting_predictions 表."""
        pass


class TechnicalFallbackAdapter:
    """DA bridge 缺席时的确定性 fallback — 用 technical 纯函数策略出方向.

    2026-07-11 (双脑清理): bridge 不存在时替代 NullVotingAdapter 的“全 SKIP”。
    设计担当: DA = 入场决策脑; DA 缺席时用确定性、非 LLM、可单测的 technical
    策略兜底，而非噪声投票。

    ⚠ 陷阱护栏: 纪律/风控无法把“随机方向入场”变成正期望。所以 fallback 必须有
    确定性依据(technical)、独立归因(strategy_id=technical_fallback)、可关停
    (playbook.fallback_when_no_bridge=none 即回退全 SKIP)。与 DA 互斥 → 任一时刻单链。

    接口兼容: vote(symbol, ctx) → vote_result dict (与 BridgeVotingAdapter 对齐)。
    entry/SL/TP 不在此产出——与 DA 同路径, 由 compute_instruction_values(data) 确定性计算。
    """

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """跑 technical 纯策略出方向; 无方向 → None (SKIP, 不硬凑成交)。"""
        try:
            from app.strategy.pure import evaluate as eval_strategy
            cand = eval_strategy("technical", ctx)
        except Exception as e:
            logger.debug(f"[TechnicalFallback] {symbol} technical 评估异常: {e}")
            return None
        if cand is None:
            return None
        direction = str(getattr(cand, "direction", "") or "").upper()
        if direction not in ("LONG", "SHORT"):
            return None
        strength = int(getattr(cand, "strength", 50) or 50)
        confidence = float(getattr(cand, "confidence", 0.5) or 0.5)
        logger.info(f"[TechnicalFallback] {symbol} technical 方向={direction} str={strength} — DA 缺席兜底")
        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": strength,
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": f"TFB-{symbol}-{int(time.time())}",
            "strategy_id": "technical_fallback",
            "conditions": list(getattr(cand, "signal_tags", []) or []),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — TechnicalFallbackAdapter 不写 voting_predictions 表."""
        pass


class BridgeVotingAdapter:
    """桥接适配器 — 从 opportunity_scan_YYYYMMDD.json 读取 deep_analysis 规划结果.

    替代 VotingCoordinator.vote()，消除 LLM 在 FTSeek 热路径。
    接口兼容: vote(symbol, ctx) → vote_result dict (同 VotingCoordinator 格式)。

    Ponytail: 一个类一个方法，不是新模块。
    """

    def __init__(self, candidates_by_symbol: Dict[str, Dict], trade_date: str):
        self._candidates = candidates_by_symbol
        self._trade_date = trade_date

    def vote(self, symbol: str, ctx) -> Optional[Dict[str, Any]]:
        """返回 bridge 中的候选, 格式与 VotingCoordinator.vote() 对齐。"""
        cand = self._candidates.get(symbol)
        if cand is None:
            return None

        direction = str(cand.get("direction", "NEUTRAL")).upper()
        if direction not in ("LONG", "SHORT"):
            return None

        score = float(cand.get("score", 5.0))
        # 2026-07-10: confidence 从辩论结果合成 (唯一有预测力的信号, r=+0.102)
        # 不用 LLM 定性 confidence (r=-0.211, 负相关)
        # 不用 score/10 (同义反复, 无区分度)
        confidence = 0.50  # 中性基准

        debate = cand.get("debate", {})
        if debate:
            agreed = debate.get("challenger_agreed", False)
            chall_dir = str(debate.get("challenger_direction", "")).upper()
            outcome = cand.get("debate_outcome", "")

            if chall_dir == direction:
                if agreed or "AGREE" in outcome.upper():
                    confidence += 0.20  # 2模型一致确认
            else:
                confidence -= 0.15  # 方向分歧

        confidence = max(0.10, min(0.95, confidence))

        return {
            "symbol": symbol,
            "direction": direction,
            "final_strength": int(score * 10),
            "final_confidence": confidence,
            "consensus_level": confidence,
            "std_dev": 0.0,
            "model_scores": [],
            "is_neutral": False,
            "decision_id": cand.get("decision_id", ""),
            "strategy_id": "deep_analysis",
            "position_suggestion": cand.get("position_suggestion", {}),
            "thesis": cand.get("thesis", ""),
            "conditions": cand.get("conditions", []),
            "entry_price": cand.get("entry_price", 0),
            "stop_loss": cand.get("stop_loss", 0),
            "take_profit": cand.get("take_profit", 0),
            "entry_strategy": cand.get("entry_strategy", ""),
            "invalidation": cand.get("invalidation", ""),
            "risks": cand.get("risks", []),
            "strategy_type": cand.get("strategy_type", ""),
            "position_size_hint": cand.get("position_size_hint", ""),
            "degradation_level": 0,
        }

    def _record_decision_to_db(self, *args, **kwargs):
        """no-op — bridge 模式不写 voting_predictions 表。"""
        pass


def _try_load_bridge() -> Optional[BridgeVotingAdapter]:
    """加载今日 bridge 文件。不存在 → None (调用方 fallback 到 VotingCoordinator)。"""
    import json as _json
    import os as _os
    from datetime import datetime, timezone, timedelta

    _et_offset = timedelta(hours=-4) if 3 <= datetime.now().month <= 11 else timedelta(hours=-5)
    _et_tz = timezone(_et_offset)
    _today_str = datetime.now(_et_tz).strftime("%Y-%m-%d")

    bridge_path = f"/opt/ftseek/FTSeek-US/data/opportunity_scan_{_today_str}.json"
    if not _os.path.exists(bridge_path):
        return None

    try:
        with open(bridge_path) as f:
            data = _json.load(f)
    except Exception as e:
        logger.warning(f"[playbook] bridge文件读取失败: {e}")
        return None

    candidates = data.get("candidates", [])
    if not candidates:
        logger.info(f"[playbook] bridge文件无候选, fallback到VotingCoordinator")
        return None

    # 按 symbol 索引
    by_symbol = {}
    for c in candidates:
        sym = c.get("symbol", "")
        if sym:
            by_symbol[sym] = c

    logger.info(
        f"[playbook] bridge加载: {len(by_symbol)} candidates "
        f"(date={data.get('trade_date', '?')}, market={data.get('market_condition', '?')[:40]})"
    )

    # ── 注入 bridge 候选到 daily_active_pool (2026-07-09) ──────────────
    # 根因: BridgeVotingAdapter.vote(symbol) 是被动查询, 只在 pipeline 遍历 pool
    # 已有 symbol 时才匹配. bridge 候选 (如 TTE/ANET/ASML) 不在 pool → 永不触发.
    # 修复: 加载 bridge 时, 把候选 symbol 追加写入 pool, source=SCAN.
    _pool_path = "/opt/ftseek/FTSeek-US/data/daily_active_pool.json"
    try:
        with open(_pool_path) as _pf:
            _pool = _json.load(_pf)
        _existing = {e.get("code", "") for e in _pool.get("entries", [])}
        _injected = []
        for _sym in by_symbol:
            if _sym not in _existing:
                _pool["entries"].append({
                    "code": _sym,
                    "source": "SCAN",
                    "added_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": None,
                    "metadata": {"bridge": True, "strategy_id": "deep_analysis"},
                })
                _injected.append(_sym)
        if _injected:
            _pool["timestamp"] = datetime.now(timezone.utc).isoformat()
            with open(_pool_path, "w") as _pf:
                _json.dump(_pool, _pf, indent=2)
            logger.info(f"[playbook] bridge候选注入pool: {_injected} ({len(_injected)} new)")
    except Exception as _e:
        logger.warning(f"[playbook] bridge候选注入pool失败(非致命): {_e}")

    return BridgeVotingAdapter(by_symbol, data.get("trade_date", _today_str))


def _normalize_regime_str(regime_context) -> str:
    """统一 regime 字符串为 RISK_ON/CAUTIOUS/RISK_OFF/NO_TRADE (大写枚举名).

    修 §2.7 (signal-source-unification-review-20260624): regime_context.regime.value 是
    小写 ("risk_on"), str(enum_member) 是 "MarketRegime.RISK_ON", 而 regime_mults 键是
    大写 "RISK_ON" — 三者不一致导致 regime_mults.get(regime_str, 0.5) 恒命中默认 0.5.
    统一用 .name 与 regime_mults 键空间对齐.
    """
    reg = getattr(regime_context, "regime", None)
    if reg is None:
        return "CAUTIOUS"
    if hasattr(reg, "name"):  # Enum → "RISK_ON" / "CAUTIOUS" / "RISK_OFF" / "NO_TRADE"
        return reg.name
    return str(reg)


def _build_vote_market_data(symbol: str, data, regime_context, data_router):
    """构造 VotingCoordinator.vote() 的输入.

    router raw dict → SymbolContext.from_raw → dataclass.
    与 Pipeline A (market_data_stage.process) 同路径同键名空间, 让 vote() 走 asdict 分支,
    LLM 从 50+ 字段做决策 (含 atr/bollinger/spread_bps/order_flow_imbalance/\
    slope_5m/day_high/opening_range 等), 并补 market_regime.

    Returns:
        SymbolContext dataclass / None (行情不可用 → 调用方 SKIP)
    """
    if data_router is None:
        return None

    try:
        raw_ctx = data_router.get_full_market_context(symbol)
    except Exception as e:
        logger.warning(f"[playbook] {symbol} get_full_market_context 异常: {e}")
        return None

    if not raw_ctx or float(raw_ctx.get("price", 0) or 0) <= 0:
        return None

    from app.core.pipeline_contracts import SymbolContext
    ctx = SymbolContext.from_raw(symbol, raw_ctx)
    ctx.vix = float(getattr(regime_context, "vix", 20.0) or 20.0)
    ctx.market_regime = _normalize_regime_str(regime_context)
    return ctx


# 2026-07-11 (双脑清理): _merge_playbook_data 已移除。
# 其唯一用途是向 vote() 的 market_data dict 注入 6 维度供已死的 3 模型
# LLM prompt 消费。现 vote() 直接收 SymbolContext, 此函数无调用方。


from app.playbook.instruction_generator import (
    Instruction,
    compute_instruction_values,
    compute_confidence,
    build_thesis,
    _compute_volatility_based_sl_tp,
)


def _build_instructions_from_votes(
    screen_results,
    voting_coordinator,
    regime_context,
    total_equity: float,
    data_router=None,
    today: str = "",
):
    """消费 VotingCoordinator 投票结果, 构建 Instruction 列表.

    数据流:
      ScreenResult → market_data → VotingCoordinator.vote() → vote_result
      → 方向映射 (LONG→BUY, SHORT→SELL_SHORT, NEUTRAL→SKIP)
      → compute_instruction_values 确定性计算数值
      → Instruction
    """
    if voting_coordinator is None:
        logger.warning("[playbook] VotingCoordinator 不可用, 全部候选 SKIP")
        return [
            Instruction(symbol=r.symbol, direction="SKIP", skip_reason="VotingCoordinator unavailable")
            for r in screen_results
        ]

    instructions = []
    # §2.7 修复: 统一用 _normalize_regime_str, 与 regime_mults 键空间对齐 (原 str(enum) 恒命中默认 0.5)
    regime_str = _normalize_regime_str(regime_context)

    # Regime 仓位乘数
    regime_mults = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
    regime_mult = regime_mults.get(regime_str, 0.5)

    # ── 过滤审计计数器 (DEB-20260628) ──
    # 记录每层过滤干掉了几个candidates, 函数末尾统一输出
    _filter_audit = {
        "total_input": len(screen_results),
        "market_data_none": 0,
        "vote_failed": 0,
        "vote_neutral": 0,
        "buy_macd_ma50_disabled": 0,  # 已禁用, 计数用
        "buy_low_strength_skip": 0,
        "buy_high_std_skip": 0,
        "low_confidence_skip": 0,
        "direction_flip": 0,
        "direction_conflict_skip": 0,
        "neg_edge_factor_skip": 0,  # 2026-07-10: 纯负edge因子拦截
        "neg_pair_skip": 0,  # 2026-07-11: 配对级负edge拦截
        "price_gap_skip": 0,
        "no_entry_price": 0,
        "final_instructions": 0,
    }

    for sr in screen_results:
        symbol = sr.symbol
        data = sr.data
        factors = sr.factors

        # 持久化信息层上下文 (新闻/内部人/财报/基本面/主题雷达)
        # 在投票前保存, 即使投票失败也有数据
        _persist_signal_context(today, symbol, data)

        # 构造 market_data 并调 VotingCoordinator (新路径走 SymbolContext, legacy 走 10 字段 dict)
        _market_ctx = _build_vote_market_data(symbol, data, regime_context, data_router)
        if _market_ctx is None:
            _filter_audit["market_data_none"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="行情不可用 (router 无数据)"
            ))
            continue

        # 2026-07-11 (双脑清理): 直接传 SymbolContext (_market_ctx) 给 vote()。
        # 原 _merge_playbook_data 只为喂已死的 3 模型 LLM prompt, 现已移除。
        # Bridge/Null/Technical 适配器都接受 ctx: Bridge 按 symbol 查, Technical 跑 eval("technical", ctx)。
        try:
            vote_result = voting_coordinator.vote(symbol, _market_ctx)
        except Exception as e:
            _filter_audit["vote_failed"] += 1
            logger.warning(f"[playbook] {symbol} 投票失败: {e}, SKIP")
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason=f"投票异常: {e}"
            ))
            continue
        
        if vote_result is None:
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票返回 None"
            ))
            continue
        
        # 方向映射: VotingCoordinator 输出 LONG/SHORT/NEUTRAL → Instruction BUY/SELL_SHORT/SKIP
        vote_direction = str(vote_result.get("direction", "NEUTRAL")).upper()
        is_neutral = vote_result.get("is_neutral", False) or vote_direction == "NEUTRAL"

        # 2026-06-30 Phase 1: 在过滤前记录信号快照（消除选择性偏差）
        # 所有信号（包括后续被SKIP的）都记录因子，这样IC能学习"被过滤的信号对不对"
        _snap_decision_id = vote_result.get("decision_id", "") or f"SNAP-{symbol}-{int(time.time()*1000)}"
        try:
            from app.analysis.signal_ic_tracker import record_signal_snapshot
            record_signal_snapshot(
                decision_id=_snap_decision_id,
                symbol=symbol,
                direction=vote_direction,
                strength=int(vote_result.get("final_strength", 50) or 50),
                confidence=float(vote_result.get("consensus_level", 0) or 0),
                consensus_level=float(vote_result.get("consensus_level", 0) or 0),
                strategy_id=vote_result.get("strategy_id", ""),
                factors=list(factors.keys()) if factors else [],
                market_context=factors if factors else None,  # P1: 持久化触发因子数值详情(非仅keys)
            )
        except Exception:
            pass  # 非关键路径

        if is_neutral:
            _filter_audit["vote_neutral"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票中性, 无方向信号"
            ))
            continue

        # ── ADR-008 (2026-06-28): 盘中技术方向仲裁 ──
        # 读取配置
        _direction_mode = "llm_vote"
        _llm_conflict_threshold = 0.70
        try:
            from app.core.config_container import get_config
            _cfg = get_config()
            _direction_mode = str(_cfg.get_dotted("playbook.direction_mode", "llm_vote")).lower()
            _llm_conflict_threshold = float(_cfg.get_dotted("playbook.technical_arbitration.llm_conflict_confidence_threshold", 0.70))
        except Exception:
            pass

        # 计算技术方向 (best-effort, 用于影子日志与技术仲裁)
        technical_candidate = None
        technical_direction = None
        try:
            from app.strategy.pure import evaluate as eval_strategy
            technical_candidate = eval_strategy("technical", _market_ctx)
            if technical_candidate is not None:
                technical_direction = str(technical_candidate.direction).upper()
        except Exception as e:
            logger.debug(f"[playbook] {symbol} 技术信号评估异常: {e}")

        # 记录方向审计日志 (无论模式)
        _log_playbook_direction_audit(symbol, vote_direction, technical_direction, vote_result)

        # 技术仲裁模式: 无技术方向则 SKIP; LLM 强反向冲突则 SKIP; 否则 technical 获胜
        if _direction_mode == "technical_arbitration":
            if not technical_direction:
                _filter_audit["technical_no_signal"] = _filter_audit.get("technical_no_signal", 0) + 1
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP", skip_reason="无技术方向信号"
                ))
                continue

            if vote_direction != technical_direction:
                _llm_conf = float(vote_result.get("final_confidence", 0) or 0)
                if _llm_conf >= _llm_conflict_threshold:
                    _filter_audit["direction_conflict_skip"] = _filter_audit.get("direction_conflict_skip", 0) + 1
                    logger.info(
                        f"[playbook] {symbol} 方向冲突 SKIP: "
                        f"LLM={vote_direction}(conf={_llm_conf:.2f}) vs technical={technical_direction}"
                    )
                    instructions.append(Instruction(
                        symbol=symbol, direction="SKIP",
                        skip_reason=f"方向冲突: LLM={vote_direction} vs technical={technical_direction}"
                    ))
                    continue
                # LLM 反向不强, technical 获胜
                vote_direction = technical_direction
                logger.info(f"[playbook] {symbol} 技术方向覆盖 LLM: {technical_direction}")
            # 同向: 使用 vote_direction (与 technical 一致)

        direction = "BUY" if vote_direction == "LONG" else "SELL_SHORT"
        decision_id = vote_result.get("decision_id", "")

        # 更新 signal_context: 追加 LLM reasoning (需要 decision_id 关联 decisions.db)
        if decision_id:
            _persist_signal_context(today, symbol, data, vote_result)

        # 2026-06-28 (DEB-20260628): 方向差异化信号强度门槛.
        # BUY 方向历史胜率低(38%), 需要比 SHORT 更强的投票强度.
        # SHORT 方向历史胜率高(91%), 保持默认阈值.
        _final_strength = int(vote_result.get("final_strength", 50) or 50)
        _vote_std = float(vote_result.get("std_dev", 0) or 0)
        _vote_consensus = float(vote_result.get("consensus_level", 0) or 0)
        if direction == "BUY":
            # 2026-07-10: 删除 BUY_min_strength gate (strength=confidence×100同义反复, r=-0.063无预测力, 拦截0笔)
            # 保留 BUY_max_std gate (模型分歧 r=+0.102, 唯一弱正相关信号)
            _vote_sid = vote_result.get("strategy_id", "") or ""
            try:
                from app.core.strategy_registry import get_strategy_config
                _strat_cfg = get_strategy_config(_vote_sid)
                _buy_max_std = float(_strat_cfg["direction_filter"].get("BUY_max_std", 15))
            except Exception:
                _buy_max_std = 20
            # 模型一致性: BUY 要求分歧更小
            if _vote_std > _buy_max_std:
                _filter_audit["buy_high_std_skip"] = _filter_audit.get("buy_high_std_skip", 0) + 1
                logger.info(
                    f"[playbook] {symbol} BUY 模型分歧 std={_vote_std:.1f} > {_buy_max_std} → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP", skip_reason="BUY 模型分歧过大"
                ))
                continue

        # ── 2026-07-11 (双脑清理): deep_analysis 方向门禁已删除 ──────────────
        # 原门禁(L786-868)用于用 deep_analysis 交叉验证 voting_llm 方向。
        # 3 模型 voting 退休后可证空转: bridge 候选在上方刚写入自己的 snapshot,
        # 门禁又查到自己刚写的行 → 永远自我背书; 且 conditions 注入对 list 调
        # .get() 必抛异常 → 恒 no-op。technical_fallback 日无 DA snapshot → 恒放行。
        # 删除零行为变化, 且省掉每标的 2 次 DB 连接。

        # ── 过滤审计日志 (DEB-20260628) ──────────────────────────────
        # 目标: 记录每层过滤干掉了几个candidates, 为调优提供数据
        # 审计计数器在函数末尾统一输出
        # ──────────────────────────────────────────────────────────────

        # §2026-06-26: BUY 入场质量过滤 — MACD 柱确认 + MA50 方向过滤.
        # ⚠️ DEB-20260628: 暂时禁用 (6/26新增, 导致6/25从19→1的断崖式下降)
        # 根因: 弱势市场中MACD柱≤0 + 价格≤MA50 会批量SKIP所有BUY方向候选
        # 恢复条件: IC数据达标(≥30笔)后, 用数据验证是否重新启用
        # if direction == "BUY":
        #     macd_hist = market_data.macd - market_data.macd_signal
        #     if macd_hist <= 0:
        #         ...SKIP
        #     if market_data.ma50 > 0 and market_data.price > 0 and market_data.price <= market_data.ma50:
        #         ...SKIP
        #
        # 审计: 记录本会被这两层过滤干掉的 BUY 候选数(禁用期间只计数, 不 SKIP)
        if direction == "BUY":
            _macd = float(getattr(_market_ctx, "macd", 0) or 0)
            _macd_sig = float(getattr(_market_ctx, "macd_signal", 0) or 0)
            _ma50 = float(getattr(_market_ctx, "ma50", 0) or 0)
            _price = float(getattr(_market_ctx, "price", 0) or 0)
            _macd_hist = _macd - _macd_sig
            _would_skip_macd = _macd_hist <= 0
            _would_skip_ma50 = (
                _ma50 > 0
                and _price > 0
                and _price <= _ma50
            )
            if _would_skip_macd or _would_skip_ma50:
                _filter_audit["buy_macd_ma50_disabled"] += 1
                logger.debug(
                    f"[playbook] {symbol} BUY 原 MACD/MA50 过滤命中 "
                    f"(macd_hist={_macd_hist:.4f}, price={_price:.2f}, ma50={_ma50:.2f})"
                )

        # 2026-06-30: 方向冲突检查 — 从硬编码翻转改为IC加权+记录不拦截
        # 原逻辑: 看空条件数>看多 → 翻转方向; 相等 → SKIP
        # 问题1: BEARISH/BULLISH列表方向和真实IC相反(above_ma20被当bullish但IC=-0.553)
        # 问题2: 因子冲突SKIP杀掉8只大市值(GOOG/META/ORCL) — 信号不可见
        # 新逻辑: 用因子IC加权判断净方向; 冲突时记录但放行(让IC gate+退出策略管理风险)
        from app.playbook.condition_taxonomy import BEARISH_CONDITIONS, BULLISH_CONDITIONS
        _conditions = list(factors.keys()) if factors else []
        _bearish_hits = [c for c in _conditions if c in BEARISH_CONDITIONS]
        _bullish_hits = [c for c in _conditions if c in BULLISH_CONDITIONS]
        _bear_count = len(_bearish_hits)
        _bull_count = len(_bullish_hits)

        if _bear_count > 0 or _bull_count > 0:
            # 用因子IC加权判断净方向（如果有IC数据）
            try:
                import json as _json
                from pathlib import Path as _Path
                _ic_path = _Path("data/factor_ic_latest.json")
                _factor_ic = {}
                if _ic_path.exists():
                    with open(_ic_path) as _f:
                        _factor_ic = _json.load(_f)
            except Exception:
                _factor_ic = {}

            # 计算因子IC加权的净方向分
            _net_score = 0.0
            for c in _bullish_hits:
                _ic_val = float(_factor_ic.get(c, {}).get("ic", 0))
                _n_val = int(_factor_ic.get(c, {}).get("n", 0))
                if _n_val >= 30:  # 只有统计显著的因子才参与
                    _net_score += _ic_val
            for c in _bearish_hits:
                _ic_val = float(_factor_ic.get(c, {}).get("ic", 0))
                _n_val = int(_factor_ic.get(c, {}).get("n", 0))
                if _n_val >= 30:
                    _net_score -= _ic_val

            # 2026-07-04: IC加权方向与LLM方向冲突时→SKIP (原"记录但放行"导致insider_net_sell出现在BUY中)
            # Karpathy做减法: 已有IC加权逻辑算出了正确冲突信号, 只需把"放行"改"拦截"
            if _net_score > 0.05 and direction == "SELL_SHORT":
                _filter_audit["direction_flip"] += 1
                logger.info(
                    f"[playbook] {symbol} IC加权方向(LONG)与LLM方向(SELL_SHORT)冲突 "
                    f"(net_ic={_net_score:+.2f}) → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"IC方向冲突: 因子净IC={_net_score:+.2f}指向LONG但LLM=SHORT"
                ))
                continue
            elif _net_score < -0.05 and direction == "BUY":
                _filter_audit["direction_flip"] += 1
                logger.info(
                    f"[playbook] {symbol} IC加权方向(SHORT)与LLM方向(BUY)冲突 "
                    f"(net_ic={_net_score:+.2f}) → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"IC方向冲突: 因子净IC={_net_score:+.2f}指向SHORT但LLM=BUY"
                ))
                continue
        
        # ── 2026-07-10: 负 edge 因子硬拦截 (数据驱动, OKR factor 表) ──
        # 2026-07-11 停用 (audit 结论, 见 _FACTOR_BLACKLIST_ENABLED):
        #   该黑名单证据 99% 来自旧 config v1; v2 下 news_positive 反而净正(+$70/35笔),
        #   pe/rsi v2 样本≈零 → 基于旧策略给新策略定黑名单=打影子, 暂停用。
        # 规则: 当【全部】触发因子都在黑名单时 SKIP (纯负 edge 信号).
        _NEG_EDGE_FACTORS = {"pe_below_industry", "news_positive", "rsi_oversold"}
        if _FACTOR_BLACKLIST_ENABLED and _conditions:
            _non_neg = [c for c in _conditions if c not in _NEG_EDGE_FACTORS]
            if not _non_neg:
                _filter_audit["neg_edge_factor_skip"] += 1
                logger.info(
                    f"[playbook] {symbol} {direction} 纯负edge因子 {_conditions} "
                    f"(WR16-25%) \u2192 SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol, direction="SKIP",
                    skip_reason=f"负edge因子拦截: 唯一触发={_conditions} (胜率16-25%)"
                ))
                continue

        # ── 2026-07-11: 配对级负 edge 闸门 (数据驱动, playbook_factor_stats) ──
        # 2026-07-11 停用 (audit 结论): 同单因子黑名单, 99.5% 亏损证据来自旧 v1,
        #   v2 下这些配对几乎不交易/打平 → 暂停用, 待 v2 足量样本后再决定. 见 _FACTOR_BLACKLIST_ENABLED.
        # 规则: 触发因子的任一配对在 playbook_factor_stats 中 verdict=negative 且 n>=8 且
        #   total_pnl<0 → SKIP. 用配对级 verdict 避免误伤 (macd+pe 56%WR +$97 / news+volume +$286 放行).
        if _FACTOR_BLACKLIST_ENABLED and _conditions and len(_conditions) >= 2:
            _neg_pairs = _get_negative_factor_pairs()
            if _neg_pairs:
                _hit_pair = None
                for _pi in range(len(_conditions)):
                    for _pj in range(_pi + 1, len(_conditions)):
                        if frozenset((_conditions[_pi], _conditions[_pj])) in _neg_pairs:
                            _hit_pair = (_conditions[_pi], _conditions[_pj])
                            break
                    if _hit_pair:
                        break
                if _hit_pair:
                    _filter_audit["neg_pair_skip"] = _filter_audit.get("neg_pair_skip", 0) + 1
                    logger.info(
                        f"[playbook] {symbol} {direction} 负edge配对 {_hit_pair} "
                        f"(实证 verdict=negative/亏损) → SKIP"
                    )
                    instructions.append(Instruction(
                        symbol=symbol, direction="SKIP",
                        skip_reason=f"负edge配对拦截: {_hit_pair[0]}+{_hit_pair[1]} (实证亏损组合)"
                    ))
                    continue

        # 2026-06-26: Horizon 选择 — 基于个股阶段特征选择 short/long
        try:
            _horizon_decision = _get_horizon_selector().select(sr, regime_context)
            _horizon = _horizon_decision.horizon
            _horizon_confidence = _horizon_decision.confidence
            _horizon_reasoning = _horizon_decision.reasoning
        except Exception as e:
            logger.warning(f"[playbook] {symbol} Horizon 选择失败: {e}, 默认 short")
            _horizon = "short"
            _horizon_confidence = 0.0
            _horizon_reasoning = {"source": "error_fallback", "error": str(e)}

        # 构造 LLM decision dict (compute_instruction_values 期望的格式)
        decision = {
            "symbol": symbol,
            "direction": direction,
            "selected_conditions": list(factors.keys()) if factors else [],
            "thesis_template": _select_thesis_template(factors, direction),
        }

        # 数值计算 (确定性, LLM 不参与)
        # 2026-07-09: risk_pct从配置读取(原硬编码1.0)
        from app.playbook.config import get_playbook_config as _gpc
        _risk_pct = float(_gpc().get("risk_pct", 1.0))
        values = compute_instruction_values(
            decision=decision,
            data=data,
            total_equity=total_equity,
            risk_pct=_risk_pct,
            regime_multiplier=regime_mult,
            horizon=_horizon,
            regime_context=regime_context,
        )

        if not values or not values.get("entry_price"):
            _filter_audit["no_entry_price"] += 1
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="价格缺失, 无法计算数值"
            ))
            continue

        # 2026-06-25 P0 修复: 盘前 last_price 与 bid/ask 偏离 guardrail.
        # 若 last_traded_price 与真实可成交价偏离 >2%, 说明信号生成后价格已大幅异动,
        # 原条件 (如 rsi_oversold) 可能失效, 应跳过.
        _last_price = float(data.technical.price or 0)
        _entry = float(values.get("entry_price", 0))
        if _last_price > 0 and _entry > 0:
            _price_gap = abs(_entry - _last_price) / _last_price
            if _price_gap > 0.02:
                _filter_audit["price_gap_skip"] += 1
                logger.warning(
                    f"[playbook] {symbol} 价格偏离过大: last_price={_last_price:.2f} "
                    f"entry={_entry:.2f} 偏离={_price_gap:.2%} → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason=f"价格偏离过大 ({_price_gap:.1%}), 信号失效",
                ))
                continue
        
        # 2026-07-10: bridge候选用LLM分析confidence, 非bridge用数据质量confidence
        # 根因: compute_confidence()是纯数据质量分(数据维度ok数×12+条件验证×28,封顶90)
        #   → 数据完整时永远=90, 与分析确信度无关
        #   → confidence=90但胜率31%, 因为"90"是数据完整度不是确信度
        # 修复: bridge候选(deep_analysis)用vote_result.final_confidence(已含LLM定性+辩论调整)
        #       非bridge保持compute_confidence不变
        _is_bridge = (vote_result.get("strategy_id", "") == "deep_analysis" 
                      or str(vote_result.get("decision_id", "")).startswith("DA"))
        if _is_bridge:
            _llm_conf = float(vote_result.get("final_confidence", 0) or 0)
            confidence = int(_llm_conf * 100)  # 0.0-1.0 → 0-100
        else:
            confidence = compute_confidence(data, decision["selected_conditions"], factors)

        # 2026-07-02: 跨日方向翻转检查 — 同标的24h内方向翻转需更高confidence
        # 根因: LLM方向翻转率34%, ORCL 6/23 SHORT赚→6/24 LONG亏(conf=90但方向错)
        # 方案: 查前一天同标的方向, 翻转时confidence门槛+15
        try:
            from datetime import timedelta as _td, datetime as _dt
            _today_date = _today_et_str()
            _prev_date = (_dt.now(_now_et()) - _td(days=1)).date().isoformat()
            # 跳过周末
            while _prev_date < _today_date:
                _prev_dt = _dt.strptime(_prev_date, "%Y-%m-%d")
                if _prev_dt.weekday() < 5:
                    break
                _prev_date = (_prev_dt - _td(days=1)).date().isoformat()
            from app.playbook.db import PlaybookDB as _PDB
            _pdb = _PDB()
            _prev_inst = _pdb.get_instruction(_prev_date, symbol)
            if _prev_inst and _prev_inst.get("direction") in ("BUY", "SELL_SHORT"):
                _prev_dir = _prev_inst["direction"]
                if _prev_dir != direction:
                    # 方向翻转 — 标记, 在confidence门槛检查中提高门槛
                    _filter_audit["direction_flip"] += 1
                    logger.info(
                        f"[playbook] {symbol} 跨日方向翻转: {_prev_dir}→{direction} "
                        f"(prev={_prev_date}), confidence门槛+15"
                    )
                    _direction_flipped = True
                else:
                    _direction_flipped = False
            else:
                _direction_flipped = False
        except Exception as _flip_err:
            logger.debug(f"[playbook] {symbol} 方向翻转检查失败(忽略): {_flip_err}")
            _direction_flipped = False

        # 2026-06-30: confidence门槛从strategy_registry读取（原从playbook config读）
        # 数据修复后: LONG IC=+0.238(强), SHORT IC=-0.051(弱) → 不再需要方向不对称门槛
        # 默认 BUY=70, SHORT=70 (对称), 可在registry的direction_filter中覆盖
        try:
            from app.core.strategy_registry import get_strategy_config
            _sr_cfg = get_strategy_config(vote_result.get("strategy_id", ""))
            _buy_min_conf = int(_sr_cfg["direction_filter"].get("BUY_min_confidence", 70))
            _short_min_conf = int(_sr_cfg["direction_filter"].get("SHORT_min_confidence", 70))
        except Exception:
            _buy_min_conf = 70
            _short_min_conf = 70

        _dir_conf_map = {"BUY": _buy_min_conf, "SELL_SHORT": _short_min_conf}
        _min_conf_for_dir = _dir_conf_map.get(direction, 65)

        # P0 (2026-07-11): edge 影子闸门 — 记录"数据驱动 edge 判定 vs 实盘置信度闸门"对照
        # 纯影子: 不改实盘指令; 待影子数据验证 edge 闸门更优后, 再经人工/灰度审批切换
        try:
            from app.decision.edge_shadow_gate import get_edge_shadow_gate
            get_edge_shadow_gate().evaluate(
                symbol, direction, float(confidence),
                live_pass=(confidence >= _min_conf_for_dir),
            )
        except Exception:
            pass  # 非关键路径

        if confidence < _min_conf_for_dir:
            _filter_audit.setdefault("low_confidence_skip", 0)
            _filter_audit["low_confidence_skip"] += 1
            logger.info(
                f"[playbook] {symbol} {direction} confidence={confidence:.0f} "
                f"< 门槛={_min_conf_for_dir} → SKIP"
            )
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason=f"{direction} confidence {confidence:.0f} < {_min_conf_for_dir}"
            ))
            continue
        
        data_quality = {
            "fundamental": data.fundamental.quality,
            "technical": data.technical.quality,
            "insider": data.insider.quality,
            "news": data.news.quality,
            "earnings": data.earnings.quality,
            "sentiment": data.sentiment.quality,
        }
        
        thesis = build_thesis(decision["thesis_template"], decision["selected_conditions"])
        
        risks = []
        ok_count = sum(1 for q in data_quality.values() if q == "ok")
        if ok_count < 4:
            risks.append(f"数据不完整 ({ok_count}/6 维度 ok)")
        if data.earnings.days_until is not None and data.earnings.days_until < 14:
            risks.append(f"财报 {data.earnings.days_until} 天后")
        if values.get("atr_source", "").startswith("FALLBACK"):
            risks.append("ATR 缺失, 使用 fallback 止损")
        
        inst = Instruction(
            symbol=symbol,
            direction=direction,
            entry_price=values.get("entry_price", 0),
            stop_loss=values.get("stop_loss", 0),
            take_profit=values.get("take_profit", 0),
            position_size=values.get("position_size", 0),
            risk_amount=values.get("risk_amount", 0),
            risk_reward=values.get("risk_reward", 0),
            confidence=confidence,
            thesis=thesis,
            conditions=decision["selected_conditions"],
            data_quality=data_quality,
            risks=risks,
            sector=data.fundamental.industry or "",
            entry_price_source=values.get("entry_price_source", ""),
            atr_source=values.get("atr_source", ""),
            decision_id=decision_id,
            horizon=_horizon,
            horizon_confidence=_horizon_confidence,
            horizon_reasoning=_horizon_reasoning,
            # 2026-07-11 Stage 1: 保留 DA 失效条件 + 真实论点 (bridge 缺失时为空串)
            invalidation=vote_result.get("invalidation", "") or "",
            da_thesis=vote_result.get("thesis", "") or "",
        )
        # 方向翻转时降仓50% — 同guardrail方式, 不SKIP信号只降风险敞口
        if _direction_flipped:
            _orig_size = inst.position_size or 0
            if _orig_size > 0:
                inst.position_size = max(1, int(_orig_size * 0.5))
                inst.skip_reason = f"跨日方向翻转 降仓50% (原{_orig_size}→{inst.position_size})"
        instructions.append(inst)
        
        logger.info(
            f"[playbook] {symbol} vote→instruction: {vote_direction}→{direction} "
            f"strength={vote_result.get('final_strength', '?')} "
            f"consensus={vote_result.get('consensus_level', '?')} "
            f"decision_id={decision_id}"
        )

    # ── 过滤审计日志输出 (DEB-20260628) ──
    _filter_audit["final_instructions"] = sum(1 for i in instructions if i.direction != "SKIP")
    _skip_count = _filter_audit["total_input"] - _filter_audit["final_instructions"]
    logger.info(
        f"[playbook] 过滤审计: input={_filter_audit['total_input']} → "
        f"instructions={_filter_audit['final_instructions']} skips={_skip_count} | "
        f"breakdown: mkt_data_none={_filter_audit['market_data_none']} "
        f"vote_fail={_filter_audit['vote_failed']} neutral={_filter_audit['vote_neutral']} "
        f"buy_str_skip={_filter_audit['buy_low_strength_skip']} "
        f"buy_std_skip={_filter_audit['buy_high_std_skip']} "
        f"low_conf_skip={_filter_audit['low_confidence_skip']} "
        f"buy_filter_disabled={_filter_audit['buy_macd_ma50_disabled']} "
        f"dir_flip={_filter_audit['direction_flip']} "
        f"dir_conflict={_filter_audit['direction_conflict_skip']} "
        f"price_gap={_filter_audit['price_gap_skip']} "
        f"no_entry={_filter_audit['no_entry_price']}"
    )

    # 组合级 guardrail: 方向偏斜 / sector 集中度 / 最低 R:R
    instructions = _apply_portfolio_guardrails(instructions)

    return instructions


def _select_thesis_template(factors: Dict[str, Any], direction: str) -> str:
    """根据触发因子选择 thesis 模板"""
    if not factors:
        return "momentum_continuation" if direction == "BUY" else "mean_reversion"

    factor_keys = set(factors.keys())

    if "insider_cluster_buy" in factor_keys or "insider_net_buy" in factor_keys:
        return "insider_confidence"
    if "macd_golden_cross" in factor_keys:
        return "breakout" if direction == "BUY" else "mean_reversion"
    if any("rsi" in k for k in factor_keys):
        return "trend_pullback" if direction == "BUY" else "value_reversal"

    return "momentum_continuation" if direction == "BUY" else "mean_reversion"


def _apply_portfolio_guardrails(
    instructions: List[Any],
    *,
    max_sector_pct: float = 0.40,
    max_direction_skew: float = 0.80,  # 2026-06-30: 0.65→0.80, 允许cautious regime偏SHORT
    min_rr: float = 1.0,  # 2026-07-16: 0.8→1.0, R:R≥1 保证正期望 (还需胜率>50%)
) -> List[Any]:
    """组合级风险 guardrail (2026-06-25).

    2026-06-30: 放宽 max_direction_skew 和 min_rr.
    根因: cautious regime 下 3 模型一致看空 → 86% SHORT 被截杀;
          rr_ratio=1.0 < min_rr=1.5 → 全部指令被杀.
    原则: guardrail 是安全网, 不是信号杀手. 真正的方向风险由仓位管理控制.
    """
    from collections import Counter

    # 先过滤掉 SKIP / 无方向 / R:R 不达标的
    active = []
    for inst in instructions:
        if getattr(inst, "direction", None) in ("SKIP", None, ""):
            active.append(inst)
            continue
        rr = getattr(inst, "risk_reward", 0) or 0
        if rr < min_rr:
            logger.warning(
                f"[playbook guardrail] {inst.symbol} R:R {rr:.2f} < {min_rr}, 转为 SKIP"
            )
            inst.direction = "SKIP"
            inst.skip_reason = f"R:R {rr:.2f} < 最低 {min_rr}"
        active.append(inst)

    # 只统计非 SKIP 的有效指令
    valid = [i for i in active if i.direction in ("BUY", "SELL_SHORT")]
    if len(valid) <= 1:
        return active

    total = len(valid)

    # 方向偏斜
    buy_count = sum(1 for i in valid if i.direction == "BUY")
    short_count = total - buy_count
    majority_pct = max(buy_count, short_count) / total if total > 0 else 0

    # 如果方向偏斜超过阈值, 砍掉低 conv 的多数方信号直到平衡
    if majority_pct > max_direction_skew:
        majority_dir = "BUY" if buy_count > short_count else "SELL_SHORT"
        logger.warning(
            f"[playbook guardrail] 方向偏斜 {majority_pct:.0%} 超限, "
            f"{_trim_majority_direction(valid, majority_dir, max_direction_skew)} 条信号被降级为 SKIP"
        )

    # Sector 集中度 (仅当有 sector 信息时)
    sectors = Counter(getattr(i, "sector", "") or "Unknown" for i in valid)
    for sector, count in sectors.items():
        if count / total > max_sector_pct:
            logger.warning(
                f"[playbook guardrail] sector {sector} 占比 {count/total:.0%} 超限, "
                f"{_trim_majority_sector(valid, sector, max_sector_pct)} 条信号被降级为 SKIP"
            )

    return active


def _trim_majority_direction(valid: List[Any], majority_dir: str, target_skew: float) -> int:
    """多数方信号降仓, 控制方向风险.
    
    2026-06-30 根因修复: SKIP → position_size *= 0.5
    原则: guardrail 是安全网, 不是信号杀手. 方向风险由仓位管理控制.
    多数方信号全部降仓50%, 保留信号产出率.
    """
    majority = [i for i in valid if i.direction == majority_dir]
    if not majority:
        return 0
    trimmed = 0
    for inst in majority:
        _orig = getattr(inst, "position_size", 0) or 0
        if _orig > 0:
            inst.position_size = max(1, int(_orig * 0.5))
            inst.skip_reason = f"方向偏斜 guardrail 降仓50% (原{_orig}→{inst.position_size})"
            trimmed += 1
    return trimmed


def _trim_majority_sector(valid: List[Any], sector: str, target_pct: float) -> int:
    """将指定 sector confidence 最低的信号降级, 直到占比 ≤ target_pct."""
    trimmed = 0
    from collections import Counter
    while True:
        active_valid = [i for i in valid if i.direction in ("BUY", "SELL_SHORT")]
        total = len(active_valid)
        sector_count = Counter(
            getattr(i, "sector", "") or "Unknown" for i in active_valid
        ).get(sector, 0)
        if total == 0 or sector_count / total <= target_pct:
            break
        candidates = [
            i for i in active_valid
            if (getattr(i, "sector", "") or "Unknown") == sector
        ]
        candidates.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        candidates[0].direction = "SKIP"
        candidates[0].skip_reason = f"sector 集中度 guardrail 降级 ({sector})"
        trimmed += 1
    return trimmed


# ── 入口 1: 盘前生成 (regime → 收集 → 筛选 → VotingCoordinator → 入库 → 飞书) ──
def run_playbook_generate(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘前生成今日交易指令 (Layer 0 + Layer 1)。

    幂等: scheduler._playbook_generated_date == today 即跳过 (force=True 可重跑)。
    节假日跳过。任何异常都 catch 并 log, 不向上抛 (保证不影响 scheduler 主循环)。
    """
    try:
        today = _today_et_str()

        if not _playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘前生成 (上线闸门关闭)")
            return None

        if not force:
            if not _is_trading_day_today():
                logger.debug(f"[playbook] {today} 非交易日, 跳过盘前生成")
                return None
            # 2026-07-09: 生成时间窗口限制 — ET 09:00后才生成
            # 根因: 原无时间窗口, PRE_MARKET(04:00-09:30)第一次循环就触发生成
            # → ET 04:xx 生成 → 用昨日数据, 5小时gap信号衰减
            # 改为 ET 09:00 生成 → pre-market价格已反映隔夜消息, 更接近开盘reality
            from datetime import datetime as _dt
            import pytz as _pz
            _now_et = _dt.now(_pz.timezone('US/Eastern'))
            _generate_hour = _now_et.hour
            if _generate_hour < 9:
                logger.debug(
                    f"[playbook] {today} ET {_now_et.strftime('%H:%M')} < 09:00, "
                    f"等待盘前数据成熟后生成 (目标: ET 09:00+)"
                )
                return None
            # 2026-07-09: 如果voting_coordinator是NullVotingAdapter, 跳过生成
            # 根因: NullVotingAdapter.vote()返回None → 所有候选SKIP → save_instruction
            # 会UPSERT覆盖bridge已写的BUY/SELL_SHORT instruction为SKIP, 破坏bridge候选
            _vc = getattr(scheduler, "voting_coordinator", None)
            if _vc is not None and type(_vc).__name__ == "NullVotingAdapter":
                logger.info(
                    f"[playbook] {today} voting_coordinator=NullVotingAdapter, "
                    f"跳过生成 (bridge候选已由deep_analysis skill直写DB, 不需要VotingCoordinator)"
                )
                return None
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB，不再依赖内存属性
            # 内存属性在进程重启后丢失 → 重启后重新执行 → 覆盖当天数据
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                existing = db.get_instructions_by_date(today)
                if existing and len(existing) > 0:
                    logger.debug(f"[playbook] {today} DB 已有 {len(existing)} 条指令，跳过生成 (幂等)")
                    scheduler._playbook_generated_date = today  # 同步内存标志
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")
                if getattr(scheduler, "_playbook_generated_date", None) == today:
                    logger.debug(f"[playbook] {today} 已生成 (内存标志), 跳过")
                    return None

        logger.info(f"[playbook] 🚀 盘前生成开始 (date={today})")

        from app.playbook.db import PlaybookDB
        from app.playbook.market_regime import MarketRegimeGate
        from app.playbook.screener import PlaybookScreener

        db = PlaybookDB()
        db.ensure_tables()

        # Layer 0: 市场环境门禁
        regime_ctx = MarketRegimeGate().assess()
        db.save_regime_log({
            "date": today,
            "regime": regime_ctx.regime.value,
            "vix": regime_ctx.vix,
            "spy_price": regime_ctx.spy_price,
            "spy_ma20": regime_ctx.spy_ma20,
            "spy_ma50": regime_ctx.spy_ma50,
            "score": regime_ctx.score,
            "notes": f"top_n={regime_ctx.top_n}, pos_mult={regime_ctx.position_multiplier}",
        })
        logger.info(f"[playbook] Layer0 regime={regime_ctx.regime.value} VIX={regime_ctx.vix}")

        # Layer 1: 数据收集 + 规则筛选 (不截断, 全量通过门槛的候选)
        watchlist = _get_watchlist(scheduler)
        if not watchlist:
            logger.warning("[playbook] watchlist 为空, 跳过生成")
            scheduler._playbook_generated_date = today  # 标记已尝试, 避免空跑循环
            return {"date": today, "instructions": 0, "reason": "empty_watchlist"}

        collector = _build_data_collector()
        screener = PlaybookScreener(collector)
        positions = _get_positions_as_dicts(scheduler)
        screen_results = screener.screen(
            watchlist=watchlist,
            regime=regime_ctx.regime,
            positions=positions,
        )
        logger.info(f"[playbook] Layer1 筛选出 {len(screen_results)} 只候选 (无截断)")

        if not screen_results:
            logger.info("[playbook] 无候选通过质量门槛, 跳过生成")
            scheduler._playbook_generated_date = today
            return {"date": today, "instructions": 0, "reason": "no_candidates_passed_filter"}

        # Layer 2: VotingCoordinator 统一方向决策
        # 信号源唯一化: Playbook 消费 VotingCoordinator 的多模型投票结果,
        # 不再跑独立 LLM — 消除平行管线, 方向决策统一归因.
        voting_coordinator = _get_voting_coordinator(scheduler)
        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        instructions = _build_instructions_from_votes(
            screen_results=screen_results,
            voting_coordinator=voting_coordinator,
            regime_context=regime_ctx,
            total_equity=_get_total_equity(scheduler),
            data_router=data_router,
            today=today,
        )

        # 入库
        saved = 0
        for inst in instructions:
            try:
                inst_dict = _instruction_to_db_dict(inst, regime_ctx, today)
                db.save_instruction(inst_dict)
                saved += 1
            except Exception as e:
                logger.warning(f"[playbook] 指令入库失败 {getattr(inst, 'symbol', '?')}: {e}")

        logger.info(f"[playbook] ✅ 生成完成: {saved}/{len(instructions)} 条指令入库")

        # 2026-07-16: 盘前推送已移除内联触发, 统一归 run_playbook_morning_push 独占。
        # 根因: 内联走 sync=False, 而独立任务走 sync=True (绕过 PushHub dedup) →
        #       两条路径都发 → 用户收到两次。独立任务有持久化幂等+重试, 足以覆盖送达。

        scheduler._playbook_generated_date = today
        return {"date": today, "instructions": saved, "regime": regime_ctx.regime.value}

    except Exception as e:
        logger.exception(f"[playbook] 盘前生成异常 (已隔离, 不影响 scheduler): {e}")
        return {"error": str(e)}


_MORNING_PUSH_STATE = "/opt/ftseek/FTSeek-US/data/playbook_push_state.json"


def run_playbook_morning_push(scheduler=None) -> Optional[bool]:
    """独立的盘前指令推送任务 — 不依赖生成内联触发, 每日幂等(持久化), 失败下轮重试.

    背景 (2026-07-11): 原推送仅在 run_playbook_generate 末尾内联触发, 但该函数
    每日幂等早退 → 生成后再调到不了推送行; 且走异步 PushHub, 重启 churn 中易丢失,
    导致 GOOG/NU 等指令从未送达。本任务在 PRE_MARKET/TRADING 每轮调用:
      - 已成功推送(持久化标记) → 跳过, 不重复;
      - PlaybookDB 无当日指令 → 跳过, 下轮重试;
      - 同步发送(真实送达确认) → 成功才落幂等标记, 失败下轮自动重试。
    """
    import json as _json
    from datetime import datetime as _dt
    try:
        from app.utils.timezone import now_et
        today = now_et().date().isoformat()
    except Exception:
        today = _dt.now().date().isoformat()

    try:
        with open(_MORNING_PUSH_STATE) as f:
            state = _json.load(f)
    except Exception:
        state = {}
    morning = state.setdefault("morning", {})
    if morning.get(today):
        return None  # 今日已推送, 幂等跳过

    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.feishu_push import PlaybookPusher
        db = PlaybookDB()
        instructions = db.get_instructions_by_date(today)
        if not instructions:
            return None  # 尚无指令, 下轮重试

        ok = PlaybookPusher(db).push_morning_instructions(today, sync=True)
        if ok:
            morning[today] = True
            # 仅保留最近 30 天, 防止文件膨胀
            if len(morning) > 30:
                for _k in sorted(morning)[:-30]:
                    morning.pop(_k, None)
            try:
                with open(_MORNING_PUSH_STATE, "w") as f:
                    _json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception as _we:
                logger.warning(f"[playbook] 推送幂等状态写入失败: {_we}")
            logger.info(f"[playbook] ✅ 盘前指令推送成功 ({today}, {len(instructions)}条), 已标记幂等")
        else:
            logger.warning(f"[playbook] ⚠️ 盘前指令推送未成功 ({today}), 下轮重试")
        return ok
    except Exception as e:
        logger.warning(f"[playbook] 盘前指令推送任务异常 (已隔离): {e}")
        return None


def _compute_key_levels(data: Any) -> Dict[str, Any]:
    """从 PlaybookData 计算支撑/阻力位. 纯Python, 零LLM.

    支撑: 52周低点 + MA20 + MA50(如果有)
    阻力: 52周高点 + 前高(如果有)
    """
    try:
        fund = getattr(data, "fundamental", None)
        high_52w = getattr(fund, "high_52w", None) or 0 if fund else 0
        low_52w = getattr(fund, "low_52w", None) or 0 if fund else 0

        # 从 technical 维度获取 MA
        tech = getattr(data, "technical", None)
        ma20 = getattr(tech, "ma20", None) or 0 if tech else 0
        ma50 = getattr(tech, "ma50", None) or 0 if tech else 0

        supports = []
        resistances = []
        if low_52w > 0:
            supports.append(round(low_52w, 2))
        if ma50 > 0:
            supports.append(round(ma50, 2))
        if ma20 > 0:
            supports.append(round(ma20, 2))
        if high_52w > 0:
            resistances.append(round(high_52w, 2))

        # 去重+排序
        supports = sorted(set(supports))
        resistances = sorted(set(resistances))

        return {"support": supports, "resistance": resistances}
    except Exception:
        return {"support": [], "resistance": []}


def _persist_signal_context(
    date: str, symbol: str, data: Any, vote_result: Optional[Dict] = None,
) -> None:
    """持久化信息层上下文 (PlaybookData 6 维度 + Deepulse + LLM reasoning).

    在 pipeline 生成时调用, 把此前纯内存的 PlaybookData 落盘到 playbook_signal_context 表.
    单步异常隔离, 不影响主流程.
    """
    try:
        from app.playbook.db import PlaybookDB
        from dataclasses import asdict, is_dataclass
        import json as _json

        db = PlaybookDB()

        # PlaybookData → dict (安全提取, dataclass 字段可能 None)
        ctx: Dict[str, Any] = {"date": date, "symbol": symbol}

        # 新闻
        news = getattr(data, "news", None)
        if news:
            ctx["news_headlines"] = list(getattr(news, "headlines", []) or [])[:5]
            ctx["news_sentiment"] = getattr(news, "sentiment_score", 0) or 0

        # 内部人交易
        ins = getattr(data, "insider", None)
        if ins:
            ctx["insider_signal"] = getattr(ins, "signal", "neutral")
            ctx["insider_cluster_score"] = getattr(ins, "cluster_score", 0) or 0
            ctx["insider_net_shares"] = getattr(ins, "net_shares_30d", 0) or 0
            ctx["insider_cluster_buyers"] = list(getattr(ins, "cluster_buyers", []) or [])

        # 财报
        ern = getattr(data, "earnings", None)
        if ern:
            ctx["earnings_next_date"] = getattr(ern, "next_date", None)
            ctx["earnings_days_until"] = getattr(ern, "days_until", None)
            ctx["earnings_beat_rate"] = getattr(ern, "beat_rate", 0) or 0

        # 基本面
        fund = getattr(data, "fundamental", None)
        if fund:
            ctx["pe"] = getattr(fund, "pe", None)
            ctx["market_cap"] = getattr(fund, "market_cap", None)
            ctx["industry"] = getattr(fund, "industry", None)
            ctx["revenue_growth_yoy"] = getattr(fund, "revenue_growth_yoy", None)
            ctx["roe"] = getattr(fund, "roe", None)
            ctx["debt_to_equity"] = getattr(fund, "debt_to_equity", None)
            ctx["high_52w"] = getattr(fund, "high_52w", None)
            ctx["low_52w"] = getattr(fund, "low_52w", None)

        # Deepulse 主题雷达 (从 theme_context.json 读取 per-symbol)
        try:
            from pathlib import Path
            root = Path(__file__).resolve().parent.parent.parent
            tc_path = root / "data" / "theme_context.json"
            if tc_path.exists():
                with open(tc_path, encoding="utf-8") as f:
                    tc = _json.load(f)
                # 2026-07-03 fix: theme_context.json 的 symbols 在顶层, 不在 domains[*].per_symbol
                bare_sym = symbol.replace("US.", "")
                tc_symbols = tc.get("symbols") or {}
                if bare_sym in tc_symbols:
                    sym_val = tc_symbols[bare_sym]
                    ctx["theme_momentum"] = sym_val.get("net_momentum")
                    ctx["theme_wind"] = sym_val.get("net_wind")
                    ctx["theme_narratives"] = list(sym_val.get("narratives", []) or [])
                # 也从 domains 补充全局主题方向 (per-domain momentum)
                if "theme_momentum" not in ctx:
                    # 用该 symbol 所属 domain 的全局 momentum 作为 fallback
                    for sym_dom in (tc_symbols.get(bare_sym, {}).get("domains") or []):
                        dom_name = sym_dom.get("domain", "")
                        dom_data = (tc.get("domains") or {}).get(dom_name, {})
                        if dom_data:
                            ctx["theme_momentum"] = dom_data.get("theme_momentum")
                            ctx["theme_wind"] = dom_data.get("sentiment_direction")
                            ctx["theme_narratives"] = list(dom_data.get("key_narratives", []) or [])[:3]
                            break
        except Exception:
            pass

        # LLM reasoning (从 decisions.db 关联)
        if vote_result:
            decision_id = vote_result.get("decision_id", "")
            if decision_id:
                try:
                    import sqlite3 as _sqlite3
                    root = Path(__file__).resolve().parent.parent.parent
                    dec_db = root / "data" / "decisions.db"
                    if dec_db.exists():
                        conn = _sqlite3.connect(str(dec_db))
                        conn.row_factory = _sqlite3.Row
                        rows = conn.execute(
                            "SELECT model_id, direction, strength, confidence, reasoning "
                            "FROM model_votes WHERE decision_id = ?",
                            (decision_id,),
                        ).fetchall()
                        conn.close()
                        ctx["llm_reasoning"] = [
                            {"model_id": r["model_id"], "direction": r["direction"],
                             "strength": r["strength"], "confidence": r["confidence"],
                             "reasoning": r["reasoning"] or ""}
                            for r in rows
                        ]
                except Exception:
                    pass

        # 原始数据快照 (完整 PlaybookData → JSON, 诊断用)
        # 2026-07-04: brief字段也编码到raw_data_json, 不加DB新列 (ponytail)
        try:
            if is_dataclass(data):
                _raw = asdict(data)
            else:
                _raw = {}
        except Exception:
            _raw = {}

        # 2026-07-04: Python计算关键位 (纯数值, 不依赖LLM)
        _key_levels = _compute_key_levels(data)
        _raw["key_levels"] = _key_levels

        # 2026-07-04: 从LLM reasoning中提取brief字段 (已由_parse_response编码到reasoning)
        # reasoning格式: "brief text | {json}" — 解析JSON部分
        try:
            _reasoning_str = ""
            if vote_result:
                _reasoning_str = str(vote_result.get("reasoning", "") or "")
            elif ctx.get("llm_reasoning"):
                _first = ctx["llm_reasoning"][0] if isinstance(ctx["llm_reasoning"], list) else {}
                _reasoning_str = str(_first.get("reasoning", "") or "") if isinstance(_first, dict) else ""
            if "|" in _reasoning_str:
                _json_part = _reasoning_str.rsplit("|", 1)[-1].strip()
                _brief_data = _json.loads(_json_part) if _json_part.startswith("{") else {}
                for _bf in ("brief_conclusion", "brief_reasons", "brief_risks",
                            "entry_strategy", "key_support", "key_resistance", "stop_trigger"):
                    if _bf in _brief_data:
                        _raw[_bf] = _brief_data[_bf]
        except Exception:
            pass

        ctx["raw_data"] = _raw

        db.save_signal_context(ctx)
    except Exception as e:
        logger.warning(f"[playbook] signal_context 持久化失败 {symbol}: {e}")


def _instruction_to_db_dict(inst, regime_ctx, today: str) -> Dict[str, Any]:
    """Instruction dataclass → save_instruction 期望的 dict (字段映射, spec 6.1)"""
    conditions = list(getattr(inst, "conditions", []) or [])
    risks = list(getattr(inst, "risks", []) or [])
    dq = getattr(inst, "data_quality", {}) or {}
    ok_dims = sum(1 for v in dq.values() if v == "ok")
    return {
        "date": today,
        "symbol": inst.symbol,
        "direction": inst.direction,
        "skip_reason": getattr(inst, "skip_reason", "") or "",
        "entry_price": inst.entry_price,
        "stop_loss": inst.stop_loss,
        "take_profit": inst.take_profit,
        "position_size": inst.position_size,
        "risk_amount": getattr(inst, "risk_amount", 0.0),
        "risk_reward": getattr(inst, "risk_reward", 0.0),
        "confidence": inst.confidence,
        "conditions": conditions,
        "thesis": getattr(inst, "thesis", "") or "",
        "sector": getattr(inst, "sector", "") or "",
        "risks": risks,
        "market_regime": regime_ctx.regime.value,
        "vix_at_generation": regime_ctx.vix,
        "spy_trend_at_generation": "above_ma20" if regime_ctx.spy_ma20 >= regime_ctx.spy_ma50 else "below_ma20",
        "data_quality": dq,
        "ok_dimensions": ok_dims,
        "matched_patterns": [],
        "pattern_verdict": None,
        "raw_instruction": {
            "entry_price_source": getattr(inst, "entry_price_source", ""),
            "atr_source": getattr(inst, "atr_source", ""),
            # 2026-07-11 Stage 1: DA 真实论点存入 raw json (不占独立列, 不覆盖模板 thesis)
            "da_thesis": getattr(inst, "da_thesis", "") or "",
        },
        "decision_id": getattr(inst, "decision_id", "") or "",
        "invalidation": getattr(inst, "invalidation", "") or "",
        "horizon": getattr(inst, "horizon", "short"),
        "horizon_confidence": getattr(inst, "horizon_confidence", 0.0),
        "horizon_reasoning": getattr(inst, "horizon_reasoning", {}),
    }


def _run_ic_backfill_only(scheduler, today: str) -> None:
    """ponytail: 幂等跳过daily_tracking时仍跑IC backfill — Bug A修复."""
    try:
        from app.playbook.ic_tracker import backfill_forward_returns
        from app.playbook.db import PlaybookDB
        db = PlaybookDB()
        market_data = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if market_data:
            backfilled = backfill_forward_returns(db, market_data, today)
            logger.info(f"[playbook] IC backfill (幂等路径): {backfilled} 条")
    except Exception as e:
        logger.debug(f"[playbook] IC backfill (幂等路径) 失败: {e}")


# ── 入口 2: 盘后追踪 (shadow PnL + MFE/MAE + 因子更新 + 飞书) ──────────
def run_playbook_track(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘后追踪 (Layer 3 每日盘后)。

    幂等: scheduler._playbook_tracked_date == today 即跳过 (force=True 可重跑)。
    """
    try:
        today = _today_et_str()

        if not _playbook_enabled():
            logger.debug("[playbook] playbook.enabled=false, 跳过盘后追踪 (上线闸门关闭)")
            return None

        if not force:
            if not _is_trading_day_today():
                logger.debug(f"[playbook] {today} 非交易日, 跳过盘后追踪")
                return None
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB
            # 2026-06-23 修复: any→all. 原逻辑 any(...) = 批次内任意一条到终态即跳过整批,
            #   导致 (a) 盘后追踪后才生成的指令永久不追踪 (b) 'holding' 仓位被同批的终态指令
            #   连带跳过, 无法每日重追踪直到产生最终 outcome. 改为 all: 仅当全部指令到终态
            #   (win/loss/skipped/missed) 才跳过, 否则重跑追踪未终态的.
            # 2026-06-23 修复(OKR): 去掉 "shadow_pnl is not None" 条件 — 旧逻辑把
            #   shadow_pnl=0.0 但 outcome=NULL 的指令 (如未走 SKIP 分支的 SKIP 指令)
            #   误判为已追踪, 导致其 outcome 永远停留在 NULL, OKR 样本泄漏.
            #   终态必须由 outcome 字段本身判定, shadow_pnl 只是辅助浮盈不计终态.
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                instructions = db.get_instructions_by_date(today)

                # 内存幂等优先: 已标记今日追踪完成 → 跳过 (空列表/正常列表都覆盖)
                if getattr(scheduler, "_playbook_tracked_date", None) == today:
                    _run_ic_backfill_only(scheduler, today)
                    return None

                # 检查是否已追踪：所有指令均有终态 outcome (win/loss/skipped/missed)
                # outcome="holding"/NULL 均不算终态 — 持仓中或未追踪的指令需每日重追踪直到产生最终 outcome
                # 空列表 (PRE_MARKET错过/重启) 也算已追踪 — 没有指令就没有需要追踪的
                tracked = not instructions or all(
                    inst.get("outcome") in ("win", "loss", "skipped", "missed")
                    for inst in instructions
                )
                if tracked:
                    scheduler._playbook_tracked_date = today
                    # ponytail: backfill 不能跳过 — Bug A: 幂等guard跳过backfill导致IC永远0
                    # 即使今日指令已全部终态, 前几天的指令仍需要 T+3/T+5 backfill
                    _run_ic_backfill_only(scheduler, today)
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")
                if getattr(scheduler, "_playbook_tracked_date", None) == today:
                    logger.debug(f"[playbook] {today} 已追踪 (内存标志), 跳过")
                    return None

        logger.info(f"[playbook] 📊 盘后追踪开始 (date={today})")

        from app.playbook.db import PlaybookDB
        from app.playbook.shadow_tracker import ShadowTracker
        from app.playbook.feishu_push import PlaybookPusher

        db = PlaybookDB()

        tracker = ShadowTracker(
            playbook_db=db,
            market_data=getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None),
            trade_store=getattr(scheduler, "trade_store", None),
        )
        # OKR (2026-06-23): EOD 强制平仓兜底 — monitor_playbook_sim_exits 的 PRE_CLOSE_EXIT
        # 只在 15:52-16:00 ET 的 8 分钟窗口内、且实时报价可用时才触发; 一旦该窗口错过 (报价
        # 失败/时序错位), 仓位永久孤儿化 (exit_price=0), 永不产生 win/loss → OKR 样本永不增长.
        # 这里在盘后追踪前, 对所有仍未平仓的 PLAYBOOK_SIM 仓位用当前价 (≈收盘价) 强制平仓,
        # 保证每个执行仓位当天必出最终 outcome. force=True 历史回补时也适用 (按当前价实现已开仓位).
        # 2026-06-26: PLAYBOOK_SHADOW 已废弃, 只兜底 PLAYBOOK_SIM 真实模拟仓位.
        try:
            sim_closed = _force_close_orphaned_sim_positions(scheduler)
            if sim_closed:
                logger.info(f"[playbook] EOD sim 强制平仓: {sim_closed} 个 sim 仓位已 broker 平仓")
        except Exception as _e:
            logger.warning(f"[playbook] EOD sim 强制平仓异常 (已隔离): {_e}")

        tracker.daily_tracking(today)

        # 双层因子统计每日增量更新 (spec 6.3) — 冷启动期 trades 不足时自动返回 0
        # 注意: Screener._load_factor_ic_adjustments 已在运行时动态调整因子权重
        # (IC>0.05→+1, IC<-0.05→-1, 读factor_ic_latest.json), 无需额外调用WeightAdjuster
        # 2026-07-04: 原计划接通WeightAdjuster但发现与Screener重复, 撤销避免双重惩罚
        try:
            from app.playbook.factor_analyzer import FactorAnalyzer
            updated = FactorAnalyzer(db).update_all_stats()
            logger.info(f"[playbook] 因子统计更新: {updated}")
        except Exception as e:
            logger.debug(f"[playbook] 因子更新失败 (不影响追踪): {e}")

        # 2026-06-26: Horizon 收益模型每日增量更新（样本不足时跳过，避免无效 CPU）
        try:
            from app.playbook.horizon_return_model import HorizonReturnModel
            total_with_outcome = db._conn().execute(
                "SELECT COUNT(*) FROM playbook_instructions WHERE outcome IN ('win','loss')"
            ).fetchone()[0]
            if total_with_outcome >= 50:
                hrm_updated = HorizonReturnModel(db).update(days_back=90)
                logger.info(f"[playbook] Horizon 收益模型更新: {hrm_updated}")
            else:
                logger.debug(
                    f"[playbook] Horizon 收益模型跳过 (样本不足: {total_with_outcome} < 50)"
                )
        except Exception as e:
            logger.debug(f"[playbook] Horizon 收益模型更新失败 (不影响追踪): {e}")

        # IC 追踪 (Phase 1: 2026-06-19) — 回填前瞻收益 + 计算 IC
        try:
            from app.playbook.ic_tracker import backfill_forward_returns, compute_playbook_ic, format_ic_report
            market_data = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
            if market_data:
                backfilled = backfill_forward_returns(db, market_data, today)
                logger.info(f"[playbook] IC 前瞻收益回填: {backfilled} 条")
                
                # 每周五推送 IC 报告
                from datetime import datetime
                now_et = datetime.now(_now_et())
                if now_et.weekday() == 4:  # 周五
                    ic_report = compute_playbook_ic(db, days=30)
                    if ic_report.get("sample_t3", 0) > 0:
                        report_text = format_ic_report(ic_report)
                        try:
                            PlaybookPusher(db)._send(report_text)
                        except Exception as e:
                            logger.debug(f"[playbook] IC 周报推送失败: {e}")
        except Exception as e:
            logger.debug(f"[playbook] IC 追踪失败 (不影响主流程): {e}")

        # P2 (2026-06-29): Signal IC 统一回填 — 与 Playbook IC 共享 router 缓存
        try:
            from app.core.feature_flags import FeatureFlags
            if FeatureFlags().is_enabled("unified_backfill"):
                from app.analysis.signal_ic_tracker import run_signal_ic_backfill
                signal_stats = run_signal_ic_backfill(dry_run=False)
                total_signal = sum(signal_stats.values())
                logger.info(f"[playbook] Signal IC 统一回填: {total_signal} 条, {len(signal_stats)} 只股票")
        except Exception as e:
            logger.debug(f"[playbook] Signal IC 统一回填失败 (不影响主流程): {e}")

        # 2026-07-03: 同步 signal_lifecycle 表 (trade_records ↔ signal_snapshots 关联)
        # 之前此脚本只能手动运行, 导致 fwd_ret 字段长期不更新
        try:
            import subprocess, sys
            subprocess.run(
                [sys.executable, "scripts/update_signal_lifecycle.py"],
                cwd=str(Path(__file__).resolve().parent.parent.parent),
                timeout=60, capture_output=True,
            )
            logger.info(f"[playbook] signal_lifecycle 同步完成")
        except Exception as e:
            logger.debug(f"[playbook] signal_lifecycle 同步失败 (不影响主流程): {e}")

        try:
            PlaybookPusher(db).push_evening_tracking(today)
        except Exception as e:
            logger.debug(f"[playbook] 盘后推送失败 (不影响追踪): {e}")

        scheduler._playbook_tracked_date = today
        return {"date": today, "tracked": True}

    except Exception as e:
        logger.exception(f"[playbook] 盘后追踪异常 (已隔离): {e}")
        return {"error": str(e)}


# ── 入口 3: 周报 + 权重闭环 (周五) ─────────────────────────────────────
def run_playbook_weekly(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    每周五: 双层因子分析 + 权重自动调整 + LLM 周报 + 飞书推送 (Layer 3 每周)。

    幂等: scheduler._playbook_weekly_done == 本周结束日 即跳过 (force=True 可重跑)。
    """
    try:
        from datetime import datetime, timedelta
        now_et = datetime.now(_now_et())

        # 本周结束日 (周五, 若今天是周五则是今天)
        days_to_friday = (4 - now_et.weekday()) % 7
        week_end = (now_et.date() + timedelta(days=days_to_friday)).isoformat()

        if not _playbook_enabled() or not _playbook_cfg("enable_weekly", True):
            logger.debug("[playbook] playbook 周报闸门关闭, 跳过周度复盘")
            return None

        if not force:
            # 幂等检查: 先查内存标志 (快路径), 再查 DB (重启兜底)
            if getattr(scheduler, "_playbook_weekly_done", None) == week_end:
                logger.debug(f"[playbook] 本周 ({week_end}) 已出周报 (内存标志), 跳过")
                return None
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                regime_log = db.get_regime_log(days=7, include_markers=True)
                has_weekly = any(
                    r.get("regime") == "weekly_report" or "weekly" in str(r.get("regime", ""))
                    for r in regime_log
                )
                if has_weekly:
                    logger.info(f"[playbook] 本周 ({week_end}) DB 已有周报，跳过 (幂等)")
                    scheduler._playbook_weekly_done = week_end
                    return None
            except Exception as e:
                logger.debug(f"[playbook] DB 幂等检查失败，降级为内存检查: {e}")

        logger.info(f"[playbook] 📈 周度复盘开始 (week_end={week_end})")

        from app.playbook.db import PlaybookDB
        from app.playbook.weekly_report import WeeklyReportGenerator

        db = PlaybookDB()
        generator = WeeklyReportGenerator(playbook_db=db)
        report = generator.generate(_today_et_str())

        # 权重自动闭环 (正收益因子 +1, 负收益 -1) + 周报飞书推送
        generator.format_report(report)
        try:
            generator.push_report(report)
        except Exception as e:
            logger.debug(f"[playbook] 周报推送失败 (不影响分析): {e}")

        # P0 配套 (2026-07-11): edge 影子闸门 & 信号质量 周度汇总推送 (飞书)
        try:
            from app.decision.edge_weekly_report import push_edge_weekly_report
            push_edge_weekly_report(days=7)
        except Exception as e:
            logger.debug(f"[playbook] edge 周报推送失败 (不影响分析): {e}")

        # 周五风控熔断状态检查 (spec 5.4) — 仅记录告警, 不阻断
        try:
            from app.playbook.risk_circuit_breaker import RiskCircuitBreaker
            status = RiskCircuitBreaker(db).check_status(_get_total_equity(scheduler))
            if status.get("action") in ("reduce", "stop"):
                logger.warning(f"[playbook] 风控熔断触发: {status}")
        except Exception as e:
            logger.debug(f"[playbook] 风控检查失败 (不影响周报): {e}")

        # Phase 1: 反馈学习 (从用户跳过/修改中学习)
        try:
            from app.playbook.feedback_learner import FeedbackLearner
            learner = FeedbackLearner(db)
            feedback_report = learner.learn_from_feedback(days=7)
            
            # 如果有足够的反馈数据，将学习报告附加到周报
            if feedback_report.get("feedback_count", 0) > 0:
                report["feedback_learning"] = feedback_report
                logger.info(f"[playbook] 反馈学习完成: {feedback_report['feedback_count']} 条反馈")
                
                # 如果有 Screener 调整建议，记录到日志（不自动应用，需要人工确认）
                screener_adj = feedback_report.get("screener_adjustments", {})
                if screener_adj:
                    logger.info(f"[playbook] Screener 调整建议: {screener_adj}")
                    report["screener_adjustments_suggested"] = screener_adj
        except Exception as e:
            logger.debug(f"[playbook] 反馈学习失败 (不影响周报): {e}")

        logger.info(f"[playbook] ✅ 周度复盘完成: {report.get('week', week_end)}")
        scheduler._playbook_weekly_done = week_end
        # DB 幂等标记: 重启后仍可跳过
        try:
            db.save_regime_log({
                "date": week_end,
                "regime": "weekly_report",
                "vix": 0,
                "spy_price": 0,
                "spy_ma20": 0,
                "spy_ma50": 0,
                "score": 0,
                "notes": f"weekly_report generated at {datetime.utcnow().isoformat()}",
            })
        except Exception as e:
            logger.debug(f"[playbook] DB 幂等标记写入失败: {e}")
        return report

    except Exception as e:
        logger.exception(f"[playbook] 周度复盘异常 (已隔离): {e}")
        return {"error": str(e)}


# ── 入口 4: 盘中自动执行 Paper Trade (Phase 1: 2026-06-19) ──────────


def _has_consecutive_losses(n: int = 5) -> bool:
    """检查最近是否有连续 n 笔亏损 (按时间倒序, 仅看终态 win/loss).

    fail-close: 异常返回 True (阻止实盘).
    """
    try:
        from app.playbook.db import PlaybookDB
        db = PlaybookDB()
        instructions = db.get_recent_instructions(limit=200)
        consecutive = 0
        for inst in instructions:
            outcome = inst.get("outcome")
            if outcome == "loss":
                consecutive += 1
                if consecutive >= n:
                    return True
            elif outcome == "win":
                consecutive = 0
            # 其他 outcome (holding/skipped/missed/null) 不重置也不累加
        return False
    except Exception as e:
        logger.warning(f"[playbook] 连续亏损检查失败 (fail-close): {e}")
        return True


def _check_graduation_gate() -> Dict[str, Any]:
    """毕业门禁 (Phase 2, 2026-06-24) — 实盘自动交易的前置条件.

    检查 (在模拟数据上):
      1. IC(T+3) > 0.05  (ic_tracker._check_graduation)
      2. 样本量 ≥ 30    (ic_tracker._check_graduation)
      3. 无连续 5 笔亏损 (_has_consecutive_losses)

    fail-close: 任何异常都返回 graduated=False (阻止实盘).
    用于: playbook execute_mode=live 路径 + dashboard 实盘开关.

    Returns: {"graduated": bool, "checks": {...}, "reason": str}
    """
    try:
        from app.playbook.ic_tracker import compute_playbook_ic, _check_graduation
        from app.playbook.db import PlaybookDB

        db = PlaybookDB()
        ic_report = compute_playbook_ic(db, days=30)
        grad = _check_graduation(ic_report)

        checks = dict(grad.get("checks", {}))
        # 补 "无连续5亏" 检查
        checks["no_consecutive_5_losses"] = not _has_consecutive_losses(5)
        graduated = all(checks.values()) and grad.get("graduated", False)

        failed = [k for k, v in checks.items() if not v]
        return {
            "graduated": graduated,
            "checks": checks,
            "ic_t3": grad.get("ic_t3"),
            "sample_count": grad.get("sample_count"),
            "ic_t1": ic_report.get("overall_ic_t1"),
            "ic_t5": ic_report.get("overall_ic_t5"),
            "sample_t1": ic_report.get("sample_t1", 0),
            "sample_t3": ic_report.get("sample_t3", 0),
            "sample_t5": ic_report.get("sample_t5", 0),
            "reason": "" if graduated else f"未毕业: {failed}",
        }
    except Exception as e:
        logger.error(f"[playbook] graduation gate 异常 (fail-close): {e}")
        return {"graduated": False, "checks": {}, "reason": f"门禁异常: {e}"}


def _emit_exec_gate_audit(inst: Dict[str, Any], *, action: str, entry: float,
                          size: float, notional: float, extra: Optional[Dict[str, Any]] = None) -> None:
    """统一收口审计 (2026-07-17): 记录每个到达执行漏斗的候选单 → data/shadow/exec_gate_audit.jsonl.

    背景: DA-bridge 直写 DB 的指令绕过 _generate_instructions 的 G6 confidence 门与
    EdgeShadowGate; 实盘走量全是 DA-bridge → 无任何统一、可解释的下单前收口点.
    本函数装在 run_playbook_auto_execute 下单前唯一汇合点, 无论来源一律记录, 让全链路可见.

    同时对所有执行候选 (含 DA-bridge) 跑一次 EdgeShadowGate — edge 判定首次覆盖 DA-bridge.
    纯影子: 只记录, 绝不拦单、不改任何交易动作. best-effort, 异常一律吞掉.
    """
    try:
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        _did = str(inst.get("decision_id", "") or "")
        if _did.startswith("DA-"):
            _source = "da_bridge"
        elif _did.startswith("universe_expansion"):
            _source = "universe_expansion"
        elif _did:
            _source = "internal"
        else:
            _source = "unknown"

        _direction = inst.get("direction", "")
        _conf = float(inst.get("confidence", 0) or 0)

        # EdgeShadowGate — 数据驱动 edge 判定首次覆盖 DA-bridge (纯影子, live_pass=None 语义: 无实盘门)
        _edge = {}
        try:
            from app.decision.edge_shadow_gate import get_edge_shadow_gate
            _edge = get_edge_shadow_gate().evaluate(
                inst.get("symbol", ""), _direction, _conf, live_pass=True, log=False,
            ) or {}
        except Exception:
            _edge = {}

        _row = {
            "ts": _dt.now().isoformat(timespec="seconds"),
            "date": _dt.now().strftime("%Y-%m-%d"),
            "inst_id": inst.get("id"),
            "symbol": inst.get("symbol", ""),
            "direction": _direction,
            "confidence": round(_conf, 2),
            "source": _source,
            "decision_id": _did,
            "action": action,  # executed / skipped_* / error
            "entry": round(float(entry or 0), 4),
            "size": int(size or 0),
            "notional": round(float(notional or 0), 2),
            "edge_bucket": _edge.get("bucket"),
            "edge_expectancy": _edge.get("expectancy"),
            "edge_verdict": _edge.get("edge_verdict"),
            "edge_reason": _edge.get("reason"),
        }
        if extra:
            _row.update(extra)

        _p = _Path(__file__).resolve().parents[2] / "data" / "shadow" / "exec_gate_audit.jsonl"
        _p.parent.mkdir(parents=True, exist_ok=True)
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 纯审计, 绝不影响交易主流程


def run_playbook_auto_execute(scheduler, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    盘中自动执行 triggered 的指令 (Paper Trade 模式)。

    流程:
    1. 检查 playbook.auto_execute 配置开关 (默认 false)
    2. 获取今日指令
    3. 对每条 triggered=1 且 executed=0 且 direction != SKIP 的指令，自动执行
    4. 调用 playbook_service.execute_instruction()

    幂等: 同一指令不会重复执行 (executed=1 后跳过)。
    """
    try:
        from app.playbook.db import PlaybookDB
        from app.playbook.playbook_service import PlaybookService

        # 1. 检查配置开关
        if not _playbook_cfg("auto_execute", False):
            logger.info("[playbook] auto_execute=false, 跳过自动执行")
            return None

        today = _today_et_str()
        if not force and not _is_trading_day_today():
            logger.info(f"[playbook] {today} 非交易日, 跳过自动执行")
            return None

        # 2026-06-24: 开盘后5分钟内不执行 — 避开开盘最大噪声 (gap fill + 做市商调仓)
        # 数据依据: 6/23 shadow 22笔中21笔在 9:31:14 批量执行, 2笔瞬间止损 (78ms/473ms)
        # 延迟5min → 入场价更稳定, SL不被秒杀
        from datetime import datetime as _dt
        _et_tz = _now_et()
        _now_et_dt = _dt.now(_et_tz)
        _market_open_min = 9 * 60 + 30  # 9:30 ET
        _now_min = _now_et_dt.hour * 60 + _now_et_dt.minute
        if not force and _now_min < _market_open_min + 5:
            logger.info(f"[playbook] 开盘5分钟内不执行 (now={_now_et_dt.strftime('%H:%M')} ET)")
            return None

        # 2026-06-30: 全局风险/市场环境检查 — 极端 regime 或风险状态禁止新开仓
        try:
            from app.playbook.market_regime import MarketRegime, MarketRegimeGate
            from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
            regime_ctx = MarketRegimeGate().assess()
            if regime_ctx.regime == MarketRegime.NO_TRADE:
                logger.warning(
                    f"[playbook] 当前 regime=NO_TRADE (VIX={regime_ctx.vix:.2f}), 跳过 auto_execute"
                )
                return {"date": today, "executed": 0, "skipped": 0, "reason": "regime_no_trade"}
            risk_state = RiskStateMachine().get_state()
            if risk_state in (RiskState.RISK_OFF, RiskState.HALT):
                logger.warning(
                    f"[playbook] 当前 risk_state={risk_state}, 跳过 auto_execute"
                )
                return {"date": today, "executed": 0, "skipped": 0, "reason": f"risk_state_{risk_state}"}
        except Exception as e:
            logger.warning(f"[playbook] 风险状态检查异常 (放行): {e}")

        db = PlaybookDB()
        service = PlaybookService(db)
        instructions = db.get_instructions_by_date(today)

        logger.info(f"[playbook] auto_execute 开始: {len(instructions)} 条指令 (now={_now_et_dt.strftime('%H:%M')} ET)")

        if not instructions:
            logger.info(f"[playbook] {today} 无指令, 跳过自动执行")
            return {"date": today, "executed": 0, "skipped": 0}

        executed_count = 0
        skipped_count = 0
        errors = []

        # P0 fix (2026-06-25): 开仓前 buying_power 总量检查
        # 根因: 批量同时触发 (6笔/2秒), 每笔独立算仓位 → $388K on $82K 账户
        # 修复: 累计已开 PLAYBOOK_SIM notional, 超过预算上限则跳过后续
        _max_deploy = 0.0
        try:
            from app.core.db_utils import safe_sqlite_connect
            from app.core.paths import get_trade_records_db as _get_trdb
            _bp_conn = safe_sqlite_connect(_get_trdb(), timeout=5)
            _row = _bp_conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trade_records "
                "WHERE source_type='PLAYBOOK_SIM' AND (exit_time IS NULL OR exit_time = '') "
                "AND entry_time >= date('now','start of day','-1 day')"
            ).fetchone()
            _bp_conn.close()
            _max_deploy = _row[0] if _row else 0.0
        except Exception:
            pass

        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()

        # 2026-06-29: 统一从 capital_safety.get_available_budget_for_auto() 取预算
        # 替代旧的 buying_power × 0.8 — 该方法已扣除 manual+auto 已用敞口,
        # 且受 conservative/normal/aggressive 模式约束, Playbook 与 ORB 共享同一预算源
        from app.risk.capital_safety import get_capital_safety_system
        _bp_limit = 0.0
        _bp_source = "unavailable"
        try:
            _cs = get_capital_safety_system()
            _bp_limit = _cs.get_available_budget_for_auto()
            _bp_source = f"capital_safety_auto_budget"
        except Exception as e:
            logger.warning(f"[playbook] capital_safety auto_budget 读取失败: {e}")
        # 降级: 用 risk_base_capital × 80% 兜底
        if _bp_limit <= 0:
            _risk_base = float(pb_cfg.get("risk_base_capital", 82000.0))
            _bp_limit = _risk_base * 0.8
            _bp_source = f"fallback_{_risk_base:.0f}×80%"
        logger.info(f"[playbook] 预算上限: ${_bp_limit:,.0f} (来源={_bp_source}), 已部署=${_max_deploy:,.0f}")

        # 2026-06-28: 单日交易 guardrail — 防止恢复BUY过滤后市场过冷/过热时爆量
        # 2026-07-02: risk_base 从硬编码 $82K 改为实时 equity (via _get_total_equity)
        # 原因: 实际 equity=$176K, 硬编码 $82K 导致 cap=$16.4K 过低, 3单就满
        _risk_base_capital = _get_total_equity(scheduler)
        if _risk_base_capital <= 0:
            _risk_base_capital = float(pb_cfg.get("risk_base_capital", 82000.0))
        _daily_notional_cap_pct = float(pb_cfg.get("daily_notional_cap_pct", 0.20))
        _daily_notional_cap = _risk_base_capital * _daily_notional_cap_pct
        _max_daily_positions = int(pb_cfg.get("max_daily_positions", 5))

        # 2026-07-09: 从DB查当日已开仓notional/count，替代每次调用从0开始 (P0修复)
        _today_opened_notional = 0.0
        _today_opened_count = 0
        try:
            import sqlite3 as _sql3
            _db_path = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
            _db_conn = _sql3.connect(_db_path)
            _row = _db_conn.execute(
                "SELECT COALESCE(SUM(entry_price * quantity), 0), COUNT(*) "
                "FROM trade_records WHERE substr(entry_time,1,10) = ?",
                (today,)
            ).fetchone()
            _today_opened_notional = float(_row[0] or 0)
            _today_opened_count = int(_row[1] or 0)
            _db_conn.close()
        except Exception as _e:
            logger.warning(f"[playbook] 查询当日已开仓失败, 从0开始: {_e}")

        logger.info(
            f"[playbook] 单日 guardrail: notional_cap=${_daily_notional_cap:,.0f} "
            f"({_daily_notional_cap_pct*100:.0f}% of ${_risk_base_capital:,.0f}), "
            f"max_daily_positions={_max_daily_positions}, "
            f"已开仓: ${_today_opened_notional:,.0f}/{_today_opened_count}笔"
        )

        # 按confidence降序排序 — 购买力优先分配给最强信号 (DEB-20260629)
        # 漏斗最后一层: 7层过滤后剩N只, BP只够开2-3笔 → 先执行最强的
        # 2026-07-09: bridge候选优先 — deep_analysis信号比VotingCoordinator更高质量,
        # 但confidence=70 < VotingCoordinator的88-100, 排序时被排在后面吃不到预算.
        # 修复: bridge候选(decision_id以DA-开头)排前面.
        instructions.sort(key=lambda x: (
            1 if (x.get("decision_id", "") or "").startswith("DA-") else 0,
            x.get("confidence", 0) or 0
        ), reverse=True)

        for inst in instructions:
            # 跳过已执行、SKIP 方向的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue

            # 2026-06-29 P0: 执行前查broker持仓 — 有仓则跳过，防重复下单+孤儿仓循环
            # 根因: fill=0时playbook拒绝记账但broker已成交, 下次auto_execute又下单 → 仓位叠加
            _symbol = inst.get("symbol", "")
            try:
                from app.execution.dual_broker import get_dual_broker
                _dual = get_dual_broker()
                _broker_positions = _dual.get_positions(target_env="sim")
                _already_held = any(
                    p.get("code") == _symbol and abs(float(p.get("qty", 0) or 0)) > 0
                    for p in (_broker_positions or [])
                )
                if _already_held:
                    logger.info(f"[playbook] {_symbol} broker已有持仓, 跳过 (防重复下单)")
                    db.update_instruction_tracking(inst["id"], {"executed": 1, "execution_price": 0, "execution_time": datetime.now().isoformat(), "outcome": "already_held"})
                    skipped_count += 1
                    continue
            except Exception as _bh_err:
                logger.debug(f"[playbook] broker持仓检查失败 (放行): {_bh_err}")

            # 盘中实时触发检查: 价格是否进入入场区间 ±5%
            # shadow_tracker 只在盘后跑, 盘中 triggered=0 是正常的
            # auto_execute 每30分钟跑一次, 实时检查价格是否触发
            if not inst.get("triggered"):
                triggered = _check_intraday_trigger(scheduler, inst)
                if not triggered:
                    continue
                # 触发了 → 先更新 DB
                db.update_instruction_tracking(inst["id"], {"triggered": 1})
                logger.info(f"[playbook] 盘中触发: {inst['symbol']} {inst['direction']}")

            # ── 2026-06-29: 触发后重新定价 (re-price at trigger) ──
            # 根因: 盘前生成的 entry_price 用的是盘前 bid/ask, 开盘后 gap 导致 entry/stop 失准
            # 修复: 触发瞬间取实时 bid/ask, 重算 entry_price + stop_loss, 传 custom_params 给 executor
            # DB 原值保留不动 (审计可追溯盘前价), custom_params 覆盖执行价
            _reprice_params = None
            try:
                _dr = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
                if _dr:
                    _ctx = _dr.get_full_market_context(inst["symbol"])
                    if _ctx and _ctx.get("price", 0) > 0:
                        _cur = float(_ctx["price"])
                        _bid = float(_ctx.get("lv3_bid") or _ctx.get("bid") or _cur)
                        _ask = float(_ctx.get("lv3_ask") or _ctx.get("ask") or _cur)
                        _dir = inst.get("direction", "")
                        _hp = pb_cfg.get_horizon_params(inst.get("horizon", "short"))

                        # Phase 2/4 (2026-06-30): re-price 必须复用波动率分桶逻辑, 否则固定 SL/TP 会覆盖盘前生成的动态值
                        if _dir == "BUY":
                            _new_entry = _ask
                        else:
                            _new_entry = _bid

                        # 优先用真实 ATR 计算 atr_pct, 未取到则回退到 ctx 中的 atr_percent
                        # 2026-07-16 P1 修复: ATR=0 时保留 instruction 原始 SL/TP, 不用固定 fallback 覆盖.
                        #   根因: 盘前生成 instruction 时 ATR 可用 (vol_bucket 正确匹配),
                        #   但盘中 re-price 时 OpenD 可能不可用 → _atr=0 → atr_pct=0 →
                        #   vol_bucket 返回 None → 固定 fallback (2.0-2.5%) 覆盖原 SL.
                        #   修复: ATR=0 时从 instruction 原始 SL 反算 sl_pct, 保持 vol_bucket 比例不变.
                        _atr = float(_ctx.get("atr") or _ctx.get("atr_14") or 0)
                        atr_pct = 0.0
                        if _new_entry > 0 and _atr > 0:
                            atr_pct = _atr / _new_entry
                        if atr_pct <= 0:
                            atr_pct = (float(_ctx.get("atr_percent") or 0)) / 100.0

                        _vol_info = _compute_volatility_based_sl_tp(atr_pct, _dir, _hp, pb_cfg)
                        if _vol_info:
                            _sl_pct = _vol_info["sl_pct"]
                            _tp_mult = _vol_info["tp_multiplier"]
                        elif inst.get("stop_loss") and _new_entry > 0:
                            # 2026-07-16: ATR=0 → 从 instruction 原始 SL/TP 反算比例, 保持 vol_bucket 级别
                            _orig_sl = float(inst.get("stop_loss", 0))
                            _orig_tp = float(inst.get("take_profit", 0))
                            if _orig_sl > 0:
                                _sl_pct = abs(_new_entry - _orig_sl) / _new_entry * 100
                            else:
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5)) if _dir == "BUY" else float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            # TP: 用原始 TP 距离 / 新 entry 反算倍数
                            if _orig_tp > 0 and _orig_sl > 0:
                                _orig_sl_dist = abs(float(inst.get("entry_price", _new_entry)) - _orig_sl)
                                _orig_tp_dist = abs(_orig_tp - float(inst.get("entry_price", _new_entry)))
                                _tp_mult = _orig_tp_dist / _orig_sl_dist if _orig_sl_dist > 0 else float(_hp.get("rr_ratio", 1.5))
                            else:
                                _tp_mult = float(_hp.get("rr_ratio", 1.5))
                            logger.info(
                                f"[playbook] {inst['symbol']} re-price: ATR=0, 保留 instruction SL%={_sl_pct:.2f}% "
                                f"(从原始 SL=${_orig_sl:.2f} 反算)"
                            )
                        else:
                            # 未配置 vol_buckets 且无 instruction SL 时回退固定百分比
                            if _dir == "BUY":
                                _sl_pct = float(_hp.get("buy_fixed_stop_loss_pct", 2.5))
                            else:
                                _sl_pct = float(_hp.get("sell_fixed_stop_loss_pct", 2.0))
                            _tp_mult = float(_hp.get("rr_ratio", 1.5))

                        # 计算新 stop_loss
                        if _dir == "BUY":
                            _new_sl = round(_new_entry * (1 - _sl_pct / 100), 2)
                        else:
                            _new_sl = round(_new_entry * (1 + _sl_pct / 100), 2)

                        # 处理 rr_ratio:999 哨兵 (禁用止盈); 否则基于波动率分桶的 tp_multiplier
                        if _tp_mult and _tp_mult < 100:
                            if _dir == "BUY":
                                _new_tp = round(_new_entry * (1 + _sl_pct * _tp_mult / 100), 2)
                            else:
                                _new_tp = round(_new_entry * (1 - _sl_pct * _tp_mult / 100), 2)
                        else:
                            _new_tp = 0.0

                        # 2026-07-09: position_size 兜底计算 — bridge直写DB时未计算仓位
                        # 公式: risk_amount / per_share_risk, 截断 max_position_pct
                        # 置信度加权 (设计原则): 期望值 = 胜率×盈亏比, 高置信信号 edge 更大 →
                        #   在正期望前提下按 Kelly 思想放大仓位, 低置信信号缩仓. 以 min_confidence
                        #   为 1.0 中性基准, 限幅 [0.6, 1.4] 防止单点误判造成极端仓位.
                        #   max_position_pct 仍是硬上限, confidence 只在其内部分配.
                        _inst_ps = inst.get("position_size") or 0
                        if _inst_ps <= 0 and _new_sl > 0:
                            _risk_base = float(pb_cfg.get("risk_base_capital", 82000.0))
                            _risk_pct = float(pb_cfg.get("risk_pct", 0.5))
                            _max_pos_pct = float(pb_cfg.get("max_position_pct", 0.04))
                            _conf = float(inst.get("confidence", 0) or 0)
                            _conf_base = float(pb_cfg.get("min_confidence", 60) or 60)
                            # 2026-07-12: confidence 加权默认关闭 (拍平为 1.0). exit_scorecard 实测
                            #   当前 DA confidence 未校准且反向 (低信心桶反而更好, Playbook IC 为负) →
                            #   按 confidence 放大仓位 = 给更差的信号加钱. 校准为正前保持中性.
                            #   config: playbook.risk_parameters.confidence_sizing_enabled (默认 false).
                            _conf_factor = 1.0
                            if bool(pb_cfg.get("confidence_sizing_enabled", False)) and _conf > 0 and _conf_base > 0:
                                _conf_factor = max(0.6, min(1.4, _conf / _conf_base))
                            # 2026-07-16 P1: LONG conf<50 降仓50%.
                            #   数据: LONG 38笔 39.5%WR -$2435 vs SHORT 23笔 43.5%WR +$1467.
                            #   LONG conf=35 (2模型分歧) 占多数, 系统性亏损.
                            #   修复: LONG conf<50 时 _conf_factor *= 0.5 (降仓但不SKIP, 保留方向验证).
                            if _dir == "BUY" and _conf < 50:
                                _conf_factor *= 0.5
                                logger.info(
                                    f"[playbook] {inst['symbol']} LONG conf={_conf}<50, 降仓50% "
                                    f"(conf_factor={_conf_factor:.2f})"
                                )
                            _risk_amount = _risk_base * (_risk_pct / 100) * _conf_factor
                            _per_share_risk = abs(_new_entry - _new_sl)
                            if _per_share_risk > 0:
                                _inst_ps = int(_risk_amount / _per_share_risk)
                            _max_pos_val = _risk_base * _max_pos_pct
                            if _inst_ps * _new_entry > _max_pos_val:
                                _inst_ps = max(1, int(_max_pos_val / _new_entry))
                            if _inst_ps > 0:
                                logger.info(
                                    f"[playbook] {inst['symbol']} position_size兜底: "
                                    f"{_inst_ps}股 (risk=${_risk_amount:.0f}×conf{_conf_factor:.2f}"
                                    f"/share_risk=${_per_share_risk:.2f}, "
                                    f"上限{int(_max_pos_pct*100)}%=${_max_pos_val:.0f})"
                                )

                        _reprice_params = {
                            "entry_price": round(_new_entry, 2),
                            "stop_loss": _new_sl,
                            "take_profit": _new_tp,
                            # 2026-06-29: 必须传 position_size, 否则 execute_instruction
                            # 走 custom_params 分支但取不到 position_size → None → 降为1股
                            "position_size": _inst_ps or 1,
                            # 2026-07-13: risk_amount 派生落库 (显示/审计字段) = |entry-SL|×实际仓位.
                            #   此前 DA/bridge 路径 risk_amount 恒 NULL → 盘前推送 {risk:.0f} 曾崩
                            #   (已加 or 0 兜底); 此处补写真实值, 让推送/周报显示每笔风险敞口.
                            #   纯派生, 不影响 sizing 决策.
                            "risk_amount": round(abs(_new_entry - _new_sl) * (_inst_ps or 1), 2),
                            # 2026-06-29: 审计字段 — 记录 re-price 来源
                            "reprice_from": round(float(inst.get("entry_price", 0) or 0), 2),
                            "reprice_to": round(_new_entry, 2),
                            "reprice_original_sl": round(float(inst.get("stop_loss", 0) or 0), 2),
                        }
                        # 更新 DB 记录 re-priced 值 (审计可追溯)
                        db.update_instruction_tracking(inst["id"], _reprice_params)
                        # 2026-07-16: 盘中数据质量护栏 — 记录 re-price 时的数据完整性
                        _dq_flags = []
                        if _atr <= 0: _dq_flags.append("atr_missing")
                        if not _ctx.get("lv3_bid") or not _ctx.get("lv3_ask"):
                            _dq_flags.append("no_lv3_bidask")
                        if _dq_flags:
                            logger.warning(
                                f"[playbook] {inst['symbol']} re-price 数据质量: {', '.join(_dq_flags)} "
                                f"(使用 instruction 原始 SL 反算, 不影响执行)"
                            )
                        logger.info(
                            f"[playbook] re-price: {inst['symbol']} {_dir} "
                            f"盘前=${inst.get('entry_price', 0):.2f} → 实时=${_new_entry:.2f} "
                            f"SL={_new_sl:.2f} ({_sl_pct:.2f}%) TP={_new_tp:.2f}"
                        )

                        # 2026-07-12 L1: SL/TP 验证影子 (record-only, 不改任何交易动作)。
                        #   记录 LLM 原始 SL/TP vs 代码波动率桶 SL/TP 的分歧, 供日后与 pnl 联合校准。
                        #   自带 try/except, 绝不影响 reprice 与下单。
                        try:
                            from app.playbook.sltp_verify_shadow import record_sltp_shadow
                            record_sltp_shadow(
                                inst_id=inst.get("id"), symbol=inst.get("symbol", ""),
                                direction=_dir, entry=_new_entry,
                                llm_sl=inst.get("stop_loss", 0), llm_tp=inst.get("take_profit", 0),
                                code_sl=_new_sl, code_tp=_new_tp,
                                sl_pct=_sl_pct, bucket=(_vol_info or {}).get("bucket", ""),
                                atr_pct=atr_pct, regime=inst.get("market_regime", ""),
                            )
                        except Exception:
                            pass
            except Exception as _rp_err:
                logger.debug(f"[playbook] re-price 失败, 用DB原值: {_rp_err}")

            # 2026-07-10: 删除 confidence<65 执行层gate (G6 conf<70已覆盖, E1从不拦截)
            # 原代码: _min_confidence=65, if conf<65 → SKIP
            # 删除原因: G6(line 1065, conf<70)已过滤, conf≥70的信号到执行层必>65, E1冗余

            # P0 (2026-06-25): buying_power 总量检查 — 防止批量触发耗尽购买力
            # 2026-07-10 fix: 优先用 re-price 后的 _reprice_params (含兜底计算的 position_size),
            # 否则 bridge 候选 position_size=None → _size=0 → _new_notional=0 → guardrail 全部空过
            if _reprice_params and _reprice_params.get("position_size"):
                _entry = _reprice_params.get("entry_price") or 0.0
                _size = _reprice_params.get("position_size") or 0
            else:
                _entry = inst.get("entry_price") or 0.0
                _size = inst.get("position_size") or 0
            _new_notional = _entry * _size if _entry > 0 and _size > 0 else 0.0
            if _bp_limit > 0 and (_max_deploy + _new_notional) > _bp_limit:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 购买力上限 "
                    f"(已用=${_max_deploy:,.0f} + 新=${_new_notional:,.0f} > 限=${_bp_limit:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_bp_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 2026-06-28: 单日 notional/数量 guardrail
            if _today_opened_notional + _new_notional > _daily_notional_cap:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日notional上限 "
                    f"(已用=${_today_opened_notional:,.0f} + 新=${_new_notional:,.0f} > 限=${_daily_notional_cap:,.0f})"
                )
                _emit_exec_gate_audit(inst, action="skipped_notional_cap", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            if _today_opened_count >= _max_daily_positions:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 单日开仓数上限 "
                    f"(已达到 {_max_daily_positions} 笔)"
                )
                _emit_exec_gate_audit(inst, action="skipped_max_positions", entry=_entry,
                                      size=_size, notional=_new_notional)
                skipped_count += 1
                continue

            # 自动执行
            _audit_action = "error"
            try:
                result = service.execute_instruction(
                    instruction_id=inst["id"],
                    acknowledge_risk=True,  # Paper Trade 自动确认风险
                    custom_params=_reprice_params,  # 2026-06-29: 重新定价后的 entry/stop
                )
                if result.get("success"):
                    executed_count += 1
                    _max_deploy += _new_notional  # 累加已用额度
                    _today_opened_notional += _new_notional
                    _today_opened_count += 1
                    _audit_action = "executed"
                    logger.info(f"[playbook] ✅ 自动执行: {inst['symbol']} {inst['direction']}")
                else:
                    skipped_count += 1
                    _audit_action = "skipped_fail"
                    errors.append(f"{inst['symbol']}: {result.get('message', 'unknown')}")
            except Exception as e:
                skipped_count += 1
                _audit_action = "error"
                errors.append(f"{inst['symbol']}: {str(e)}")
                logger.warning(f"[playbook] 自动执行失败 {inst['symbol']}: {e}")
            # 统一收口审计 (2026-07-17): 记录到达下单漏斗并被执行/失败的候选 (含 DA-bridge)
            _emit_exec_gate_audit(inst, action=_audit_action, entry=_entry,
                                  size=_size, notional=_new_notional)

        logger.info(f"[playbook] 自动执行完成: {executed_count} 成功, {skipped_count} 跳过")
        # 盘中实时化: 广播轻量 nudge, 已连 dashboard 收到后自拉缓存接口刷新. 没人连时 broadcast 直接 return.
        try:
            from app.web_dashboard.routes.ws import publish_event_sync
            publish_event_sync("pipeline_update", {
                "date": today,
                "executed": executed_count,
                "skipped": skipped_count,
                "errors": len(errors),
            })
        except Exception:
            pass  # WS 推送失败不影响交易主流程
        return {
            "date": today,
            "executed": executed_count,
            "skipped": skipped_count,
            "errors": errors[:5],  # 最多返回 5 个错误
        }

    except Exception as e:
        logger.exception(f"[playbook] 自动执行异常 (已隔离): {e}")
        return {"error": str(e)}


def _check_intraday_trigger(scheduler, inst: Dict[str, Any]) -> bool:
    """盘中实时检查指令是否触发 (突破当日高低点).

    2026-06-29: 从MA20突破改为当日高低点突破.
    根因: MA20突破在BULL regime下SHORT几乎不可能触发(需跌破MA20).
    修复: BUY触发=突破当日高点; SHORT触发=跌破当日低点. 不依赖MA20方向.
    成交量确认保留 (防止低流动性假突破).
    """
    entry_price = inst.get("entry_price")
    if not entry_price or entry_price <= 0:
        return False

    symbol = inst.get("symbol", "")
    if not symbol:
        return False

    direction = inst.get("direction", "")
    is_short = "SHORT" in direction.upper()

    # 2026-07-11: 只读埋点 — 记录未触发原因/实时价/偏离幅度.
    # 目的: 量化 never-trigger 里 "追高/接飞刀被正确拒绝" vs "盘前参考价过期但盘中价合理".
    # 零行为改动: 仅 append JSONL, best-effort, 不影响触发判定与返回值.
    def _emit_trigger_miss(reason: str, cur):
        try:
            import json as _json
            from datetime import datetime as _dt
            from pathlib import Path as _Path
            _e = float(entry_price or 0)
            _cur = float(cur or 0)
            _gap = (abs(_cur - _e) / _e * 100.0) if (_e > 0 and _cur > 0) else 0.0
            _row = {
                "ts": _dt.now().isoformat(timespec="seconds"),
                "date": _dt.now().strftime("%Y-%m-%d"),
                "inst_id": inst.get("id"),
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "entry_price": round(_e, 4),
                "current_price": round(_cur, 4),
                "gap_pct": round(_gap, 3),
                "reason": reason,
            }
            _tp = _Path(__file__).resolve().parents[2] / "data" / "trigger_miss_telemetry.jsonl"
            with open(_tp, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # 2026-07-15: 只读影子埋点 (入场时机门验证 v3) — 记录门的原始微结构特征
    #   + 司机(突破触发)的真实决策. 离线切片验证"过滤价值": 真实入场单中,
    #   同时过新门(VWAP/量能/大盘)的那批 PnL 是否更好. 全源通用.
    # 零行为改动: 仅 append JSONL, best-effort, 不影响触发判定与返回值 (镜像 _emit_trigger_miss).
    def _emit_entry_shadow(triggered_val, breakout_val=None, gate_enabled=False,
                           gate_block=None, gate_reason=None, vwap_dev=None,
                           vwap_populated=None):
        try:
            import json as _json
            from datetime import datetime as _dt
            from pathlib import Path as _Path
            _c = ctx if isinstance(ctx, dict) else {}
            _keys = [
                "vwap", "ticker_vwap_deviation", "slope_5m", "rsi", "atr",
                "volume", "volume_ratio", "opening_high", "opening_low",
                "opening_range_ready", "order_flow_imbalance", "ob_spread_bps",
                "ob_depth_imbalance", "ticker_buy_sell_ratio",
                "spy_price", "spy_ma20", "spy_ma50", "spy_ma200",
                "change_percent", "trend",
            ]
            _feat = {k: _c.get(k) for k in _keys}
            _did = str(inst.get("decision_id") or "")
            _src = _did.split(":")[0] if ":" in _did else ""
            if not _src:
                try:
                    _raw = _json.loads(inst.get("raw_instruction_json") or "{}")
                    _src = str(_raw.get("source") or "")
                except Exception:
                    _src = ""
            try:
                _et = _dt.now(_now_et())
                _hm = _et.hour * 100 + _et.minute
                _sess = "open" if _hm < 945 else ("late" if _hm >= 1530 else "mid")
                _ts = _et.isoformat(timespec="seconds")
            except Exception:
                _sess = "unknown"
                _ts = _dt.now().isoformat(timespec="seconds")
            _row = {
                "ts": _ts,
                "session": _sess,
                "inst_id": inst.get("id"),
                "decision_id": _did,
                "source": _src,
                "symbol": symbol,
                "direction": "SHORT" if is_short else "BUY",
                "entry_price": round(float(entry_price or 0), 4),
                "current_price": round(float(current_price or 0), 4),
                "live_triggered": bool(triggered_val),
                "breakout_triggered": (bool(breakout_val) if breakout_val is not None else None),
                "gate_enabled": bool(gate_enabled),
                "gate_block": (bool(gate_block) if gate_block is not None else None),
                "gate_reason": gate_reason,
                "vwap_dev": vwap_dev,
                "vwap_populated": bool(vwap_populated),
                "features": _feat,
            }
            _tp = _Path(__file__).resolve().parents[2] / "data" / "entry_timing_shadow.jsonl"
            with open(_tp, "a", encoding="utf-8") as _f:
                _f.write(_json.dumps(_row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if not data_router:
        return False

    # 获取实时行情 (含 day_high/day_low)
    current_price = None
    day_high = None
    day_low = None
    vol_ratio = None
    ctx = None  # 2026-07-15: 预置, 供 _emit_entry_shadow 安全引用 (取数异常时保持 None)
    try:
        ctx = data_router.get_full_market_context(symbol)
        if ctx and isinstance(ctx, dict):
            current_price = float(ctx.get("price", 0) or 0)
            day_high = float(ctx.get("day_high", 0) or ctx.get("high_price", 0) or 0)
            day_low = float(ctx.get("day_low", 0) or ctx.get("low_price", 0) or 0)
            vol_ratio = float(ctx.get("volume_5d_avg_ratio", 0) or ctx.get("volume_ratio", 0) or 0)
    except Exception:
        pass

    # fallback: 用 get_quote 取价格
    if not current_price or current_price <= 0:
        try:
            quote = data_router.get_quote(symbol)
            if quote and isinstance(quote, dict):
                current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
        except Exception:
            pass

    if not current_price or current_price <= 0:
        return False

    # 如果没有日内高低点 (开盘第一根K线), 用 entry_price 作为基准
    if day_high is None or day_high <= 0:
        day_high = entry_price
    if day_low is None or day_low <= 0:
        day_low = entry_price

    # ── 触发条件: 方向穿越触发 (2026-07-02: 从±0.5%改为方向穿越) ──
    # 根因: ±0.5% touch zone导致0%触发率(17/17 miss).
    # 修复: 限价单逻辑 — 价格从entry_price的盈利方向穿越即触发.
    #   BUY:  当前价 ≤ entry_price × 1.01 (回落到入场价或更低 → 买)
    #   SHORT: 当前价 ≥ entry_price × 0.99 (反弹到入场价或更高 → 做空)
    # SL控制风险: 触发后仍检查是否已越过stop_loss.
    from app.playbook.config import get_playbook_config
    pb_cfg = get_playbook_config()

    # ── 触发条件: 顺势直接触发 + 逆势追入需量能确认 (质量门设计) ──
    # 设计原则 (非历史回填): 入场质量决定胜率, 分两类:
    #   (1) 有利方向 (回调买入 / 反弹做空): 价格来到计划入场价的有利一侧 → 最优 RR, 直接触发.
    #       仅设"接飞刀"下限, 避免价格已深度背离计划位仍入场.
    #   (2) 追入 (突破追涨 / 破位追空): 价格越过计划位, 属动量确认场景 →
    #       必须有成交量放大确认 (vol_ratio ≥ 阈值), 无量突破视为假突破, 不追.
    # 量能数据缺失时 fail-open, 避免因行情源无该字段而全部漏单.
    # P0 修复 (2026-07-10): vol_ratio 的"无数据"哨兵是 1.0 而非 0 —
    #   market_data_router.setdefault("volume_ratio", 1.0) 与 lv3_technical_calculator
    #   在 vol_ma5 缺失时均回落 1.0. Futu LV3 主链路盘中不算 volume_ratio → 恒为 1.0.
    #   原 fail-open 只覆盖 <=0, 1.0 会走 "1.0 < 1.5 → False" → 所有追入被永久阻断.
    #
    # 终局修复 (2026-07-10): _vol_ok = True (移除量能门).
    #   根因: volume_ratio 来自日K线 (get_daily_kline 返回历史日线, 非盘中实时),
    #   即 昨日成交量/5日均量 ≈ 0.7-1.3. 这对盘中突破量能确认零预测力.
    #   <=1.0 修复仍留 dead zone (1.01-1.49 → ~45%正常股票被无理由阻断).
    #   日级别 vol_ratio 不是 intraday breakout 的有效信号 → 移除门, 不假装有信号.
    #   保护仍来自: +5%追入带 + 3%接飞刀 + 8%极端gap过滤 + SL检查.
    #   未来: 实现真正的盘中 vol_ratio (1分钟K线聚合今日累计量/同时段均量) 后再加回.
    # 单一真源 app/playbook/entry_band.py — 执行端与 DA 直写共用同一 band 定义, 防百分比漂移/记录冲突.
    from app.playbook.entry_band import CHASE_ZONE_PCT as _chase_zone, KNIFE_FLOOR_PCT as _knife_floor
    _vol_ok = True

    if is_short:
        if current_price >= entry_price:
            # 反弹到入场价或更高 → 更优做空点, 直接触发 (设上限防已冲高背离)
            triggered = current_price <= entry_price * (1 + _knife_floor)
        elif current_price >= entry_price * (1 - _chase_zone):
            # 跌破入场价 → 追空, 需量能确认 (防无量假跌)
            triggered = _vol_ok
        else:
            triggered = False
    else:
        if current_price <= entry_price:
            # 回调到入场价或更低 → 更优买点, 直接触发 (设下限防接飞刀)
            triggered = current_price >= entry_price * (1 - _knife_floor)
        elif current_price <= entry_price * (1 + _chase_zone):
            # 突破入场价 → 追涨, 需量能确认 (防无量假突破)
            triggered = _vol_ok
        else:
            triggered = False

    if not triggered and current_price > 0:
        logger.debug(
            f"[playbook] 触发门未过: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} vol_ratio={vol_ratio} "
            f"dir={'SHORT' if is_short else 'BUY'}"
        )

    # 极端gap过滤 (>8%): 暴涨暴跌中不入场
    _price_gap = abs(current_price - entry_price) / entry_price
    if _price_gap > 0.08:
        logger.warning(
            f"[playbook] ⚔️ {symbol} 极端gap, 跳过触发: "
            f"current=${current_price:.2f} entry=${entry_price:.2f} gap={_price_gap:.1%}"
        )
        _emit_trigger_miss("extreme_gap", current_price)
        return False

    # 2026-07-11: 入场重锚 (Lever A) — 带内未触发但漂移在上限内, 用盘中价重锚触发.
    # 下游 auto_execute 的 re-price 块会以实时 bid/ask 重算 entry/SL/TP (风险% 不变),
    # 故这里无需改 DB/SL; 超上限→不追, 维持跳过. extreme_gap>8% 已在上方拦截.
    _reanchored = False
    if not triggered:
        try:
            _ra_enabled = bool(pb_cfg.get("entry_reanchor_enabled", False))
            _ra_max_drift = float(pb_cfg.get("entry_reanchor_max_drift_pct", 4.0)) / 100.0
        except Exception:
            _ra_enabled, _ra_max_drift = False, 0.04
        if _ra_enabled and _price_gap <= _ra_max_drift:
            _reanchored = True
            triggered = True
            _emit_trigger_miss("reanchored", current_price)
            logger.info(
                f"[playbook] 入场重锚触发: {symbol} price=${current_price:.2f} "
                f"entry=${entry_price:.2f} 漂移={_price_gap:.1%} (<= {_ra_max_drift:.0%}), 下游将 re-price"
            )

    # 入场前止损检查: 如果当前价格已越过 stop_loss, 不入场
    # 重锚单跳过此检查: 旧 SL 是盘前值, 下游 re-price 会基于新 entry 重算 SL
    if triggered and not _reanchored:
        stop_loss = float(inst.get("stop_loss", 0) or 0)
        if stop_loss > 0:
            if is_short and current_price >= stop_loss:
                logger.warning(f"[playbook] ⚔️ {symbol} current=${current_price:.2f} 已越过 SHORT SL=${stop_loss:.2f}")
                _emit_trigger_miss("sl_crossed", current_price)
                return False
            if not is_short and current_price <= stop_loss:
                logger.warning(f"[playbook] ⚔️ {symbol} current=${current_price:.2f} 已越过 LONG SL=${stop_loss:.2f}")
                _emit_trigger_miss("sl_crossed", current_price)
                return False

    # ── 2026-07-15: VWAP 方向一致性硬门 (Tier1, 免标定) ──
    #   规则: LONG 要价在当日 VWAP 上侧(dev>=tol), SHORT 要价在下侧(dev<=-tol), 逆侧拦.
    #   2026-07-16 修正: 改用当日累计 VWAP ctx["vwap"] (K线 cum(tp×vol)/cum(vol), K_1M 可靠填充);
    #     原 ticker_vwap_deviation 仅 ~250 笔滚动VWAP — 语义错(非当日锚, 突破瞬间近乎恒真),
    #     且依赖 futu push tickers>=10 常缺失. dev=(price-vwap)/vwap; VWAP缺失(盘初K线不足)→fail-open.
    #   config: entry_vwap_gate_enabled(开关) / entry_vwap_gate_tolerance(0.003=0.3%, 模块噪声底).
    breakout_triggered = triggered
    _vwap_dev = None
    _vwap_populated = False
    _vwap_val = None
    try:
        if isinstance(ctx, dict) and current_price and current_price > 0:
            _vw = ctx.get("vwap")
            if _vw is not None and float(_vw) > 0:
                _vwap_val = float(_vw)
                _vwap_dev = (float(current_price) - _vwap_val) / _vwap_val
                _vwap_populated = True
    except Exception:
        _vwap_dev, _vwap_populated, _vwap_val = None, False, None

    try:
        _gate_tol = float(pb_cfg.get("entry_vwap_gate_tolerance", 0.0) or 0.0)
    except Exception:
        _gate_tol = 0.0
    _gate_block = False
    if not _vwap_populated:
        _gate_reason = "vwap_missing"  # fail-open: 无 VWAP 数据不拦
    else:
        if is_short:
            _gate_block = _vwap_dev > _gate_tol   # 价在 VWAP 上侧 → 逆 SHORT
        else:
            _gate_block = _vwap_dev < -_gate_tol  # 价在 VWAP 下侧 → 逆 LONG
        _gate_reason = "wrong_side" if _gate_block else "aligned"

    try:
        _gate_enabled = bool(pb_cfg.get("entry_vwap_gate_enabled", False))
    except Exception:
        _gate_enabled = False

    if _gate_enabled and triggered and _gate_block:
        triggered = False
        logger.info(
            f"[playbook] VWAP门拦截: {symbol} price=${current_price:.2f} vwap=${_vwap_val:.2f} "
            f"dev={_vwap_dev:+.4f} dir={'SHORT' if is_short else 'BUY'} (当日VWAP逆侧)"
        )
        _emit_trigger_miss("vwap_gate_block", current_price)

    if triggered:
        logger.info(
            f"[playbook] 方向穿越触发: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} dir={'SHORT' if is_short else 'BUY'}"
        )
    else:
        logger.debug(
            f"[playbook] 未触发: {symbol} price=${current_price:.2f} "
            f"entry=${entry_price:.2f} dir={'SHORT' if is_short else 'BUY'}"
        )
        # 2026-07-11: 带内未触发 — 分类追高/接飞刀 (BUY: >+5%追 / <-3%刀; SHORT 对称)
        if is_short:
            _miss_reason = "knife_above" if current_price > entry_price else "runaway_down"
        else:
            _miss_reason = "runaway_up" if current_price > entry_price else "knife_below"
        _emit_trigger_miss(_miss_reason, current_price)

    _emit_entry_shadow(
        triggered,
        breakout_val=breakout_triggered,
        gate_enabled=_gate_enabled,
        gate_block=_gate_block,
        gate_reason=_gate_reason,
        vwap_dev=_vwap_dev,
        vwap_populated=_vwap_populated,
    )
    return triggered


def _close_trade_record(conn, trade_id: int, exit_price: float, exit_reason: str,
                        pnl: float, pnl_pct: float, holding_hours: float,
                        inst_id = None, symbol: str = None,
                        close_futu_order_id: str = None) -> None:
    """统一平仓写入 — 原子更新 trade_records + playbook_instructions + open_positions.

    根因修复 (2026-07-01): 之前只更新 trade_records + instructions,
    不碰 open_positions → position_sync 从 DB 重建幽灵仓 → 开盘后异常平仓.
    现在直接 DELETE open_positions, 从源头消灭状态分裂.
    2026-07-14: 软删除→物理删除 (trade_records已有完整审计记录, 软删除行无消费者).
    根因修复 (2026-07-07): 
      1. 增加 close_futu_order_id 参数, 确保平仓订单可追溯.
      2. 从 trade_records 查 entry_time, 用 get_market_hours_us 重算 holding_hours.
         旧逻辑: 调用方传 wall-clock 差值 (naive-aware TypeError→0, 或 CST时间差).
         新逻辑: 统一用交易时段计算, 与 models.py holding_hours property 对齐.
    2026-07-09: 从 metadata_json 提取 mfe_pct/mae_pct 写入独立列 (MFE/MAE采集).
    """
    # 2026-07-03 fix: 用 ET 时区写入 exit_time, 与 entry_time 格式对齐 (均 tz-aware)
    try:
        now_iso = datetime.now(_now_et()).isoformat()
    except Exception:
        now_iso = datetime.now().isoformat()

    # 2026-07-09: 从 metadata 提取 MFE/MAE
    # 2026-07-10 fix: metadata["mfe_pct"] 键几乎从不写 — monitor_playbook_sim_exits 只在
    #   `if not exit_reason` 分支累积 mfe_pct, 触发退出那一轮直接平仓跳过累积块,
    #   导致 393/397 笔 mfe_pct=0 (含 121/122 盈利单, 逻辑违规).
    #   根因: metadata["peak_price"] 是可靠持久化的 (入场写入 + 每轮更新), 但从未回算成 mfe_pct.
    #   修复: mfe_pct 缺失/为0时, 用真峰值 peak_price + entry_price/direction 反算真实 MFE%.
    #   历史无 intraday 缓存无法回填, 此修复对新平仓的单子向前生效 (纯 surgical, 不碰热路径).
    _mfe_pct = 0.0
    _mae_pct = 0.0
    try:
        _meta_row = conn.execute(
            "SELECT metadata_json, direction, entry_price FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if _meta_row and _meta_row[0]:
            _meta = json.loads(_meta_row[0])
            _mfe_pct = float(_meta.get("mfe_pct", 0) or 0)
            _mae_pct = float(_meta.get("mae_pct", 0) or 0)
            # mfe_pct 缺失 → 从 peak_price 反算 (peak_price 是持久化的真峰值)
            if _mfe_pct <= 0:
                _peak = float(_meta.get("peak_price", 0) or 0)
                _entry = float(_meta_row[2] or 0)
                _dir = (_meta_row[1] or "LONG").upper()
                if _peak > 0 and _entry > 0:
                    if _dir == "LONG":
                        _calc_mfe = (_peak - _entry) / _entry * 100
                    else:
                        _calc_mfe = (_entry - _peak) / _entry * 100
                    if _calc_mfe > 0:
                        _mfe_pct = round(_calc_mfe, 4)
            # 2026-07-16: MAE 对称反算 — 与 MFE 同理, mae_pct 缺失时从 trough_price 反算.
            #   根因: trough_price 入场写入 + 每轮更新 (与 peak 对称), 但 close 时从未回算成 mae_pct,
            #   导致 MAE 常年为 0, 无法评估止损距离/浮亏承受度. 纯 surgical, 对新平仓向前生效.
            if _mae_pct <= 0:
                _trough = float(_meta.get("trough_price", 0) or 0)
                _entry = float(_meta_row[2] or 0)
                _dir = (_meta_row[1] or "LONG").upper()
                if _trough > 0 and _entry > 0:
                    if _dir == "LONG":
                        _calc_mae = (_entry - _trough) / _entry * 100
                    else:
                        _calc_mae = (_trough - _entry) / _entry * 100
                    if _calc_mae > 0:
                        _mae_pct = round(_calc_mae, 4)
            # 定义兜底: MFE 必须 ≥ 已落袋的有利变动 (pnl_pct 为分数, 转百分比).
            #   处理采样粒度问题: 峰值轮询未抓到最高点 (如 META peak<exit) 时不至低估 MFE.
            _realized_fav = round(pnl_pct * 100, 4)
            if _realized_fav > _mfe_pct:
                _mfe_pct = _realized_fav
            # 对称兜底: 亏损单的 MAE 必须 ≥ 已实现的不利变动 (谷值轮询未抓到最低点时不低估 MAE).
            _realized_adv = round(-pnl_pct * 100, 4)
            if _realized_adv > _mae_pct:
                _mae_pct = _realized_adv
    except Exception:
        pass
    # 2026-07-11: 量纲夹取 — 日内单笔 MFE/MAE >50% 基本是脏价位 (entry_price 异常等),
    #   判脏归 0 (0 落库视同缺失, 由回填/后续采集修正)。与 monitor 循环采集处阈值一致。
    _MFE_MAE_SANE_MAX = 50.0
    if _mfe_pct > _MFE_MAE_SANE_MAX:
        _mfe_pct = 0.0
    if _mae_pct > _MFE_MAE_SANE_MAX:
        _mae_pct = 0.0

    # 2026-07-13: 退出参数影子 — close 时对比"更紧 breakeven/drawdown 变体"反事实 capture。
    #   record-only, 读 metadata._exit_shadow(各变体首触发)+peak/trough, 落 JSONL。自带 try/except。
    try:
        from app.playbook.exit_shadow import finalize_exit_shadow
        _es_row = conn.execute(
            "SELECT metadata_json, direction, entry_price FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if _es_row and _es_row[0]:
            _es_meta = json.loads(_es_row[0])
            finalize_exit_shadow(
                symbol=symbol or "", direction=(_es_row[1] or "LONG"),
                entry_price=float(_es_row[2] or 0), live_exit_price=float(exit_price or 0),
                peak_price=float(_es_meta.get("peak_price", 0) or 0),
                trough_price=float(_es_meta.get("trough_price", 0) or 0),
                live_exit_reason=exit_reason, held_minutes=float(holding_hours or 0) * 60,
                meta=_es_meta, inst_id=inst_id,
            )
    except Exception:
        pass

    # ── 根因修复 (2026-07-07): 从 DB 查 entry_time, 用 get_market_hours_us 重算 holding_hours ──
    # 消灭所有调用方传 wall-clock 差值的变体 (naive TypeError, CST时间差, 纯秒数差).
    try:
        _row = conn.execute("SELECT entry_time FROM trade_records WHERE id=?", (trade_id,)).fetchone()
        if _row and _row[0]:
            _entry_dt = datetime.fromisoformat(_row[0])
            _et_tz = _now_et()
            _now_dt = datetime.now(_et_tz)
            # 2026-07-16 P1 修复: entry_time 可能是 naive (CST 无 tz), now_dt 是 tz-aware (ET).
            #   两者相减抛 TypeError → holding_hours 回退为调用方传入值 (0.0).
            #   修复: naive entry_dt 加上 ET tz 再相减, 或用 wall-clock fallback.
            if _entry_dt.tzinfo is None:
                _entry_dt = _entry_dt.replace(tzinfo=_et_tz)
            from app.risk.position_exit_monitor import get_market_hours_us
            _market_hh = get_market_hours_us(_entry_dt, _now_dt)
            if _market_hh > 0:
                holding_hours = round(_market_hh, 1)
            else:
                # fallback: wall-clock 差值 (比 0 好)
                holding_hours = round((_now_dt - _entry_dt).total_seconds() / 3600, 1)
    except Exception as _hh_err:
        logger.debug(f"[_close_trade_record] holding_hours 重算失败 trade_id={trade_id}: {_hh_err}")
        # 降级: 用调用方传入的 holding_hours (比0好)

    # 2026-07-16 P1 修复: pnl_pct 兜底反算.
    #   旧逻辑: pnl_pct 由调用方传入, 但某些路径 (旧管线写入/早期代码) 传 0 →
    #   下游所有按 pnl_pct 的分析 (影子退出/归因/回测) 全部出错.
    #   修复: pnl_pct=0 且 pnl≠0 时, 用 exit_price/entry_price 反算.
    if abs(pnl_pct) < 1e-10 and abs(pnl) > 1e-6 and exit_price:
        try:
            _ep_row = conn.execute(
                "SELECT direction, entry_price FROM trade_records WHERE id=?", (trade_id,)
            ).fetchone()
            if _ep_row:
                _dir = str(_ep_row[0] or "LONG").upper()
                _ent = float(_ep_row[1] or 0)
                if _ent > 0:
                    if _dir == "SHORT":
                        pnl_pct = (_ent - exit_price) / _ent
                    else:
                        pnl_pct = (exit_price - _ent) / _ent
        except Exception:
            pass

    # 2026-07-11: exit_telemetry — 平仓时集中组装退出画像写回 metadata_json,
    #   供 config_version×regime×session 分层复盘 (命中规则/峰谷价位/MFE-MAE/保本触发).
    #   只聚合已持久化字段, 不虚构 effective_sl (未持久化 → 存原料 initial_sl+breakeven_triggered).
    try:
        _tel_row = conn.execute(
            "SELECT metadata_json, entry_price FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if _tel_row and _tel_row[0]:
            _tmeta = json.loads(_tel_row[0])
            _tmeta["exit_telemetry"] = {
                "exit_reason": exit_reason,
                "entry_price": float(_tel_row[1] or 0),
                "exit_price": round(float(exit_price or 0), 4),
                "peak_price": float(_tmeta.get("peak_price", 0) or 0),
                "trough_price": float(_tmeta.get("trough_price", 0) or 0),
                "mfe_pct": round(_mfe_pct, 4),
                "mae_pct": round(_mae_pct, 4),
                "pnl_pct": round(pnl_pct * 100, 4),
                "holding_hours": round(holding_hours, 2),
                "breakeven_triggered": bool(_tmeta.get("breakeven_triggered", False)),
                "initial_sl": float(_tmeta.get("stop_loss", 0) or 0),
                "take_profit": float(_tmeta.get("take_profit", 0) or 0),
                "recorded_at": now_iso,
            }
            conn.execute(
                "UPDATE trade_records SET metadata_json=? WHERE id=?",
                (json.dumps(_tmeta, ensure_ascii=False, default=str), trade_id),
            )
    except Exception as _tel_err:
        logger.debug(f"[_close_trade_record] exit_telemetry 组装失败 trade_id={trade_id}: {_tel_err}")

    if close_futu_order_id:
        conn.execute("""
            UPDATE trade_records
            SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, close_futu_order_id=?, mfe_pct=?, mae_pct=?, max_drawdown=?
            WHERE id=?
        """, (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), close_futu_order_id, round(_mfe_pct, 4), round(_mae_pct, 4), round(_mae_pct, 4), trade_id))
    else:
        conn.execute("""
            UPDATE trade_records
            SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?, mfe_pct=?, mae_pct=?, max_drawdown=?
            WHERE id=?
        """, (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), round(_mfe_pct, 4), round(_mae_pct, 4), round(_mae_pct, 4), trade_id))
    if inst_id:
        _is_ghost = "ORPHAN" in exit_reason.upper() or "PHANTOM" in exit_reason.upper() or "GHOST" in exit_reason.upper()
        _outcome = "void" if _is_ghost else ("win" if pnl > 0 else "loss")
        # 2026-07-16 P0 修复: 幂等条件加入 'void', 防止已标 void 的幽灵仓被重复处理.
        #   旧逻辑 WHERE outcome NOT IN ('win','loss') — void 不在排除列表 →
        #   幽灵仓可被重复平仓写入. 修复: 排除 'void' 终态.
        conn.execute(
            "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
            "holding_period_hours=? WHERE id=? AND outcome NOT IN ('win','loss','void')",
            (_outcome, exit_price, exit_reason, round(pnl, 2),
             round(pnl_pct, 4), round(holding_hours, 1), inst_id),
        )
    # 根因修复: 同时关闭 open_positions, 消灭幽灵仓源头
    # 2026-07-14: 软删除→直接DELETE, 软删除行无消费者(所有查询都过滤sync_status),
    #             且auto_position_reconcile只在09:30 ET运行, 非交易日/非开盘时残留→EOD报告误读
    if symbol:
        # 2026-07-16 P1 修复: sync_status NULL 行也会被删除 (旧 NOT IN 不匹配 NULL).
        #   正常流程 sync_status 会被设置, 但重启恢复或异常写入可能 NULL.
        conn.execute(
            "DELETE FROM open_positions WHERE symbol=? "
            "AND (sync_status IS NULL OR sync_status NOT IN "
            "('closed_by_daemon','closed_by_sync','closed_emergency','stale_not_in_broker'))",
            (symbol,)
        )
    conn.commit()

    # 2026-07-03: PLAYBOOK_SIM 平仓后自动归因 (之前只在 manual_order_service 调用, 212笔0归因)
    if symbol and exit_price > 0:
        try:
            from app.analysis.trade_attribution import compute_attribution
            _row = conn.execute(
                "SELECT direction, entry_price, quantity, entry_time, metadata_json FROM trade_records WHERE id=?",
                (trade_id,)
            ).fetchone()
            if _row:
                # 7/7 增强: 传入 peak_price 用于真实 MFE 计算
                # 2026-07-13: 同时传 trough_price (07-11 起记录), 让入场时机/止损效率维度用真实谷值
                _peak_price = None
                _trough_price = None
                try:
                    _tmp_meta = json.loads(_row[4]) if _row[4] else {}
                    _peak_price = float(_tmp_meta.get("peak_price", 0)) or None
                    _trough_price = float(_tmp_meta.get("trough_price", 0)) or None
                except Exception:
                    pass
                _attr = compute_attribution(
                    symbol=symbol,
                    direction=_row[0] or "LONG",
                    entry_price=float(_row[1] or 0),
                    exit_price=exit_price,
                    quantity=int(_row[2] or 0),
                    entry_time=_row[3] or now_iso,
                    exit_time=now_iso,
                    peak_price=_peak_price,
                    trough_price=_trough_price,
                )
                logger.info(f"[归因] {symbol}: {_attr.summary} | {_attr.improvement}")
                # 写入 metadata_json (与 manual_order_service 一致)
                try:
                    _existing_meta = json.loads(_row[4]) if _row[4] else {}
                except Exception:
                    _existing_meta = {}
                _existing_meta["attribution"] = _attr.to_dict()
                conn.execute(
                    "UPDATE trade_records SET metadata_json=? WHERE id=?",
                    (json.dumps(_existing_meta, ensure_ascii=False), trade_id)
                )
                conn.commit()
        except Exception as _attr_e:
            logger.warning(f"[归因] {symbol} 自动归因失败 (不影响平仓): {_attr_e}", exc_info=True)

    # 2026-07-16: playbook 平仓逐笔飞书推送 (复用 PushHub trade_exit)。
    #   来源精准标注 — 按 trade_records.source_type 映射 strategy (PLAYBOOK_SIM→playbook_sim,
    #   PLAYBOOK_LIVE→playbook_live); shadow 仓不推 (纯影子追踪, 非真实成交)。
    #   自带 try/except, 绝不影响平仓。dedup 用 trade_id 保证每笔平仓唯一推送。
    try:
        _pb_src_row = conn.execute(
            "SELECT direction, source_type FROM trade_records WHERE id=?", (trade_id,)
        ).fetchone()
        _pb_src_type = ((_pb_src_row[1] if _pb_src_row else "") or "").upper()
        _pb_strategy_map = {"PLAYBOOK_SIM": "playbook_sim", "PLAYBOOK_LIVE": "playbook_live"}
        _pb_strategy = _pb_strategy_map.get(_pb_src_type)
        if symbol and _pb_strategy:
            from app.push import get_push_hub, trade_exit as _pb_exit
            get_push_hub().push(_pb_exit(
                symbol=symbol,
                direction=(_pb_src_row[0] if _pb_src_row else "LONG") or "LONG",
                pnl=round(float(pnl or 0), 2),
                exit_reason=exit_reason,
                strategy=_pb_strategy,
                trace_id=str(trade_id),
            ))
    except Exception as _pb_push_e:
        logger.warning(f"[playbook推送] {symbol} 平仓推送失败 (不影响平仓): {_pb_push_e}")


def _get_factor_exit_config(conditions: list, factor_rules: dict, direction: str = None) -> dict:
    """根据触发因子选择对应的退出规则配置 (Phase 3 2026-06-30).

    7/7 fix: 方向感知匹配。BUY 优先匹配 BULLISH_CONDITIONS, SHORT 优先匹配 BEARISH_CONDITIONS。
    避免 SHORT 指令因第一个 condition 是趋势类而配上 trailing=true 的退出策略。

    Args:
        conditions: 触发条件列表 (e.g. ['insider_cluster_buy', 'above_ma20'])
        factor_rules: playbook_horizon.yaml 顶层 factor_exit_rules 字典
        direction: "LONG"/"SHORT" — 方向感知匹配 (None 时退回原始逻辑)

    Returns:
        匹配到的因子退出规则, 未命中返回 default
    """
    default = factor_rules.get("default", {})
    if not conditions:
        return default

    # 7/7 fix: 方向感知 — 优先匹配与方向一致的 condition
    if direction:
        from app.playbook.condition_taxonomy import BULLISH_CONDITIONS, BEARISH_CONDITIONS
        _preferred = BULLISH_CONDITIONS if direction == "LONG" else BEARISH_CONDITIONS
        # 第一轮: 方向一致 + 有 exit_rule 的 condition
        for cond in conditions:
            if cond in factor_rules and cond in _preferred:
                return factor_rules[cond]

    # 第二轮: 方向一致的没匹配到, 退回第一个有 exit_rule 的 condition
    for cond in conditions:
        if cond in factor_rules:
            return factor_rules[cond]

    return default


def _compute_dynamic_effective_sl(
    direction: str,
    entry_price: float,
    initial_sl: float,
    peak_profit_pct: float,
    exit_cfg: dict,
) -> float:
    """根据因子退出规则计算动态 effective stop loss.

    逻辑:
    - breakeven_at_pct: 浮盈达到该阈值, SL 提到 entry (保本)
    - drawdown_from_peak_pct: 从峰值回撤超过该百分比时, SL锁定到 peak×(1-drawdown)
      2026-07-09: 替代原步进trailing. 原trailing_step=2%但MFE中位数仅1.5%, 永远触发不了.
      新逻辑: 浮盈2%→峰值, 回撤0.6%→SL锁在peak-0.6%=1.4%利润. 高波动美股也能落袋.
    - 兼容: 若无drawdown_from_peak_pct配置, 回退原步进逻辑(最多3步).
    """
    breakeven_pct = float(exit_cfg.get("breakeven_at_pct", 0.5))
    effective_sl = initial_sl

    if peak_profit_pct < breakeven_pct:
        return effective_sl

    # 浮盈超过 breakeven: 先保本
    if direction == "LONG":
        effective_sl = max(effective_sl, entry_price)
    else:
        effective_sl = min(effective_sl if effective_sl > 0 else entry_price, entry_price)

    if not exit_cfg.get("trailing_enabled", False):
        return effective_sl

    # ── 回撤止损 (新逻辑, 2026-07-09) ──
    drawdown_pct = float(exit_cfg.get("drawdown_from_peak_pct", 0))
    if drawdown_pct > 0 and peak_profit_pct > breakeven_pct:
        # SL = peak_price × (1 - drawdown_pct/100), 但不低于保本
        if direction == "LONG":
            peak_price = entry_price * (1 + peak_profit_pct / 100)
            dd_sl = peak_price * (1 - drawdown_pct / 100)
            effective_sl = max(effective_sl, dd_sl)
        else:
            peak_price = entry_price * (1 - peak_profit_pct / 100)
            dd_sl = peak_price * (1 + drawdown_pct / 100)
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, dd_sl)
        return effective_sl

    # ── 回退: 原步进 trailing (兼容旧配置) ──
    trailing_step = float(exit_cfg.get("trailing_step_pct", 2.0))
    if trailing_step > 0:
        for step in range(1, 4):  # 最多 3 步
            threshold = breakeven_pct + trailing_step * step
            if peak_profit_pct < threshold:
                break
            locked_pct = breakeven_pct + trailing_step * (step - 1) + trailing_step * 0.5
            if direction == "LONG":
                effective_sl = max(effective_sl, entry_price * (1 + locked_pct / 100))
            else:
                effective_sl = min(effective_sl if effective_sl > 0 else entry_price,
                                 entry_price * (1 - locked_pct / 100))

    return effective_sl


def is_playbook_open_position(symbol: str) -> bool:
    """统一判定: 该 symbol 是否有未平仓的 Playbook 仓位 (由 monitor_playbook_sim_exits 自管).

    所有旧管线入口 (position_sync, position_monitor, scheduler_core 等) 必须调用此函数
    跳过 Playbook 管理的仓位, 防止双重管理/重复平仓.

    判定标准: trade_records 中存在 source_type='PLAYBOOK_SIM' 且 exit_time 为空的记录.
    不使用时间窗口 (旧代码用 yesterday/2-days-ago 导致跨日/跨窗口漏判).
    """
    try:
        from app.core.paths import get_trade_records_db
        import sqlite3 as _pb_sql
        _c = _pb_sql.connect(str(get_trade_records_db()), timeout=3)
        _row = _c.execute(
            "SELECT 1 FROM trade_records WHERE symbol=? AND source_type='PLAYBOOK_SIM' "
            "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL) LIMIT 1",
            (symbol,)
        ).fetchone()
        _c.close()
        return _row is not None
    except Exception:
        return False


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str):
    """共用退出规则判断 — 被 monitor_playbook_sim_exits 调用.

    计算盈利百分比 → peak tracking → breakeven ratchet / trailing stop (factor-specific) → 5 条退出规则.
    退出规则 (按优先级):
      [1] SL_INITIAL/SL_BREAKEVEN/SL_TRAIL_LOCK: effective_sl 触及 (按初始止损/保本/锁利区分)
      [2] OVERNIGHT_GAP_STOP: cohort B 隔夜仓跳空 >3%
      [3] PRE_CLOSE_EXIT: 收盘前 8min (cohort B 盈利可跳过)
      [4] TIME_EXIT: 持仓 > factor_time_exit_hours 且 盈利<0 (factor-specific)
      [5] MAX_HOLD_T5: cohort B 持仓≥5 交易日

    Args:
        direction: "LONG"/"SHORT"
        entry_price: 入场价
        current_price: 当前报价
        initial_sl: 初始 stop_loss (meta["stop_loss"])
        peak_price: 历史峰值价 (从 meta 恢复, 函数内更新后返回)
        meta: metadata dict (读取 cohort/_skip_allowed/_log_tag/_symbol/conditions)
        held_minutes: 持仓分钟数
        minutes_to_close: 距收盘剩余分钟 (ET)
        minutes_from_open: 开盘后分钟数 (ET)
        entry_time_str: entry_time ISO 字符串

    Returns: (exit_reason, exit_price, peak_price, breakeven_triggered)
        exit_reason 为 "" 表示不退出; 调用方负责 peak_price metadata 写回.
    """
    _log_tag = meta.get("_log_tag", "playbook")
    _symbol = meta.get("_symbol", "")

    # ── 盈利百分比计算 ──
    if direction == "LONG":
        profit_pct = (current_price - entry_price) / entry_price * 100
    else:
        profit_pct = (entry_price - current_price) / entry_price * 100

    # ── peak tracking (从参数恢复, 更新后返回) ──
    if direction == "LONG":
        if current_price > peak_price:
            peak_price = current_price
    else:
        if peak_price == 0 or current_price < peak_price:
            peak_price = current_price

    if peak_price > 0:
        if direction == "LONG":
            peak_profit_pct = (peak_price - entry_price) / entry_price * 100
        else:
            peak_profit_pct = (entry_price - peak_price) / entry_price * 100
    else:
        peak_profit_pct = 0

    # ── 读取 horizon 配置 + 因子退出规则 (Phase 3 2026-06-30) ──
    _cohort = meta.get("cohort", "A")
    _horizon = meta.get("horizon", "short")
    # _skip_allowed: 盈利仓跳过 EOD 的条件
    _skip_allowed = meta.get("_skip_allowed", _cohort == "B" or _horizon == "long")
    _horizon_cfg = _load_horizon_config(_horizon)
    _overnight_gap_stop_pct = _horizon_cfg.get("overnight_gap_stop_pct", 3.0)

    from app.playbook.config import get_playbook_config
    _pb_cfg = get_playbook_config()
    _factor_exit_rules = _pb_cfg.get_horizon_top("factor_exit_rules", {})
    _default_exit_cfg = _factor_exit_rules.get("default", {})

    # 改动9 [已退役 2026-07-11]: exit_param_override.json 合并 — 测量优先重构下移除.
    #   根因: 消费端只匹配 TIME_EXIT, 但 Hermes feedback_loop 生成端按 direction+exit_reason
    #   分组【混合 v1+v2】全样本, 可能产出 LONG+TIME_EXIT 从而【静默篡改】factor_exit_rules —
    #   典型"打影子"(用旧策略数据硬调新策略退出参数). 此处移除消费端后影响链已切断,
    #   生成的 json 现无人读取(惰性). 生成端在 Hermes skill (工作区外, feedback_loop.py:348/439),
    #   需手动注掉那两行调用才能彻底停写. 参数重设留到 Section 5 启用门槛满足后、在 v2 数据上另起执行阶段.
    #   (原合并逻辑见 git 历史; 此处保持 no-op, factor_exit_rules 仅由 yaml 决定.)

    # 根据触发因子选择退出规则 (7/7: 传 direction 做方向感知匹配)
    _conditions = meta.get("conditions", [])
    _exit_cfg = _get_factor_exit_config(_conditions, _factor_exit_rules, direction=direction)

    # 最大持有天数: 优先因子级, 其次 horizon 级
    _max_hold_days = int(
        _exit_cfg.get(
            "max_hold_days",
            _horizon_cfg.get("max_hold_days", 5 if _horizon == "long" else 1),
        )
    )

    # ── effective SL (factor-specific breakeven ratchet + trailing stop) ──
    # Phase 3 (2026-06-30): 按因子类型动态调整利润锁定/跟踪止损
    effective_sl = _compute_dynamic_effective_sl(
        direction, entry_price, initial_sl, peak_profit_pct, _exit_cfg
    )
    breakeven_triggered = peak_profit_pct >= float(_exit_cfg.get("breakeven_at_pct", 0.5))

    # ── 退出判断 (按优先级, if/elif/elif 链) ──
    # 优先级: SL > TAKE_PROFIT > OVERNIGHT_GAP > PRE_CLOSE > TIME_EXIT > MAX_HOLD
    exit_reason = ""
    exit_price = current_price

    # 规则0: 开盘冷却期 — 前15分钟SL放宽到4% (不改SL参数本身, 只加时间窗口保护)
    # 原因: 3/5笔在0-6min秒杀, MFE=0%, 价格从未浮盈. 开盘噪声大, 2%SL被瞬间打掉.
    # 诊断: 修后MFE>0%=时机问题, MFE仍=0%=信号问题
    _cooldown_pct = float(_pb_cfg.get("cooldown_sl_pct", 4.0))  # 2026-07-16: 从 config 读取, 默认 4%
    _cooldown_sl = entry_price * (1 - _cooldown_pct / 100.0) if direction == "LONG" else entry_price * (1 + _cooldown_pct / 100.0)
    _in_cooldown = minutes_from_open is not None and minutes_from_open < 15
    if _in_cooldown:
        _active_sl = min(effective_sl, _cooldown_sl) if direction == "LONG" else max(effective_sl, _cooldown_sl)
    else:
        _active_sl = effective_sl

    # 规则1: SL止损 (最高优先级 — 亏损仓位必须先检查止损)
    # 2026-07-14: 拆分原 SL_FIXED_PCT 复合标签. 同一标签曾混合三种机制(近30天9笔仅1笔
    #   真止损, 其余为保本+锁利), 污染退出侧诊断. 按 effective_sl 相对 entry 的位置区分,
    #   便于 exit_shadow / 报表按真实语义归因. 保留 SL_ 前缀以兼容 LIKE 'SL%' 过滤.
    if _active_sl > 0 and ((direction == "LONG" and current_price <= _active_sl) or
                           (direction == "SHORT" and current_price >= _active_sl)):
        # 2026-07-16 P0 修复: exit_price 用 current_price 而非 _active_sl.
        #   _active_sl 是止损线价格, 实际市价单成交价可能远低于 SL (滑点/gap).
        #   旧逻辑 exit_price = _active_sl 系统性高估退出价 → PnL 偏高.
        #   修复: 用 current_price (本轮报价) 作为退出价, 与 broker 实际成交更接近.
        #   最终退出价在调用方 (monitor_playbook_sim_exits) 用 broker filled_price 覆盖.
        exit_price = current_price
        if not breakeven_triggered:
            exit_reason = "SL_INITIAL"        # 浮盈未达 breakeven → 初始固定止损被打掉
        elif (direction == "LONG" and effective_sl > entry_price) or \
             (direction == "SHORT" and 0 < effective_sl < entry_price):
            exit_reason = "SL_TRAIL_LOCK"     # 回撤锁利 → SL 已锁在成本价之上(空:之下)
        else:
            exit_reason = "SL_BREAKEVEN"      # 保本 ratchet → SL 提到成本价附近

    # 规则1.5 (2026-06-28): 止盈退出 — 价格触及 take_profit 时主动锁定利润.
    # 智能跳过: rr_ratio=999 / take_profit=0 的指令不触发 (用户决策).
    # 2026-07-10: 增加 RR 底线 (MIN_TP_RR=1.5). 根因: short_horizon rr_ratio=999
    #   (设计上不设TP, 让 trailing/EOD 捕获利润) 但指令级 analyst TP 仍会触发,
    #   出现 NVDA SL-4.5%/TP+1.7%(RR=0.38) 的畸形几何 — 冒大风险博小利润.
    #   修法: TP 相对【初始SL】的 RR < 1.5 时忽略该 TP, 交给 trailing/EOD;
    #   RR>=1.5 的好几何(如 NU RR=2.26)仍正常止盈. 无初始SL时不做门禁(退回原行为).
    _take_profit = meta.get("take_profit", 0)
    _MIN_TP_RR = 1.5
    if not exit_reason and _take_profit and float(_take_profit) > 0:
        _tp = float(_take_profit)
        _tp_risk = abs(entry_price - initial_sl) if (initial_sl and initial_sl > 0) else 0
        _tp_reward = abs(_tp - entry_price)
        _tp_rr = (_tp_reward / _tp_risk) if _tp_risk > 0 else 999  # 无SL→不门禁
        if _tp_rr < _MIN_TP_RR:
            logger.debug(f"[TP-RR门禁] {_symbol} {direction} TP RR={_tp_rr:.2f}<{_MIN_TP_RR}, "
                         f"忽略指令TP(交给trailing/EOD锁利)")
        elif direction == "LONG" and current_price >= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price
        elif direction == "SHORT" and current_price <= _tp:
            exit_reason = "TAKE_PROFIT"
            exit_price = current_price

    # 规则2 (RFC-061 #6): 隔夜跳空止损
    if not exit_reason and _skip_allowed and _is_overnight_position(entry_time_str) and profit_pct <= -_overnight_gap_stop_pct:
        exit_reason = "OVERNIGHT_GAP_STOP"
        exit_price = current_price

    # 规则4: 盘末退出 — 7/7 fix: 恢复此规则
    # 原因: 7/3 fix 让 ExitMonitor 不再管 PLAYBOOK_SIM/LIVE 仓位 (reconciler 拦截→PROTECTED),
    #        6/30 DEB 把 pre_close 交给 ExitMonitor 的决策前提已不成立。
    #        PLAYBOOK 仓位的 pre_close 必须由 Pipeline 自管, 否则收盘前8min 盈利落袋无人执行。
    if not exit_reason and 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
        if _skip_allowed and profit_pct > 0:
            pass  # cohort B 盈利仓跳过, 持过夜
        else:
            exit_reason = "PRE_CLOSE_EXIT"
            exit_price = current_price

    # 规则3: 时间退出 — 7/7 fix: 恢复此规则, 由 factor_exit_rules.time_exit_hours 驱动
    # 原因: 6/30 DEB 禁用时把 time_exit 交给 exit_policy.yaml, 但 7/3 fix 后 ExitMonitor 不再管
    #        PLAYBOOK 仓位。PLAYBOOK 仓位的 time_exit 必须由 Pipeline 自管。
    # 安全保障: time_exit_only_if_loss=true → 只砍亏损仓, 盈利仓不受影响
    #           factor 差异化: volume_expansion 0.5h / default 2h / macd_golden_cross 6h
    _time_exit_enabled = True
    _time_exit_hours = float(_exit_cfg.get("time_exit_hours", 2.0))
    _time_exit_only_if_loss = bool(_exit_cfg.get("time_exit_only_if_loss", True))
    if _time_exit_enabled and not exit_reason and held_minutes > (_time_exit_hours * 60):
        if not _time_exit_only_if_loss or profit_pct < 0:
            exit_reason = "TIME_EXIT"
            exit_price = current_price

    # 规则5: MAX_HOLD — long horizon 最大持有期检查 (short horizon 默认不持夜, _skip_allowed 控制)
    if not exit_reason and _skip_allowed and held_minutes > 0:
        if _horizon_max_hold_hit(entry_time_str, _max_hold_days):
            exit_reason = "MAX_HOLD"
            exit_price = current_price

    return exit_reason, exit_price, peak_price, breakeven_triggered


def _load_horizon_config(horizon: str) -> Dict[str, Any]:
    """读取指定 horizon 的配置参数"""
    try:
        from app.playbook.config import get_playbook_config
        pb_cfg = get_playbook_config()
        return pb_cfg.get_horizon_params(horizon)
    except Exception as e:
        logger.debug(f"[playbook] 读取 horizon 配置失败: {e}, 使用默认")
        return {}


# 2026-07-11 (audit 结论): 因子黑名单(单因子 + 配对级) 暂停用.
#   审计证明其 99.5% 证据来自旧 config v1(v2 下这些组合几乎不交易/打平,
#   news_positive 单因子 v2 反而 +$70/35笔). 基于旧策略数据给新策略(v2)硬编码黑名单 = 打影子.
#   待 v2 积累足量真实样本后重新分层统计, 再决定是否启用. 重新启用: 置 True.
_FACTOR_BLACKLIST_ENABLED = False


# 2026-07-11: 配对级负 edge 闸门缓存 (从 playbook_factor_stats 读实证毒性配对)
_NEG_PAIR_CACHE: Dict[str, Any] = {"ts": 0.0, "pairs": None}


def _get_negative_factor_pairs(min_trades: int = 8, ttl_sec: int = 1800) -> set:
    """从 playbook_factor_stats 加载实证负 edge 的因子配对.

    条件: analysis_level='pair' 且 verdict='negative' 且 total_trades>=min_trades 且 total_pnl<0.
    返回: set[frozenset({factor_a, factor_b})].
    TTL 缓存 30min — 统计表由 deep_analysis 定期刷新, 无需每信号查库.
    命名与 pipeline 内 _conditions 同一套 condition_taxonomy, 直接匹配.
    """
    _now = time.time()
    if _NEG_PAIR_CACHE["pairs"] is not None and (_now - _NEG_PAIR_CACHE["ts"]) < ttl_sec:
        return _NEG_PAIR_CACHE["pairs"]
    pairs = set()
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute(
            "SELECT factor_key FROM playbook_factor_stats "
            "WHERE analysis_level='pair' AND verdict='negative' "
            "AND total_trades>=? AND total_pnl<0",
            (min_trades,),
        ).fetchall()
        conn.close()
        for (fk,) in rows:
            if fk and "|" in fk:
                parts = [p for p in fk.split("|") if p]
                if len(parts) == 2:
                    pairs.add(frozenset(parts))
    except Exception as e:
        logger.debug(f"[playbook] 负配对闸门加载失败 (忽略): {e}")
    _NEG_PAIR_CACHE["pairs"] = pairs
    _NEG_PAIR_CACHE["ts"] = _now
    return pairs


def _horizon_max_hold_hit(entry_time_str: str, max_hold_days: int) -> bool:
    """判断 long horizon 仓位是否已到 max_hold_days 交易日."""
    if not entry_time_str or max_hold_days <= 0:
        return False
    try:
        from datetime import datetime
        from app.utils.holiday_calendar import nth_trading_day_after
        _et = _now_et()
        _entry = datetime.fromisoformat(entry_time_str)
        if hasattr(_entry, "tzinfo") and _entry.tzinfo is not None:
            _entry_date = _entry.astimezone(_et).date()
        else:
            _entry_date = _entry.date()
        deadline = nth_trading_day_after("US", _entry_date, max_hold_days)
        if deadline is None:
            return False
        return datetime.now(_et).date() >= deadline
    except Exception as e:
        logger.debug(f"[playbook·horizon] MAX_HOLD 日期计算异常 (不强平): {e}")
        return False


# ── Phase 1 (2026-06-24): futu_simulate 真实仓位退出监控 ──────────────

def monitor_playbook_sim_exits(scheduler) -> int:
    """盘中监控 PLAYBOOK_SIM 真实模拟仓位的退出.

    与 monitor_playbook_shadow_exits 的区别: PLAYBOOK_SIM 是富途模拟账户的真实持仓,
    退出时必须调 DualBrokerAdapter.close_position(target_env="sim") 真实平仓,
    而非仅更新 trade_records 字段.

    退出规则 (与 shadow 对齐, 复用 exit_policy 阈值):
      [1] SL_INITIAL: 当前价触及原始 stop_loss (兜底)
      [2] TIME_EXIT: 持仓>2h 且 盈利<-0.5% → 退出 (2026-06-24放宽)
      [3] PRE_CLOSE_EXIT: 收盘前 8min → 全平

    Returns: 本轮平仓数量
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime
        from app.core.scheduler_triggers import ET as _et

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            conn.close()
            return 0

        logger.info(f"[playbook dual] 退出监控: {len(rows)} 个未平仓 sim/live 仓位待检查")

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook sim] 无 data_router, 跳过退出检查")
            conn.close()
            return 0

        # 市场时段 (ET) — 复用 scheduler_triggers.ET (pytz, 处理夏令时)
        now_et = datetime.now(_et)
        now_minutes = now_et.hour * 60 + now_et.minute
        minutes_to_close = 16 * 60 - now_minutes
        market_open_minutes = 9 * 60 + 30
        minutes_from_open = now_minutes - market_open_minutes

        # M1 修复 (2026-07-16): 开盘冷却 SL 放宽参数 — 与 _check_playbook_exit_rules 规则0 对齐.
        #   原始 SL 硬兜底 (下方) 此前直接比原始 stop_loss, 绕过了冷却放宽,
        #   导致开盘 15min 内价格触及原始(较紧)SL 就被兜底先斩, 架空"防开盘秒杀"意图.
        #   此处预取冷却参数 (循环不变量), 兜底处按 entry_price 逐仓放宽 SL floor.
        try:
            from app.playbook.config import get_playbook_config as _get_pb_cfg_m1
            _cooldown_sl_pct = float(_get_pb_cfg_m1().get("cooldown_sl_pct", 4.0))
        except Exception:
            _cooldown_sl_pct = 4.0
        _in_open_cooldown = minutes_from_open is not None and minutes_from_open < 15

        exited = 0
        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            if entry_price <= 0:
                continue

            meta = {}
            try:
                meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                pass
            stop_loss = float(meta.get("stop_loss", 0))
            inst_id = meta.get("playbook_instruction_id")
            qty = r["quantity"] or 0

            # 当前价
            current_price = None
            try:
                quote = data_router.get_quote(symbol)
                if quote and isinstance(quote, dict):
                    current_price = float(quote.get("last", 0) or quote.get("price", 0) or 0)
            except Exception:
                pass
            if not current_price or current_price <= 0:
                continue

            # 持仓时长 (在止损兜底之前计算, 兜底需要用到)
            # 2026-07-03 fix: entry_time 带 tz 后缀 (如 -04:00) 是 aware, datetime.now() 是 naive,
            # 两者相减抛 TypeError → held_minutes=0 → holding_hours=0.0. 改用 tz-aware now.
            held_minutes = 0
            try:
                _entry_dt = datetime.fromisoformat(r["entry_time"])
                _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
                held_minutes = (_now_ref - _entry_dt).total_seconds() / 60
            except Exception:
                pass

            # OKR 优化 (2026-06-26): 入场后 30 分钟冷却期.
            # 根因: PLAYBOOK_SIM 最近 7 笔闭环交易中, 3 笔在 1-5 分钟内被 SL_FIXED_PCT 扫出,
            #       属开盘噪声/假突破, 不是信号本身失效. 冷却期内跳过 SL/TIME_EXIT,
            #       仅保留 PRE_CLOSE_EXIT 兜底 (收盘前 8min 必须平).
            # 2026-07-02: grace期从30min降到5min — 30min让SL形同虚设
            # 原因: 开盘噪声在5min内消退, 30min内若暴跌SL不触发=无保护
            _GRACE_MINUTES = 5
            if held_minutes < _GRACE_MINUTES and minutes_to_close > 8:
                logger.debug(
                    f"[playbook sim] {symbol} 处于冷却期 "
                    f"(held={held_minutes:.1f}min < {_GRACE_MINUTES}min), 跳过退出检查"
                )
                continue

            # ponytail: 止损兜底 — 同 monitor_playbook_shadow_exits, 防止隔夜跳空越过 SL 被 TIME_EXIT 抢占
            if stop_loss > 0:
                # M1 修复 (2026-07-16): 开盘冷却期放宽兜底 SL, 与规则0一致.
                #   冷却窗内取原始 SL 与冷却 SL 中"更松"的一侧 (LONG 取更低价, SHORT 取更高价),
                #   避免开盘噪声触及较紧的原始 SL 被兜底秒杀.
                _sl_floor = stop_loss
                if _in_open_cooldown:
                    _cd_sl = entry_price * (1 - _cooldown_sl_pct / 100.0) if direction == "LONG" \
                        else entry_price * (1 + _cooldown_sl_pct / 100.0)
                    _sl_floor = min(stop_loss, _cd_sl) if direction == "LONG" else max(stop_loss, _cd_sl)
                _sl_hit = (direction == "LONG" and current_price <= _sl_floor) or \
                          (direction == "SHORT" and current_price >= _sl_floor)
                if _sl_hit:
                    logger.warning(
                        f"[playbook sim] {symbol} {direction} 止损兜底: "
                        f"price=${current_price:.2f} 触及 SL=${_sl_floor:.2f} "
                        f"(原始SL=${stop_loss:.2f}{' 冷却放宽' if _in_open_cooldown else ''}, entry=${entry_price:.2f})"
                    )
                    # 真实平仓 (broker)
                    _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
                    try:
                        from app.execution.dual_broker import get_dual_broker
                        dual = get_dual_broker()
                        close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
                    except Exception as _e:
                        logger.error(f"[playbook dual] 止损兜底平仓异常 {symbol} ({_row_target}): {_e}")
                        continue
                    if not close_res.get("success"):
                        _sl_err = close_res.get("error", "")
                        if "broker_no_position" in _sl_err or "持仓不足" in _sl_err or "insufficient" in _sl_err.lower():
                            # 2026-06-30 fix: broker 无此持仓 → DB-only 平仓
                            logger.warning(f"[playbook dual] {symbol} 止损兜底: broker持仓不足, DB-only平仓")
                            _db_exit = current_price
                            _db_pnl = (_db_exit - entry_price) * qty if direction == "LONG" else (entry_price - _db_exit) * qty
                            _db_pct = (_db_exit - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _db_exit) / entry_price * 100
                            _close_trade_record(conn, r["id"], _db_exit, "BROKER_GHOST_CLOSE",
                                                _db_pnl, _db_pct / 100, round(held_minutes / 60, 1), inst_id=inst_id, symbol=symbol)
                            exited += 1
                            logger.info(f"[playbook sim] 🔧 {symbol} SL DB-only平仓: exit=${_db_exit:.2f}")
                            continue
                        logger.warning(f"[playbook dual] 止损兜底平仓失败 {symbol}: {_sl_err}")
                        continue
                    _exit_price = close_res.get("filled_price", 0) or current_price
                    _sl_src = close_res.get("price_source", "unknown")
                    _sl_broker_after = close_res.get("broker_qty_after", -1)
                    _sl_order_filled = close_res.get("order_filled", False)
                    _sl_close_futu_id = close_res.get("futu_order_id", "")
                    # ── 根因修复 (2026-07-07): 用 order_filled 判定, 不用 broker_qty_after ──
                    # SL 兜底: 订单未成交 → 不写 trade_records
                    if not _sl_order_filled:
                        logger.warning(
                            f"[playbook dual] {symbol} SL 兜底平仓未成交: "
                            f"order_filled={_sl_order_filled} (src={_sl_src}), 不写 trade_records"
                        )
                        continue
                    # fill=0 但 broker 已无仓 → 用当前报价记账 (标记来源)
                    if _exit_price <= 0:
                        logger.info(f"[playbook dual] {symbol} SL fill=0 但 broker 已无仓 (src={_sl_src}), 用报价记账")
                        _exit_price = current_price
                        _sl_src = "quote_fallback"
                    if _exit_price <= 0:
                        logger.warning(f"[playbook dual] {symbol} SL 无法获取退出报价, 跳过记账")
                        continue
                    _pnl = (_exit_price - entry_price) * qty if direction == "LONG" else (entry_price - _exit_price) * qty
                    _pnl_pct = (_exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _exit_price) / entry_price * 100
                    _hh = round(held_minutes / 60, 1)
                    # Fix 3: 写入 exit_price_source
                    try:
                        _sl_meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                    except Exception:
                        _sl_meta = {}
                    _sl_meta["exit_price_source"] = _sl_src
                    _sl_meta["broker_qty_before"] = close_res.get("broker_qty_before")
                    _sl_meta["broker_qty_after"] = _sl_broker_after
                    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                                 (_json.dumps(_sl_meta), r["id"]))
                    conn.commit()
                    # 2026-07-14: 兜底用原始 initial stop_loss(L3361 判定) → 归为 SL_INITIAL.
                    _close_trade_record(conn, r["id"], _exit_price, "SL_INITIAL",
                                        _pnl, _pnl_pct / 100, _hh, inst_id=inst_id, symbol=symbol,
                                        close_futu_order_id=_sl_close_futu_id)
                    exited += 1
                    logger.info(f"[playbook sim] ✅ {symbol} SL_INITIAL 兜底: exit=${_exit_price:.2f} src={_sl_src} pnl=${_pnl:.2f}")
                    continue

            if abs(current_price - entry_price) / entry_price > 0.15:
                continue  # 价格异常跳过 (2026-07-02: 50%→15%, 50%让巨亏/巨盈仓位失保护)

            # cohort B skip 判定 (sim 特有: LIVE 需显式开 overnight)
            _cohort = meta.get("cohort", "A")
            _src = r["source_type"]
            from app.storage.trade_store import cohort_b_skip_allowed as _skip_ok
            _skip_allowed = (_cohort == "B" and _skip_ok(_src))  # LIVE 需显式开

            # peak_price 从 metadata 恢复 (sim 此前未做 peak tracking, 现与 shadow 对齐)
            peak_price = float(meta.get("peak_price", 0))
            # 注入共用函数所需字段
            meta["_skip_allowed"] = _skip_allowed
            meta["_log_tag"] = "playbook sim"
            meta["_symbol"] = symbol

            # ── 共用退出规则判断 (与 shadow 完全对齐) ──
            exit_reason, _exit_price_hint, peak_price, _be = _check_playbook_exit_rules(
                direction, entry_price, current_price, stop_loss,
                peak_price, meta, held_minutes, minutes_to_close,
                minutes_from_open, r["entry_time"],
            )

            # 2026-07-16 P1 修复: MFE/MAE/peak/trough 在每一轮都采集 (含退出轮).
            #   旧逻辑: 这段代码在 `if not exit_reason:` 内, 退出轮跳过 →
            #   peak_price/trough_price 从未在退出轮写回 → _close_trade_record 读到旧值 →
            #   mfe_pct=0/mae_pct=0 (393/397 笔为0, 含121/122盈利单).
            #   修复: 移到 exit_reason 判断之前, 退出轮也更新 peak/trough/mfe/mae.
            if direction == "LONG":
                _cur_mfe = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                _cur_mae = (entry_price - current_price) / entry_price * 100 if entry_price > 0 else 0
            else:
                _cur_mfe = (entry_price - current_price) / entry_price * 100 if entry_price > 0 else 0
                _cur_mae = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            _cur_mfe = max(_cur_mfe, 0)
            _cur_mae = max(_cur_mae, 0)
            _MFE_MAE_SANE_MAX = 50.0
            if 0 < _cur_mfe <= _MFE_MAE_SANE_MAX and _cur_mfe > float(meta.get("mfe_pct", 0) or 0):
                meta["mfe_pct"] = round(_cur_mfe, 4)
            if 0 < _cur_mae <= _MFE_MAE_SANE_MAX and _cur_mae > float(meta.get("mae_pct", 0) or 0):
                meta["mae_pct"] = round(_cur_mae, 4)

            _trough_old = float(meta.get("trough_price", 0) or 0)
            _trough = _trough_old if _trough_old > 0 else entry_price
            if direction == "LONG":
                _trough = min(_trough, current_price)
            else:
                _trough = max(_trough, current_price)
            _trough = round(_trough, 4)

            # 退出参数影子 (record-only) — 仅在非退出轮评估 (退出轮无意义).
            _exit_shadow_new = False
            if not exit_reason:
                try:
                    from app.playbook.exit_shadow import record_exit_shadow_poll
                    _peak_prof = 0.0
                    if entry_price > 0 and peak_price > 0:
                        _peak_prof = (((peak_price - entry_price) if direction == "LONG"
                                       else (entry_price - peak_price)) / entry_price * 100)
                    _exit_shadow_new = record_exit_shadow_poll(
                        meta, direction=direction, entry_price=entry_price,
                        initial_sl=stop_loss, current_price=current_price,
                        peak_profit_pct=_peak_prof, held_minutes=held_minutes,
                    )
                except Exception:
                    pass

            # invalidation 影子 — 仅在非退出轮评估.
            _inval_shadow_new = False
            if not exit_reason and not meta.get("invalidation_shadow") and (meta.get("invalidation") or ""):
                try:
                    from app.playbook.invalidation_shadow import evaluate_invalidation_shadow
                    _sh = evaluate_invalidation_shadow(
                        invalidation=meta.get("invalidation"), direction=direction,
                        entry_price=entry_price, current_price=current_price,
                    )
                    if _sh.get("would_trigger"):
                        meta["invalidation_shadow"] = {
                            "reason": _sh["reason"],
                            "held_minutes_at_hit": held_minutes,
                            "price_at_hit": current_price,
                        }
                        _inval_shadow_new = True
                except Exception:
                    pass

            # peak/trough/mfe/mae metadata 写回 (每一轮, 含退出轮 — 这是修复的核心)
            if peak_price != float(meta.get("peak_price", 0)) or _cur_mfe > 0 or _cur_mae > 0 or _trough != _trough_old or _inval_shadow_new or _exit_shadow_new:
                for _k in ("_log_tag", "_symbol", "_skip_allowed"):
                    meta.pop(_k, None)
                meta["peak_price"] = peak_price
                meta["trough_price"] = _trough
                meta["breakeven_triggered"] = _be
                conn.execute(
                    "UPDATE trade_records SET metadata_json = ? WHERE id = ?",
                    (_json.dumps(meta), r["id"]),
                )
                conn.commit()

            if not exit_reason:
                continue

            # 真实平仓 (broker) — target_env 按 source_type (PLAYBOOK_LIVE→real, 否则 sim)
            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            try:
                from app.execution.dual_broker import get_dual_broker
                dual = get_dual_broker()
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.error(f"[playbook dual] 平仓异常 {symbol} ({_row_target}): {_e}")
                continue

            if not close_res.get("success"):
                _err = close_res.get("error", "")
                # 2026-06-30 fix: "持仓不足"/"broker_no_position" = broker 无此持仓 (永久错误, 不重试)
                # 查询 broker 实际持仓, 若为 0 则 DB-only 平仓 (用当前报价)
                # 2026-07-06 fix: 补 "broker_no_position" 匹配 (SL/EOD 路径已有, 正常退出路径遗漏)
                if "broker_no_position" in _err or "持仓不足" in _err or "insufficient" in _err.lower():
                    logger.warning(
                        f"[playbook dual] {symbol} ({_row_target}) broker 持仓不足, "
                        f"检查实际持仓并 DB-only 平仓"
                    )
                    _broker_qty = 0.0
                    try:
                        broker_positions = dual.get_positions(target_env=_row_target)
                        for _bp in broker_positions:
                            _bp_code = _bp.get("code", _bp.get("symbol", ""))
                            if _bp_code == symbol:
                                _broker_qty = float(_bp.get("qty", 0) or 0)
                                break
                    except Exception as _be:
                        logger.warning(f"[playbook dual] 查询 broker 持仓异常 {symbol}: {_be}")
                    if _broker_qty <= 0:
                        # Broker 无此持仓 → DB-only 平仓 (ghost position)
                        _db_exit_price = current_price if current_price and current_price > 0 else entry_price
                        _db_pnl = (_db_exit_price - entry_price) * qty if direction == "LONG" else (entry_price - _db_exit_price) * qty
                        _db_pnl_pct = (_db_exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _db_exit_price) / entry_price * 100
                        _db_hh = round(held_minutes / 60, 1)
                        _close_trade_record(conn, r["id"], _db_exit_price, "BROKER_GHOST_CLOSE",
                                            _db_pnl, _db_pnl_pct / 100, _db_hh, inst_id=inst_id, symbol=symbol)
                        exited += 1
                        logger.info(
                            f"[playbook sim] 🔧 {symbol} DB-only 平仓 (broker无持仓): "
                            f"exit=${_db_exit_price:.2f} pnl=${_db_pnl:.2f} reason={exit_reason}"
                        )
                        continue
                    elif _broker_qty < qty:
                        # Broker 有部分持仓 → 用实际数量平仓
                        logger.info(f"[playbook dual] {symbol} broker实际qty={_broker_qty} < DB qty={qty}, 用broker数量平仓")
                        try:
                            close_res2 = dual.close_position(symbol, _broker_qty, direction, target_env=_row_target)
                            if close_res2.get("success"):
                                _fill = close_res2.get("filled_price", 0) or current_price
                                _pnl2 = (_fill - entry_price) * _broker_qty if direction == "LONG" else (entry_price - _fill) * _broker_qty
                                _pnl_pct2 = (_fill - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _fill) / entry_price * 100
                                _hh2 = round(held_minutes / 60, 1)
                                _close_trade_record(conn, r["id"], _fill, exit_reason,
                                                    _pnl2, _pnl_pct2 / 100, _hh2, inst_id=inst_id, symbol=symbol)
                                exited += 1
                                logger.info(f"[playbook sim] ✅ {symbol} 部分平仓: qty={_broker_qty} exit=${_fill:.2f}")
                                continue
                        except Exception as _e2:
                            logger.error(f"[playbook dual] 部分平仓异常 {symbol}: {_e2}")
                logger.warning(f"[playbook dual] 平仓失败 {symbol} ({_row_target}): {_err}")
                continue

            exit_price = close_res.get("filled_price", 0) or current_price
            _price_source = close_res.get("price_source", "unknown")
            _broker_qty_after = close_res.get("broker_qty_after", -1)

            # ── 根因修复 (2026-07-07): 用 order_filled 判定成交, 不用 broker_qty_after ──
            # 旧逻辑: if _broker_qty_after > 0.5 → 平仓未成交 (依赖持仓列表, SIM 有延迟)
            # 新逻辑: if not _order_filled → 平仓未成交 (依赖订单状态, 无延迟问题)
            _order_filled = close_res.get("order_filled", False)
            _close_futu_id = close_res.get("futu_order_id", "")

            # 订单未成交 → 不写 trade_records (与 SL 路径/EOD 路径对齐)
            if not _order_filled:
                logger.warning(
                    f"[playbook dual] {symbol} {direction} {exit_reason} 平仓未成交: "
                    f"order_filled={_order_filled} (src={_price_source}), 不写 trade_records"
                )
                continue

            # 订单已成交 → 用 filled_price 记账 (优先), fill=0 时用当前报价兜底
            if exit_price <= 0:
                logger.info(f"[playbook dual] {symbol} order_filled 但 fill=0 (src={_price_source}), 用报价记账")
                try:
                    q = data_router.get_quote(symbol)
                    exit_price = float(q.get("last", 0) or q.get("price", 0) or 0) if q else 0
                except Exception:
                    pass
                _price_source = "quote_fallback"
            if exit_price <= 0:
                logger.warning(f"[playbook dual] {symbol} 无法获取退出报价, 跳过记账")
                continue

            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = round(held_minutes / 60, 1)

            # 写入 exit_price_source 到 metadata
            try:
                _meta_out = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                _meta_out = {}
            _meta_out["exit_price_source"] = _price_source
            _meta_out["broker_qty_before"] = close_res.get("broker_qty_before")
            _meta_out["broker_qty_after"] = _broker_qty_after
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                         (_json.dumps(_meta_out), r["id"]))
            conn.commit()

            _close_trade_record(conn, r["id"], exit_price, exit_reason,
                                pnl, pnl_pct / 100, holding_hours, inst_id=inst_id, symbol=symbol,
                                close_futu_order_id=_close_futu_id)
            exited += 1
            logger.info(
                f"[playbook sim] 平仓: {symbol} {direction} reason={exit_reason} "
                f"entry={entry_price} exit={exit_price} pnl=${pnl:.2f}"
            )

        conn.close()
        return exited

    except Exception as e:
        # 2026-07-16: debug→exception — 退出监控循环顶层崩溃(如 pb_cfg NameError)
        # 在 debug 级别下生产 INFO 日志完全隐身, 曾导致 P0 潜伏. 必须带 traceback 可见.
        logger.exception(f"[playbook sim] exit monitor 异常 (已隔离): {e}")
        return 0


def _force_close_orphaned_sim_positions(scheduler) -> int:
    """EOD 兜底: 强制平仓所有未平仓的 PLAYBOOK_SIM/ORB_ENGINE 真实仓位 (via broker).

    与 _force_close_orphaned_shadow_positions 的区别: PLAYBOOK_SIM 是真实模拟持仓,
    必须调 broker.close_position 真实平仓, 不能只更新 DB.

    2026-06-24: 也覆盖 ORB_ENGINE 仓位 — ORB daemon 崩溃时, 其自管 EOD (_force_close_all)
    不执行, 仓位会孤儿化. 此函数作为 scheduler EOD 兜底, 用 DualBroker.close_position 平仓.
    ORB 在 sim 账户交易, target_env="sim".
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime

        db_path = str(get_trade_records_db())
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, quantity, metadata_json, entry_time, source_type
            FROM trade_records
            WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'ORB_ENGINE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
            ORDER BY id DESC
        """).fetchall()

        if not rows:
            conn.close()
            return 0

        data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        if not data_router:
            logger.warning("[playbook dual] EOD 兜底跳过: 无 data_router")
            conn.close()
            return 0

        from app.execution.dual_broker import get_dual_broker
        dual = get_dual_broker()
        closed = 0
        now_iso = datetime.now().isoformat()

        for r in rows:
            symbol = r["symbol"]
            direction = r["direction"]
            entry_price = float(r["entry_price"] or 0)
            qty = r["quantity"] or 0
            if entry_price <= 0 or qty <= 0:
                continue

            # RFC-061 v2: cohort B 盈利仓跳过 EOD broker 兜底 (持过夜); 5 交易日仍平 (MAX_HOLD_T5).
            # 仅 PLAYBOOK_SIM/LIVE 且 cohort_b_skip_allowed (LIVE 需显式开); ORB 强制平 (日内语义).
            try:
                _meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            except Exception:
                _meta = {}
            _cohort = _meta.get("cohort", "A")
            from app.storage.trade_store import cohort_b_skip_allowed as _skip_ok
            _skip_allowed = (_cohort == "B" and _skip_ok(r["source_type"]))
            _eod_reason = "EOD_FORCE_CLOSE"
            if _skip_allowed and _cohort_b_max_hold_hit(r["entry_time"]):
                _eod_reason = "MAX_HOLD_T5"  # 5 交易日硬上限, 仍须 broker 平仓
            elif _skip_allowed:
                # 估算盈利决定是否跳过 (取当前报价)
                _est_price = 0.0
                try:
                    _q = data_router.get_quote(symbol)
                    if _q and isinstance(_q, dict):
                        _est_price = float(_q.get("last", 0) or _q.get("price", 0) or 0)
                except Exception:
                    pass
                if _est_price > 0:
                    _est_pnl_pct = ((_est_price - entry_price) / entry_price * 100) if direction == "LONG" \
                        else ((entry_price - _est_price) / entry_price * 100)
                    if _est_pnl_pct > 0:
                        logger.info(
                            f"[playbook sim] EOD 兜底跳过 {symbol} cohort=B profit~{_est_pnl_pct:.2f}% 持过夜 (RFC-061)"
                        )
                        continue
                else:
                    # 2026-07-04: 报价缺失时不跳过 — 原逻辑已对, 但加显式日志便于诊断
                    logger.warning(
                        f"[playbook sim] EOD 兜底 {symbol} cohort=B 报价缺失 → 强制平仓 (保守)"
                    )
                # 报价缺失或亏损 → 不跳过, 走兜底平仓 (保守)

            _row_target = "real" if r["source_type"] == "PLAYBOOK_LIVE" else "sim"
            # P1 fix (2026-06-25): 连续失败 3 次后放弃, 停止死循环日志刷屏
            _fail_count = _meta.get("eod_close_fail_count", 0)
            if _fail_count >= 3:
                logger.debug(f"[playbook dual] EOD 兜底放弃 {symbol} ({_row_target}) — 连续失败 {_fail_count} 次")
                continue
            try:
                close_res = dual.close_position(symbol, qty, direction, target_env=_row_target)
            except Exception as _e:
                logger.warning(f"[playbook dual] EOD 兜底平仓异常 {symbol} ({_row_target}): {_e}")
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                continue
            if not close_res.get("success"):
                _eod_err = close_res.get("error", "")
                # 2026-07-03: close_position 内置持仓检查, broker 无仓时返回 "broker_no_position"
                # 2026-06-30 fix: "持仓不足" = 永久错误, 查 broker 实际持仓决定 DB-only 平仓
                if "broker_no_position" in _eod_err or "持仓不足" in _eod_err or "insufficient" in _eod_err.lower():
                    _broker_qty = 0.0
                    try:
                        _bpos = dual.get_positions(target_env=_row_target)
                        for _bp in _bpos:
                            if _bp.get("code", _bp.get("symbol", "")) == symbol:
                                _broker_qty = float(_bp.get("qty", 0) or 0)
                                break
                    except Exception:
                        pass
                    if _broker_qty <= 0:
                        # Broker 无此持仓 → DB-only 平仓 (ghost)
                        _eod_price = 0.0
                        try:
                            _q = data_router.get_quote(symbol)
                            if _q and isinstance(_q, dict):
                                _eod_price = float(_q.get("last", 0) or _q.get("price", 0) or 0)
                        except Exception:
                            pass
                        if _eod_price <= 0:
                            _eod_price = entry_price  # 无报价用入场价
                        _eod_pnl = (_eod_price - entry_price) * qty if direction == "LONG" else (entry_price - _eod_price) * qty
                        _eod_pct = (_eod_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _eod_price) / entry_price * 100
                        _eod_hh = 0.0
                        try:
                            _entry_dt = datetime.fromisoformat(r["entry_time"])
                            _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
                            _eod_hh = (_now_ref - _entry_dt).total_seconds() / 3600
                        except Exception:
                            pass
                        _close_trade_record(conn, r["id"], _eod_price, "BROKER_GHOST_CLOSE",
                                            _eod_pnl, _eod_pct / 100, _eod_hh,
                                            inst_id=_meta.get("playbook_instruction_id"), symbol=symbol)
                        closed += 1
                        logger.info(f"[playbook sim] 🔧 EOD {symbol} DB-only平仓 (broker无持仓): exit=${_eod_price:.2f} pnl=${_eod_pnl:.2f}")
                        continue
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.warning(f"[playbook dual] EOD 兜底 {symbol} ({_row_target}) 无报价/平仓失败 (第{_fail_count}次): {_eod_err}")
                continue

            # 2026-07-03: close_position 已内置平仓前/后持仓验证 + price_source
            exit_price = close_res.get("filled_price", 0) or 0
            _price_source = close_res.get("price_source", "unknown")
            _broker_qty_after = close_res.get("broker_qty_after", -1)
            _eod_order_filled = close_res.get("order_filled", False)
            _eod_close_futu_id = close_res.get("futu_order_id", "")

            # ── 根因修复 (2026-07-07): 用 order_filled 判定, 不用 broker_qty_after ──
            # EOD: 订单未成交 → 不写 trade_records, 记录失败次数
            if not _eod_order_filled:
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                _meta["exit_price_source"] = _price_source
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.warning(
                    f"[playbook dual] EOD {symbol} 订单未成交 "
                    f"order_filled={_eod_order_filled} (第{_fail_count}次) src={_price_source}, 不写 trade_records"
                )
                continue

            # 订单已成交 → 用 filled_price 记账 (优先), fill=0 时用当前报价兜底
            if exit_price <= 0:
                logger.info(f"[playbook dual] EOD {symbol} order_filled 但 fill=0 (src={_price_source}), 用报价记账")
                try:
                    q = data_router.get_quote(symbol)
                    exit_price = float(q.get("last", 0) or q.get("price", 0) or 0) if q else 0
                except Exception:
                    pass
                _price_source = "quote_fallback"
            if exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = 0.0
            try:
                _entry_dt = datetime.fromisoformat(r["entry_time"])
                _now_ref = datetime.now(_entry_dt.tzinfo) if _entry_dt.tzinfo else datetime.now()
                holding_hours = (_now_ref - _entry_dt).total_seconds() / 3600
            except Exception:
                pass

            # Fix 3: 写入 exit_price_source 到 metadata
            _meta["exit_price_source"] = _price_source
            _meta["broker_qty_before"] = close_res.get("broker_qty_before")
            _meta["broker_qty_after"] = _broker_qty_after
            conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                         (_json.dumps(_meta), r["id"]))
            conn.commit()

            _close_trade_record(conn, r["id"], exit_price, _eod_reason,
                                pnl, pnl_pct / 100, holding_hours,
                                inst_id=_meta.get("playbook_instruction_id"), symbol=symbol,
                                close_futu_order_id=_eod_close_futu_id)
            closed += 1
            logger.info(f"[playbook sim] EOD 兜底平仓: {symbol} reason={_eod_reason} exit={exit_price} src={_price_source} pnl=${pnl:.2f}")

        conn.close()
        return closed

    except Exception as e:
        logger.warning(f"[playbook sim] EOD force_close 异常 (已隔离): {e}")
        return 0

