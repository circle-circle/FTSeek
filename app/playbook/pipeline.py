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
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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
    """从 scheduler.watchlist 取候选 (与 llm_pipeline 一致, 纯字符串列表)"""
    wl = list(getattr(scheduler, "watchlist", []) or [])
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
    """从 scheduler 或 DI 容器获取 VotingCoordinator 单例"""
    vc = getattr(scheduler, "voting_coordinator", None)
    if vc is not None:
        return vc
    try:
        from app.core.di_container import get_di_container
        return get_di_container().voting_coordinator()
    except Exception as e:
        logger.warning(f"[playbook] VotingCoordinator 获取失败: {e}")
        return None


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


def _build_instructions_from_votes(
    screen_results,
    voting_coordinator,
    regime_context,
    total_equity: float,
    data_router=None,
):
    """消费 VotingCoordinator 投票结果, 构建 Instruction 列表.

    数据流:
      ScreenResult → market_data → VotingCoordinator.vote() → vote_result
      → 方向映射 (LONG→BUY, SHORT→SELL_SHORT, NEUTRAL→SKIP)
      → compute_instruction_values 确定性计算数值
      → Instruction
    """
    from app.playbook.instruction_generator import (
        Instruction,
        compute_instruction_values,
        compute_confidence,
        build_thesis,
    )

    if voting_coordinator is None:
        logger.warning("[playbook] VotingCoordinator 不可用, 全部候选 SKIP")
        return [
            Instruction(symbol=r.symbol, direction="SKIP", skip_reason="VotingCoordinator unavailable")
            for r in screen_results
        ]

    instructions = []
    # §2.7 修复: 统一用 _normalize_regime_str, 与 regime_mults 键空间对齐 (原 str(enum) 恒命中默认 0.5)
    regime_str = _normalize_regime_str(regime_context)

    # Regime 仓位乘数 (与 InstructionGenerator.REGIME_MULTIPLIERS 对齐)
    regime_mults = {"RISK_ON": 1.0, "CAUTIOUS": 0.5, "RISK_OFF": 0.3, "NO_TRADE": 0.0}
    regime_mult = regime_mults.get(regime_str, 0.5)

    for sr in screen_results:
        symbol = sr.symbol
        data = sr.data
        factors = sr.factors

        # 构造 market_data 并调 VotingCoordinator (新路径走 SymbolContext, legacy 走 10 字段 dict)
        market_data = _build_vote_market_data(symbol, data, regime_context, data_router)
        if market_data is None:
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="行情不可用 (router 无数据)"
            ))
            continue

        try:
            vote_result = voting_coordinator.vote(symbol, market_data)
        except Exception as e:
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
        
        if is_neutral:
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="投票中性, 无方向信号"
            ))
            continue
        
        direction = "BUY" if vote_direction == "LONG" else "SELL_SHORT"
        decision_id = vote_result.get("decision_id", "")

        # §2026-06-26: BUY 入场质量过滤 — MACD 柱确认 + MA50 方向过滤.
        # 背景: MADP 辩论 (DEB-20260626-horizon_model) 结论 — BUY 的问题是入场质量,
        # 不是持仓时间. T+1 数据显示 BUY 赢家 alpha 仅 +0.36%, 亏损仓持有更久更差.
        # 实现:
        #   - MACD 柱 = macd - macd_signal, 要求 > 0 (多头动量确认)
        #   - 价格 > MA50, 作为 MA50 方向/位置过滤的代理
        # 注意: 这里只过滤 BUY, SHORT 保持当前高胜率路径不变.
        if direction == "BUY":
            macd_hist = market_data.macd - market_data.macd_signal
            if macd_hist <= 0:
                logger.warning(
                    f"[playbook] {symbol} BUY 入场过滤: MACD 柱非正 "
                    f"(macd={market_data.macd:.4f}, signal={market_data.macd_signal:.4f}, hist={macd_hist:.4f}) → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason=f"MACD 柱非正 ({macd_hist:.4f}), BUY 入场过滤",
                ))
                continue
            if market_data.ma50 > 0 and market_data.price > 0 and market_data.price <= market_data.ma50:
                logger.warning(
                    f"[playbook] {symbol} BUY 入场过滤: 价格 {market_data.price:.2f} "
                    f"<= MA50 {market_data.ma50:.2f} → SKIP"
                )
                instructions.append(Instruction(
                    symbol=symbol,
                    direction="SKIP",
                    skip_reason=f"价格低于 MA50 ({market_data.price:.2f} <= {market_data.ma50:.2f}), BUY 入场过滤",
                ))
                continue

        # ponytail: 方向约束 — 条件与方向矛盾时翻转/跳过.
        # 根因: voting_aggregator 用 strength>50 决方向, 无视 conditions 语义.
        # 数据: 35个BUY中19个含 insider_net_sell = 直接矛盾, LONG -$1624, SHORT +$926.
        # 修法:
        #   - 看空条件数 > 看多条件数 且 direction==BUY   → 翻转为 SELL_SHORT
        #   - 看多条件数 > 看空条件数 且 direction==SELL_SHORT → 翻转为 BUY
        #   - 双方条件数相等且均非空 → 因子冲突, SKIP
        from app.playbook.condition_taxonomy import BEARISH_CONDITIONS, BULLISH_CONDITIONS
        _conditions = list(factors.keys()) if factors else []
        _bearish_hits = [c for c in _conditions if c in BEARISH_CONDITIONS]
        _bullish_hits = [c for c in _conditions if c in BULLISH_CONDITIONS]
        _bear_count = len(_bearish_hits)
        _bull_count = len(_bullish_hits)

        if direction == "BUY" and _bear_count > _bull_count:
            logger.warning(
                f"[playbook] {symbol} 方向矛盾: BUY 含{_bear_count}个看空/仅{_bull_count}个看多条件 {_bearish_hits} → 翻转为 SELL_SHORT"
            )
            direction = "SELL_SHORT"
        elif direction == "SELL_SHORT" and _bull_count > _bear_count:
            logger.warning(
                f"[playbook] {symbol} 方向矛盾: SELL_SHORT 含{_bull_count}个看多/仅{_bear_count}个看空条件 {_bullish_hits} → 翻转为 BUY"
            )
            direction = "BUY"
        elif _bear_count == _bull_count and _bear_count > 0:
            logger.warning(
                f"[playbook] {symbol} 因子方向冲突数持平 ({_bear_count} vs {_bull_count}): {_bearish_hits} vs {_bullish_hits} → SKIP"
            )
            instructions.append(Instruction(
                symbol=symbol, direction="SKIP", skip_reason="因子方向冲突, 无法判定方向"
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
        values = compute_instruction_values(
            decision=decision,
            data=data,
            total_equity=total_equity,
            risk_pct=1.0,
            regime_multiplier=regime_mult,
            horizon=_horizon,
        )

        if not values or not values.get("entry_price"):
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
        
        confidence = compute_confidence(data, decision["selected_conditions"], factors)
        
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
        )
        instructions.append(inst)
        logger.info(
            f"[playbook] {symbol} vote→instruction: {vote_direction}→{direction} "
            f"strength={vote_result.get('final_strength', '?')} "
            f"consensus={vote_result.get('consensus_level', '?')} "
            f"decision_id={decision_id}"
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
    max_direction_skew: float = 0.65,
    min_rr: float = 1.5,
) -> List[Any]:
    """组合级风险 guardrail (2026-06-25)."""
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
    """将多数方 confidence 最低的信号降级, 直到偏斜 ≤ target_skew."""
    trimmed = 0
    while True:
        total = len([i for i in valid if i.direction in ("BUY", "SELL_SHORT")])
        buy_count = sum(1 for i in valid if i.direction == "BUY")
        short_count = total - buy_count
        majority_pct = max(buy_count, short_count) / total if total > 0 else 0
        if majority_pct <= target_skew:
            break
        candidates = [i for i in valid if i.direction == majority_dir and i.direction != "SKIP"]
        if not candidates:
            break
        # 按 confidence 升序, 把最低的降级
        candidates.sort(key=lambda x: getattr(x, "confidence", 0) or 0)
        candidates[0].direction = "SKIP"
        candidates[0].skip_reason = "方向偏斜 guardrail 降级"
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
            # 根因修复 (2026-06-18): 幂等检查改为查询 DB，不再依赖内存属性
            # 内存属性在进程重启后丢失 → 重启后重新执行 → 覆盖当天数据
            try:
                from app.playbook.db import PlaybookDB
                db = PlaybookDB()
                existing = db.get_instructions_by_date(today)
                if existing and len(existing) > 0:
                    logger.info(f"[playbook] {today} DB 已有 {len(existing)} 条指令，跳过生成 (幂等)")
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
        from app.playbook.feishu_push import PlaybookPusher

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

        # Layer 2: VotingCoordinator 统一方向决策 (替代独立 LLM InstructionGenerator)
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

        # 飞书盘前推送
        try:
            PlaybookPusher(db).push_morning_instructions(today)
        except Exception as e:
            logger.debug(f"[playbook] 盘前推送失败 (不影响生成): {e}")

        scheduler._playbook_generated_date = today
        return {"date": today, "instructions": saved, "regime": regime_ctx.regime.value}

    except Exception as e:
        logger.exception(f"[playbook] 盘前生成异常 (已隔离, 不影响 scheduler): {e}")
        return {"error": str(e)}


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
        },
        "decision_id": getattr(inst, "decision_id", "") or "",
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
                regime_log = db.get_regime_log(days=7)
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
            "reason": "" if graduated else f"未毕业: {failed}",
        }
    except Exception as e:
        logger.error(f"[playbook] graduation gate 异常 (fail-close): {e}")
        return {"graduated": False, "checks": {}, "reason": f"门禁异常: {e}"}


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
            logger.debug("[playbook] auto_execute=false, 跳过自动执行")
            return None

        today = _today_et_str()
        if not force and not _is_trading_day_today():
            logger.debug(f"[playbook] {today} 非交易日, 跳过自动执行")
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
            logger.debug(f"[playbook] 开盘5分钟内不执行 (now={_now_et_dt.strftime('%H:%M')} ET)")
            return None

        db = PlaybookDB()
        service = PlaybookService(db)
        instructions = db.get_instructions_by_date(today)

        if not instructions:
            logger.debug(f"[playbook] {today} 无指令, 跳过自动执行")
            return {"date": today, "executed": 0, "skipped": 0}

        executed_count = 0
        skipped_count = 0
        errors = []

        # P0 fix (2026-06-25): 开仓前 buying_power 总量检查
        # 根因: 批量同时触发 (6笔/2秒), 每笔独立算仓位 → $388K on $82K 账户
        # 修复: 累计已开 PLAYBOOK_SIM notional, 超过 buying_power × 0.8 则跳过后续
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

        # 获取 buying_power — 从 capital_safety 读取富途接口返回的真实购买力
        # 2026-06-27 修复: 不用硬编码$82K, 改读 capital_safety._state.buying_power
        #   capital_safety 由 position_sync 每30s从富途 accinfo_query 更新, 是动态真实值
        #   单笔仓位仍用 risk_base_capital=$82K (sim→live统一口径), 总量上限用真实BP
        from app.risk.capital_safety import get_capital_safety_system
        _buying_power = float(pb_cfg.get("risk_base_capital", 82000.0))
        _bp_source = "HARDCODED_82000"
        try:
            _cs = get_capital_safety_system()
            _real_bp = float(getattr(_cs._state, 'buying_power', 0) or 0)
            if _real_bp > 0:
                _buying_power = _real_bp
                _bp_source = f"capital_safety_{_real_bp:.0f}"
        except Exception as e:
            logger.warning(f"[playbook] capital_safety buying_power 读取失败, 回退到 $82K: {e}")
        _bp_limit = _buying_power * 0.8  # 真实购买力的80%作为总量部署上限
        _fallback_alert = bool(pb_cfg.get("buying_power_fallback_alert", True))
        logger.info(f"[playbook] buying_power={_buying_power:,.0f} limit={_bp_limit:,.0f} source={_bp_source}")

        for inst in instructions:
            # 跳过已执行、SKIP 方向的指令
            if inst.get("executed"):
                continue
            if inst.get("direction") == "SKIP":
                continue

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

            # 质量门禁 (2026-06-24): 低 confidence 指令跳过, 避免垃圾信号批量执行
            # 阈值从配置读取, 默认 65
            _min_confidence = float(pb_cfg.get("min_confidence", 65))
            _inst_conf = inst.get("confidence", 0) or 0
            if _inst_conf < _min_confidence:
                logger.info(f"[playbook] {inst['symbol']} confidence={_inst_conf} < {_min_confidence}, 跳过")
                skipped_count += 1
                continue

            # P0 (2026-06-25): buying_power 总量检查 — 防止批量触发耗尽购买力
            _entry = inst.get("entry_price") or 0.0
            _size = inst.get("position_size") or 0
            _new_notional = _entry * _size if _entry > 0 and _size > 0 else 0.0
            if _bp_limit > 0 and (_max_deploy + _new_notional) > _bp_limit:
                logger.info(
                    f"[playbook] {inst['symbol']} 跳过 — 购买力上限 "
                    f"(已用=${_max_deploy:,.0f} + 新=${_new_notional:,.0f} > 限=${_bp_limit:,.0f})"
                )
                skipped_count += 1
                continue

            # 自动执行
            try:
                result = service.execute_instruction(
                    instruction_id=inst["id"],
                    acknowledge_risk=True,  # Paper Trade 自动确认风险
                )
                if result.get("success"):
                    executed_count += 1
                    _max_deploy += _new_notional  # 累加已用额度
                    logger.info(f"[playbook] ✅ 自动执行: {inst['symbol']} {inst['direction']}")
                else:
                    skipped_count += 1
                    errors.append(f"{inst['symbol']}: {result.get('message', 'unknown')}")
            except Exception as e:
                skipped_count += 1
                errors.append(f"{inst['symbol']}: {str(e)}")
                logger.warning(f"[playbook] 自动执行失败 {inst['symbol']}: {e}")

        logger.info(f"[playbook] 自动执行完成: {executed_count} 成功, {skipped_count} 跳过")
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
    """盘中实时检查指令是否触发 (突破MA20 + 成交量确认).

    2026-06-25: 从 ±5% 价格区间触发改为突破确认触发.
    根因: ±5% 区间 = 开盘后几乎所有股票都在昨收附近 → 全部同时触发 → 开盘噪声接盘.
    修复: 只在价格突破 MA20 (方向一致) 且成交量 > 5日均量×1.5 时触发.
    效果: 自然错开入场时间, 只有真正有动量的股票才触发.
    """
    entry_price = inst.get("entry_price")
    if not entry_price or entry_price <= 0:
        return False

    symbol = inst.get("symbol", "")
    if not symbol:
        return False

    direction = inst.get("direction", "")
    is_short = "SHORT" in direction.upper()

    data_router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if not data_router:
        return False

    # 获取 SymbolContext (100+字段, 含 ma20/price/volume_ratio)
    current_price = None
    ma20 = None
    vol_ratio = None
    try:
        ctx = data_router.get_full_market_context(symbol)
        if ctx and isinstance(ctx, dict):
            current_price = float(ctx.get("price", 0) or 0)
            ma20 = float(ctx.get("ma20", 0) or 0)
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
    if not ma20 or ma20 <= 0:
        return False

    # ── 突破条件: 价格突破 MA20 (方向一致, 最小百分比从配置读取) ──
    # 2026-06-26: 0.2%→0.5% — 0.2%是bid-ask噪声, 秒杀根因. 0.5%过滤开盘噪声.
    from app.playbook.config import get_playbook_config
    pb_cfg = get_playbook_config()
    _breakout_threshold = float(pb_cfg.get("intraday_breakout_pct", 0.5))
    _breakout_pct = (current_price - ma20) / ma20 * 100 if ma20 > 0 else 0
    if is_short:
        breakout = current_price < ma20 and _breakout_pct <= -_breakout_threshold
    else:
        breakout = current_price > ma20 and _breakout_pct >= _breakout_threshold

    # ── 成交量确认: volume_ratio >= 配置阈值 (5日均量×阈值=放量) ──
    # fail-closed: vol_ratio=0或None时 NOT confirmed (不默认1.0)
    _volume_threshold = float(pb_cfg.get("intraday_volume_ratio", 1.5))
    vol_confirmed = vol_ratio is not None and vol_ratio >= _volume_threshold

    # ── fallback 触发已删除 (2026-06-26) ──
    # 原±3% fallback让所有confidence>=65的指令在入场价附近触发, 等于无突破过滤.
    # 删除后: 只有真正突破MA20+放量才触发, 消灭开盘批量触发.
    _entry_zone_triggered = False

    triggered = (breakout and vol_confirmed) or _entry_zone_triggered

    # 入场前止损检查: 如果当前价格已越过 stop_loss, 不入场 (避免秒退)
    if triggered:
        stop_loss = float(inst.get("stop_loss", 0) or 0)
        if stop_loss > 0:
            if is_short and current_price >= stop_loss:
                logger.warning(
                    f"[playbook] ⚔️ 入场拦截: {symbol} current=${current_price:.2f} "
                    f"已越过 SHORT stop_loss=${stop_loss:.2f}, 跳过 (避免入场即止损)"
                )
                return False
            if not is_short and current_price <= stop_loss:
                logger.warning(
                    f"[playbook] ⚔️ 入场拦截: {symbol} current=${current_price:.2f} "
                    f"已越过 LONG stop_loss=${stop_loss:.2f}, 跳过 (避免入场即止损)"
                )
                return False

    if triggered:
        logger.info(
            f"[playbook] 盘中触发: {symbol} price=${current_price:.2f} "
            f"ma20=${ma20:.2f} vol_ratio={vol_ratio:.2f} ({'突破' if breakout else '未突破'} + {'放量' if vol_confirmed else '缩量'})"
        )
    else:
        logger.debug(
            f"[playbook] 未触发: {symbol} price=${current_price:.2f} ma20=${ma20:.2f} "
            f"vol={vol_ratio:.2f} breakout={breakout} vol_ok={vol_confirmed}"
        )

    return triggered


def _close_trade_record(conn, trade_id: int, exit_price: float, exit_reason: str,
                        pnl: float, pnl_pct: float, holding_hours: float,
                        inst_id = None) -> None:
    """ponytail: 统一平仓写入 — 4处UPDATE合并为1个函数, 消灭状态不一致.

    之前: shadow/sim/EOD/orphan 各自写 UPDATE, 字段集不一致, 有时忘同步 instruction.
    现在: 一个入口, 原子更新 trade_records + playbook_instructions.
    """
    now_iso = datetime.now().isoformat()
    conn.execute("""
        UPDATE trade_records
        SET exit_price=?, exit_time=?, exit_reason=?, realized_pnl=?, pnl_pct=?, holding_hours=?
        WHERE id=?
    """, (exit_price, now_iso, exit_reason, round(pnl, 2), round(pnl_pct, 4), round(holding_hours, 1), trade_id))
    if inst_id:
        conn.execute(
            "UPDATE playbook_instructions SET outcome=?, exit_price=?, exit_reason=?, pnl=?, pnl_pct=?, "
            "holding_period_hours=? WHERE id=? AND outcome NOT IN ('win','loss')",
            ("win" if pnl > 0 else "loss", exit_price, exit_reason, round(pnl, 2),
             round(pnl_pct, 4), round(holding_hours, 1), inst_id),
        )
    conn.commit()


def _check_playbook_exit_rules(direction, entry_price, current_price, initial_sl,
                               peak_price, meta, held_minutes, minutes_to_close,
                               minutes_from_open, entry_time_str):
    """共用退出规则判断 — 被 monitor_playbook_shadow_exits / monitor_playbook_sim_exits 调用.

    计算盈利百分比 → peak tracking → breakeven ratchet → 5 条退出规则.
    退出规则 (按优先级):
      [1] SL_ATR_STOP: effective_sl 触及 (含 breakeven ratchet)
      [2] OVERNIGHT_GAP_STOP: cohort B 隔夜仓跳空 >3%
      [3] PRE_CLOSE_EXIT: 收盘前 8min (cohort B 盈利可跳过)
      [4] TIME_EXIT: 持仓>2h 且 盈利<-0.5%
      [5] MAX_HOLD_T5: cohort B 持仓≥5 交易日

    Args:
        direction: "LONG"/"SHORT"
        entry_price: 入场价
        current_price: 当前报价
        initial_sl: 初始 stop_loss (meta["stop_loss"])
        peak_price: 历史峰值价 (从 meta 恢复, 函数内更新后返回)
        meta: metadata dict (读取 cohort/_skip_allowed/_log_tag/_symbol)
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

    # ── effective SL (stepped ratchet: 阶梯式利润锁定) ──
    # peak_profit 只上不下: SL 随峰值阶梯上移, 永不倒退
    # 2026-06-25: 阈值 1.5%→0.5% — 数据: 77% trades 从未达 1.5% peak, breakeven 形同虚设
    #   ≥0.5% → SL=entry (保本)
    #   ≥1.5% → SL=entry×1.005 (锁0.5%利润)
    #   ≥3.0% → SL=entry×1.02 (锁2%利润)
    effective_sl = initial_sl
    breakeven_triggered = peak_profit_pct >= 0.5
    if direction == "LONG":
        if peak_profit_pct >= 3.0:
            effective_sl = max(effective_sl, entry_price * 1.02)
        elif peak_profit_pct >= 1.5:
            effective_sl = max(effective_sl, entry_price * 1.005)
        elif breakeven_triggered:
            effective_sl = max(effective_sl, entry_price)
    else:  # SHORT: 对称
        if peak_profit_pct >= 3.0:
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, entry_price * 0.98)
        elif peak_profit_pct >= 1.5:
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, entry_price * 0.995)
        elif breakeven_triggered:
            effective_sl = min(effective_sl if effective_sl > 0 else entry_price, entry_price)

    # ── 退出判断 (按优先级, if/elif/elif 链) ──
    # 优先级: ATR止损 > 盘末退出 > 时间退出
    _cohort = meta.get("cohort", "A")
    _horizon = meta.get("horizon", "short")
    # _skip_allowed: 盈利仓跳过 EOD 的条件
    #   - cohort B (RFC-061 实验组)
    #   - 或 horizon == "long" (2026-06-26: 个股阶段驱动的长线策略)
    _skip_allowed = meta.get("_skip_allowed", _cohort == "B" or _horizon == "long")

    # 读取 horizon 配置 (用于 overnight_gap_stop_pct / max_hold_days)
    _horizon_cfg = _load_horizon_config(_horizon)
    _overnight_gap_stop_pct = _horizon_cfg.get("overnight_gap_stop_pct", 3.0)
    _max_hold_days = _horizon_cfg.get("max_hold_days", 5 if _horizon == "long" else 1)

    exit_reason = ""
    exit_price = current_price

    # 规则1: ATR止损 (最高优先级 — 亏损仓位必须先检查止损)
    if effective_sl > 0:
        if direction == "LONG" and current_price <= effective_sl:
            exit_reason = "SL_ATR_STOP"
            exit_price = effective_sl
        elif direction == "SHORT" and current_price >= effective_sl:
            exit_reason = "SL_ATR_STOP"
            exit_price = effective_sl

    # 规则2 (RFC-061 #6): 隔夜跳空止损 — long horizon 持夜仓, 次日跳空亏损超过阈值 → 快平
    if not exit_reason and _skip_allowed and _is_overnight_position(entry_time_str) and profit_pct <= -_overnight_gap_stop_pct:
        exit_reason = "OVERNIGHT_GAP_STOP"
        exit_price = current_price
        logger.warning(
            f"[{_log_tag}] {_symbol} horizon={_horizon} 隔夜跳空 profit={profit_pct:.2f}% ≤ -{_overnight_gap_stop_pct:.1f}% → OVERNIGHT_GAP_STOP"
        )

    # 规则4: 盘末退出 (收盘前8min)
    # RFC-061 / Horizon Strategy: long horizon 盈利仓跳过 EOD 强平, 持有过夜到 ~T+5 (capture 周级 alpha)
    if not exit_reason and 0 <= minutes_to_close <= 8 and minutes_from_open > 0:
        if _skip_allowed and profit_pct > 0:
            logger.info(
                f"[{_log_tag}] {_symbol} horizon={_horizon} profit={profit_pct:.2f}% → 跳过 PRE_CLOSE 持有过夜"
            )
        else:
            exit_reason = "PRE_CLOSE_EXIT"
            exit_price = current_price

    # 规则3: 时间退出 (2026-06-25: 只砍亏损, 不砍盈利 — 赢家跑到EOD)
    # 数据: TIME_EXIT 赢家6笔均+0.1-0.3%被砍 → 跑到EOD可+2%; 输家9笔均-1.13%应砍
    # 修法: profit_pct < 0 才触发 (原 -0.5% → 0%, 盈利仓不退)
    if not exit_reason and held_minutes > 120 and profit_pct < 0:
        exit_reason = "TIME_EXIT"
        exit_price = current_price  # 市价退出

    # 规则5: long horizon 硬上限 — 持仓 ≥ max_hold_days 交易日强制了结
    if not exit_reason and _skip_allowed and held_minutes > 0:
        if _horizon_max_hold_hit(entry_time_str, _max_hold_days):
            exit_reason = "MAX_HOLD"
            exit_price = current_price
            logger.info(
                f"[{_log_tag}] {_symbol} horizon={_horizon} → MAX_HOLD 强平 ({_max_hold_days} 交易日)"
            )

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


def monitor_playbook_shadow_exits(scheduler) -> int:
    """[已废弃] PLAYBOOK_SHADOW 纯虚拟路径已于 2026-06-26 停止.

    保留函数签名以兼容旧代码引用, 但不再执行任何 shadow 退出逻辑.
    所有 Playbook 执行统一走 PLAYBOOK_SIM/PLAYBOOK_LIVE 真实下单路径.
    """
    logger.debug("[playbook shadow] monitor 已废弃, 忽略")
    return 0


def _force_close_orphaned_shadow_positions(scheduler) -> int:
    """[已废弃] PLAYBOOK_SHADOW 纯虚拟路径已于 2026-06-26 停止.

    保留函数签名以兼容旧代码引用, 但不再执行任何 shadow EOD 兜底逻辑.
    所有 Playbook 执行统一走 PLAYBOOK_SIM/PLAYBOOK_LIVE 真实下单路径.
    """
    logger.debug("[playbook shadow] _force_close_orphaned_shadow_positions 已废弃, 忽略")
    return 0


# ── Phase 1 (2026-06-24): futu_simulate 真实仓位退出监控 ──────────────

def monitor_playbook_sim_exits(scheduler) -> int:
    """盘中监控 PLAYBOOK_SIM 真实模拟仓位的退出.

    与 monitor_playbook_shadow_exits 的区别: PLAYBOOK_SIM 是富途模拟账户的真实持仓,
    退出时必须调 DualBrokerAdapter.close_position(target_env="sim") 真实平仓,
    而非仅更新 trade_records 字段.

    退出规则 (与 shadow 对齐, 复用 exit_policy 阈值):
      [1] SL_ATR_STOP: 当前价触及 stop_loss
      [2] TIME_EXIT: 持仓>2h 且 盈利<-0.5% → 退出 (2026-06-24放宽)
      [3] PRE_CLOSE_EXIT: 收盘前 8min → 全平

    Returns: 本轮平仓数量
    """
    try:
        import sqlite3
        import json as _json
        from app.core.paths import get_trade_records_db
        from datetime import datetime, timezone, timedelta

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

        # 市场时段 (ET)
        ET = timezone(timedelta(hours=-4))
        now_et = datetime.now(ET)
        now_minutes = now_et.hour * 60 + now_et.minute
        minutes_to_close = 16 * 60 - now_minutes
        market_open_minutes = 9 * 60 + 30
        minutes_from_open = now_minutes - market_open_minutes

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
            held_minutes = 0
            try:
                held_minutes = (datetime.now() - datetime.fromisoformat(r["entry_time"])).total_seconds() / 60
            except Exception:
                pass

            # OKR 优化 (2026-06-26): 入场后 30 分钟冷却期.
            # 根因: PLAYBOOK_SIM 最近 7 笔闭环交易中, 3 笔在 1-5 分钟内被 SL_ATR_STOP 扫出,
            #       属开盘噪声/假突破, 不是信号本身失效. 冷却期内跳过 SL/TIME_EXIT,
            #       仅保留 PRE_CLOSE_EXIT 兜底 (收盘前 8min 必须平).
            _GRACE_MINUTES = 30
            if held_minutes < _GRACE_MINUTES and minutes_to_close > 8:
                logger.debug(
                    f"[playbook sim] {symbol} 处于冷却期 "
                    f"(held={held_minutes:.1f}min < {_GRACE_MINUTES}min), 跳过退出检查"
                )
                continue

            # ponytail: 止损兜底 — 同 monitor_playbook_shadow_exits, 防止隔夜跳空越过 SL 被 TIME_EXIT 抢占
            if stop_loss > 0:
                _sl_hit = (direction == "LONG" and current_price <= stop_loss) or \
                          (direction == "SHORT" and current_price >= stop_loss)
                if _sl_hit:
                    logger.warning(
                        f"[playbook sim] {symbol} {direction} 止损兜底: "
                        f"price=${current_price:.2f} ≤ SL=${stop_loss:.2f} (entry=${entry_price:.2f})"
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
                        logger.warning(f"[playbook dual] 止损兜底平仓失败 {symbol}: {close_res.get('error')}")
                        continue
                    _exit_price = close_res.get("filled_price", 0) or current_price
                    _pnl = (_exit_price - entry_price) * qty if direction == "LONG" else (entry_price - _exit_price) * qty
                    _pnl_pct = (_exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - _exit_price) / entry_price * 100
                    _hh = round(held_minutes / 60, 1)
                    _close_trade_record(conn, r["id"], _exit_price, "SL_ATR_STOP",
                                        _pnl, _pnl_pct / 100, _hh, inst_id=inst_id)
                    exited += 1
                    logger.info(f"[playbook sim] ✅ {symbol} SL_ATR_STOP 兜底: exit=${_exit_price:.2f} pnl=${_pnl:.2f}")
                    continue

            if abs(current_price - entry_price) / entry_price > 0.5:
                continue  # 价格异常跳过

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

            if not exit_reason:
                # peak_price metadata 写回 (即使不退出也要跟踪, 与 shadow 对齐)
                if peak_price != float(meta.get("peak_price", 0)):
                    # 清理临时注入字段, 不持久化到 DB
                    for _k in ("_log_tag", "_symbol", "_skip_allowed"):
                        meta.pop(_k, None)
                    meta["peak_price"] = peak_price
                    meta["breakeven_triggered"] = _be
                    conn.execute(
                        "UPDATE trade_records SET metadata_json = ? WHERE id = ?",
                        (_json.dumps(meta), r["id"]),
                    )
                    conn.commit()
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
                logger.warning(f"[playbook dual] 平仓失败 {symbol} ({_row_target}): {close_res.get('error')}")
                continue

            exit_price = close_res.get("filled_price", 0) or current_price
            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = round(held_minutes / 60, 1)

            _close_trade_record(conn, r["id"], exit_price, exit_reason,
                                pnl, pnl_pct / 100, holding_hours, inst_id=inst_id)
            exited += 1
            logger.info(
                f"[playbook sim] 平仓: {symbol} {direction} reason={exit_reason} "
                f"entry={entry_price} exit={exit_price} pnl=${pnl:.2f}"
            )

        conn.close()
        return exited

    except Exception as e:
        logger.debug(f"[playbook sim] exit monitor 异常 (已隔离): {e}")
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
                _fail_count += 1
                _meta["eod_close_fail_count"] = _fail_count
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()
                logger.warning(f"[playbook dual] EOD 兜底 {symbol} ({_row_target}) 无报价/平仓失败 (第{_fail_count}次): {close_res.get('error')}")
                continue

            exit_price = close_res.get("filled_price", 0) or 0
            if exit_price <= 0:
                # fallback 用当前报价
                try:
                    q = data_router.get_quote(symbol)
                    exit_price = float(q.get("last", 0) or q.get("price", 0) or 0) if q else 0
                except Exception:
                    pass
            if exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * qty if direction == "LONG" else (entry_price - exit_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100 if direction == "LONG" else (entry_price - exit_price) / entry_price * 100
            holding_hours = 0.0
            try:
                holding_hours = (datetime.now() - datetime.fromisoformat(r["entry_time"])).total_seconds() / 3600
            except Exception:
                pass

            _close_trade_record(conn, r["id"], exit_price, _eod_reason,
                                pnl, pnl_pct / 100, holding_hours,
                                inst_id=_meta.get("playbook_instruction_id"))
            closed += 1
            logger.info(f"[playbook sim] EOD 兜底平仓: {symbol} reason={_eod_reason} exit={exit_price} pnl=${pnl:.2f}")

        conn.close()
        return closed

    except Exception as e:
        logger.warning(f"[playbook sim] EOD force_close 异常 (已隔离): {e}")
        return 0

